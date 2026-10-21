"""HTTP 状态处理：4xx 错误页不当作有效内容返回。"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp import server
from scrape_mcp.core.fetcher import FetchOutcome


class _FakeFetcher:
    def __init__(self, status: int, blocked: str = "none") -> None:
        self.status = status
        self.blocked = blocked

    async def fetch(self, url: str, **_kwargs) -> FetchOutcome:
        return FetchOutcome(
            url=url,
            final_url=url,
            status=self.status,
            tier="l1",
            blocked=self.blocked,
            block_reason="waf" if self.blocked != "none" else "",
            content_type="text/html",
            html=f"<html><body><article><p>{'正文' * 200}</p></article></body></html>",
            elapsed_ms=3,
        )

    async def aclose(self) -> None:
        pass


def _run(monkeypatch, status: int, blocked: str = "none") -> dict:
    async def ok(_url: str, *_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(server, "_compliance_check", ok)
    monkeypatch.setattr(server, "_get_fetcher", lambda: _FakeFetcher(status, blocked))
    return json.loads(asyncio.run(server.web_fetch("https://a.test/x", 500)))


def test_404_is_reported_as_http_4xx(monkeypatch):
    out = _run(monkeypatch, 404)
    assert out["ok"] is False
    assert out["error_code"] == "http_4xx"
    assert out["status"] == 404


def test_403_stays_blocked(monkeypatch):
    out = _run(monkeypatch, 403, blocked="waf")
    assert out["ok"] is False
    assert out["error_code"] == "blocked"
