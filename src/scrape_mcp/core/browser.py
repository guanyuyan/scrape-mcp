"""L2 抓取层：Playwright 渲染，用于 JS 空壳页与反爬挑战页。

只在 L1 被判为拦截时才启用：启动浏览器比 curl_cffi 贵一到两个数量级。
浏览器实例按需创建、跨请求复用；池化管理三件事：
- 同一 host + session 共用一个 context（各带独立登录态），超出上限按最久未用淘汰
- 同时打开的页面数受限，超出排队而不是把浏览器压满
- 空闲超过阈值后自动关掉浏览器进程，下次用到再启动
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import Settings
from .detector import detect_block, is_terminal_rejection, looks_like_shell
from .session import SessionStore
from .singleflight import SingleFlight

# 挑战特征：命中说明 JS 还在跑或还没放行，值得继续等
_CHALLENGE_TYPES = ("cloudflare", "captcha", "empty_spa", "empty")

# 等待窗口的封顶值（秒）。分档是因为"等下去会不会有结果"差别很大：
# Cloudflare 通常几秒内自动放行，值得等满；验证码要人去点，等再久也不会自己消失；
# 骨架页只等 JS 渲染，几秒足够。
_SETTLE_CAP = {"cloudflare": None, "empty": None, "empty_spa": None, "captcha": 1.2, "shell": 4.0}

# 深度指纹对抗（M5）：清理自动化泄漏属性、伪造 UA 数据、给 Canvas/WebGL 加确定性噪声。
# 强调"确定性"：同一会话内同一输入恒得同一输出，规避"机器回复过速/结果完全一致"那类判定，
# 同时也不把正常业务逻辑（读 canvas 像素）搞坏。
_STEALTH_JS = r"""
// 1. 抹掉最显眼的自动化标记
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => %(languages)s});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'pdfViewerEnabled', {get: () => true});
if (!('chrome' in window)) window.chrome = {runtime: {}};
// 2. 清掉工具在原型上注入的泄漏属性（很多自动化网关抓 cdc_ / __selenium 之类的探针）
for (const k of Object.getOwnPropertyNames(Object.prototype)) {
  if (k.indexOf('cdc_') === 0 || k.indexOf('__selenium') === 0 || k.indexOf('__webdriver') === 0) {
    try { delete Object.prototype[k]; } catch (e) {}
  }
}
// 3. navigator.userAgentData 补全（headless 时它常缺失/带 anomaly 标记）
try {
  Object.defineProperty(navigator, 'userAgentData', {get: () => ({
    brands: [{brand:'Chromium',version:'124'},{brand:'Google Chrome',version:'124'},{brand:'Not.A.Brand',version:'8'}],
    mobile: false, platform: 'Windows', getHighEntropyValues: function(){ return Promise.resolve({}); }})});
} catch (e) {}
// 4. Canvas 确定性指纹噪声：复制一份、多画 1 像素，让指纹结果带"像真人机器"的质感应答
(() => {
  const orig = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function() {
    const probe = document.createElement('canvas');
    probe.width = this.width + 1; probe.height = this.height + 1;
    const c = probe.getContext('2d');
    if (c) {
      c.drawImage(this, 0, 0);
      c.fillStyle = 'rgba(%(cr)d,%(cg)d,%(cb)d,0.02)';
      c.fillRect(0, 0, 1, 1);
    }
    return orig.call(probe, arguments[0], arguments[1]);
  };
})();
// 5. WebGL GPU 型号暴露成常见 Intel/ANGLE 串，避免原生值被做黑名单比对
try {
  const gp = WebGL2RenderingContext.prototype.getParameter;
  WebGL2RenderingContext.prototype.getParameter = function(name) {
    if (name === 37445) return 'Google Inc. (Intel)';
    if (name === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)';
    return gp.apply(this, arguments);
  };
} catch (e) {}
// 6. 对齐常见桌面探针
try { Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8}); } catch (e) {}
try { Object.defineProperty(navigator, 'deviceMemory', {get: () => 8}); } catch (e) {}
"""

_LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--lang=zh-CN",
    "--window-size=1440,900",
)

_SETTLE_INTERVAL_MS = 400

# settle 阶段判定"还在等骨架页"的可见文本下限，比 detector 的 L1 口径（2000 字）低。
# 浏览器已经把 JS 跑过一遍，此时仍有上千字可见文本的页面就不是骨架页了：
# 首页类页面（如 svelte.dev 1943 字、thepaper.cn 1431 字）正文早已稳定，
# 继续按 2000 字判定只会空转到 shell 封顶，白等几秒。
_SETTLE_SHELL_MAX_VISIBLE = 1000

# 空闲检查的最小间隔（秒）：阈值很小时不至于把检查开成忙轮询
_REAP_MIN_INTERVAL = 1.0


def _reap_interval(idle_timeout: float) -> float:
    """空闲检查间隔：取阈值的 1/4，限制在 1~30 秒之间。"""
    return max(_REAP_MIN_INTERVAL, min(30.0, idle_timeout / 4))


@dataclass
class BrowserOutcome:
    ok: bool
    url: str
    final_url: str
    status: int
    html: str
    elapsed_ms: int
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    # 交互诊断：None 表示未请求该项，False 表示请求了但没成功
    wait_for_hit: bool | None = None
    clicked: bool | None = None
    scroll_rounds: int = 0
    # 动作链逐步结果：[{"action": "click", "ok": True}, ...]
    actions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ScreenshotOutcome:
    ok: bool
    url: str
    final_url: str
    status: int
    png: bytes
    elapsed_ms: int
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    # pdf=True 时的整页 PDF；此时 png 为空
    pdf: bytes = b""


class BrowserRenderer:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._sessions = SessionStore(settings.data_dir)
        self._playwright: Any = None
        self._browser: Any = None
        # 池里浏览器是有头还是无头：登录要求与当前模式不一致时另起临时浏览器，
        # 避免把正在服务抓取请求的池关掉。
        self._browser_headless: bool | None = None
        # 每站（按会话名隔离）一个 context，各带一份独立的登录态；
        # 按最近使用排序，超过 l2_max_contexts 时淘汰最久未用的那个。
        # key 带上代理：不同出口的 context 不能混用，否则请求会走错代理。
        self._contexts: OrderedDict[tuple[str, str, str], Any] = OrderedDict()
        self._lock = asyncio.Lock()
        # 并发页上限：Semaphore 会绑定首次使用时的事件循环，换循环就重建
        self._slots: asyncio.Semaphore | None = None
        self._slots_loop: asyncio.AbstractEventLoop | None = None
        # 在途渲染/登录数：>0 时不做空闲回收，避免把正在用的浏览器关掉
        self._active = 0
        # 等待页位（l2_max_pages）的请求数：只用于 /metrics 观测排队压力
        self._queued = 0
        self._last_used = time.monotonic()
        self._reaper: asyncio.Task[None] | None = None
        # 完全相同的渲染请求合并：并发打开同一页面时只跑一次
        self._inflight = SingleFlight()

    def has_session(self, host: str, session: str = "default") -> bool:
        return self._sessions.has(host, session)

    def snapshot(self) -> dict[str, int]:
        """当前占用（给 `/metrics` 用）：在途页数、常驻 context 数与两项上限。

        只读、不触发任何浏览器操作；未启动时各项都是 0/配置值。
        """
        return {
            "running": 1 if self._browser is not None else 0,
            "active_pages": max(0, self._active),
            "queued": max(0, self._queued),
            "contexts": len(self._contexts),
            "max_pages": max(1, int(self._s.l2_max_pages)),
            "max_contexts": max(1, int(self._s.l2_max_contexts)),
        }

    # ---------- 生命周期 ----------

    async def _context_for(
        self,
        host: str,
        session: str = "default",
        *,
        proxy: str | None = None,
        mobile: bool = False,
    ) -> Any:
        # 移动端需要 is_mobile/has_touch/UA，这些是 context 级属性，不能对已有页面临时改
        key = (host, session, proxy or "", "mobile" if mobile else "desktop")
        async with self._lock:
            context = self._contexts.get(key)
            if context is not None:
                self._contexts.move_to_end(key)
                return context
            try:
                context = await self._new_context(host, session, proxy=proxy, mobile=mobile)
            except Exception:
                # 半途失败会留下已启动的浏览器/驱动进程，先收干净再抛，避免每次重试都漏一个
                await self._shutdown()
                raise
            self._contexts[key] = context
            await self._trim_contexts()
            return context

    async def _trim_contexts(self) -> None:
        """超出上限时关掉最久未用的 context，避免跨站抓取把内存越堆越多。"""
        limit = max(1, int(self._s.l2_max_contexts))
        while len(self._contexts) > limit:
            _key, stale = self._contexts.popitem(last=False)
            with contextlib.suppress(Exception):
                await stale.close()

    def _page_slots(self) -> asyncio.Semaphore:
        """取并发页配额；换事件循环（多次 asyncio.run）时重建，理由同 HTTP 会话。"""
        loop = asyncio.get_running_loop()
        if self._slots is None or self._slots_loop is not loop:
            self._slots = asyncio.Semaphore(max(1, int(self._s.l2_max_pages)))
            self._slots_loop = loop
        return self._slots

    def _touch(self) -> None:
        """记录最近活动时间，并确保空闲回收协程在跑（阈值 0 表示不回收）。"""
        self._last_used = time.monotonic()
        if self._s.l2_idle_timeout <= 0:
            return
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_idle_loop())

    async def _reap_idle_loop(self) -> None:
        while True:
            await asyncio.sleep(_reap_interval(self._s.l2_idle_timeout))
            if self._browser is None:
                return  # 已被别处关掉（启动失败清理 / 进程退出）
            if await self._reap_if_idle():
                return

    async def _reap_if_idle(self) -> bool:
        """空闲超过阈值就回收；返回 True 表示已经关掉。

        判断放在锁内：避免与正在创建 context 的请求撞车，把刚启动的浏览器关掉。
        """
        async with self._lock:
            if self._browser is None:
                return True
            if self._s.l2_idle_timeout <= 0:
                return False
            if self._active > 0:
                return False
            if time.monotonic() - self._last_used < self._s.l2_idle_timeout:
                return False
            await self._shutdown()
            return True

    async def _shutdown(self) -> None:
        """关掉 context / 浏览器 / 驱动；可重复调用，关闭失败不影响后续重来。"""
        contexts, self._contexts = list(self._contexts.values()), OrderedDict()
        for context in contexts:
            with contextlib.suppress(Exception):
                await context.close()
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._playwright = self._browser = None
        self._browser_headless = None

    async def _ensure_playwright(self) -> Any:
        """取 Playwright 驱动；延迟导入，没装 playwright 时 L1 仍然可用。"""
        from playwright.async_api import async_playwright

        if self._playwright is None:
            self._playwright = await async_playwright().start()
        return self._playwright

    async def _new_context(
        self,
        host: str,
        session: str = "default",
        *,
        headless: bool | None = None,
        proxy: str | None = None,
        mobile: bool = False,
        browser: Any = None,
    ) -> Any:
        playwright = await self._ensure_playwright()
        target = browser if browser is not None else self._browser
        if target is None:
            target = await self._launch(
                playwright, headless=self._s.l2_headless if headless is None else headless
            )
            self._browser = target
            self._browser_headless = bool(self._s.l2_headless if headless is None else headless)
        version = getattr(target, "version", "") or ""
        # 单请求代理优先于全局配置；两者都没有时不开代理
        server = proxy or self._s.proxy
        kwargs: dict[str, Any] = {
            "locale": self._s.l2_locale,
            "timezone_id": self._s.l2_timezone,
            "user_agent": self._user_agent(version),
            "viewport": {"width": 1440, "height": 900},
            "proxy": {"server": server} if server else None,
        }
        if mobile:
            # 设备预设的移动端：改 UA 并开启触屏，视口尺寸由调用方随后 set_viewport_size
            kwargs["user_agent"] = (
                "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
            )
            kwargs["is_mobile"] = True
            kwargs["has_touch"] = True
        # 该站该会话已有登录态则直接带上，跳过登录墙/再登录
        saved = self._sessions.load(host, session)
        if saved:
            kwargs["storage_state"] = saved
        context = await target.new_context(**kwargs)
        await context.add_init_script(
            _STEALTH_JS
            % {
                "languages": _js_languages(self._s.accept_language),
                # 每会话一套种子，确保 canvas 噪声跨站点一致且本会话内同输入恒同输出
                "cr": self._seed_part(61),
                "cg": self._seed_part(3),
                "cb": self._seed_part(37),
            }
        )
        return context

    def _seed_part(self, mul: int) -> int:
        # 由进程内时间与因子派生一个稳定的小整数，同会话内不变
        return int(time.time() * mul) % 251

    async def _launch(self, playwright: Any, *, headless: bool | None = None) -> Any:
        last_exc: Exception | None = None
        for kwargs in self._launch_kwargs(headless=headless):
            try:
                return await playwright.chromium.launch(**kwargs)
            except Exception as exc:  # 渠道缺失/未安装 Chromium 都属预期内失败，继续降级
                last_exc = exc
        raise RuntimeError(f"所有浏览器渠道均启动失败: {last_exc}")

    def _launch_kwargs(self, *, headless: bool | None = None) -> list[dict[str, Any]]:
        headless = self._s.l2_headless if headless is None else headless
        base: dict[str, Any] = {"headless": headless, "args": list(_LAUNCH_ARGS)}
        if self._s.l2_channel:
            return [{"channel": self._s.l2_channel, **base}]
        # 优先 channel="chromium"：它用完整浏览器的新版无头模式，
        # 比默认的 headless shell 少一堆可识别特征。再退回自带 shell 与系统浏览器。
        return [
            {"channel": "chromium", **base},
            dict(base),
            {"channel": "chrome", **base},
            {"channel": "msedge", **base},
        ]

    @staticmethod
    def _user_agent(browser_version: str) -> str:
        # headless shell 的 UA 里带 HeadlessChrome，是最廉价的识别特征之一
        major = (browser_version.split(".")[0] or "140").strip()
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        )

    async def aclose(self) -> None:
        """进程退出或显式释放：先停掉空闲回收协程，再关掉浏览器。"""
        reaper, self._reaper = self._reaper, None
        if reaper is not None and not reaper.done():
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
        async with self._lock:
            await self._shutdown()

    # ---------- 渲染 ----------

    async def render(
        self,
        url: str,
        host: str | None = None,
        *,
        wait_for: str | None = None,
        scroll: bool = False,
        click: str | None = None,
        actions: list[dict[str, Any]] | None = None,
        session: str = "default",
        timeout: float | None = None,
        proxy: str | None = None,
    ) -> BrowserOutcome:
        t0 = time.perf_counter()
        host = host or urlparse(url).netloc.lower()
        # 渲染结果只由 URL + 会话 + 交互参数决定；完全相同的并发请求合并成一次，
        # 其余等待并复用同一份 HTML（多租户/批量同时打开同一页面时很常见）
        identify = (
            url,
            host,
            wait_for or "",
            bool(scroll),
            click or "",
            session,
            proxy or "",
            timeout or 0.0,
            json.dumps(actions or [], sort_keys=True, ensure_ascii=False),
        )
        return await self._inflight.run(
            identify,
            lambda: self._render_queued(
                url,
                host,
                t0,
                wait_for=wait_for,
                scroll=scroll,
                click=click,
                actions=actions,
                session=session,
                timeout=timeout,
                proxy=proxy,
            ),
        )

    async def _render_queued(
        self,
        url: str,
        host: str,
        t0: float,
        *,
        wait_for: str | None,
        scroll: bool,
        click: str | None,
        actions: list[dict[str, Any]] | None,
        session: str,
        timeout: float | None,
        proxy: str | None = None,
    ) -> BrowserOutcome:
        # 并发页上限：超出时排队；排队期间不计入在途，空闲回收仍可正常关浏览器
        slots = self._page_slots()
        self._queued += 1
        try:
            await slots.acquire()
        finally:
            self._queued -= 1
        self._active += 1
        self._touch()
        try:
            return await self._render_once(
                url,
                host,
                t0,
                wait_for=wait_for,
                scroll=scroll,
                click=click,
                actions=actions,
                session=session,
                timeout=timeout,
                proxy=proxy,
            )
        finally:
            self._active -= 1
            slots.release()

    async def _render_once(
        self,
        url: str,
        host: str,
        t0: float,
        *,
        wait_for: str | None,
        scroll: bool,
        click: str | None,
        actions: list[dict[str, Any]] | None,
        session: str,
        timeout: float | None,
        proxy: str | None = None,
    ) -> BrowserOutcome:
        # 单步超时可由调用方的总预算压缩（deadline_ms），默认仍取配置值
        nav_ms = int((timeout or self._s.l2_navigate_timeout) * 1000)
        try:
            context = await self._context_for(host, session, proxy=proxy)
        except Exception as exc:
            return BrowserOutcome(
                ok=False,
                url=url,
                final_url=url,
                status=0,
                html="",
                elapsed_ms=_ms(t0),
                error=f"浏览器启动失败: {type(exc).__name__}: {exc}",
            )

        page = await context.new_page()
        notes: list[str] = []
        wait_for_hit: bool | None = None
        clicked: bool | None = None
        scroll_rounds = 0
        action_trail: list[dict[str, Any]] = []
        try:
            resp = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=nav_ms,
            )
            status = resp.status if resp is not None else 0
            wait_for_hit, clicked, scroll_rounds = await self._interact(
                page, wait_for=wait_for, scroll=scroll, click=click, notes=notes, timeout=timeout
            )
            if actions:
                action_trail = await self._apply_actions(page, actions, notes, timeout)
            html = await self._settle(page, status=status)
            return BrowserOutcome(
                ok=True,
                url=url,
                final_url=page.url,
                status=status,
                html=html,
                elapsed_ms=_ms(t0),
                notes=notes,
                wait_for_hit=wait_for_hit,
                clicked=clicked,
                scroll_rounds=scroll_rounds,
                actions=action_trail,
            )
        except Exception as exc:
            return BrowserOutcome(
                ok=False,
                url=url,
                final_url=url,
                status=0,
                html="",
                elapsed_ms=_ms(t0),
                error=f"{type(exc).__name__}: {exc}",
                notes=notes,
                wait_for_hit=wait_for_hit,
                clicked=clicked,
                scroll_rounds=scroll_rounds,
                actions=action_trail,
            )
        finally:
            with contextlib.suppress(Exception):
                await page.close()

    async def _interact(
        self,
        page: Any,
        *,
        wait_for: str | None,
        scroll: bool,
        click: str | None,
        notes: list[str],
        timeout: float | None = None,
    ) -> tuple[bool | None, bool | None, int]:
        """渲染后的可选交互：等节点、点"加载更多"、滚动触发懒加载。

        每一环失败都不致命：只记录到 notes 并继续，避免因页面改版导致整次抓取失败。
        返回 (wait_for 是否命中, 是否点击成功, 实际滚动轮数) 供诊断使用。
        """
        wait_for_hit: bool | None = None
        clicked: bool | None = None
        scroll_rounds = 0

        if wait_for:
            wait_for_hit = False
            try:
                wait_ms = int((timeout or self._s.l2_navigate_timeout) * 1000)
                await page.wait_for_selector(wait_for, timeout=wait_ms)
                wait_for_hit = True
            except Exception:
                notes.append(f"wait_for 未命中: {wait_for}")
        if click:
            clicked = False
            try:
                await page.click(click, timeout=5000)
                await page.wait_for_timeout(300)
                clicked = True
            except Exception:
                notes.append(f"click 未命中: {click}")
        if scroll:
            for _ in range(max(0, int(self._s.l2_scroll_steps))):
                try:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    break
                scroll_rounds += 1
                await page.wait_for_timeout(self._s.l2_scroll_delay_ms)
        return wait_for_hit, clicked, scroll_rounds

    async def _apply_actions(
        self,
        page: Any,
        actions: list[dict[str, Any]],
        notes: list[str],
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """按顺序执行动作链；单步失败只记 notes，后续动作继续（已拿到的正文不受影响）。

        动作由 server 层校验并规范化，这里只认 click/type/press/select/wait/scroll，
        不提供任意 JS 执行入口。
        """
        nav_ms = int((timeout or self._s.l2_navigate_timeout) * 1000)
        step_ms = max(1000, min(nav_ms, 10000))
        trail: list[dict[str, Any]] = []

        for index, item in enumerate(actions, start=1):
            name = str(item.get("action") or "")
            selector = item.get("selector")
            try:
                if name == "click":
                    await page.click(selector, timeout=step_ms)
                elif name == "type":
                    await page.fill(selector, item.get("text") or "", timeout=step_ms)
                elif name == "press":
                    if selector:
                        await page.press(selector, item["key"], timeout=step_ms)
                    else:
                        await page.keyboard.press(item["key"])
                elif name == "select":
                    await page.select_option(selector, item["value"], timeout=step_ms)
                elif name == "wait":
                    if selector:
                        wait_ms = min(step_ms, int(item.get("ms") or step_ms))
                        await page.wait_for_selector(selector, timeout=wait_ms)
                    else:
                        await page.wait_for_timeout(min(int(item.get("ms") or 1000), nav_ms))
                elif name == "scroll":
                    if selector:
                        await page.locator(selector).scroll_into_view_if_needed(timeout=step_ms)
                    else:
                        for _ in range(max(1, min(int(item.get("times") or 1), 20))):
                            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            await page.wait_for_timeout(200)
                trail.append({"action": name, "ok": True})
            except Exception as exc:
                notes.append(f"action[{index}] {name} 失败: {type(exc).__name__}")
                trail.append({"action": name, "ok": False, "detail": type(exc).__name__})
        return trail

    # ---------- 截图 ----------

    async def screenshot(
        self,
        url: str,
        host: str | None = None,
        *,
        full_page: bool = False,
        width: int = 1280,
        height: int = 800,
        wait_ms: int = 0,
        selector: str | None = None,
        hide: list[str] | None = None,
        pdf: bool = False,
        mobile: bool = False,
        actions: list[dict[str, Any]] | None = None,
        session: str = "default",
        timeout: float | None = None,
        proxy: str | None = None,
    ) -> ScreenshotOutcome:
        """渲染并截图；复用同站同会话的 context，带登录态与 stealth 初始化脚本。"""
        t0 = time.perf_counter()
        host = host or urlparse(url).netloc.lower()
        slots = self._page_slots()
        self._queued += 1
        try:
            await slots.acquire()
        finally:
            self._queued -= 1
        self._active += 1
        self._touch()
        try:
            return await self._screenshot_once(
                url,
                host,
                t0,
                full_page=full_page,
                width=width,
                height=height,
                wait_ms=wait_ms,
                selector=selector,
                hide=hide,
                pdf=pdf,
                mobile=mobile,
                actions=actions,
                session=session,
                timeout=timeout,
                proxy=proxy,
            )
        finally:
            self._active -= 1
            slots.release()

    async def _screenshot_once(
        self,
        url: str,
        host: str,
        t0: float,
        *,
        full_page: bool,
        width: int,
        height: int,
        wait_ms: int,
        selector: str | None,
        hide: list[str] | None,
        pdf: bool,
        mobile: bool,
        actions: list[dict[str, Any]] | None,
        session: str,
        timeout: float | None,
        proxy: str | None,
    ) -> ScreenshotOutcome:
        nav_ms = int((timeout or self._s.l2_navigate_timeout) * 1000)
        try:
            context = await self._context_for(host, session, proxy=proxy, mobile=mobile)
        except Exception as exc:
            return ScreenshotOutcome(
                ok=False,
                url=url,
                final_url=url,
                status=0,
                png=b"",
                elapsed_ms=_ms(t0),
                error=f"浏览器启动失败: {type(exc).__name__}: {exc}",
            )

        page = await context.new_page()
        notes: list[str] = []
        status = 0
        try:
            await page.set_viewport_size({"width": width, "height": height})
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=nav_ms)
            status = resp.status if resp is not None else 0
            if actions:
                await self._apply_actions(page, actions, notes, timeout)
            if hide:
                # 用行内样式隐藏而不是删节点：保留布局上下文，避免页面重排后截图错位
                for css in hide:
                    try:
                        await page.locator(css).evaluate_all(
                            "els => els.forEach((el) => "
                            "el.style.setProperty('display', 'none', 'important'))"
                        )
                    except Exception as exc:
                        notes.append(f"hide 失败: {css} ({type(exc).__name__})")

            hit = True
            if selector:
                # 选择器既是"等它出现再拍"的稳定点，也是元素裁剪的目标
                try:
                    await page.wait_for_selector(selector, timeout=min(nav_ms, 5000))
                except Exception:
                    notes.append(f"selector 未命中: {selector}")
                    hit = False
            if wait_ms > 0:
                await page.wait_for_timeout(wait_ms)

            if pdf:
                # PDF 始终是整页；selector 只当等待点，元素裁剪不适用
                try:
                    data = await page.pdf(print_background=True)
                except Exception as exc:
                    # 非 Chromium 内核不支持 page.pdf；用固定前缀让上层映射成稳定错误码
                    return ScreenshotOutcome(
                        ok=False,
                        url=url,
                        final_url=page.url,
                        status=status,
                        png=b"",
                        elapsed_ms=_ms(t0),
                        error=f"Unsupported: PDF 导出失败（需 Chromium）: {type(exc).__name__}",
                        notes=notes,
                    )
                return ScreenshotOutcome(
                    ok=True,
                    url=url,
                    final_url=page.url,
                    status=status,
                    png=b"",
                    pdf=data,
                    elapsed_ms=_ms(t0),
                    notes=notes,
                )
            if selector:
                if hit:
                    try:
                        png = await page.locator(selector).screenshot(timeout=min(nav_ms, 5000))
                    except Exception as exc:
                        # 元素不可见/被遮挡时不让整次调用失败，回退整页并留下线索
                        notes.append(f"selector 元素截图失败，已回退整页: {type(exc).__name__}")
                        png = await page.screenshot(full_page=True)
                else:
                    png = await page.screenshot(full_page=True)
            else:
                png = await page.screenshot(full_page=full_page)
            return ScreenshotOutcome(
                ok=True,
                url=url,
                final_url=page.url,
                status=status,
                png=png,
                elapsed_ms=_ms(t0),
                notes=notes,
            )
        except Exception as exc:
            return ScreenshotOutcome(
                ok=False,
                url=url,
                final_url=url,
                status=status,
                png=b"",
                elapsed_ms=_ms(t0),
                error=f"{type(exc).__name__}: {exc}",
                notes=notes,
            )
        finally:
            with contextlib.suppress(Exception):
                await page.close()

    # ---------- 登录（M3） ----------

    async def login(
        self,
        url: str,
        timeout: float,
        session: str = "default",
        *,
        headless: bool = False,
        qr_selector: str | None = None,
        cdp_url: str | None = None,
    ) -> dict[str, Any]:
        """打开浏览器登录并把登录态落盘。

        默认有头（可在窗口里手动操作）；``headless=True`` 用于无 GUI 服务器，此时
        会把登录页（或 ``qr_selector`` 指定的二维码）截图落盘，调用方拿手机扫码/查看。
        ``cdp_url`` 可直接连到远程浏览器（用户在那台机器上操作），无需本地 GUI。
        轮询等待"离开登录页、正文变实"，超时未登录也会把当时的 storage_state
        存下来（便于下次从这里继续）。返回结构见 login() 解析方。
        """
        host = urlparse(url).netloc.lower()
        had_session = self.has_session(host, session)
        t0 = time.perf_counter()
        # 手动登录可能等上几分钟：计入在途，避免空闲回收把登录窗口关掉
        self._active += 1
        self._touch()
        try:
            return await self._login_once(
                url,
                host,
                timeout,
                session,
                had_session,
                t0,
                headless=headless,
                qr_selector=qr_selector,
                cdp_url=cdp_url,
            )
        finally:
            self._active -= 1

    async def _login_target(self, *, headless: bool, cdp_url: str | None) -> tuple[Any, bool]:
        """选择登录要用的浏览器，返回 (browser, 是否临时启动需要关闭)。

        池里已有同模式的浏览器就复用；模式不一致（比如抓取先以无头启动、随后要开
        有头登录）时另起一个临时浏览器，避免关掉正在服务请求的池。
        """
        playwright = await self._ensure_playwright()
        if cdp_url:
            return await playwright.chromium.connect_over_cdp(cdp_url), False
        if self._browser is not None and self._browser_headless == bool(headless):
            return self._browser, False
        return await self._launch(playwright, headless=headless), True

    async def _capture_login_shot(
        self, page: Any, host: str, session: str, qr_selector: str | None
    ) -> tuple[str | None, str | None]:
        """把登录页或二维码截图落到 DATA_DIR/login/；返回 (路径, 失败说明)。"""
        root = Path(os.path.expanduser(self._s.data_dir)) / "login"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", host).strip("._-") or "unknown"
        target = root / f"{safe}_{session}_{int(time.time() * 1000)}.png"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if qr_selector:
                with contextlib.suppress(Exception):
                    await page.wait_for_selector(qr_selector, timeout=5000)
                try:
                    await page.locator(qr_selector).first.screenshot(path=str(target))
                    return str(target), None
                except Exception as exc:
                    note = f"二维码选择器未命中（{qr_selector}），已回退整页截图: {exc}"
                await page.screenshot(path=str(target), full_page=False)
                return str(target), note
            await page.screenshot(path=str(target), full_page=False)
            return str(target), None
        except Exception as exc:
            return None, f"登录页截图失败: {type(exc).__name__}: {exc}"

    async def _login_once(
        self,
        url: str,
        host: str,
        timeout: float,
        session: str,
        had_session: bool,
        t0: float,
        *,
        headless: bool = False,
        qr_selector: str | None = None,
        cdp_url: str | None = None,
    ) -> dict[str, Any]:
        # 无界面/远程登录时用户看不到窗口：把登录页（或二维码）截图落盘，供查看或扫码
        notes: list[str] = []
        screenshot: str | None = None
        browser: Any = None
        context: Any = None
        page: Any = None
        owned = False
        try:
            browser, owned = await self._login_target(headless=headless, cdp_url=cdp_url)
            context = await self._new_context(host, session, headless=headless, browser=browser)
            page = await context.new_page()
            await page.goto(
                url, wait_until="domcontentloaded", timeout=self._s.l2_navigate_timeout * 1000
            )

            if headless or qr_selector:
                screenshot, note = await self._capture_login_shot(page, host, session, qr_selector)
                if note:
                    notes.append(note)

            deadline = time.perf_counter() + timeout
            logged_in = False
            while True:
                html = await _content(page)
                if _is_logged_in(page.url, html):
                    logged_in = True
                    break
                if time.perf_counter() >= deadline:
                    break
                await page.wait_for_timeout(2000)

            if not logged_in and headless:
                # 超时前刷新一次截图：二维码过期或页面状态变化时调用方能拿到最新画面
                refreshed, note = await self._capture_login_shot(page, host, session, qr_selector)
                if refreshed:
                    screenshot = refreshed
                if note:
                    notes.append(note)

            state = await context.storage_state()
            self._sessions.save(host, state, session)
            # 该站该会话若还留着带旧登录态的 context（桌面/移动各一份），都关掉，
            # 下次抓取重新加载新登录态
            stale_keys = [k for k in self._contexts if k[0] == host and k[1] == session]
            for key in stale_keys:
                stale = self._contexts.pop(key, None)
                if stale is not None:
                    with contextlib.suppress(Exception):
                        await stale.close()

            return {
                "ok": True,
                "host": host,
                "session": session,
                "url": page.url,
                "logged_in": logged_in,
                "had_session": had_session,
                "headless": headless,
                "screenshot": screenshot,
                "notes": notes,
                "elapsed_ms": _ms(t0),
            }
        except Exception as exc:
            return {
                "ok": False,
                "host": host,
                "session": session,
                "url": url,
                "logged_in": False,
                "headless": headless,
                "screenshot": screenshot,
                "notes": notes,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": _ms(t0),
            }
        finally:
            if page is not None:
                with contextlib.suppress(Exception):
                    await page.close()
            if context is not None:
                with contextlib.suppress(Exception):
                    await context.close()
            if owned and browser is not None:
                with contextlib.suppress(Exception):
                    await browser.close()

    async def _settle(self, page: Any, status: int | None = None) -> str:
        """等 JS 渲染/挑战放行：反复取内容，一旦得到"正常页"就提前返回。

        这里刻意用 status=200 调 detector：Cloudflare 挑战的首个响应常是 403/503，
        若带上真实状态码，页面放行后仍会因状态码被判成 waf，永远等不到出口。

        ``status`` 是导航的真实响应码，只用于识别"已成定局的拒绝"：裸 4xx/5xx 拒绝页
        等满窗口也只会拿到同一份内容，提前收工；挑战页/登录墙/骨架页照旧等下去。
        """
        start = time.perf_counter()
        deadline = start + self._s.l2_settle_timeout
        html = await _content(page)

        while True:
            kind = _pending_kind(html)
            if kind is None:
                return html
            if status is not None and is_terminal_rejection(status, html):
                return html
            cap = _SETTLE_CAP.get(kind)
            if cap is not None:
                deadline = min(start + cap, deadline)
            if time.perf_counter() >= deadline:
                return html
            await page.wait_for_timeout(_SETTLE_INTERVAL_MS)
            new_html = await _content(page)
            if new_html:
                html = new_html


def _pending_kind(html: str) -> str | None:
    """还需要继续等的类型；返回 None 表示已经可以收工。"""
    blocked, _ = detect_block(200, html)
    if blocked in _CHALLENGE_TYPES:
        return blocked
    # 骨架页的可见文本在渲染后会涨上来，故仍按可见文本判断"是否还在等"；
    # 但渲染后门槛放低：上千字可见文本已经不是骨架页，再等也等不出新正文
    return "shell" if looks_like_shell(html, max_visible=_SETTLE_SHELL_MAX_VISIBLE) else None


# 登录页 URL 上的常见路径特征；命中说明用户还停留在登录页、尚未完成登录
_LOGIN_PATH_TOKENS = (
    "signin",
    "sign-in",
    "login",
    "passport",
    "auth",
    "ucenter",
    "account/login",
    "switchlogin",
)


def _is_logged_in(url: str, html: str) -> bool:
    """认为"已登录"：正文变实、且离开了登录页。

    反过来讲：要么内容仍是空壳/挑战页（JS 未渲染完），要么 URL 还在 signin 上，
    两者都说明登录态还没拿到，继续等。
    """
    blocked, _ = detect_block(200, html)
    if blocked in _CHALLENGE_TYPES or looks_like_shell(html):
        return False
    path = urlparse(url).path.lower()
    return not any(token in path for token in _LOGIN_PATH_TOKENS)


async def _content(page: Any) -> str:
    try:
        return await page.content()
    except Exception:
        return ""


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _js_languages(accept_language: str) -> str:
    """把 Accept-Language 转成 JS 数组字面量，供 stealth 脚本伪装 navigator.languages。"""
    langs = [part.split(";")[0].strip() for part in (accept_language or "").split(",")]
    langs = [lang for lang in langs if lang]
    if "en" not in langs:
        langs.append("en")
    return "[" + ", ".join(f"'{lang}'" for lang in langs) + "]"
