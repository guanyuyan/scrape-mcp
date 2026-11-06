"""云端控制面 broker：按租户分配上游浏览器 / 代理租约。

P2-4b 的边界只到控制面：本模块决定"把哪个上游分给哪个租户、能用多久、
配额还剩多少"，不代理流量、不托管浏览器进程、也不生成计费账单。上游由
运维提供（自建 Playwright 集群 / 代理网关），租约信息返回给调用方直连。

语义：
- ``POST /cloud/<kind>/lease`` 申请一张新租约；``GET`` 复用当前未过期租约，
  没有时发放一张（复用不消耗日配额）
- 租约到期自动回收；单租户并发租约与每日租约配额按类型独立计算
- 上游按轮询分配；代理上游可带 ``|region`` 地区标签
"""

from __future__ import annotations

import math
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ..core.proxy import normalize_region, valid_proxy

if TYPE_CHECKING:  # 避免与 lease_store 循环导入；运行时按需加载本地实现
    from .lease_store import LeaseStore

# 浏览器上游是 CDP 端点：Browserless 常见 http/ws，自建集群两种都支持
BROWSER_SCHEMES = frozenset({"http", "https", "ws", "wss"})
# 与代理池一致：逗号、分号、空白都接受，方便一行写多个
_SPLIT = re.compile(r"[,;\s]+")
_REGION_SEP = "|"

KINDS = ("browser", "proxy")
_KIND_LABELS = {"browser": "浏览器", "proxy": "代理"}

# REST 端点专用的稳定错误码。与 MCP 工具错误码分开维护：
# 这些只在 /cloud/* 出现，不进入 errors.ALL_CODES 的对外承诺列表
CLOUD_DISABLED = "cloud_disabled"
CLOUD_UNAVAILABLE = "cloud_unavailable"
CLOUD_LEASE_LIMIT = "cloud_lease_limit"
CLOUD_QUOTA_EXCEEDED = "cloud_quota_exceeded"

# 默认租户名：未配置令牌的纯本机模式没有租户身份，按同一个本地租户计数
LOCAL_TENANT = "local"


class CloudConfigError(ValueError):
    """上游或租约配置不合法；消息可直接打印给运维。"""


@dataclass(frozen=True)
class Upstream:
    """一条可分配的上游：地址 + 可选地区标签（仅代理使用）。"""

    url: str
    region: str = ""


def _items(raw: str) -> list[str]:
    return [item for item in _SPLIT.split((raw or "").strip()) if item]


def _valid_browser_url(text: str) -> bool:
    """CDP 端点：协议必须是 http/https/ws/wss，且带主机名。"""
    parsed = urlparse(text)
    try:
        return (
            parsed.scheme.lower() in BROWSER_SCHEMES
            and bool(parsed.hostname)
            and (parsed.port is None or parsed.port > 0)
        )
    except ValueError:
        return False


def _dedupe(items: list[Upstream]) -> list[Upstream]:
    """按 URL 去重并保持输入顺序，重复项保留首次出现的地区标签。"""
    seen: set[str] = set()
    result: list[Upstream] = []
    for item in items:
        if item.url in seen:
            continue
        seen.add(item.url)
        result.append(item)
    return result


def parse_browser_upstreams(raw: str) -> list[Upstream]:
    """解析浏览器上游列表；非法项直接报错，避免"以为配了其实没生效"。"""
    upstreams: list[Upstream] = []
    for item in _items(raw):
        if not _valid_browser_url(item):
            raise CloudConfigError(f"浏览器上游不合法: {item}；需为 http/https/ws/wss 的 CDP 端点")
        upstreams.append(Upstream(item))
    return _dedupe(upstreams)


def parse_proxy_upstreams(raw: str) -> list[Upstream]:
    """解析代理上游列表，条目可写成 ``url|region``。"""
    upstreams: list[Upstream] = []
    for item in _items(raw):
        url_part, _, region_part = item.partition(_REGION_SEP)
        url = valid_proxy(url_part)
        if url is None:
            raise CloudConfigError(
                f"代理上游不合法: {item}；需为 http/https/socks4/socks5 代理地址"
            )
        region = ""
        if region_part.strip():
            region = normalize_region(region_part) or ""
            if not region:
                raise CloudConfigError(
                    f"代理上游 {url} 的地区标签不合法: {region_part.strip()}；"
                    "只允许字母、数字、下划线、短横线（1-32 位）"
                )
        upstreams.append(Upstream(url, region))
    return _dedupe(upstreams)


@dataclass(frozen=True)
class Lease:
    """一张租约：租户在一段时间内独占的上游。"""

    lease_id: str
    kind: str
    tenant: str
    url: str
    region: str
    issued_at: float
    expires_at: float

    def payload(self, now: float) -> dict[str, object]:
        """对外的租约载荷；不包含其它租户或上游池明细。"""
        return {
            "lease_id": self.lease_id,
            "kind": self.kind,
            "url": self.url,
            "region": self.region,
            "expires_at": datetime.fromtimestamp(self.expires_at, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "expires_in_s": max(0, math.ceil(self.expires_at - now)),
        }


class BrokerError(Exception):
    """租约申请被拒；``status`` 与 ``code`` 直接映射到 REST 响应。"""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


class LeaseBroker:
    """租约分配器：轮询上游、并发上限、TTL 回收与按日配额。

    所有操作都在事件循环内同步完成，单进程内不需要额外加锁；
    多实例部署时应由反向代理把同一租户固定到同一个实例（本模块不共享状态）。
    """

    def __init__(
        self,
        *,
        browser_upstreams: str = "",
        proxy_upstreams: str = "",
        lease_ttl: float = 300.0,
        max_leases_per_tenant: int = 2,
        daily_leases: int = 0,
        clock=time.time,
        store: LeaseStore | None = None,
    ) -> None:
        self.browser = parse_browser_upstreams(browser_upstreams)
        self.proxy = parse_proxy_upstreams(proxy_upstreams)
        self.ttl = max(float(lease_ttl or 0), 1.0)
        # 0 表示不限；上限按租户、按类型分别计算
        self.max_per_tenant = max(int(max_leases_per_tenant or 0), 0)
        self.daily_limit = max(int(daily_leases or 0), 0)
        self._clock = clock
        # 默认进程内状态；配置 CLOUD_BROKER_REDIS_URL 后换成多实例共享实现
        self.shared = store is not None
        if store is None:
            from .lease_store import LocalLeaseStore

            store = LocalLeaseStore()
        self._store = store

    def now(self) -> float:
        """当前时间（秒）；测试可注入时钟。"""
        return float(self._clock())

    def ping(self) -> None:
        """检查共享存储连通性；启动阶段调用，本地存储恒可用。"""
        self._store.ping()

    def upstream_count(self, kind: str) -> int:
        return len(self._pool(kind))

    def _pool(self, kind: str) -> list[Upstream]:
        if kind == "browser":
            return self.browser
        if kind == "proxy":
            return self.proxy
        raise ValueError(f"未知租约类型: {kind}")

    def acquire(self, kind: str, tenant: str, *, reuse: bool = False) -> Lease:
        """申请或复用租约。

        ``reuse=True``（GET）时先返回当前未过期租约；没有可用租约才发放新的，
        并照常占用配额。``reuse=False``（POST）总是申请新租约。
        """
        pool = self._pool(kind)
        if not pool:
            raise BrokerError(
                503,
                CLOUD_UNAVAILABLE,
                f"未配置{_KIND_LABELS[kind]}上游；请联系运维设置 "
                f"SCRAPE_MCP_CLOUD_{kind.upper()}_UPSTREAMS",
            )

        with self._store.lock(tenant, kind):
            now = self.now()
            active = self._store.list_active(tenant, kind, now)
            if reuse and active:
                return min(active, key=lambda lease: lease.expires_at)

            if self.max_per_tenant and len(active) >= self.max_per_tenant:
                earliest = min(lease.expires_at for lease in active)
                raise BrokerError(
                    429,
                    CLOUD_LEASE_LIMIT,
                    f"租户 {tenant} 同时持有的{_KIND_LABELS[kind]}租约已达上限 "
                    f"({self.max_per_tenant})；请等待现有租约到期或复用 GET 端点",
                    retry_after=max(0.5, earliest - now),
                )

            self._store.reserve_daily(tenant, kind, now, self.daily_limit)

            index = self._store.next_index(kind) % len(pool)
            upstream = pool[index]

            lease = Lease(
                lease_id=uuid.uuid4().hex[:16],
                kind=kind,
                tenant=tenant,
                url=upstream.url,
                region=upstream.region,
                issued_at=now,
                expires_at=now + self.ttl,
            )
            self._store.save(lease, now)
            return lease

    def usage(self, tenant: str) -> dict[str, object]:
        """本租户用量：只含自己的租约与配额，不泄露其它租户明细。"""
        now = self.now()

        blocks: dict[str, object] = {}
        for kind in KINDS:
            active = self._store.list_active(tenant, kind, now)
            used = self._store.daily_used(tenant, kind, now)
            blocks[kind] = {
                "active": len(active),
                "limit": self.max_per_tenant,
                "daily_used": used,
                "daily_limit": self.daily_limit,
                "daily_remaining": (max(0, self.daily_limit - used) if self.daily_limit else None),
                "upstreams": len(self._pool(kind)),
            }

        return {
            "tenant": tenant,
            "lease_ttl_s": self.ttl,
            "upstreams": {"browser": len(self.browser), "proxy": len(self.proxy)},
            **blocks,
        }


def build_broker(settings, store: LeaseStore | None = None) -> LeaseBroker:
    """按配置构建 broker；上游非法时抛 ``CloudConfigError``（启动即失败）。

    ``CLOUD_BROKER_REDIS_URL`` 非空时返回共享状态实现，多实例看到同一批租约；
    否则沿用进程内状态，行为与单实例部署一致。
    """
    if store is None and getattr(settings, "cloud_broker_redis_url", ""):
        from .lease_store import RedisLeaseStore, create_client

        store = RedisLeaseStore(
            create_client(settings.cloud_broker_redis_url), settings.redis_prefix
        )
    return LeaseBroker(
        browser_upstreams=settings.cloud_browser_upstreams,
        proxy_upstreams=settings.cloud_proxy_upstreams,
        lease_ttl=settings.cloud_lease_ttl,
        max_leases_per_tenant=settings.cloud_max_leases_per_tenant,
        daily_leases=settings.cloud_daily_leases,
        store=store,
    )
