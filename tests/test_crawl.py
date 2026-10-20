"""web_crawl 离线单测：用假 _fetch_html 驱动 BFS，不联网。"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp import server
from scrape_mcp.core.fetcher import FetchOutcome

BODY = "正文内容" * 200
PAGES = {
    "https://site.test/": (
        f"<html><head><title>首页</title></head><body><article><p>{BODY}</p></article>"
        '<a href="/a">A</a><a href="/b">B</a><a href="https://other.test/x">外</a></body></html>'
    ),
    "https://site.test/a": f"""<html><body><article><p>{BODY}</p></article>
        <a href="/c">C</a></body></html>""",
    "https://site.test/b": f"<html><body><article><p>{BODY}</p></article></body></html>",
    "https://site.test/c": f"<html><body><article><p>{BODY}</p></article></body></html>",
}


def _outcome(url: str, html: str) -> FetchOutcome:
    return FetchOutcome(
        url=url,
        final_url=url,
        status=200,
        tier="l1",
        blocked="none",
        block_reason="",
        content_type="text/html",
        html=html,
        elapsed_ms=5,
    )


def _install(monkeypatch, called: list[str] | None = None):
    async def fake_fetch_html(url, **kwargs):
        if called is not None:
            called.append(url)
        html = PAGES.get(url)
        if html is None:
            return None, {"ok": False, "url": url, "error_code": "http_4xx", "error": "404"}
        return _outcome(url, html), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)


def _run(**kwargs) -> dict:
    return json.loads(asyncio.run(server.web_crawl(**kwargs)))


def test_depth_zero_fetches_only_start(monkeypatch):
    called: list[str] = []
    _install(monkeypatch, called)
    out = _run(url="https://site.test/", depth=0)
    assert out["ok"] is True
    assert out["pages_fetched"] == 1
    assert called == ["https://site.test/"]


def test_depth_one_follows_internal_links(monkeypatch):
    _install(monkeypatch)
    out = _run(url="https://site.test/", depth=1)
    urls = [r["url"] for r in out["results"]]
    assert urls == ["https://site.test/", "https://site.test/a", "https://site.test/b"]
    assert out["ok_count"] == 3


def test_limit_caps_pages(monkeypatch):
    _install(monkeypatch)
    out = _run(url="https://site.test/", depth=3, limit=2)
    assert out["pages_fetched"] == 2


def test_failed_page_is_reported_without_breaking(monkeypatch):
    _install(monkeypatch)
    out = _run(url="https://site.test/missing", depth=1)
    assert out["pages_fetched"] == 1
    assert out["ok_count"] == 0
    assert out["results"][0]["error_code"] == "http_4xx"


def test_invalid_depth_rejected(monkeypatch):
    _install(monkeypatch)
    out = _run(url="https://site.test/", depth=9)
    assert out["ok"] is False and out["error_code"] == "invalid_argument"


def test_external_links_not_followed(monkeypatch):
    called: list[str] = []
    _install(monkeypatch, called)
    _run(url="https://site.test/", depth=1)
    assert "https://other.test/x" not in called
