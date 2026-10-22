"""L2 浏览器池：context 复用/淘汰、并发页上限与空闲回收（全部用假对象，不起浏览器）。"""

from __future__ import annotations

import asyncio
import time

from scrape_mcp.config import Settings
from scrape_mcp.core import browser as b

URL = "https://a.test/article"
NORMAL_HTML = (
    "<html><body><article><h1>标题</h1><p>" + "正文内容" * 80 + "</p></article></body></html>"
)


class _Resource:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False
        self.stopped = False

    async def close(self) -> None:
        self.closed = True

    async def stop(self) -> None:
        self.stopped = True


class _Resp:
    status = 200


class _PoolPage:
    """统计同时在跑的页面数，用来验证并发页上限。"""

    def __init__(self, gauge: dict[str, int]) -> None:
        self.url = "https://a.test/final"
        self._gauge = gauge

    async def goto(self, _url: str, **_kwargs):
        self._gauge["in_flight"] += 1
        self._gauge["peak"] = max(self._gauge["peak"], self._gauge["in_flight"])
        await asyncio.sleep(0.05)
        return _Resp()

    async def content(self) -> str:
        return NORMAL_HTML

    async def wait_for_timeout(self, _ms: float) -> None:
        return None

    async def close(self) -> None:
        self._gauge["in_flight"] -= 1


class _PoolContext:
    def __init__(self, gauge: dict[str, int]) -> None:
        self._gauge = gauge

    async def new_page(self) -> _PoolPage:
        return _PoolPage(self._gauge)


def _renderer(tmp_path, **overrides) -> b.BrowserRenderer:
    return b.BrowserRenderer(Settings(data_dir=str(tmp_path), **overrides))


def test_context_reused_per_host_and_session(tmp_path, monkeypatch):
    renderer = _renderer(tmp_path)
    created: list[tuple[str, str]] = []

    async def fake_new_context(host: str, session: str = "default", *, headless=None, proxy=None):
        created.append((host, session))
        return _Resource(f"{host}/{session}")

    monkeypatch.setattr(renderer, "_new_context", fake_new_context)

    async def run():
        first = await renderer._context_for("a.test")
        second = await renderer._context_for("a.test")
        third = await renderer._context_for("a.test", "work")
        return first, second, third

    first, second, third = asyncio.run(run())

    assert first is second
    assert first is not third
    assert created == [("a.test", "default"), ("a.test", "work")]


def test_contexts_evicted_lru_beyond_limit(tmp_path, monkeypatch):
    renderer = _renderer(tmp_path, l2_max_contexts=2)
    store: dict[tuple[str, str], _Resource] = {}

    async def fake_new_context(host: str, session: str = "default", *, headless=None, proxy=None):
        resource = _Resource(f"{host}/{session}")
        store[(host, session)] = resource
        return resource

    monkeypatch.setattr(renderer, "_new_context", fake_new_context)

    async def run():
        await renderer._context_for("a.test")
        await renderer._context_for("b.test")
        await renderer._context_for("a.test")  # 刷新 a 的最近使用时间
        await renderer._context_for("c.test")  # 超限，应淘汰最久未用的 b

    asyncio.run(run())

    assert set(renderer._contexts) == {
        ("a.test", "default", ""),
        ("c.test", "default", ""),
    }
    assert store[("a.test", "default")].closed is False
    assert store[("b.test", "default")].closed is True


def test_page_slots_limit_concurrent_renders(tmp_path, monkeypatch):
    renderer = _renderer(tmp_path, l2_max_pages=1, l2_idle_timeout=0)
    gauge = {"in_flight": 0, "peak": 0}
    context = _PoolContext(gauge)

    async def fake_context(_host: str, session: str = "default", **_kwargs):
        return context

    monkeypatch.setattr(renderer, "_context_for", fake_context)

    async def run():
        return await asyncio.gather(*(renderer.render(URL) for _ in range(3)))

    outcomes = asyncio.run(run())

    assert all(out.ok for out in outcomes)
    assert gauge["peak"] == 1


def test_page_slots_rebuilt_when_event_loop_changes(tmp_path):
    renderer = _renderer(tmp_path, l2_max_pages=2)

    async def same_loop():
        return renderer._page_slots(), renderer._page_slots()

    first, second = asyncio.run(same_loop())
    assert first is second

    async def other_loop():
        return renderer._page_slots()

    assert asyncio.run(other_loop()) is not first


def test_idle_reaper_closes_browser_and_contexts(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "_REAP_MIN_INTERVAL", 0.01)
    renderer = _renderer(tmp_path, l2_idle_timeout=0.05)
    browser = _Resource("browser")
    playwright = _Resource("playwright")
    context = _Resource("context")
    renderer._browser = browser
    renderer._playwright = playwright
    renderer._contexts[("a.test", "default", "")] = context

    async def run():
        renderer._touch()
        await asyncio.sleep(0.3)
        return renderer._browser

    assert asyncio.run(run()) is None
    assert browser.closed is True
    assert playwright.stopped is True
    assert context.closed is True
    assert renderer._contexts == {}


def test_reaper_skips_while_render_in_flight(tmp_path):
    renderer = _renderer(tmp_path, l2_idle_timeout=0.01)
    renderer._browser = _Resource("browser")
    renderer._active = 1
    renderer._last_used = time.monotonic() - 10

    assert asyncio.run(renderer._reap_if_idle()) is False
    assert renderer._browser is not None

    renderer._active = 0
    assert asyncio.run(renderer._reap_if_idle()) is True
    assert renderer._browser is None


def test_idle_timeout_zero_keeps_browser(tmp_path):
    renderer = _renderer(tmp_path, l2_idle_timeout=0)
    browser = _Resource("browser")
    renderer._browser = browser
    renderer._last_used = time.monotonic() - 10

    assert asyncio.run(renderer._reap_if_idle()) is False
    assert renderer._browser is browser
    assert renderer._reaper is None


def test_aclose_stops_reaper_and_releases(tmp_path):
    renderer = _renderer(tmp_path, l2_idle_timeout=0.05)
    browser = _Resource("browser")

    async def run():
        renderer._browser = browser
        renderer._touch()
        reaper = renderer._reaper
        await renderer.aclose()
        return reaper

    reaper = asyncio.run(run())

    assert reaper is not None and reaper.done()
    assert renderer._reaper is None
    assert browser.closed is True
