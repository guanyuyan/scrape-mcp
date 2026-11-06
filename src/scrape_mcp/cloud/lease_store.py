"""租约状态存储：默认进程内，多实例部署时可切到 Redis 共享。

接口刻意保持很小（读取活跃租约、当日用量、轮询游标、写入租约），
分配决策仍留在 :mod:`scrape_mcp.cloud.broker`；Redis 实现只在写路径上加
一把按「租户 + 类型」的短锁，保证并发上限与日配额在多实例下不会被击穿。
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict
from typing import Any

from .broker import (
    _KIND_LABELS,
    CLOUD_QUOTA_EXCEEDED,
    CLOUD_UNAVAILABLE,
    KINDS,
    BrokerError,
    Lease,
)

LOCK_TTL_S = 5
LOCK_RETRIES = 50
LOCK_SLEEP_S = 0.01


def create_client(url: str) -> Any:
    """按 URL 建同步 Redis 客户端；未安装可选依赖时给出可执行的安装提示。"""
    try:
        import redis
    except ImportError as exc:  # pragma: no cover - 取决于运行环境是否装了 extras
        raise RuntimeError(
            "使用 SCRAPE_MCP_CLOUD_BROKER_REDIS_URL 需要可选依赖，"
            "请先 pip install 'mcp-scrape[redis]'"
        ) from exc
    return redis.Redis.from_url(url, decode_responses=True)


def _redis_guard(method):
    """存储抖动时统一转成 503 + 可重试，避免向调用方抛裸异常。"""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(
                503,
                CLOUD_UNAVAILABLE,
                f"租约状态存储不可用: {exc}",
                retry_after=1.0,
            ) from exc

    return wrapper


class LocalLeaseStore:
    """进程内状态：单实例默认实现，行为与 0.26.0 完全一致。"""

    def __init__(self) -> None:
        self._leases: dict[str, Lease] = {}
        self._next: dict[str, int] = dict.fromkeys(KINDS, 0)
        # (租户, 类型) -> (UTC 日序号, 当日已发放数)
        self._daily: dict[tuple[str, str], tuple[int, int]] = {}

    def list_active(self, tenant: str, kind: str, now: float) -> list[Lease]:
        """回收过期租约后返回该租户该类型的活跃租约。"""
        expired = [key for key, lease in self._leases.items() if lease.expires_at <= now]
        for key in expired:
            del self._leases[key]
        return [
            lease
            for lease in self._leases.values()
            if lease.tenant == tenant and lease.kind == kind
        ]

    def daily_used(self, tenant: str, kind: str, now: float) -> int:
        day = int(now // 86400)
        old_day, used = self._daily.get((tenant, kind), (day, 0))
        return used if old_day == day else 0

    def reserve_daily(self, tenant: str, kind: str, now: float, limit: int) -> None:
        """占一个当日名额；超限抛 429，跨 UTC 自然日自动归零。"""
        if not limit:
            return
        day = int(now // 86400)
        old_day, used = self._daily.get((tenant, kind), (day, 0))
        if old_day != day:
            used = 0
        if used >= limit:
            raise BrokerError(
                429,
                CLOUD_QUOTA_EXCEEDED,
                f"租户 {tenant} 的{_KIND_LABELS[kind]}租约已达当日配额 ({limit})；UTC 零点后重置",
                retry_after=max(1.0, 86400.0 - now % 86400),
            )
        self._daily[(tenant, kind)] = (day, used + 1)

    def next_index(self, kind: str) -> int:
        index = self._next[kind]
        self._next[kind] = index + 1
        return index

    def save(self, lease: Lease, now: float) -> None:
        self._leases[lease.lease_id] = lease

    @contextlib.contextmanager
    def lock(self, tenant: str, kind: str):
        """本地单进程无并发竞争，保持与 Redis 实现相同的调用形态。"""
        yield

    def ping(self) -> None:
        return None


class RedisLeaseStore:
    """Redis 共享状态：多实例都能看到同一批租约与配额。

    键结构（``<base>`` 默认 ``scrape_mcp:cloud``）：

    - ``<base>:lease:<tenant>:<kind>:<id>`` 租约 JSON，带 TTL
    - ``<base>:leases:<tenant>:<kind>``      租约 ID 集合，用于按租户列举
    - ``<base>:daily:<tenant>:<kind>:<day>`` 当日已发放数，带 TTL
    - ``<base>:rr:<kind>``                   轮询游标（跨实例共享）
    - ``<base>:lock:<tenant>:<kind>``        写路径短锁（SET NX）

    租约真实到期由键 TTL 兜底，读取时再按 ``expires_at`` 过滤一次，
    避免实例时钟偏差或 TTL 设置失败时拿到过期租约。
    """

    def __init__(
        self,
        client: Any,
        prefix: str = "scrape_mcp",
    ) -> None:
        self._client = client
        self._base = f"{(prefix or 'scrape_mcp').rstrip(':')}:cloud"

    # ---------- 键构造 ----------

    def _tenant_part(self, tenant: str) -> str:
        """租户名可能含空格/中文，做可读清洗并附加哈希避免碰撞。"""
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", tenant)[:40] or "tenant"
        digest = hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:8]
        return f"{safe}-{digest}"

    def _lease_key(self, tenant: str, kind: str, lease_id: str) -> str:
        return f"{self._base}:lease:{self._tenant_part(tenant)}:{kind}:{lease_id}"

    def _set_key(self, tenant: str, kind: str) -> str:
        return f"{self._base}:leases:{self._tenant_part(tenant)}:{kind}"

    def _daily_key(self, tenant: str, kind: str, day: int) -> str:
        return f"{self._base}:daily:{self._tenant_part(tenant)}:{kind}:{day}"

    def _rr_key(self, kind: str) -> str:
        return f"{self._base}:rr:{kind}"

    def _lock_key(self, tenant: str, kind: str) -> str:
        return f"{self._base}:lock:{self._tenant_part(tenant)}:{kind}"

    # ---------- 与 LocalLeaseStore 同形的接口 ----------

    @_redis_guard
    def list_active(self, tenant: str, kind: str, now: float) -> list[Lease]:
        set_key = self._set_key(tenant, kind)
        ids = sorted(self._client.smembers(set_key) or ())
        if not ids:
            return []
        raws = self._client.mget([self._lease_key(tenant, kind, i) for i in ids])
        leases: list[Lease] = []
        stale: list[str] = []
        for lease_id, raw in zip(ids, raws, strict=True):
            if not raw:
                stale.append(lease_id)
                continue
            try:
                lease = Lease(**json.loads(raw))
            except (TypeError, ValueError, KeyError):
                stale.append(lease_id)
                continue
            if lease.expires_at <= now:
                stale.append(lease_id)
                continue
            leases.append(lease)
        if stale:
            self._client.srem(set_key, *stale)
        return leases

    @_redis_guard
    def daily_used(self, tenant: str, kind: str, now: float) -> int:
        raw = self._client.get(self._daily_key(tenant, kind, int(now // 86400)))
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    @_redis_guard
    def reserve_daily(self, tenant: str, kind: str, now: float, limit: int) -> None:
        if not limit:
            return
        day = int(now // 86400)
        key = self._daily_key(tenant, kind, day)
        used = self.daily_used(tenant, kind, now)
        if used >= limit:
            raise BrokerError(
                429,
                CLOUD_QUOTA_EXCEEDED,
                f"租户 {tenant} 的{_KIND_LABELS[kind]}租约已达当日配额 ({limit})；UTC 零点后重置",
                retry_after=max(1.0, 86400.0 - now % 86400),
            )
        self._client.incr(key)
        # 计数器跨过 UTC 零点即失效；多留 1 小时避免边界抖动
        self._client.expire(key, int(86400.0 - now % 86400) + 3600)

    @_redis_guard
    def next_index(self, kind: str) -> int:
        return int(self._client.incr(self._rr_key(kind))) - 1

    @_redis_guard
    def save(self, lease: Lease, now: float) -> None:
        ttl = max(5, int(lease.expires_at - now) + 5)
        self._client.setex(
            self._lease_key(lease.tenant, lease.kind, lease.lease_id),
            ttl,
            json.dumps(asdict(lease), ensure_ascii=False),
        )
        set_key = self._set_key(lease.tenant, lease.kind)
        self._client.sadd(set_key, lease.lease_id)
        # 集合里可能残留已过期 ID，给集合一个宽限 TTL 由读取时清理兜底
        self._client.expire(set_key, ttl + 86400)

    @contextlib.contextmanager
    def lock(self, tenant: str, kind: str):
        """写路径短锁：拿不到就快速失败为可重试 503，避免请求堆积。"""
        key = self._lock_key(tenant, kind)
        token = uuid.uuid4().hex
        acquired = False
        try:
            for _ in range(LOCK_RETRIES):
                if self._client.set(key, token, nx=True, ex=LOCK_TTL_S):
                    acquired = True
                    break
                time.sleep(LOCK_SLEEP_S)
        except Exception as exc:
            raise BrokerError(
                503,
                CLOUD_UNAVAILABLE,
                f"租约状态存储不可用: {exc}",
                retry_after=1.0,
            ) from exc
        if not acquired:
            raise BrokerError(
                503,
                CLOUD_UNAVAILABLE,
                "租约状态繁忙，请稍后重试",
                retry_after=0.5,
            )
        try:
            yield
        finally:
            try:
                if self._client.get(key) == token:
                    self._client.delete(key)
            except Exception:
                # 释放失败最多让锁在 LOCK_TTL_S 后自然过期，不覆盖原异常
                pass

    def ping(self) -> None:
        try:
            self._client.ping()
        except Exception as exc:
            raise RuntimeError(f"无法连接云端 broker 的 Redis：{exc}") from exc
