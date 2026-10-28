"""HTTP 传输访问控制：单/多令牌鉴权与租户级配额，未配置时只允许回环访问。"""

from __future__ import annotations

import hmac
import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from . import metrics

# 绑定与来源判断共用的回环地址集合（含 IPv4-mapped IPv6）
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"})

# 容器探针与网关巡检需要无凭证访问；响应里只有版本与开关，不含抓取数据
EXEMPT_PATHS = frozenset({"/healthz"})

# 指标端点按固定间隔被抓取，若计入 QPS/日配额，监控自己就会吃掉调用额度；
# 这里只跳过配额，鉴权照旧（配置令牌后仍需 Bearer）
QUOTA_EXEMPT_PATHS = frozenset({"/metrics"})


class TokenConfigError(ValueError):
    """令牌配置不合法；消息可直接打印给运维。"""


@dataclass(frozen=True)
class TokenPolicy:
    """一个调用方（租户）：令牌 + 独立配额。"""

    name: str
    token: str
    qps: float = 0.0
    daily_quota: int = 0


def _entry_policy(entry: object, index: int, default_qps: float, default_daily: int) -> TokenPolicy:
    """校验并落地一条 JSON 配置；qps / daily_quota 缺省时继承全局值。"""
    if not isinstance(entry, dict):
        raise TokenConfigError(
            f'第 {index + 1} 项必须是对象，例如 {{"name": "team-a", "token": "..."}}'
        )

    name = str(entry.get("name") or "").strip()
    token = str(entry.get("token") or "").strip()
    if not name:
        raise TokenConfigError(f"第 {index + 1} 项缺少 name（租户名，用于配额与指标标签）")
    if not token:
        raise TokenConfigError(f"租户 {name} 缺少 token")

    raw_qps = entry.get("qps", default_qps)
    raw_daily = entry.get("daily_quota", default_daily)
    try:
        qps = max(float(raw_qps), 0.0)
        daily = max(int(raw_daily), 0)
    except (TypeError, ValueError) as exc:
        raise TokenConfigError(f"租户 {name} 的 qps / daily_quota 必须是数字") from exc

    return TokenPolicy(name=name, token=token, qps=qps, daily_quota=daily)


def parse_token_policies(
    raw: str, *, default_qps: float = 0.0, default_daily: int = 0
) -> list[TokenPolicy]:
    """解析多令牌 JSON：数组，或 ``{"tokens": [...]}``。

    每项形如 ``{"name": "team-a", "token": "...", "qps": 2, "daily_quota": 2000}``；
    租户名与令牌都必须唯一，重复时直接报错而不是静默覆盖。
    """
    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TokenConfigError(f"不是合法 JSON: {exc}") from exc

    if isinstance(data, dict):
        data = data.get("tokens")
    if not isinstance(data, list):
        raise TokenConfigError('期望形如 [{"name": "team-a", "token": "..."}] 的数组')

    policies: list[TokenPolicy] = []
    names: set[str] = set()
    tokens: set[str] = set()
    for index, entry in enumerate(data):
        policy = _entry_policy(entry, index, default_qps, default_daily)
        if policy.name in names:
            raise TokenConfigError(f"租户名重复: {policy.name}")
        if policy.token in tokens:
            raise TokenConfigError(f"令牌重复（租户 {policy.name} 与其他租户相同）")
        names.add(policy.name)
        tokens.add(policy.token)
        policies.append(policy)
    if not policies:
        raise TokenConfigError("多令牌配置为空数组；不需要鉴权时请清空该变量")
    return policies


def load_token_policies(
    *,
    raw: str = "",
    file: str = "",
    single: str | None = None,
    default_qps: float = 0.0,
    default_daily: int = 0,
) -> list[TokenPolicy]:
    """按单令牌 / 多令牌（环境变量 JSON / JSON 文件）得到生效的令牌列表。

    三者互斥：同时配置说明运维意图不一致，直接拒绝启动，避免“以为改了其实没生效”。
    """
    raw_text = (raw or "").strip()
    file_path = (file or "").strip()
    single_token = (single or "").strip()

    if raw_text and file_path:
        raise TokenConfigError("同时设置了 HTTP_TOKENS 与 HTTP_TOKENS_FILE，只保留一处")
    if single_token and (raw_text or file_path):
        raise TokenConfigError("同时设置了单个 HTTP_TOKEN 与多令牌配置，只保留一处")

    if file_path:
        source = Path(file_path).expanduser()
        try:
            raw_text = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise TokenConfigError(f"读取 {source} 失败: {exc}") from exc

    if raw_text:
        return parse_token_policies(raw_text, default_qps=default_qps, default_daily=default_daily)
    if single_token:
        return [
            TokenPolicy(
                name="default", token=single_token, qps=default_qps, daily_quota=default_daily
            )
        ]
    return []


def is_loopback(host: str | None) -> bool:
    """来源或绑定地址是否为回环。"""
    return (host or "").lower() in LOOPBACK_HOSTS


def ensure_http_access(host: str, token: str | None, *, tokens: Sequence[TokenPolicy] = ()) -> None:
    """绑定到非回环地址时必须配置至少一个令牌，否则拒绝启动。"""
    if (token or "").strip() or tokens or is_loopback(host):
        return
    raise SystemExit(
        f"HTTP 服务绑定在非回环地址 {host} 时必须设置 SCRAPE_MCP_HTTP_TOKEN"
        "（或 SCRAPE_MCP_HTTP_TOKENS / SCRAPE_MCP_HTTP_TOKENS_FILE）；"
        "未配置令牌时仅允许 127.0.0.1 / localhost / ::1"
    )


def _client_host(scope: Scope) -> str | None:
    client = scope.get("client")
    return client[0] if client else None


class QuotaLimiter:
    """单令牌的 QPS 与日配额：超限直接拒绝，不做排队等待。

    QPS 用令牌桶（容量等于速率，允许一个有界突发）；日配额按 UTC 自然日计数，
    跨日自动清零。两项都可以设 0 关闭。
    """

    def __init__(self, qps: float = 0.0, daily: int = 0) -> None:
        self._qps = max(float(qps or 0), 0.0)
        self._daily = max(int(daily or 0), 0)
        self._tokens = self._qps
        self._last = time.monotonic()
        self._day = self._today()
        self._used = 0

    @staticmethod
    def _today() -> int:
        """UTC 日序号：同一个自然日内计数，零点归零。"""
        return int(time.time() // 86400)

    def remaining(self) -> int | None:
        """当前 UTC 日剩余配额；未设置日配额时返回 None（表示不限）。"""
        if not self._daily:
            return None
        if int(time.time() // 86400) != self._day:
            return self._daily
        return max(0, self._daily - self._used)

    def hit(self) -> tuple[str, float] | None:
        """记一次已通过鉴权的请求；超限返回 ``(错误码, 建议重试秒数)``。"""
        wall = time.time()
        day = int(wall // 86400)
        if day != self._day:
            self._day = day
            self._used = 0
        if self._daily and self._used >= self._daily:
            return "quota_exceeded", max(1.0, 86400.0 - wall % 86400)
        if self._qps > 0:
            now = time.monotonic()
            self._tokens = min(self._qps, self._tokens + (now - self._last) * self._qps)
            self._last = now
            if self._tokens < 1.0:
                return "rate_limited", (1.0 - self._tokens) / self._qps
            self._tokens -= 1.0
        self._used += 1
        return None


async def _send_error(
    send: Send, status: int, error: str, retry_after: float | None = None
) -> None:
    """统一的无凭证/越权/超限响应：不返回内部路径、版本或统计信息。"""
    payload: dict[str, object] = {"ok": False, "error": error}
    if retry_after is not None:
        payload["retry_after_s"] = round(retry_after, 1)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    if status == 401:
        headers.append((b"www-authenticate", b'Bearer realm="scrape-mcp"'))
    if status == 429 and retry_after is not None:
        headers.append((b"retry-after", str(math.ceil(retry_after)).encode("ascii")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """配置令牌后校验 ``Authorization: Bearer <token>``；未配置时仅放行回环来源。

    多令牌时按令牌归属的租户各自限流：令牌通过后再按该租户的 QPS / 日配额
    判断，超限返回 429；未配置令牌（纯回环本地用）不受配额约束。
    """

    def __init__(
        self,
        app: ASGIApp,
        token: str | None = None,
        *,
        tokens: Sequence[TokenPolicy] = (),
        qps: float = 0.0,
        daily_quota: int = 0,
    ) -> None:
        self.app = app
        self.policies: list[TokenPolicy] = list(tokens) or load_token_policies(
            single=token, default_qps=qps, default_daily=daily_quota
        )
        # 每个租户独立令牌桶与日计数，互不挤占
        self._limiters = {
            policy.name: QuotaLimiter(policy.qps, policy.daily_quota) for policy in self.policies
        }
        if self.policies:
            # 配额余量是动态值，交给 /metrics 在渲染时回调，避免维护第二份计数
            metrics.set_tenant_quota_provider(self._quota_snapshot)

    def _quota_snapshot(self) -> dict[str, int]:
        """各租户的当日剩余配额；未设置日配额的租户不输出（“不限”不适合当数字展示）。"""
        return {
            name: left
            for name, limiter in self._limiters.items()
            if (left := limiter.remaining()) is not None
        }

    def _match(self, credentials: str) -> TokenPolicy | None:
        """常量时间比较令牌；比对全部条目，避免通过响应时间推断前缀。"""
        raw = credentials.encode("utf-8")
        matched: TokenPolicy | None = None
        for policy in self.policies:
            if hmac.compare_digest(raw, policy.token.encode("utf-8")):
                matched = policy
        return matched

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        path = scope.get("path")

        if self.policies:
            header = Headers(scope=scope).get("authorization", "")
            scheme, _, credentials = header.partition(" ")
            policy = self._match(credentials.strip()) if scheme.lower() == "bearer" else None
            if policy is None:
                metrics.record_rejection("unauthorized")
                await _send_error(send, 401, "unauthorized")
                return

            if path not in QUOTA_EXEMPT_PATHS:
                rejected = self._limiters[policy.name].hit()
                if rejected is not None:
                    error, retry_after = rejected
                    metrics.record_rejection(error)
                    metrics.record_tenant_rejection(policy.name, error)
                    await _send_error(send, 429, error, retry_after=retry_after)
                    return
                metrics.record_tenant_request(policy.name)

            scope.setdefault("state", {})["scrape_mcp_authenticated"] = True
            scope.setdefault("state", {})["scrape_mcp_tenant"] = policy.name
            await self.app(scope, receive, send)
            return

        if is_loopback(_client_host(scope)):
            await self.app(scope, receive, send)
            return
        metrics.record_rejection("forbidden")
        await _send_error(send, 403, "forbidden")
