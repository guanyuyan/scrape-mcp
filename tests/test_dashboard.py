"""内置看板：HTML 空壳可免鉴权打开，数据端点仍要令牌。"""

from __future__ import annotations

import asyncio

from scrape_mcp import __version__, dashboard, server
from scrape_mcp.http_auth import EXEMPT_PATHS, BearerAuthMiddleware


def test_dashboard_page_returns_html_shell():
    resp = asyncio.run(server.dashboard_page(None))

    assert resp.status_code == 200
    assert resp.media_type == "text/html"
    body = resp.body.decode("utf-8")
    assert "scrape-mcp 运行看板" in body
    assert __version__ in body
    # 占位符都已替换，页面里没有残留
    assert "__VERSION__" not in body and "__REFRESH_S__" not in body
    assert "__REFRESH_MS__" not in body
    # 数据全靠页面自己去读，壳里不含抓取内容
    assert "/stats" in body and "/metrics" in body


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _ok_app(scope, receive, send):
    scope["reached"] = True
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _status_for(path: str, token: str = "s3cret") -> tuple[int, dict]:
    scope = {
        "type": "http",
        "path": path,
        "headers": [],
        "client": ("10.0.0.5", 5000),
        "state": {},
    }
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    asyncio.run(BearerAuthMiddleware(_ok_app, token)(scope, _receive, send))
    start = next(item for item in sent if item["type"] == "http.response.start")
    return start["status"], scope


def test_dashboard_shell_is_exempt_but_stats_still_requires_token():
    assert "/" in EXEMPT_PATHS

    shell_status, shell_scope = _status_for("/")
    assert shell_status == 200 and shell_scope.get("reached") is True

    stats_status, stats_scope = _status_for("/stats")
    assert stats_status == 401 and stats_scope.get("reached") is None


def test_render_embeds_refresh_interval():
    html = dashboard.render()

    assert f"{dashboard.REFRESH_MS // 1000} 秒刷新" in html
    assert f"const REFRESH_MS = {dashboard.REFRESH_MS};" in html
