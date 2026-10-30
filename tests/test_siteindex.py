"""站点索引：sitemap 解析（含 gz / index）、状态对比与服务层载荷。"""

from __future__ import annotations

import asyncio
import gzip
import json

import pytest

from scrape_mcp import server
from scrape_mcp.core import errors
from scrape_mcp.core.fetcher import FetchOutcome
from scrape_mcp.core.siteindex import (
    SitemapError,
    decode_sitemap,
    diff_index,
    parse_sitemap,
    sitemap_candidates,
)

ROBOTS = "https://a.test/robots.txt"
SITEMAP = "https://a.test/sitemap.xml"


def _urlset(entries: list[tuple[str, str]]) -> str:
    rows = "".join(
        f"<url><loc>{url}</loc>" + (f"<lastmod>{mod}</lastmod>" if mod else "") + "</url>"
        for url, mod in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + rows + "</urlset>"
    )


def _index(children: list[str]) -> str:
    rows = "".join(f"<sitemap><loc>{url}</loc></sitemap>" for url in children)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + rows
        + "</sitemapindex>"
    )


def _outcome(html: str = "", *, content: bytes = b"", **overrides) -> FetchOutcome:
    base = {
        "url": SITEMAP,
        "final_url": SITEMAP,
        "status": 200,
        "tier": "l1",
        "blocked": "none",
        "block_reason": "",
        "content_type": "application/xml",
        "html": html,
        "content": content,
        "elapsed_ms": 5,
    }
    base.update(overrides)
    return FetchOutcome(**base)


class _FakeFetcher:
    def __init__(self, pages: dict[str, FetchOutcome]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    async def fetch(self, url: str, **_kwargs) -> FetchOutcome:
        self.calls.append(url)
        return self.pages.get(url) or _outcome(status=404, final_url=url)


def _patch(monkeypatch, pages: dict[str, FetchOutcome]) -> _FakeFetcher:
    async def allowed(_url: str, *_args, **_kwargs) -> None:
        return None

    fake = _FakeFetcher(pages)
    monkeypatch.setattr(server, "_compliance_check", allowed)
    monkeypatch.setattr(server, "_get_fetcher", lambda: fake)
    return fake


def _run(coro) -> dict:
    return json.loads(asyncio.run(coro))


def _standard_pages(xml: str) -> dict[str, FetchOutcome]:
    return {
        ROBOTS: _outcome(f"Sitemap: {SITEMAP}"),
        SITEMAP: _outcome(xml, content=xml.encode()),
    }


# ---- 纯解析 ----


def test_sitemap_candidates_reads_robots_lines():
    text = "User-agent: *\nSitemap: https://a.test/sitemap.xml\nsitemap: /sitemap2.xml\n"
    assert sitemap_candidates(text, "https://a.test") == [
        "https://a.test/sitemap.xml",
        "https://a.test/sitemap2.xml",
    ]


def test_parse_urlset_with_namespace_and_lastmod():
    entries, children = parse_sitemap(
        _urlset([("https://x.test/a", "2026-01-01"), ("https://x.test/b", "")])
    )
    assert entries == [
        {"url": "https://x.test/a", "lastmod": "2026-01-01"},
        {"url": "https://x.test/b", "lastmod": ""},
    ]
    assert children == []


def test_parse_sitemapindex_returns_children():
    entries, children = parse_sitemap(_index(["https://a.test/s1.xml", "https://a.test/s2.xml"]))
    assert entries == []
    assert children == ["https://a.test/s1.xml", "https://a.test/s2.xml"]


def test_decode_sitemap_unpacks_gzip_by_magic():
    raw = gzip.compress(_urlset([("https://a.test/x", "")]).encode())
    assert "<urlset" in decode_sitemap(raw, "garbage")
    assert decode_sitemap(_urlset([("https://a.test/x", "")]).encode(), "") != ""


def test_parse_sitemap_rejects_broken_xml():
    with pytest.raises(SitemapError):
        parse_sitemap("<html>not a sitemap</html>")


def test_diff_index_classifies_new_changed_unchanged_removed():
    previous = {
        "entries": {
            "https://a.test/1": {"lastmod": "2026-01-01"},
            "https://a.test/2": {"lastmod": "2026-02-01"},
        }
    }
    current = [
        {"url": "https://a.test/1", "lastmod": "2026-03-01"},
        {"url": "https://a.test/3", "lastmod": "2026-04-01"},
    ]
    diff = diff_index(previous, current)
    assert diff["new"] == ["https://a.test/3"]
    assert diff["changed"] == ["https://a.test/1"]
    assert diff["unchanged"] == []
    assert diff["removed"] == ["https://a.test/2"]


# ---- 服务层 ----


def test_web_index_first_run_persists_baseline(monkeypatch, tmp_path):
    xml = _urlset([("https://a.test/a", "2026-01-01"), ("https://a.test/b", "2026-02-01")])
    _patch(monkeypatch, _standard_pages(xml))
    out = _run(server.web_index("https://a.test/"))
    assert set(out) == {
        "ok",
        "url",
        "host",
        "sitemaps",
        "total",
        "entries",
        "new",
        "changed",
        "unchanged",
        "removed",
        "new_urls",
        "changed_urls",
        "removed_urls",
        "content_checked",
        "first_seen",
        "truncated",
        "dry_run",
        "elapsed_ms",
        "errors",
    }
    assert out["ok"] is True and out["new"] == 2 and out["first_seen"] is True
    assert out["sitemaps"] == 1 and out["errors"] == []
    assert (tmp_path / "data" / "index" / "a.test.json").exists()


def test_web_index_second_run_reports_diff(monkeypatch):
    first = _urlset([("https://a.test/a", "2026-01-01"), ("https://a.test/b", "2026-02-01")])
    second = _urlset([("https://a.test/a", "2026-03-01"), ("https://a.test/c", "2026-04-01")])
    _patch(monkeypatch, _standard_pages(first))
    assert _run(server.web_index("https://a.test/"))["new"] == 2
    _patch(monkeypatch, _standard_pages(second))
    out = _run(server.web_index("https://a.test/"))
    assert out["new"] == 1 and out["changed"] == 1
    assert out["unchanged"] == 0 and out["removed"] == 1
    assert out["first_seen"] is False


def test_web_index_applies_filters_and_limit(monkeypatch):
    xml = _urlset(
        [
            ("https://a.test/docs/1", ""),
            ("https://a.test/docs/2", ""),
            ("https://a.test/blog/1", ""),
        ]
    )
    _patch(monkeypatch, _standard_pages(xml))
    out = _run(server.web_index("https://a.test/", exclude="blog", limit=1, dry_run=True))
    assert out["total"] == 2 and out["entries"] == 1 and out["truncated"] is True
    assert out["dry_run"] is True and out["new_urls"] == ["https://a.test/docs/1"]


def test_web_index_recurses_sitemap_index(monkeypatch):
    s1 = "https://a.test/s1.xml"
    s2 = "https://a.test/s2.xml"
    pages = {
        ROBOTS: _outcome(f"Sitemap: {SITEMAP}"),
        SITEMAP: _outcome(_index([s1, s2])),
        s1: _outcome(_urlset([("https://a.test/a", "")])),
        s2: _outcome(_urlset([("https://a.test/b", "")])),
    }
    _patch(monkeypatch, pages)
    out = _run(server.web_index("https://a.test/", dry_run=True))
    assert out["sitemaps"] == 3 and out["total"] == 2


def test_web_index_supports_gzip_sitemap(monkeypatch):
    xml = _urlset([("https://a.test/a", "")])
    gz = gzip.compress(xml.encode())
    pages = {ROBOTS: _outcome("Sitemap: https://a.test/sitemap.xml.gz")}
    pages["https://a.test/sitemap.xml.gz"] = _outcome(
        "garbage", content=gz, content_type="application/gzip"
    )
    _patch(monkeypatch, pages)
    out = _run(server.web_index("https://a.test/", dry_run=True))
    assert out["ok"] is True and out["total"] == 1


def test_web_index_direct_sitemap_url_skips_robots(monkeypatch):
    xml = _urlset([("https://a.test/a", "")])
    fake = _patch(monkeypatch, {SITEMAP: _outcome(xml, content=xml.encode())})
    out = _run(server.web_index(SITEMAP, dry_run=True))
    assert out["ok"] is True and out["sitemaps"] == 1
    assert fake.calls == [SITEMAP]


def test_web_index_reports_no_sitemap(monkeypatch):
    _patch(monkeypatch, {})
    out = _run(server.web_index("https://a.test/"))
    assert out["ok"] is False
    assert out["error_code"] == errors.HTTP_4XX
    assert "sitemap" in out["error"]


def test_web_index_rejects_bad_regex():
    out = _run(server.web_index("https://a.test/", include="["))
    assert out["error_code"] == errors.INVALID_ARGUMENT


def test_web_index_check_content_marks_unchanged_by_hash(monkeypatch, tmp_path):
    html = "<html><body><article><p>正文没有变</p></article></body></html>"
    first = _urlset([("https://a.test/a", "2026-01-01")])
    second = _urlset([("https://a.test/a", "2026-02-01")])
    pages = _standard_pages(first)
    pages["https://a.test/a"] = _outcome(html)
    _patch(monkeypatch, pages)
    _run(server.web_index("https://a.test/", check_content=True))

    pages2 = _standard_pages(second)
    pages2["https://a.test/a"] = _outcome(html)
    _patch(monkeypatch, pages2)
    out = _run(server.web_index("https://a.test/", check_content=True))
    # lastmod 变了但正文哈希相同：不应报成 changed
    assert out["changed"] == 0 and out["content_checked"] == 1
