"""web_inspect：可交互元素、稳定 ref、表单与分页候选。"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp import server
from scrape_mcp.core import errors
from scrape_mcp.core.fetcher import FetchOutcome
from scrape_mcp.extract.inspect import inspect_html

PAGE = """
<html><body>
<header><a href="/">首页</a></header>
<form id="search-form" action="/search" method="post">
  <input name="q" placeholder="搜索">
  <input type="hidden" name="token" value="x">
  <button id="go">搜索</button>
</form>
<a href="/docs/page2" rel="next">下一页</a>
<a href="/about" style="display:none">隐藏入口</a>
<div role="button" aria-label="打开菜单">菜单</div>
</body></html>
"""


def _outcome(html: str = PAGE, **overrides) -> FetchOutcome:
    base = {
        "url": "https://a.test/",
        "final_url": "https://a.test/",
        "status": 200,
        "tier": "l2",
        "blocked": "none",
        "block_reason": "",
        "content_type": "text/html",
        "html": html,
        "elapsed_ms": 12,
    }
    base.update(overrides)
    return FetchOutcome(**base)


def _run(coro) -> dict:
    return json.loads(asyncio.run(coro))


def _patch(monkeypatch, outcome: FetchOutcome) -> list[dict]:
    calls: list[dict] = []

    class _Fake:
        async def fetch(self, url: str, **kwargs) -> FetchOutcome:
            calls.append(kwargs)
            return outcome

    async def allowed(_url: str, *_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(server, "_compliance_check", allowed)
    monkeypatch.setattr(server, "_get_fetcher", lambda: _Fake())
    return calls


# ---- 纯解析 ----


def test_inspect_finds_interactive_elements_and_skips_hidden():
    found = inspect_html(PAGE, "https://a.test/")
    refs = [item["ref"] for item in found["elements"]]
    assert "#go" in refs
    assert 'input[name="q"]' in refs
    assert 'div[aria-label="打开菜单"]' in refs
    assert all("隐藏入口" not in item["text"] for item in found["elements"])
    assert all(item["name"] != "token" for item in found["elements"])


def test_inspect_refs_are_stable_across_calls():
    first = [item["ref"] for item in inspect_html(PAGE, "https://a.test/")["elements"]]
    second = [item["ref"] for item in inspect_html(PAGE, "https://a.test/")["elements"]]
    assert first == second


def test_inspect_falls_back_to_nth_child_path():
    html = "<html><body><div><span>x</span><button>提交</button></div></body></html>"
    found = inspect_html(html, "https://a.test/")
    refs = [item["ref"] for item in found["elements"]]
    assert refs == ["body > div > button:nth-child(2)"]


def test_inspect_parses_forms_with_absolute_action():
    found = inspect_html(PAGE, "https://a.test/base")
    assert len(found["forms"]) == 1
    form = found["forms"][0]
    assert form["action"] == "https://a.test/search"
    assert form["method"] == "post"
    assert [field["name"] for field in form["fields"]] == ["q"]


def test_inspect_finds_next_page_candidates():
    found = inspect_html(PAGE, "https://a.test/list")
    assert found["next"] == [{"text": "下一页", "href": "https://a.test/docs/page2"}]


def test_inspect_respects_element_limit():
    html = "<html><body>" + "".join(f"<a href='/{i}'>链接{i}</a>" for i in range(10))
    assert len(inspect_html(html, "https://a.test/", max_elements=3)["elements"]) == 3


# ---- 工具层 ----


def test_web_inspect_returns_payload_and_forces_browser(monkeypatch):
    calls = _patch(monkeypatch, _outcome())
    out = _run(server.web_inspect("https://a.test/"))
    assert out["ok"] is True
    assert out["count"] == len(out["elements"])
    assert out["tier"] == "l2"
    assert out["forms"][0]["action"] == "https://a.test/search"
    assert calls and calls[0].get("force_l2") is True


def test_web_inspect_notes_l1_fallback(monkeypatch):
    _patch(monkeypatch, _outcome(tier="l1"))
    out = _run(server.web_inspect("https://a.test/"))
    assert out["ok"] is True
    assert any("inspect:l1_fallback" in note for note in out["notes"])


def test_web_inspect_rejects_invalid_url():
    out = _run(server.web_inspect("ftp://a.test/"))
    assert out["ok"] is False and out["error_code"] == errors.INVALID_URL


def test_web_inspect_rejects_non_html(monkeypatch):
    _patch(monkeypatch, _outcome(content_type="application/pdf", kind="pdf", content=b"%PDF"))
    out = _run(server.web_inspect("https://a.test/a.pdf"))
    assert out["ok"] is False and out["error_code"] == errors.UNSUPPORTED_CONTENT


def test_web_inspect_rejects_bad_max_elements():
    out = _run(server.web_inspect("https://a.test/", max_elements=0))
    assert out["ok"] is False and out["error_code"] == errors.INVALID_ARGUMENT
