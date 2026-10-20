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


# ---------- 分页模式 ----------

NEXT = '<a class="next" href="{}">下一页</a>'
PAGINATED = {
    "https://site.test/list": (
        f"<html><body><article><p>第一页</p></article>{NEXT.format('/list?page=2')}</body></html>"
    ),
    "https://site.test/list?page=2": (
        f"<html><body><article><p>第二页</p></article>{NEXT.format('/list?page=3')}</body></html>"
    ),
    "https://site.test/list?page=3": ("<html><body><article><p>第三页</p></article></body></html>"),
}


def _install_pages(monkeypatch, pages: dict[str, str], called: list[str] | None = None) -> None:
    async def fake_fetch_html(url, **kwargs):
        if called is not None:
            called.append(url)
        html = pages.get(url)
        if html is None:
            return None, {"ok": False, "url": url, "error_code": "http_4xx", "error": "404"}
        return _outcome(url, html), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)


def test_paginate_follows_next_until_missing(monkeypatch):
    called: list[str] = []
    _install_pages(monkeypatch, PAGINATED, called)
    out = _run(url="https://site.test/list", next_selector="a.next", max_pages=10)

    assert out["mode"] == "paginate"
    assert out["depth"] == 0
    assert called == [
        "https://site.test/list",
        "https://site.test/list?page=2",
        "https://site.test/list?page=3",
    ]
    assert out["pages_fetched"] == 3
    assert out["ok_count"] == 3


def test_paginate_respects_max_pages(monkeypatch):
    called: list[str] = []
    _install_pages(monkeypatch, PAGINATED, called)
    out = _run(url="https://site.test/list", next_selector="a.next", max_pages=2)

    assert out["pages_fetched"] == 2
    assert out["limit"] == 2
    assert called == ["https://site.test/list", "https://site.test/list?page=2"]


def test_paginate_stops_on_loop(monkeypatch):
    pages = {
        "https://site.test/list": (
            f"<html><body><article><p>第一页</p></article>{NEXT.format('/list?page=2')}</body></html>"
        ),
        "https://site.test/list?page=2": (
            f"<html><body><article><p>第二页</p></article>{NEXT.format('/list')}</body></html>"
        ),
    }
    _install_pages(monkeypatch, pages)
    out = _run(url="https://site.test/list", next_selector="a.next", max_pages=10)

    assert out["pages_fetched"] == 2
    assert out["links_discovered"] == 1


def test_paginate_does_not_follow_external_next(monkeypatch):
    pages = {
        "https://site.test/list": (
            f"<html><body><article><p>第一页</p></article>"
            f"{NEXT.format('https://other.test/page2')}</body></html>"
        ),
    }
    called: list[str] = []
    _install_pages(monkeypatch, pages, called)
    out = _run(url="https://site.test/list", next_selector="a.next")

    assert out["pages_fetched"] == 1
    assert called == ["https://site.test/list"]


def test_invalid_next_selector_rejected(monkeypatch):
    _install(monkeypatch)
    out = _run(url="https://site.test/", next_selector="   ")
    assert out["ok"] is False and out["error_code"] == "invalid_argument"


def test_invalid_max_pages_rejected(monkeypatch):
    _install(monkeypatch)
    out = _run(url="https://site.test/", next_selector="a.next", max_pages=0)
    assert out["ok"] is False and out["error_code"] == "invalid_argument"


def test_default_mode_is_links(monkeypatch):
    _install(monkeypatch)
    out = _run(url="https://site.test/", depth=0)
    assert out["mode"] == "links"


def test_recipe_supplies_pagination(tmp_path, monkeypatch):
    from scrape_mcp.core.recipes import RecipeStore

    directory = tmp_path / "recipes"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "site.test.json").write_text(
        json.dumps({"crawl": {"next_selector": "a.next", "max_pages": 2}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "get_recipe_store", lambda: RecipeStore(directory))
    _install_pages(monkeypatch, PAGINATED)

    out = _run(url="https://site.test/list")

    assert out["mode"] == "paginate"
    assert out["pages_fetched"] == 2
