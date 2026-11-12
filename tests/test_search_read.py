"""web_search(read=true)：搜索即阅读的并发、去重、预算与失败隔离。"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp import server
from scrape_mcp.core import errors
from scrape_mcp.core.fetcher import FetchOutcome

SEARX = "http://searx.test/search"


def _json(*urls: str) -> str:
    return json.dumps(
        {
            "results": [
                {"title": f"结果 {i}", "url": url, "content": f"摘要 {i}"}
                for i, url in enumerate(urls, start=1)
            ]
        },
        ensure_ascii=False,
    )


def _article(title: str, body: str = "正文内容。" * 40) -> str:
    return f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1><p>{body}</p></article></body></html>"


def _outcome(url: str, html: str, **overrides) -> FetchOutcome:
    base = {
        "url": url,
        "final_url": url,
        "status": 200,
        "tier": "l1",
        "blocked": "none",
        "block_reason": "",
        "content_type": "application/json" if html.startswith("{") else "text/html",
        "html": html,
        "elapsed_ms": 5,
    }
    base.update(overrides)
    return FetchOutcome(**base)


class _Fake:
    def __init__(self, search_html: str, articles: dict[str, str], errors: set[str] | None = None):
        self.search_html = search_html
        self.articles = articles
        self.errors = errors or set()
        self.calls: list[str] = []

    async def fetch(self, url: str, **_kwargs) -> FetchOutcome:
        self.calls.append(url)
        if "searx.test" in url:
            return _outcome(url, self.search_html)
        if url in self.errors:
            return _outcome(url, "", status=504, error="ReadTimeout: timed out")
        return _outcome(url, self.articles.get(url, _article("其它")))


def _patch(monkeypatch, fake: _Fake) -> None:
    async def allowed(_url: str, *_args, **_kwargs) -> None:
        return None

    settings = server.get_settings()
    monkeypatch.setattr(settings, "search_searxng_url", SEARX)
    monkeypatch.setattr(settings, "search_fallback", False)
    monkeypatch.setattr(server, "_compliance_check", allowed)
    monkeypatch.setattr(server, "_get_fetcher", lambda: fake)


def _run(coro) -> dict:
    return json.loads(asyncio.run(coro))


def _search(**options) -> dict:
    return _run(server.web_search("x", provider="searxng", **options))


def test_read_false_keeps_legacy_payload(monkeypatch):
    fake = _Fake(_json("https://a.test/1", "https://b.test/2"), {})
    _patch(monkeypatch, fake)
    out = _search(bypass_cache=True)
    assert out["ok"] is True
    assert "content" not in out["results"][0]
    assert "tokens" not in out


def test_read_true_returns_content_and_budget(monkeypatch):
    art1, art2 = "https://a.test/1", "https://b.test/2"
    fake = _Fake(_json(art1, art2), {art1: _article("甲"), art2: _article("乙")})
    _patch(monkeypatch, fake)
    out = _search(read=True, top_k=2, content_format="markdown", bypass_cache=True)
    assert out["ok"] is True
    assert out["tokens"]["results_read"] == 2
    assert out["tokens"]["used"] > 0
    assert out["results"][0]["content"] and out["results"][0]["content_chars"] > 0
    assert out["results"][0]["truncated"] is False


def test_read_true_caps_per_host(monkeypatch):
    urls = ("https://a.test/1", "https://a.test/2", "https://a.test/3")
    fake = _Fake(_json(*urls), {url: _article("同站") for url in urls})
    _patch(monkeypatch, fake)
    out = _search(read=True, top_k=3, bypass_cache=True)
    with_content = [item for item in out["results"] if item.get("content")]
    assert len(with_content) == 2
    assert out["results"][2]["truncated"] is True
    assert out["tokens"]["results_read"] == 2


def test_read_true_respects_token_budget(monkeypatch):
    urls = ("https://a.test/1", "https://b.test/2")
    fake = _Fake(_json(*urls), {url: _article("长文", "很长的正文。" * 800) for url in urls})
    _patch(monkeypatch, fake)
    out = _search(read=True, top_k=2, max_tokens=500, bypass_cache=True)
    assert out["tokens"]["budget"] == 500
    assert out["tokens"]["results_read"] == 1
    assert out["tokens"]["results_skipped"] >= 1


def test_read_true_isolates_single_page_failure(monkeypatch):
    bad, good = "https://a.test/bad", "https://b.test/good"
    fake = _Fake(_json(bad, good), {good: _article("正常")}, errors={bad})
    _patch(monkeypatch, fake)
    out = _search(read=True, top_k=2, bypass_cache=True)
    assert out["ok"] is True
    assert out["results"][0]["read_error"].startswith("ReadTimeout")
    assert out["results"][1]["content"]


def test_read_true_dedupes_tracking_variants(monkeypatch):
    first = "https://a.test/post?utm_source=x"
    second = "https://a.test/post?utm_source=y"
    fake = _Fake(_json(first, second), {first: _article("同一篇"), second: _article("同一篇")})
    _patch(monkeypatch, fake)
    out = _search(read=True, top_k=2, bypass_cache=True)
    assert out["tokens"]["results_read"] == 1
    assert out["results"][1]["truncated"] is True


def test_read_true_text_format_strips_markdown(monkeypatch):
    url = "https://a.test/1"
    fake = _Fake(_json(url), {url: _article("标题")})
    _patch(monkeypatch, fake)
    out = _search(read=True, top_k=1, content_format="text", bypass_cache=True)
    content = out["results"][0]["content"]
    assert not content.startswith("#")
    assert "标题" in content


def test_read_true_validates_arguments():
    assert _search(read="yes")["error_code"] == errors.INVALID_ARGUMENT
    assert _search(read=True, top_k=0)["error_code"] == errors.INVALID_ARGUMENT
    assert _search(read=True, top_k=11)["error_code"] == errors.INVALID_ARGUMENT
    assert _search(read=True, max_tokens=100)["error_code"] == errors.INVALID_ARGUMENT
    assert _search(read=True, content_format="html")["error_code"] == errors.INVALID_ARGUMENT


ARTICLE = (
    "<html><body><article><h1>版本说明</h1>"
    "<p>基础版每月 29 元，适合个人开发者。</p>"
    "<h2>专业版</h2><p>专业版每月 99 元，适合团队协作。</p>"
    "</article></body></html>"
)


def test_read_excerpt_returns_relevant_passages_only(monkeypatch):
    url = "https://a.test/price"
    fake = _Fake(_json(url), {url: ARTICLE})
    _patch(monkeypatch, fake)

    out = _run(
        server.web_search(
            "专业版多少钱",
            provider="searxng",
            read="excerpt",
            top_k=1,
            bypass_cache=True,
        )
    )

    item = out["results"][0]
    assert out["tokens"]["mode"] == "excerpt"
    assert item["excerpt_fallback"] is False
    assert item["excerpts"] and "99" in item["excerpts"][0]["text"]
    assert "content" not in item  # 摘录模式不返回整页正文
    assert item["excerpt_chars"] > 0


def test_read_excerpt_falls_back_when_nothing_matches(monkeypatch):
    url = "https://a.test/price"
    fake = _Fake(_json(url), {url: ARTICLE})
    _patch(monkeypatch, fake)

    out = _run(
        server.web_search(
            "量子计算",
            provider="searxng",
            read="excerpt",
            top_k=1,
            bypass_cache=True,
        )
    )

    item = out["results"][0]
    assert item["excerpt_fallback"] is True
    assert item["excerpts"] == []
    assert "基础版每月" in item["content"]  # 回退整页，不返回空结果


def test_fetch_with_query_returns_excerpts(monkeypatch):
    url = "https://a.test/price"
    fake = _Fake("", {url: ARTICLE})
    _patch(monkeypatch, fake)

    out = _run(server.web_fetch(url, query="专业版多少钱", bypass_cache=True))

    assert out["ok"] is True
    assert out["excerpt_fallback"] is False
    assert "99" in out["content"]
    assert out["excerpts"][0]["heading"] == "专业版"
    assert out["truncated"] is True


def test_fetch_with_query_validates_and_falls_back(monkeypatch):
    url = "https://a.test/price"
    fake = _Fake("", {url: ARTICLE})
    _patch(monkeypatch, fake)

    assert (
        _run(server.web_fetch(url, query="x", format="json"))["error_code"]
        == errors.INVALID_ARGUMENT
    )
    assert _run(server.web_fetch(url, query="   "))["error_code"] == errors.INVALID_ARGUMENT

    out = _run(server.web_fetch(url, query="量子计算", bypass_cache=True))
    assert out["excerpt_fallback"] is True
    assert out["excerpts"] == []
    assert "基础版每月" in out["content"]
