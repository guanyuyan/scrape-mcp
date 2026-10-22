"""HTTP 传输访问控制：静态 Bearer Token，未配置时只允许回环访问。"""

from __future__ import annotations

import hmac
import json

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

# 绑定与来源判断共用的回环地址集合（含 IPv4-mapped IPv6）
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"})

# 容器探针与网关巡检需要无凭证访问；响应里只有版本与开关，不含抓取数据
EXEMPT_PATHS = frozenset({"/healthz"})


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


async def _send_error(send: Send, status: int, error: str) -> None:
    """统一的无凭证/越权响应：不返回内部路径、版本或统计信息。"""
    body = json.dumps({"ok": False, "error": error}, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    if status == 401:
        headers.append((b"www-authenticate", b'Bearer realm="scrape-mcp"'))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """配置令牌后校验 ``Authorization: Bearer <token>``；未配置时仅放行回环来源。"""

    def __init__(self, app: ASGIApp, token: str | None = None) -> None:
        self.app = app
        self.token = (token or "").strip() or None

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
                scope.setdefault("state", {})["scrape_mcp_authenticated"] = True
                await self.app(scope, receive, send)
                return
            await _send_error(send, 401, "unauthorized")
            return

        if is_loopback(_client_host(scope)):
            await self.app(scope, receive, send)
            return
        await _send_error(send, 403, "forbidden")
