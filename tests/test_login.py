"""无头登录：截图落盘、二维码回退、状态保存与工具参数校验。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from scrape_mcp import server
from scrape_mcp.config import Settings
from scrape_mcp.core import browser as b
from scrape_mcp.core import errors

NORMAL_HTML = (
    "<html><body><article><h1>标题</h1><p>" + "正文内容" * 80 + "</p></article></body></html>"
)
LOGIN_HTML = (
    "<html><body><form><input type='password'></form>" + "<p>请登录</p>" * 20 + "</body></html>"
)
URL = "https://a.test/passport/signin"


class _FakePage:
    def __init__(self, *, logged_in_after: int = 1, qr_ok: bool = True) -> None:
        self.url = URL
        self._logged_in_after = logged_in_after
        self._qr_ok = qr_ok
        self.calls = 0
        self.shots: list[tuple[str, str]] = []
        self.closed = False

    async def goto(self, _url: str, **_kwargs) -> None:
        return None

    async def content(self) -> str:
        self.calls += 1
        if self.calls >= self._logged_in_after:
            self.url = "https://a.test/home"
            return NORMAL_HTML
        return LOGIN_HTML

    async def wait_for_timeout(self, _ms: float) -> None:
        return None

    async def wait_for_selector(self, _selector: str, **_kwargs) -> None:
        if not self._qr_ok:
            raise RuntimeError("二维码还没出现")

    def locator(self, _selector: str) -> _FakeLocator:
        return _FakeLocator(self)

    async def screenshot(self, path: str, **_kwargs) -> None:
        Path(path).write_bytes(b"png")
        self.shots.append(("page", path))

    async def close(self) -> None:
        self.closed = True


class _FakeLocator:
    def __init__(self, page: _FakePage) -> None:
        self.page = page

    @property
    def first(self) -> _FakeLocator:
        return self

    async def screenshot(self, path: str) -> None:
        if not self.page._qr_ok:
            raise RuntimeError("二维码元素不可见")
        Path(path).write_bytes(b"qr")
        self.page.shots.append(("qr", path))


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.closed = False

    async def new_page(self) -> _FakePage:
        return self.page

    async def storage_state(self) -> dict:
        return {
            "cookies": [{"name": "sid", "value": "1", "domain": "a.test", "path": "/"}],
            "origins": [],
        }

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _renderer(tmp_path) -> b.BrowserRenderer:
    return b.BrowserRenderer(Settings(data_dir=str(tmp_path)))


def _run_login(renderer, page, *, owned: bool = True, **kwargs) -> dict:
    browser = _FakeBrowser()

    async def fake_target(*, headless: bool = False, cdp_url: str | None = None):
        assert cdp_url is None
        assert headless is kwargs.get("headless", False)
        return browser, owned

    async def fake_new_context(host, session="default", *, headless=None, browser=None, **_kw):
        assert browser is browser_obj
        return _FakeContext(page)

    browser_obj = browser
    renderer._login_target = fake_target
    renderer._new_context = fake_new_context
    result = asyncio.run(
        renderer._login_once(
            URL, "a.test", kwargs.pop("timeout", 1.0), "default", False, 0.0, **kwargs
        )
    )
    result["_browser_closed"] = browser.closed
    return result


def test_headless_login_saves_state_and_page_screenshot(tmp_path):
    renderer = _renderer(tmp_path)
    page = _FakePage(logged_in_after=2)
    result = _run_login(renderer, page, headless=True)

    assert result["ok"] is True
    assert result["logged_in"] is True
    assert result["headless"] is True
    assert Path(result["screenshot"]).is_file()
    assert page.shots and page.shots[0][0] == "page"
    assert renderer.has_session("a.test", "default") is True
    # 临时启动的浏览器用完即关，不留在进程里
    assert result["_browser_closed"] is True


def test_qr_selector_crops_qr_and_falls_back_with_note(tmp_path):
    renderer = _renderer(tmp_path)
    page = _FakePage(logged_in_after=2, qr_ok=False)
    result = _run_login(renderer, page, headless=True, qr_selector="#qr")

    assert result["ok"] is True
    assert result["screenshot"] and Path(result["screenshot"]).is_file()
    assert page.shots[0][0] == "page"
    assert any("回退整页截图" in note for note in result["notes"])


def test_headful_login_without_qr_does_not_write_screenshot(tmp_path):
    renderer = _renderer(tmp_path)
    page = _FakePage(logged_in_after=1)
    result = _run_login(renderer, page, headless=False)

    assert result["ok"] is True
    assert result["headless"] is False
    assert result["screenshot"] is None
    assert page.shots == []
    assert renderer.has_session("a.test", "default") is True


def test_headless_timeout_refreshes_screenshot_and_still_saves_state(tmp_path):
    renderer = _renderer(tmp_path)
    page = _FakePage(logged_in_after=99)
    result = _run_login(renderer, page, headless=True, timeout=0.0)

    assert result["ok"] is True
    assert result["logged_in"] is False
    assert page.shots  # 至少落了一张，超时前还会刷新最新画面
    assert renderer.has_session("a.test", "default") is True


def test_login_target_reuses_pool_browser_when_mode_matches(tmp_path):
    renderer = _renderer(tmp_path)

    class _Pw:
        pass

    renderer._playwright = _Pw()
    renderer._browser = _FakeBrowser()
    renderer._browser_headless = True

    browser, owned = asyncio.run(renderer._login_target(headless=True, cdp_url=None))
    assert browser is renderer._browser
    assert owned is False


def test_login_target_launches_temp_browser_when_mode_differs(tmp_path):
    renderer = _renderer(tmp_path)
    renderer._playwright = object()
    renderer._browser = _FakeBrowser()
    renderer._browser_headless = True
    launched = _FakeBrowser()

    async def fake_launch(_playwright, *, headless=None):
        assert headless is False
        return launched

    renderer._launch = fake_launch
    browser, owned = asyncio.run(renderer._login_target(headless=False, cdp_url=None))
    assert browser is launched
    assert owned is True


class _FakeFetcher:
    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[dict] = []
        self._result = result or {}

    async def login(self, url, timeout=None, session="default", **kwargs) -> dict:
        self.calls.append({"url": url, "timeout": timeout, "session": session, **kwargs})
        return dict(self._result)


def test_login_tool_passes_headless_options_and_mentions_screenshot(monkeypatch):
    fetcher = _FakeFetcher(
        result={
            "ok": True,
            "logged_in": False,
            "headless": True,
            "screenshot": "/data/login/a.test_default_1.png",
            "notes": [],
        }
    )
    monkeypatch.setattr(server, "_get_fetcher", lambda: fetcher)

    payload = json.loads(
        asyncio.run(
            server.login(
                "https://a.test/passwd",
                headless=True,
                qr_selector="#qr",
                session="work",
            )
        )
    )

    assert payload["ok"] is True
    assert payload["headless"] is True
    assert "/data/login/a.test_default_1.png" in payload["message"]
    assert fetcher.calls[0]["headless"] is True
    assert fetcher.calls[0]["qr_selector"] == "#qr"
    assert fetcher.calls[0]["session"] == "work"


def test_login_tool_rejects_invalid_headless_and_cdp(monkeypatch):
    fetcher = _FakeFetcher()
    monkeypatch.setattr(server, "_get_fetcher", lambda: fetcher)

    bad_headless = json.loads(asyncio.run(server.login("https://a.test", headless="yes")))
    assert bad_headless["error_code"] == errors.INVALID_ARGUMENT
    assert fetcher.calls == []

    bad_cdp = json.loads(asyncio.run(server.login("https://a.test", cdp_url="ftp://a.test")))
    assert bad_cdp["error_code"] == errors.INVALID_ARGUMENT
    assert fetcher.calls == []


def test_login_tool_adds_cookie_import_hint_on_headless_failure(monkeypatch):
    fetcher = _FakeFetcher(result={"ok": False, "error": "browser launch failed"})
    monkeypatch.setattr(server, "_get_fetcher", lambda: fetcher)

    payload = json.loads(asyncio.run(server.login("https://a.test", headless=True)))
    assert payload["ok"] is False
    assert "session_import" in payload["hint"]
