"""进度通知：web_batch / web_crawl 按完成数上报，且 ctx 不进入工具入参 schema。"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp import server


class FakeContext:
    """只实现进度所需的两个成员，模拟框架注入的 Context。"""

    def __init__(self) -> None:
        self.request_context = object()
        self.events: list[tuple[float, float | None, str | None]] = []

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.events.append((progress, total, message))


def _run(coro):
    return asyncio.run(coro)


def test_batch_reports_progress_per_url(monkeypatch):
    async def fake_fetch(url, max_tokens, link_policy, **kwargs):
        return {"ok": True, "url": url}

    monkeypatch.setattr(server, "_fetch_dict", fake_fetch)
    ctx = FakeContext()
    urls = ["https://a.test/1", "https://a.test/2", "https://a.test/3"]

    out = json.loads(_run(server.web_batch(urls, ctx=ctx)))

    assert out["ok"] is True and out["ok_count"] == 3
    assert [e[0] for e in ctx.events] == [1, 2, 3]
    assert {e[1] for e in ctx.events} == {3}
    assert ctx.events[-1][2] == "已抓取 3/3 个 URL"


def test_batch_counts_failed_urls_in_progress(monkeypatch):
    async def fake_fetch(url, max_tokens, link_policy, **kwargs):
        if url.endswith("bad"):
            raise RuntimeError("boom")
        return {"ok": True, "url": url}

    monkeypatch.setattr(server, "_fetch_dict", fake_fetch)
    ctx = FakeContext()
    out = json.loads(_run(server.web_batch(["https://a.test/ok", "https://a.test/bad"], ctx=ctx)))

    assert out["ok_count"] == 1
    assert [e[0] for e in ctx.events] == [1, 2]


def test_crawl_paginate_reports_each_page(monkeypatch):
    async def fake_page(url, **kwargs):
        index = int(url.rsplit("/p", 1)[-1])
        return {"ok": True, "url": url}, [], f"https://site.test/p{index + 1}"

    monkeypatch.setattr(server, "_crawl_page", fake_page)
    ctx = FakeContext()
    out = json.loads(
        _run(server.web_crawl("https://site.test/p1", next_selector="a.next", max_pages=3, ctx=ctx))
    )

    assert out["mode"] == "paginate" and out["pages_fetched"] == 3
    assert [e[0] for e in ctx.events] == [1, 2, 3]
    assert {e[1] for e in ctx.events} == {3}
    assert ctx.events[-1][2] == "已翻到第 3/3 页"


def test_crawl_link_mode_reports_fetched_pages(monkeypatch):
    body = "正文" * 200
    pages = {
        "https://site.test/": (
            f"<html><body><article><p>{body}</p></article>"
            '<a href="/a">A</a><a href="/b">B</a></body></html>'
        ),
        "https://site.test/a": f"<html><body><article><p>{body}</p></article></body></html>",
        "https://site.test/b": f"<html><body><article><p>{body}</p></article></body></html>",
    }

    async def fake_fetch_html(url, **kwargs):
        from scrape_mcp.core.fetcher import FetchOutcome

        return (
            FetchOutcome(
                url=url,
                final_url=url,
                status=200,
                tier="l1",
                blocked="none",
                block_reason="",
                content_type="text/html",
                html=pages[url],
                elapsed_ms=1,
            ),
            None,
        )

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)
    ctx = FakeContext()
    out = json.loads(_run(server.web_crawl("https://site.test/", depth=1, ctx=ctx)))

    assert out["pages_fetched"] == 3
    assert [e[0] for e in ctx.events] == [1, 2, 3]
    assert {e[1] for e in ctx.events} == {out["limit"]}


def test_notify_progress_skips_without_context():
    class Bare:
        request_context = None

        async def report_progress(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("不该上报")

    _run(server._notify_progress(None, 1, 2, "x"))
    _run(server._notify_progress(Bare(), 1, 2, "x"))


def test_context_is_not_exposed_in_tool_schema():
    tools = _run(server.mcp.list_tools())
    for name in ("web_batch", "web_crawl"):
        tool = next(t for t in tools if t.name == name)
        assert "ctx" not in tool.input_schema["properties"]
