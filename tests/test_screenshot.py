"""截图：服务层载荷/落盘/参数校验，以及渲染层用假 page 验证交互细节。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from scrape_mcp import server
from scrape_mcp.config import Settings
from scrape_mcp.core import browser as b
from scrape_mcp.core import errors
from scrape_mcp.core.browser import ScreenshotOutcome

URL = "https://a.test/page"


def _shot(**overrides) -> ScreenshotOutcome:
    base = {
        "ok": True,
        "url": URL,
        "final_url": URL,
        "status": 200,
        "png": b"\x89PNG\r\n\x1a\nfakepng",
        "elapsed_ms": 12,
        "notes": [],
    }
    base.update(overrides)
    return ScreenshotOutcome(**base)


class _FakeFetcher:
    def __init__(self, outcome: ScreenshotOutcome) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    async def screenshot(self, url: str, **kwargs) -> ScreenshotOutcome:
        self.calls.append(kwargs)
        return self.outcome


def _patch(monkeypatch, outcome: ScreenshotOutcome) -> _FakeFetcher:
    async def allowed(_url: str, *_args, **_kwargs) -> None:
        return None

    fake = _FakeFetcher(outcome)
    monkeypatch.setattr(server, "_compliance_check", allowed)
    monkeypatch.setattr(server, "_get_fetcher", lambda: fake)
    return fake


def _run(coro) -> dict:
    return json.loads(asyncio.run(coro))


def test_web_screenshot_success_writes_default_file(monkeypatch, tmp_path):
    fake = _patch(monkeypatch, _shot())
    out = _run(server.web_screenshot(URL))
    assert set(out) == {
        "ok",
        "url",
        "final_url",
        "status",
        "file",
        "bytes",
        "width",
        "height",
        "full_page",
        "selector",
        "wait_ms",
        "elapsed_ms",
        "notes",
    }
    path = tmp_path / "data" / "screenshots"
    assert out["ok"] is True and out["bytes"] > 0
    assert Path(out["file"]).exists() and Path(out["file"]).parent == path
    # 参数原样透传给渲染层
    assert fake.calls[0]["width"] == 1280 and fake.calls[0]["height"] == 800
    assert fake.calls[0]["proxy"] is None


def test_web_screenshot_explicit_path_and_options(monkeypatch, tmp_path):
    _patch(monkeypatch, _shot())
    target = tmp_path / "shots" / "page.png"
    out = _run(
        server.web_screenshot(
            URL,
            path=str(target),
            full_page=True,
            width=800,
            height=600,
            wait_ms=100,
            selector="#main",
        )
    )
    assert target.exists() and target.read_bytes() == _shot().png
    assert out["full_page"] is True and out["width"] == 800
    assert out["selector"] == "#main" and out["wait_ms"] == 100


def test_web_screenshot_invalid_url():
    out = _run(server.web_screenshot("ftp://a.test/x"))
    assert out["error_code"] == errors.INVALID_URL


def test_web_screenshot_validates_viewport_and_wait(tmp_path):
    out = _run(server.web_screenshot(URL, width=50))
    assert out["error_code"] == errors.INVALID_ARGUMENT
    out = _run(server.web_screenshot(URL, wait_ms=-1))
    assert out["error_code"] == errors.INVALID_ARGUMENT


def test_web_screenshot_denied_by_compliance(monkeypatch):
    async def denied(url: str, *_args, **_kwargs) -> dict:
        return {
            "ok": False,
            "url": url,
            "error_code": errors.DENIED,
            "error": "host in blacklist",
            "retryable": False,
        }

    monkeypatch.setattr(server, "_compliance_check", denied)
    out = _run(server.web_screenshot(URL))
    assert set(out) == {"ok", "url", "error_code", "error", "retryable"}
    assert out["error_code"] == errors.DENIED


def test_web_screenshot_failure_payload(monkeypatch):
    _patch(monkeypatch, _shot(ok=False, png=b"", error="Timeout: navigation timeout"))
    out = _run(server.web_screenshot(URL))
    assert set(out) == {"ok", "url", "error_code", "error", "retryable"}
    assert out["ok"] is False and out["error_code"] == errors.TIMEOUT


# ---- 渲染层 ----


class _Resp:
    status = 200


class _FakePage:
    url = "https://a.test/final"

    def __init__(self, *, fail_selector: bool = False) -> None:
        self.size: dict | None = None
        self.full_page: bool | None = None
        self.closed = False
        self._fail_selector = fail_selector

    async def set_viewport_size(self, size: dict) -> None:
        self.size = size

    async def goto(self, _url: str, **_kwargs):
        return _Resp()

    async def wait_for_selector(self, _selector: str, **_kwargs) -> None:
        if self._fail_selector:
            raise RuntimeError("not found")

    async def wait_for_timeout(self, _ms: float) -> None:
        return None

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        self.full_page = full_page
        return b"PNG"

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.page = page

    async def new_page(self) -> _FakePage:
        return self.page


def _renderer(tmp_path) -> b.BrowserRenderer:
    return b.BrowserRenderer(Settings(data_dir=str(tmp_path)))


def test_renderer_screenshot_sets_viewport_and_captures(tmp_path, monkeypatch):
    renderer = _renderer(tmp_path)
    page = _FakePage()
    context = _FakeContext(page)

    async def fake_context(_host: str, session: str = "default", **_kwargs):
        return context

    monkeypatch.setattr(renderer, "_context_for", fake_context)
    out = asyncio.run(
        renderer.screenshot(URL, full_page=True, width=800, height=600, wait_ms=50, selector="#x")
    )

    assert out.ok is True and out.png == b"PNG" and out.final_url == "https://a.test/final"
    assert page.size == {"width": 800, "height": 600}
    assert page.full_page is True and page.closed is True
    assert out.notes == []


def test_renderer_screenshot_records_selector_miss(tmp_path, monkeypatch):
    renderer = _renderer(tmp_path)
    page = _FakePage(fail_selector=True)

    async def fake_context(_host: str, session: str = "default", **_kwargs):
        return _FakeContext(page)

    monkeypatch.setattr(renderer, "_context_for", fake_context)
    out = asyncio.run(renderer.screenshot(URL, selector="#late"))
    assert out.ok is True
    assert out.notes and "selector 未命中" in out.notes[0]


def test_renderer_screenshot_reports_browser_start_failure(tmp_path, monkeypatch):
    renderer = _renderer(tmp_path)

    async def boom(_host: str, session: str = "default", **_kwargs):
        raise RuntimeError("no chromium")

    monkeypatch.setattr(renderer, "_context_for", boom)
    out = asyncio.run(renderer.screenshot(URL))
    assert out.ok is False and "浏览器启动失败" in (out.error or "")
