"""站点 recipe：目录加载、host 匹配，以及在 web_fetch / web_extract 中的套用与覆盖。"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp import server
from scrape_mcp.core.fetcher import FetchOutcome

URL = "https://example.com/a"
HTML = (
    "<html><head><title>示例</title></head><body>"
    "<article><p>默认正文</p></article>"
    '<div class="special"><p>指定正文</p></div>'
    "</body></html>"
)


def _write(directory, name: str, data: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _outcome(url: str = URL, html: str = HTML) -> FetchOutcome:
    return FetchOutcome(
        url=url,
        final_url=url,
        status=200,
        tier="l1",
        blocked="none",
        block_reason="",
        content_type="text/html",
        html=html,
        elapsed_ms=3,
    )


def _install_fetcher(monkeypatch, calls: dict):
    class _Fetcher:
        async def fetch(self, url: str, **kwargs):
            calls.update(kwargs)
            return _outcome(url)

        async def aclose(self) -> None:
            pass

    async def allowed(_url: str) -> None:
        return None

    monkeypatch.setattr(server, "_get_fetcher", lambda: _Fetcher())
    monkeypatch.setattr(server, "_compliance_check", allowed)


def _run(coro) -> dict:
    return json.loads(asyncio.run(coro))


# ---------- store ----------


def test_host_suffix_match_and_longest_wins(tmp_path):
    from scrape_mcp.core.recipes import RecipeStore

    _write(tmp_path, "example.com.json", {"wait_for": "article"})
    _write(tmp_path, "news.example.com.json", {"wait_for": ".news"})
    store = RecipeStore(tmp_path)

    assert store.for_url("https://example.com/a").wait_for == "article"
    assert store.for_url("https://www.example.com/a").wait_for == "article"
    assert store.for_url("https://news.example.com/a").wait_for == ".news"
    assert store.for_url("https://other.test/a") is None
    assert store.for_url("https://example.com:8443/a").wait_for == "article"


def test_recipe_fields_are_parsed(tmp_path):
    from scrape_mcp.core.recipes import RecipeStore

    _write(
        tmp_path,
        "example.com.json",
        {
            "wait_for": "article",
            "scroll": True,
            "click": ".more",
            "root_selector": "main article",
            "extract": {"fields": {"title": "h1"}},
            "crawl": {"next_selector": "a.next", "max_pages": 3},
            "note": "示例站点",
        },
    )
    recipe = RecipeStore(tmp_path).for_url(URL)

    assert recipe.wait_for == "article"
    assert recipe.scroll is True
    assert recipe.click == ".more"
    assert recipe.root_selector == "main article"
    assert recipe.extract == {"fields": {"title": "h1"}}
    assert recipe.next_selector == "a.next"
    assert recipe.max_pages == 3
    assert recipe.note == "示例站点"


def test_host_field_overrides_filename(tmp_path):
    from scrape_mcp.core.recipes import RecipeStore

    _write(tmp_path, "whatever.json", {"host": "example.org", "wait_for": ".x"})
    store = RecipeStore(tmp_path)
    assert store.for_url("https://example.org/a").wait_for == ".x"
    assert store.for_url("https://whatever/a") is None


def test_malformed_and_invalid_recipes_are_skipped(tmp_path):
    from scrape_mcp.core.recipes import RecipeStore

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "bad.json").write_text("{oops", encoding="utf-8")
    (tmp_path / "list.json").write_text("[]", encoding="utf-8")
    _write(tmp_path, "good.test.json", {"wait_for": "article"})

    store = RecipeStore(tmp_path)
    assert [r.host for r in store.all()] == ["good.test"]


def test_missing_directory_is_empty(tmp_path):
    from scrape_mcp.core.recipes import RecipeStore

    assert RecipeStore(tmp_path / "nope").all() == []


# ---------- 套用 ----------


def test_fetch_uses_recipe_interaction_and_root(tmp_path, monkeypatch):
    from scrape_mcp.core.recipes import RecipeStore

    directory = tmp_path / "recipes"
    _write(
        directory,
        "example.com.json",
        {"wait_for": "article", "scroll": True, "click": ".more", "root_selector": ".special"},
    )
    monkeypatch.setattr(server, "get_recipe_store", lambda: RecipeStore(directory))
    calls: dict = {}
    _install_fetcher(monkeypatch, calls)

    out = _run(server.web_fetch(URL))

    assert calls["wait_for"] == "article"
    assert calls["scroll"] is True
    assert calls["click"] == ".more"
    assert out["ok"] is True
    assert "指定正文" in out["content"]
    assert "默认正文" not in out["content"]


def test_explicit_params_override_recipe(tmp_path, monkeypatch):
    from scrape_mcp.core.recipes import RecipeStore

    directory = tmp_path / "recipes"
    _write(directory, "example.com.json", {"wait_for": "article", "scroll": True, "click": ".more"})
    monkeypatch.setattr(server, "get_recipe_store", lambda: RecipeStore(directory))
    calls: dict = {}
    _install_fetcher(monkeypatch, calls)

    _run(server.web_fetch(URL, wait_for=".mine", scroll=False, click=".self"))

    assert calls["wait_for"] == ".mine"
    assert calls["scroll"] is False
    assert calls["click"] == ".self"


def test_extract_uses_recipe_schema_when_omitted(tmp_path, monkeypatch):
    from scrape_mcp.core.recipes import RecipeStore

    directory = tmp_path / "recipes"
    _write(
        directory,
        "example.com.json",
        {"extract": {"fields": {"from_recipe": {"selector": "article p"}}}},
    )
    monkeypatch.setattr(server, "get_recipe_store", lambda: RecipeStore(directory))
    _install_fetcher(monkeypatch, {})

    out = _run(server.web_extract(URL))

    assert out["ok"] is True
    assert out["data"]["from_recipe"] == "默认正文"


def test_extract_explicit_schema_overrides_recipe(tmp_path, monkeypatch):
    from scrape_mcp.core.recipes import RecipeStore

    directory = tmp_path / "recipes"
    _write(directory, "example.com.json", {"extract": {"fields": {"from_recipe": "h1"}}})
    monkeypatch.setattr(server, "get_recipe_store", lambda: RecipeStore(directory))
    _install_fetcher(monkeypatch, {})

    out = _run(server.web_extract(URL, {"fields": {"explicit": {"selector": ".special p"}}}))

    assert out["data"] == {"explicit": "指定正文"}


def test_extract_without_schema_or_recipe_rejected(tmp_path, monkeypatch):
    from scrape_mcp.core.recipes import RecipeStore

    monkeypatch.setattr(server, "get_recipe_store", lambda: RecipeStore(tmp_path / "empty"))
    _install_fetcher(monkeypatch, {})

    out = _run(server.web_extract(URL))

    assert out["ok"] is False
    assert out["error_code"] == "invalid_argument"
    assert out["retryable"] is False
