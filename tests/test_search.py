"""搜索：结果页解析（含 DuckDuckGo 跳转还原）+ 服务层载荷与错误码。"""

from __future__ import annotations

import asyncio
import json

import pytest

from scrape_mcp import server
from scrape_mcp.core import errors
from scrape_mcp.core.fetcher import FetchOutcome
from scrape_mcp.core.search import SearchError, parse_results, search_url

URL = "https://html.duckduckgo.com/html/?q=x"

DDG_HTML = """
<html><body>
<div class="results">
  <div class="result results_links result--ad">
    <h2 class="result__title"><a class="result__a"
      href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fads.test%2F">广告位</a></h2>
  </div>
  <div class="result results_links">
    <h2 class="result__title"><a class="result__a"
      href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.test%2Fdoc">文档 A</a></h2>
    <a class="result__snippet" href="#">摘要 A</a>
  </div>
  <div class="result results_links">
    <h2 class="result__title"><a class="result__a"
      href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fb.test%2Fpost%3Fid%3D1">文档 B</a></h2>
    <a class="result__snippet" href="#">摘要 B</a>
  </div>
</div>
</body></html>
"""

SEARXNG_JSON = json.dumps(
    {
        "results": [
            {"title": "结果一", "url": "https://a.test/1", "content": "摘要一"},
            {"title": "结果二", "url": "https://a.test/2", "content": "摘要二"},
        ]
    },
    ensure_ascii=False,
)


def _outcome(html: str, **overrides) -> FetchOutcome:
    base = {
        "url": URL,
        "final_url": URL,
        "status": 200,
        "tier": "l1",
        "blocked": "none",
        "block_reason": "",
        "content_type": "text/html",
        "html": html,
        "elapsed_ms": 7,
    }
    base.update(overrides)
    return FetchOutcome(**base)


class _FakeFetcher:
    def __init__(self, outcome: FetchOutcome) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    async def fetch(self, url: str, **kwargs) -> FetchOutcome:
        self.calls.append(kwargs)
        return self.outcome


def _patch(monkeypatch, outcome: FetchOutcome) -> None:
    async def allowed(_url: str, *_args, **_kwargs) -> None:
        return None

    fake = _FakeFetcher(outcome)
    monkeypatch.setattr(server, "_compliance_check", allowed)
    monkeypatch.setattr(server, "_get_fetcher", lambda: fake)


def _run(coro) -> dict:
    return json.loads(asyncio.run(coro))


# ---- 纯解析 ----


def test_duckduckgo_parser_unwraps_links_and_skips_ads():
    hits = parse_results("duckduckgo", DDG_HTML, 10)
    assert [hit.url for hit in hits] == ["https://a.test/doc", "https://b.test/post?id=1"]
    assert hits[0].title == "文档 A"
    assert hits[0].snippet == "摘要 A"
    assert all("ads.test" not in hit.url for hit in hits)


def test_duckduckgo_parser_respects_max_results():
    assert len(parse_results("duckduckgo", DDG_HTML, 1)) == 1


def test_duckduckgo_parser_rejects_challenge_page():
    challenge = (
        '<html><body><form id="challenge-form" '
        'action="//duckduckgo.com/anomaly.js?sv=html"></form></body></html>'
    )
    with pytest.raises(SearchError):
        parse_results("duckduckgo", challenge, 10)


def test_searxng_parser_reads_json_and_limits():
    hits = parse_results("searxng", SEARXNG_JSON, 1)
    assert len(hits) == 1
    assert hits[0].url == "https://a.test/1"
    assert hits[0].snippet == "摘要一"


def test_parser_rejects_non_json_for_searxng():
    with pytest.raises(SearchError):
        parse_results("searxng", "<html>not json</html>", 10)


def test_searxng_parser_unwraps_html_pre_wrapper():
    wrapped = f"<html><body><pre>{SEARXNG_JSON}</pre></body></html>"
    hits = parse_results("searxng", wrapped, 10)
    assert [hit.url for hit in hits] == ["https://a.test/1", "https://a.test/2"]


def test_search_url_by_provider():
    assert search_url("a b", "duckduckgo").startswith("https://html.duckduckgo.com/html/?q=a+b")
    assert search_url("a b", "searxng", endpoint="http://searx.test/") == (
        "http://searx.test/search?q=a+b&format=json&language=all"
    )


# ---- 服务层 ----


def test_web_search_success_payload(monkeypatch):
    _patch(monkeypatch, _outcome(DDG_HTML))
    out = _run(server.web_search("测试", provider="duckduckgo"))
    assert set(out) == {
        "ok",
        "query",
        "provider",
        "count",
        "results",
        "elapsed_ms",
        "cached",
    }
    assert out["ok"] is True and out["provider"] == "duckduckgo"
    assert [item["rank"] for item in out["results"]] == [1, 2]
    assert set(out["results"][0]) == {"rank", "title", "url", "snippet"}


def test_web_search_searxng_payload(monkeypatch):
    settings = server.get_settings()
    settings.search_provider = "searxng"
    settings.search_searxng_url = "http://searx.test"
    _patch(monkeypatch, _outcome(SEARXNG_JSON, content_type="application/json"))
    out = _run(server.web_search("测试", max_results=2))
    assert out["ok"] is True and out["provider"] == "searxng"
    assert out["count"] == 2


def test_web_search_empty_query_is_invalid():
    out = _run(server.web_search("   "))
    assert set(out) == {"ok", "error_code", "error", "retryable"}
    assert out["error_code"] == errors.INVALID_ARGUMENT


def test_web_search_unknown_provider_is_invalid():
    out = _run(server.web_search("x", provider="google"))
    assert out["error_code"] == errors.INVALID_ARGUMENT


def test_web_search_searxng_without_endpoint_is_invalid(monkeypatch):
    monkeypatch.setattr(server.get_settings(), "search_searxng_url", "")
    out = _run(server.web_search("x", provider="searxng"))
    assert out["error_code"] == errors.INVALID_ARGUMENT
    assert "SEARXNG_URL" in out["error"]


def test_web_search_transport_error_payload(monkeypatch):
    _patch(monkeypatch, _outcome("", error="ReadTimeout: timed out"))
    out = _run(server.web_search("x"))
    assert set(out) == {"ok", "query", "provider", "error_code", "error", "retryable"}
    assert out["error_code"] == errors.TIMEOUT and out["retryable"] is True


def test_web_search_blocked_payload(monkeypatch):
    _patch(monkeypatch, _outcome("", blocked="captcha", block_reason="challenge", status=403))
    out = _run(server.web_search("x"))
    assert out["ok"] is False
    assert out["error_code"] == errors.BLOCKED


def test_web_search_unparsable_body_reports_unsupported(monkeypatch):
    settings = server.get_settings()
    settings.search_provider = "searxng"
    settings.search_searxng_url = "http://searx.test"
    _patch(monkeypatch, _outcome("<html>not json</html>"))
    out = _run(server.web_search("x"))
    assert out["error_code"] == errors.UNSUPPORTED_CONTENT


def test_web_search_returns_empty_list_for_no_results(monkeypatch):
    _patch(monkeypatch, _outcome("<html><body><div class='no-results'>无结果</div></body></html>"))
    out = _run(server.web_search("x", provider="duckduckgo"))
    assert out["ok"] is True and out["count"] == 0 and out["results"] == []
