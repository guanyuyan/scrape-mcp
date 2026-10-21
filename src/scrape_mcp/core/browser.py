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
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..config import Settings
from .detector import detect_block, looks_like_shell
from .session import SessionStore

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


class BrowserRenderer:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._sessions = SessionStore(settings.data_dir)
        self._playwright: Any = None
        self._browser: Any = None
        # 每站（按会话名隔离）一个 context，各带一份独立的登录态；
        # 按最近使用排序，超过 l2_max_contexts 时淘汰最久未用的那个
        self._contexts: OrderedDict[tuple[str, str], Any] = OrderedDict()
        self._lock = asyncio.Lock()
        # 并发页上限：Semaphore 会绑定首次使用时的事件循环，换循环就重建
        self._slots: asyncio.Semaphore | None = None
        self._slots_loop: asyncio.AbstractEventLoop | None = None
        # 在途渲染/登录数：>0 时不做空闲回收，避免把正在用的浏览器关掉
        self._active = 0
        self._last_used = time.monotonic()
        self._reaper: asyncio.Task[None] | None = None

    def has_session(self, host: str, session: str = "default") -> bool:
        return self._sessions.has(host, session)

    # ---------- 生命周期 ----------

    async def _context_for(self, host: str, session: str = "default") -> Any:
        key = (host, session)
        async with self._lock:
            context = self._contexts.get(key)
            if context is not None:
                self._contexts.move_to_end(key)
                return context
            try:
                context = await self._new_context(host, session)
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

    async def _new_context(
        self, host: str, session: str = "default", *, headless: bool | None = None
    ) -> Any:
        # 延迟导入：没装 playwright 时 L1 仍然可用
        from playwright.async_api import async_playwright

        if self._playwright is None:
            self._playwright = await async_playwright().start()
        if self._browser is None:
            self._browser = await self._launch(
                self._playwright, headless=self._s.l2_headless if headless is None else headless
            )
        version = getattr(self._browser, "version", "") or ""
        kwargs: dict[str, Any] = {
            "locale": self._s.l2_locale,
            "timezone_id": self._s.l2_timezone,
            "user_agent": self._user_agent(version),
            "viewport": {"width": 1440, "height": 900},
            "proxy": {"server": self._s.proxy} if self._s.proxy else None,
        }
        # 该站该会话已有登录态则直接带上，跳过登录墙/再登录
        saved = self._sessions.load(host, session)
        if saved:
            kwargs["storage_state"] = saved
        context = await self._browser.new_context(**kwargs)
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
        session: str = "default",
    ) -> BrowserOutcome:
        t0 = time.perf_counter()
        host = host or urlparse(url).netloc.lower()
        # 并发页上限：超出时排队；排队期间不计入在途，空闲回收仍可正常关浏览器
        async with self._page_slots():
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
                    session=session,
                )
            finally:
                self._active -= 1

    async def _render_once(
        self,
        url: str,
        host: str,
        t0: float,
        *,
        wait_for: str | None,
        scroll: bool,
        click: str | None,
        session: str,
    ) -> BrowserOutcome:
        try:
            context = await self._context_for(host, session)
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
        try:
            resp = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self._s.l2_navigate_timeout * 1000,
            )
            status = resp.status if resp is not None else 0
            wait_for_hit, clicked, scroll_rounds = await self._interact(
                page, wait_for=wait_for, scroll=scroll, click=click, notes=notes
            )
            html = await self._settle(page)
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
                await page.wait_for_selector(wait_for, timeout=self._s.l2_navigate_timeout * 1000)
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

    # ---------- 登录（M3） ----------

    async def login(self, url: str, timeout: float, session: str = "default") -> dict[str, Any]:
        """打开带界面的浏览器手动登录，成功后可保存登录态。

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
            return await self._login_once(url, host, timeout, session, had_session, t0)
        finally:
            self._active -= 1

    async def _login_once(
        self, url: str, host: str, timeout: float, session: str, had_session: bool, t0: float
    ) -> dict[str, Any]:
        # 手动登录必须能看到界面；新开一个不落缓存的 context，避免被已存登录态干扰
        context = await self._new_context(host, session, headless=False)
        page = await context.new_page()
        try:
            await page.goto(
                url, wait_until="domcontentloaded", timeout=self._s.l2_navigate_timeout * 1000
            )

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

            state = await context.storage_state()
            self._sessions.save(host, state, session)
            # 该站该会话若还留着带旧登录态的 context，关掉它，下次抓取重新加载新登录态
            stale = self._contexts.pop((host, session), None)
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
                "elapsed_ms": _ms(t0),
            }
        except Exception as exc:
            return {
                "ok": False,
                "host": host,
                "session": session,
                "url": url,
                "logged_in": False,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": _ms(t0),
            }
        finally:
            try:
                await page.close()
                await context.close()
            except Exception:
                pass

    async def _settle(self, page: Any) -> str:
        """等 JS 渲染/挑战放行：反复取内容，一旦得到"正常页"就提前返回。

        这里刻意用 status=200 调 detector：Cloudflare 挑战的首个响应常是 403/503，
        若带上真实状态码，页面放行后仍会因状态码被判成 waf，永远等不到出口。
        """
        start = time.perf_counter()
        deadline = start + self._s.l2_settle_timeout
        html = await _content(page)

        while True:
            kind = _pending_kind(html)
            if kind is None:
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
    # 骨架页的可见文本在渲染后会涨上来，故用同一把尺子判断"是否还在等"
    return "shell" if looks_like_shell(html) else None


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
