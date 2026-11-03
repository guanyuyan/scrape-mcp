"""搜索：结果页解析（含 DuckDuckGo 跳转还原）+ 服务层载荷与错误码。"""

from __future__ import annotations

import asyncio
import json

import pytest

from scrape_mcp import server
from scrape_mcp.core import errors
from scrape_mcp.core.fetcher import FetchOutcome
from scrape_mcp.core.http_client import HttpResponse
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
        "http://searx.test/search?q=a+b&format=json"
    )


def test_search_url_passes_filters_through():
    ddg = search_url("a b", "duckduckgo", site="a.test", time_range="week")
    assert "q=a+b+site%3Aa.test" in ddg and "df=w" in ddg

    searx = search_url(
        "a b",
        "searxng",
        endpoint="http://searx.test",
        site="a.test/docs",
        time_range="month",
        engines=["bing", "startpage"],
    )
    assert "q=a+b+site%3Aa.test%2Fdocs" in searx
    assert "time_range=month" in searx
    assert "engines=bing%2Cstartpage" in searx

    brave = search_url("a b", "brave", site="a.test", time_range="day", limit=5)
    assert "count=5" in brave and "freshness=pd" in brave
    assert "site%3Aa.test" in brave


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
        "attempts",
        "elapsed_ms",
        "cached",
    }
    assert out["ok"] is True and out["provider"] == "duckduckgo"
    assert [item["rank"] for item in out["results"]] == [1, 2]
    assert set(out["results"][0]) == {
        "rank",
        "title",
        "url",
        "snippet",
        "engine",
        "published_at",
    }
    assert out["results"][0]["engine"] == "duckduckgo"


def test_web_search_searxng_payload(monkeypatch):
    settings = server.get_settings()
    monkeypatch.setattr(settings, "search_provider", "searxng")
    monkeypatch.setattr(settings, "search_searxng_url", "http://searx.test")
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
    out = _run(server.web_search("x", provider="duckduckgo", fallback=False))
    assert set(out) == {
        "ok",
        "query",
        "provider",
        "error_code",
        "error",
        "retryable",
        "attempts",
    }
    assert out["error_code"] == errors.TIMEOUT and out["retryable"] is True


def test_web_search_blocked_payload(monkeypatch):
    _patch(monkeypatch, _outcome("", blocked="captcha", block_reason="challenge", status=403))
    out = _run(server.web_search("x"))
    assert out["ok"] is False
    assert out["error_code"] == errors.BLOCKED


def test_web_search_unparsable_body_reports_unsupported(monkeypatch):
    settings = server.get_settings()
    monkeypatch.setattr(settings, "search_provider", "searxng")
    monkeypatch.setattr(settings, "search_searxng_url", "http://searx.test")
    _patch(monkeypatch, _outcome("<html>not json</html>"))
    out = _run(server.web_search("x", fallback=False))
    assert out["error_code"] == errors.UNSUPPORTED_CONTENT


def test_web_search_returns_empty_list_for_no_results(monkeypatch):
    _patch(monkeypatch, _outcome("<html><body><div class='no-results'>无结果</div></body></html>"))
    out = _run(server.web_search("x", provider="duckduckgo"))
    assert out["ok"] is True and out["count"] == 0 and out["results"] == []


# ---- 多后端 / 过滤 / 回退 ----


BRAVE_JSON = json.dumps(
    {
        "web": {
            "results": [
                {
                    "title": "Brave 结果",
                    "url": "https://b.test/1",
                    "description": "Brave 摘要",
                    "page_age": "2026-01-02",
                }
            ]
        }
    },
    ensure_ascii=False,
)

TAVILY_JSON = json.dumps(
    {
        "results": [
            {
                "title": "Tavily 结果",
                "url": "https://t.test/1",
                "content": "Tavily 摘要",
                "published_date": "2026-02-03",
            }
        ]
    },
    ensure_ascii=False,
)


def test_searxng_parser_keeps_engine_and_published_at():
    body = json.dumps(
        {
            "results": [
                {
                    "title": "结果",
                    "url": "https://a.test/1",
                    "content": "摘要",
                    "engine": "bing",
                    "publishedDate": "2026-03-04T10:00:00",
                }
            ]
        }
    )
    hits = parse_results("searxng", body, 10)
    assert hits[0].engine == "bing"
    assert hits[0].published_at == "2026-03-04T10:00:00"


def test_brave_parser_reads_web_results():
    hits = parse_results("brave", BRAVE_JSON, 10)
    assert hits[0].url == "https://b.test/1"
    assert hits[0].snippet == "Brave 摘要"
    assert hits[0].engine == "brave"
    assert hits[0].published_at == "2026-01-02"


def test_tavily_parser_reads_results():
    hits = parse_results("tavily", TAVILY_JSON, 10)
    assert hits[0].url == "https://t.test/1"
    assert hits[0].engine == "tavily"
    assert hits[0].published_at == "2026-02-03"


def test_brave_parser_rejects_missing_web_results():
    with pytest.raises(SearchError):
        parse_results("brave", json.dumps({"query": "x"}), 10)


def test_web_search_brave_sends_key_and_freshness(monkeypatch):
    settings = server.get_settings()
    monkeypatch.setattr(settings, "search_brave_api_key", "brave-key")

    seen: dict = {}

    class _Fake:
        async def fetch(self, url: str, **kwargs) -> FetchOutcome:
            seen.update(url=url, headers=kwargs.get("headers"))
            return _outcome(BRAVE_JSON, content_type="application/json")

    async def allowed(_url: str, *_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(server, "_compliance_check", allowed)
    monkeypatch.setattr(server, "_get_fetcher", lambda: _Fake())
    out = _run(
        server.web_search(
            "x",
            provider="brave",
            site="a.test",
            time_range="week",
            fallback=False,
            bypass_cache=True,
        )
    )
    assert out["ok"] is True and out["provider"] == "brave"
    assert out["results"][0]["published_at"] == "2026-01-02"
    assert "freshness=pw" in seen["url"] and "count=10" in seen["url"]
    assert seen["headers"]["X-Subscription-Token"] == "brave-key"


def test_web_search_tavily_posts_json(monkeypatch):
    settings = server.get_settings()
    monkeypatch.setattr(settings, "search_tavily_api_key", "tavily-key")

    seen: dict = {}

    class _Fake:
        async def post_json(self, url: str, **kwargs) -> HttpResponse:
            seen.update(url=url, **kwargs)
            return HttpResponse(
                ok=True,
                status=200,
                url=url,
                content_type="application/json",
                text=TAVILY_JSON,
                elapsed_ms=5,
            )

    async def allowed(_url: str, *_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(server, "_compliance_check", allowed)
    monkeypatch.setattr(server, "_get_fetcher", lambda: _Fake())
    out = _run(
        server.web_search(
            "x",
            provider="tavily",
            site="a.test",
            time_range="month",
            fallback=False,
            bypass_cache=True,
        )
    )
    assert out["ok"] is True and out["provider"] == "tavily"
    assert seen["payload"]["api_key"] == "tavily-key"
    assert seen["payload"]["time_range"] == "month"
    assert seen["payload"]["include_domains"] == ["a.test"]
    assert "site:a.test" in seen["payload"]["query"]


def test_web_search_falls_back_to_searxng_when_duckduckgo_is_blocked(monkeypatch):
    settings = server.get_settings()
    monkeypatch.setattr(settings, "search_searxng_url", "http://searx.test")
    monkeypatch.setattr(settings, "search_fallback", True)

    class _Fake:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def fetch(self, url: str, **_kwargs) -> FetchOutcome:
            self.urls.append(url)
            if "duckduckgo" in url:
                return _outcome("", status=202, blocked="captcha", block_reason="anomaly")
            return _outcome(SEARXNG_JSON, content_type="application/json")

    fake = _Fake()

    async def allowed(_url: str, *_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(server, "_compliance_check", allowed)
    monkeypatch.setattr(server, "_get_fetcher", lambda: fake)
    out = _run(server.web_search("x", provider="duckduckgo", bypass_cache=True))

    assert out["ok"] is True and out["provider"] == "searxng"
    assert [item["provider"] for item in out["attempts"]] == ["duckduckgo"]
    assert out["attempts"][0]["error_code"] == errors.BLOCKED
    assert len(fake.urls) == 2


def test_web_search_fallback_off_returns_primary_error(monkeypatch):
    settings = server.get_settings()
    monkeypatch.setattr(settings, "search_searxng_url", "http://searx.test")

    _patch(monkeypatch, _outcome("", error="ReadTimeout: timed out"))
    out = _run(server.web_search("x", provider="duckduckgo", fallback=False, bypass_cache=True))

    assert out["ok"] is False and out["provider"] == "duckduckgo"
    assert out["error_code"] == errors.TIMEOUT
    assert len(out["attempts"]) == 1


def test_web_search_invalid_filters_are_rejected():
    assert _run(server.web_search("x", time_range="hour"))["error_code"] == errors.INVALID_ARGUMENT
    assert _run(server.web_search("x", site="  "))["error_code"] == errors.INVALID_ARGUMENT
    assert _run(server.web_search("x", engines=[]))["error_code"] == errors.INVALID_ARGUMENT


def test_web_search_normalizes_site_url(monkeypatch):
    _patch(monkeypatch, _outcome(DDG_HTML))
    out = _run(
        server.web_search(
            "x",
            provider="duckduckgo",
            site="https://a.test/",
            fallback=False,
            bypass_cache=True,
        )
    )
    assert out["ok"] is True and out["results"][0]["url"] == "https://a.test/doc"
