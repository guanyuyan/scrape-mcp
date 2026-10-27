"""HTTP 传输访问控制：静态 Bearer Token、单令牌配额，未配置时只允许回环访问。"""

from __future__ import annotations

import hmac
import json
import math
import time

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


def is_loopback(host: str | None) -> bool:
    """来源或绑定地址是否为回环。"""
    return (host or "").lower() in LOOPBACK_HOSTS


def ensure_http_access(host: str, token: str | None) -> None:
    """绑定到非回环地址时必须配置令牌，否则拒绝启动。"""
    if (token or "").strip() or is_loopback(host):
        return
    raise SystemExit(
        f"HTTP 服务绑定在非回环地址 {host} 时必须设置 SCRAPE_MCP_HTTP_TOKEN；"
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

    令牌通过后再按 QPS / 日配额限流，超限返回 429；未配置令牌（纯回环本地用）
    不受配额约束。
    """

    def __init__(
        self,
        app: ASGIApp,
        token: str | None = None,
        *,
        qps: float = 0.0,
        daily_quota: int = 0,
    ) -> None:
        self.app = app
        self.token = (token or "").strip() or None
        self.quota = QuotaLimiter(qps, daily_quota) if self.token else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        if self.token is not None:
            header = Headers(scope=scope).get("authorization", "")
            scheme, _, credentials = header.partition(" ")
            if scheme.lower() == "bearer" and hmac.compare_digest(
                credentials.strip().encode("utf-8"), self.token.encode("utf-8")
            ):
                if self.quota is not None and scope.get("path") not in QUOTA_EXEMPT_PATHS:
                    rejected = self.quota.hit()
                    if rejected is not None:
                        error, retry_after = rejected
                        metrics.record_rejection(error)
                        await _send_error(send, 429, error, retry_after=retry_after)
                        return
                scope.setdefault("state", {})["scrape_mcp_authenticated"] = True
                await self.app(scope, receive, send)
                return
            metrics.record_rejection("unauthorized")
            await _send_error(send, 401, "unauthorized")
            return

        if is_loopback(_client_host(scope)):
            await self.app(scope, receive, send)
            return
        metrics.record_rejection("forbidden")
        await _send_error(send, 403, "forbidden")
