"""web_map 离线单测：用假 sitemap 与假 _fetch_html 驱动，不联网。"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp import server
from scrape_mcp.core.fetcher import FetchOutcome

HOME = "https://site.test/"
HTML = (
    "<html><body>"
    '<a href="/a">A</a><a href="/blog/1">B</a>'
    '<a href="https://other.test/x">外</a>'
    "</body></html>"
)


def _outcome(url: str, html: str = HTML) -> FetchOutcome:
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


def _run(**kwargs) -> dict:
    return json.loads(asyncio.run(server.web_map(**kwargs)))


def test_merges_sitemap_and_homepage_without_duplicates(monkeypatch):
    async def fake_sitemap(url, *, deadline, proxy):
        return (
            [{"url": "https://site.test/s1"}, {"url": "https://site.test/a"}],
            [],
            1,
        )

    async def fake_fetch(url, **_kwargs):
        if url == HOME:
            return _outcome(url), None
        return None, {"ok": False, "url": url, "error_code": "http_4xx", "error": "404"}

    monkeypatch.setattr(server, "_sitemap_entries", fake_sitemap)
    monkeypatch.setattr(server, "_fetch_html", fake_fetch)

    out = _run(url=HOME)
    assert out["ok"] is True
    assert [item["url"] for item in out["urls"]] == [
        "https://site.test/s1",
        "https://site.test/a",
        "https://site.test/blog/1",
    ]
    assert out["sources"] == {"sitemap": 2, "homepage": 1, "search": 0}
    assert out["count"] == 3
    assert out["truncated"] is False


def test_homepage_links_work_when_sitemap_missing(monkeypatch):
    async def fake_sitemap(url, *, deadline, proxy):
        return (
            [],
            [{"url": "https://site.test/sitemap.xml", "error_code": "http_4xx", "error": "404"}],
            0,
        )

    async def fake_fetch(url, **_kwargs):
        return _outcome(url), None

    monkeypatch.setattr(server, "_sitemap_entries", fake_sitemap)
    monkeypatch.setattr(server, "_fetch_html", fake_fetch)

    out = _run(url=HOME)
    assert out["ok"] is True
    assert out["sources"]["homepage"] == 2
    assert out["sources"]["sitemap"] == 0


def test_filters_and_limit(monkeypatch):
    async def fake_sitemap(url, *, deadline, proxy):
        return (
            [
                {"url": "https://site.test/a"},
                {"url": "https://site.test/blog/1"},
                {"url": "https://site.test/blog/2"},
            ],
            [],
            1,
        )

    monkeypatch.setattr(server, "_sitemap_entries", fake_sitemap)
    out = _run(url=HOME, include="/blog/", homepage=False, limit=1)
    assert [item["url"] for item in out["urls"]] == ["https://site.test/blog/1"]
    assert out["truncated"] is True


def test_all_sources_fail_returns_error(monkeypatch):
    async def fake_sitemap(url, *, deadline, proxy):
        return (
            [],
            [{"url": "https://site.test/sitemap.xml", "error_code": "http_4xx", "error": "404"}],
            0,
        )

    async def fake_fetch(url, **_kwargs):
        return None, {"ok": False, "url": url, "error_code": "http_5xx", "error": "boom"}

    monkeypatch.setattr(server, "_sitemap_entries", fake_sitemap)
    monkeypatch.setattr(server, "_fetch_html", fake_fetch)

    out = _run(url=HOME)
    assert out["ok"] is False
    assert out["error_code"] == "http_4xx"
    assert out["host"] == "site.test"


def test_optional_search_results_are_merged(monkeypatch):
    async def fake_sitemap(url, *, deadline, proxy):
        return [{"url": "https://site.test/a"}], [], 1

    async def fake_search(query, **_kwargs):
        return json.dumps(
            {
                "ok": True,
                "results": [
                    {"url": "https://site.test/from-search"},
                    {"url": "https://other.test/nope"},
                ],
            }
        )

    monkeypatch.setattr(server, "_sitemap_entries", fake_sitemap)
    monkeypatch.setattr(server, "web_search", fake_search)
    out = _run(url=HOME, homepage=False, use_search=True)
    assert [item["url"] for item in out["urls"]] == [
        "https://site.test/a",
        "https://site.test/from-search",
    ]
    assert out["sources"]["search"] == 1


def test_bad_regex_is_invalid_argument():
    out = _run(url=HOME, include="[")
    assert out["ok"] is False
    assert out["error_code"] == "invalid_argument"
