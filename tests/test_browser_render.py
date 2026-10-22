"""L2 渲染层：不起浏览器的纯逻辑 + 用假 page 覆盖交互诊断。"""

from __future__ import annotations

import asyncio

import pytest

from scrape_mcp.config import Settings
from scrape_mcp.core import browser as b

URL = "https://a.test/article"
NORMAL_HTML = (
    "<html><body><article><h1>标题</h1><p>" + "正文内容" * 80 + "</p></article></body></html>"
)


class _FakePage:
    def __init__(
        self,
        *,
        html: str = NORMAL_HTML,
        url: str = "https://a.test/final",
        fail_wait: bool = False,
        fail_click: bool = False,
        fail_scroll: bool = False,
    ) -> None:
        self.url = url
        self._html = html
        self._fail_wait = fail_wait
        self._fail_click = fail_click
        self._fail_scroll = fail_scroll
        self.scrolls = 0
        self.closed = False

    async def goto(self, _url: str, **_kwargs):
        class _Resp:
            status = 200

        return _Resp()

    async def content(self) -> str:
        return self._html

    async def wait_for_selector(self, _selector: str, **_kwargs) -> None:
        if self._fail_wait:
            raise RuntimeError("not found")

    async def click(self, _selector: str, **_kwargs) -> None:
        if self._fail_click:
            raise RuntimeError("not clickable")

    async def wait_for_timeout(self, _ms: float) -> None:
        return None

    async def evaluate(self, _js: str) -> None:
        if self._fail_scroll:
            raise RuntimeError("evaluate failed")
        self.scrolls += 1

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.page = page

    async def new_page(self) -> _FakePage:
        return self.page


def _renderer(tmp_path, **overrides) -> b.BrowserRenderer:
    return b.BrowserRenderer(Settings(data_dir=str(tmp_path), **overrides))


# ---- 交互诊断 ----


def test_interact_reports_hits_and_scroll_rounds(tmp_path):
    renderer = _renderer(tmp_path, l2_scroll_steps=3, l2_scroll_delay_ms=0)
    page = _FakePage()
    notes: list[str] = []
    out = asyncio.run(
        renderer._interact(page, wait_for="article", scroll=True, click=".more", notes=notes)
    )
    assert out == (True, True, 3)
    assert page.scrolls == 3
    assert notes == []


def test_interact_records_misses_without_failing(tmp_path):
    renderer = _renderer(tmp_path, l2_scroll_steps=2, l2_scroll_delay_ms=0)
    page = _FakePage(fail_wait=True, fail_click=True, fail_scroll=True)
    notes: list[str] = []
    out = asyncio.run(
        renderer._interact(page, wait_for="#late", scroll=True, click="#more", notes=notes)
    )
    assert out == (False, False, 0)
    assert notes == ["wait_for 未命中: #late", "click 未命中: #more"]


def test_interact_skips_unrequested_steps(tmp_path):
    renderer = _renderer(tmp_path)
    notes: list[str] = []
    out = asyncio.run(
        renderer._interact(_FakePage(), wait_for=None, scroll=False, click=None, notes=notes)
    )
    assert out == (None, None, 0)
    assert notes == []


# ---- 等待/登录判定 ----


def test_pending_kind_reads_challenge_and_shell():
    assert b._pending_kind(NORMAL_HTML) is None
    assert b._pending_kind("<html><body>Just a moment...</body></html>") == "cloudflare"
    assert b._pending_kind("<html><body>请完成验证码</body></html>") == "captcha"
    assert b._pending_kind("<html><body></body></html>") == "empty"

    shell = (
        "<html><body><article>" + "<span></span>" * 2500 + "正文" * 150 + "</article></body></html>"
    )
    assert len(shell) >= 30000
    assert b._pending_kind(shell) == "shell"


def test_is_logged_in_requires_content_and_left_login_path():
    assert b._is_logged_in("https://a.test/home", NORMAL_HTML) is True
    assert b._is_logged_in("https://a.test/passport/signin", NORMAL_HTML) is False
    assert b._is_logged_in("https://a.test/home", "<html><body></body></html>") is False


# ---- 纯函数 ----


def test_js_languages_always_includes_en():
    assert b._js_languages("zh-CN,zh;q=0.9") == "['zh-CN', 'zh', 'en']"
    assert b._js_languages("") == "['en']"


def test_user_agent_never_contains_headless_marker():
    ua = b.BrowserRenderer._user_agent("124.0.6367.60")
    assert "Chrome/124.0.0.0" in ua
    assert "Headless" not in ua
    assert "Chrome/140.0.0.0" in b.BrowserRenderer._user_agent("")


def test_launch_kwargs_prefers_chromium_channel_then_fallbacks(tmp_path):
    renderer = _renderer(tmp_path, l2_channel=None)
    variants = renderer._launch_kwargs()
    assert [v.get("channel") for v in variants] == ["chromium", None, "chrome", "msedge"]

    pinned = _renderer(tmp_path, l2_channel="chrome")._launch_kwargs()
    assert pinned == [{"channel": "chrome", "headless": True, "args": list(b._LAUNCH_ARGS)}]


def test_seed_part_is_bounded(tmp_path):
    part = _renderer(tmp_path)._seed_part(61)
    assert 0 <= part < 251


def test_has_session_defaults_false(tmp_path):
    assert _renderer(tmp_path).has_session("a.test") is False


# ---- 渲染流程（假浏览器） ----


def test_render_returns_outcome_with_diagnostics(tmp_path, monkeypatch):
    renderer = _renderer(tmp_path, l2_scroll_steps=2, l2_scroll_delay_ms=0)
    page = _FakePage()
    context = _FakeContext(page)

    async def fake_context(_host: str, session: str = "default", **_kwargs):
        return context

    monkeypatch.setattr(renderer, "_context_for", fake_context)
    out = asyncio.run(renderer.render(URL, wait_for="article", scroll=True, click=".more"))

    assert out.ok is True
    assert out.final_url == "https://a.test/final"
    assert out.status == 200
    assert (out.wait_for_hit, out.clicked, out.scroll_rounds) == (True, True, 2)
    assert out.notes == []
    assert page.closed is True


def test_render_reports_browser_start_failure(tmp_path, monkeypatch):
    renderer = _renderer(tmp_path)

    async def boom(_host: str, session: str = "default", **_kwargs):
        raise RuntimeError("no chromium")

    monkeypatch.setattr(renderer, "_context_for", boom)
    out = asyncio.run(renderer.render(URL))

    assert out.ok is False
    assert out.status == 0
    assert "浏览器启动失败" in (out.error or "")


def test_launch_falls_back_across_channels(tmp_path):
    calls: list[dict] = []

    class _Chromium:
        async def launch(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("channel missing")
            return "browser"

    class _Playwright:
        chromium = _Chromium()

    renderer = _renderer(tmp_path, l2_channel=None)
    result = asyncio.run(renderer._launch(_Playwright()))
    assert result == "browser"
    assert calls[0]["channel"] == "chromium"


def test_launch_raises_when_all_channels_fail(tmp_path):
    class _Chromium:
        async def launch(self, **_kwargs):
            raise RuntimeError("nope")

    class _Playwright:
        chromium = _Chromium()

    with pytest.raises(RuntimeError, match="所有浏览器渠道均启动失败"):
        asyncio.run(_renderer(tmp_path, l2_channel=None)._launch(_Playwright()))


def test_aclose_clears_resources(tmp_path):
    renderer = _renderer(tmp_path)
    closed: list[str] = []

    class _Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self) -> None:
            closed.append(self.name)

        async def stop(self) -> None:
            closed.append(self.name)

    renderer._contexts = {("a.test", "default", ""): _Resource("context")}
    renderer._browser = _Resource("browser")
    renderer._playwright = _Resource("playwright")

    asyncio.run(renderer.aclose())

    assert set(closed) == {"context", "browser", "playwright"}
    assert renderer._contexts == {}
    assert renderer._browser is None and renderer._playwright is None
