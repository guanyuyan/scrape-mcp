"""抓取调度：L1（curl_cffi）打头阵，命中拦截才升级 L2（Playwright 渲染）。

上层只依赖 FetchOutcome，升级链路对 MCP 工具层透明。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlparse

from ..config import Settings
from . import deadline as budget
from .browser import BrowserRenderer
from .detector import detect_block, looks_like_login_wall, looks_like_shell
from .http_client import HttpClient
from .proxy import ProxyPool, split_pool

# 这些判定不值得升级浏览器：同一 IP 下换浏览器通常仍被限流，
# 只是白付一次浏览器启动成本。
_NO_ESCALATE = ("rate_limit",)

# 升级后仍未放行、很可能需要登录态的判定类型
_LOGIN_CANDIDATES = ("captcha", "waf", "empty_spa", "empty", "cloudflare")

# 剩余预算低于这个值时不再发起新的尝试/不再升级浏览器：
# 一次 L1 至少要几百毫秒，浏览器启动 + 渲染则是秒级。
_MIN_ATTEMPT_S = 0.3
_L2_MIN_BUDGET_S = 1.5

# 粘性升级：L2 真拿到正文后，在 TTL 内同一主机直接走浏览器，
# 省掉一次注定被拦的 L1。站点改版/风控解除后最多 TTL 秒就会重新走 L1。
_STICKY_TTL_S = 600.0
_STICKY_MAX_HOSTS = 512
# 明显是资源文件的 URL 不粘性升级：浏览器拿不到 PDF/图片的正文，交给 L1 更合适
_ASSET_SUFFIXES = (
    ".pdf",
    ".zip",
    ".gz",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".mp3",
    ".mp4",
    ".csv",
    ".json",
    ".xml",
    ".txt",
)


@dataclass
class FetchOutcome:
    url: str
    final_url: str
    status: int
    tier: str
    blocked: str
    block_reason: str
    content_type: str
    html: str
    elapsed_ms: int
    error: str | None = None
    session_loaded: bool = False
    login_required: bool = False
    cached: bool = False
    attempts: int = 1
    notes: list[str] = field(default_factory=list)
    # 服务端要求的下次请求间隔（秒），来自 Retry-After
    retry_after: float | None = None
    # 分层决策轨迹（如 l1_shell / escalate_l2 / l2_failed），供调用方诊断
    decisions: list[str] = field(default_factory=list)
    # L2 交互细节：wait_for 是否命中、点击是否成功、实际滚动轮数
    l2: dict[str, Any] = field(default_factory=dict)
    # 内容形态：html（默认）/ pdf / text。非 HTML 不升级 L2，由上层直接转文本
    kind: str = "html"
    # 二进制正文（目前只有 PDF 用到）；从响应体原样带出，避免提前解码破坏结构
    content: bytes = b""


class Fetcher:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http = HttpClient(settings)
        self._browser: BrowserRenderer | None = None
        # host -> 粘性结论的到期时刻（monotonic）
        self._sticky: dict[str, float] = {}
        self._pool = ProxyPool(
            split_pool(settings.proxy_pool),
            fail_threshold=settings.proxy_pool_fail_threshold,
            cooldown_s=settings.proxy_pool_cooldown,
            sticky_ttl_s=settings.proxy_pool_sticky_ttl,
        )

    def _get_browser(self) -> BrowserRenderer:
        if self._browser is None:
            self._browser = BrowserRenderer(self._settings)
        return self._browser

    def proxy_snapshot(self) -> dict[str, int] | None:
        """代理池状态（出口数/健康/冷却）；未配置代理池时返回 None。"""
        return self._pool.snapshot() if self._pool else None

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.aclose()
            self._browser = None
        await self._http.aclose()

    def _sticky_active(self, host: str, url: str) -> bool:
        """该主机上次是否确认过"必须上浏览器"，且还没过期。"""
        if not self._settings.l2_enabled or _looks_like_asset(url):
            return False
        expiry = self._sticky.get(host)
        if expiry is None:
            return False
        if expiry <= time.monotonic():
            self._sticky.pop(host, None)
            return False
        return True

    def _remember_sticky(self, host: str) -> None:
        """L2 真拿到正文才记结论，并顺手控制表的大小。"""
        now = time.monotonic()
        if len(self._sticky) >= _STICKY_MAX_HOSTS:
            self._sticky = {h: t for h, t in self._sticky.items() if t > now}
            if len(self._sticky) >= _STICKY_MAX_HOSTS:
                oldest = min(self._sticky, key=lambda h: self._sticky[h])
                self._sticky.pop(oldest, None)
        self._sticky[host] = now + _STICKY_TTL_S

    async def fetch(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        timeout: float | None = None,
        wait_for: str | None = None,
        scroll: bool = False,
        click: str | None = None,
        bypass_cache: bool = False,
        session: str = "default",
        deadline: float | None = None,
        proxy: str | None = None,
    ) -> FetchOutcome:
        """为本次抓取固定一个出口，再走分级链路。

        出口优先级：单请求 proxy > 代理池 > 全局 SCRAPE_MCP_PROXY。池化出口按站点
        粘性分配，L1、重试与 L2 升级都走同一个 IP；结束后把结果回报给池用于冷却判定。
        """
        explicit = proxy is not None
        if not explicit:
            proxy = self._pool.pick(_host_of(url))
        outcome = await self._fetch(
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            wait_for=wait_for,
            scroll=scroll,
            click=click,
            # 显式指定的出口可能让同一 URL 拿到不同内容，这类结果不进共享缓存
            bypass_cache=bypass_cache,
            allow_cache=not explicit,
            session=session,
            deadline=deadline,
            proxy=proxy,
        )
        if proxy is not None and not explicit:
            self._pool.report(proxy, ok=not _egress_failed(outcome))
        return outcome

    async def _fetch(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        timeout: float | None = None,
        wait_for: str | None = None,
        scroll: bool = False,
        click: str | None = None,
        bypass_cache: bool = False,
        allow_cache: bool = True,
        session: str = "default",
        deadline: float | None = None,
        proxy: str | None = None,
    ) -> FetchOutcome:
        host = _host_of(url)
        sticky_failed = False
        if self._sticky_active(host, url):
            left = budget.remaining(deadline)
            if left is None or left >= _L2_MIN_BUDGET_S:
                sticky = await self._fetch_l2_sticky(
                    url,
                    wait_for=wait_for,
                    scroll=scroll,
                    click=click,
                    session=session,
                    deadline=deadline,
                    proxy=proxy,
                )
                if sticky is not None:
                    return sticky
                # 浏览器这次没跑起来：结论不可靠，清掉后照常走 L1
                self._sticky.pop(host, None)
                sticky_failed = True

        outcome = await self._fetch_l1_with_retry(
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            bypass_cache=bypass_cache,
            allow_cache=allow_cache,
            deadline=deadline,
            proxy=proxy,
        )
        if sticky_failed:
            outcome.decisions.insert(0, "sticky_l2_failed:fallback_l1")
        reason = self._escalation_reason(outcome)
        if reason is None:
            if outcome.blocked == "rate_limit":
                outcome.decisions.append("no_escalate:rate_limit")
            elif outcome.error:
                outcome.decisions.append("no_escalate:transport_error")
            elif outcome.kind != "html":
                outcome.decisions.append(f"no_escalate:{outcome.kind}")
            elif not self._settings.l2_enabled and outcome.blocked != "none":
                outcome.decisions.append("no_escalate:l2_disabled")
            return outcome
        left = budget.remaining(deadline)
        if left is not None and left < _L2_MIN_BUDGET_S:
            # 预算不够跑浏览器：把 L1 的判定如实返回，并说明为什么没升级
            outcome.decisions.append("deadline:skip_l2")
            return outcome
        outcome.decisions.append(f"escalate_l2:{reason}")
        result = await self._fetch_l2(
            url,
            outcome,
            wait_for=wait_for,
            scroll=scroll,
            click=click,
            session=session,
            deadline=deadline,
            proxy=proxy,
        )
        if result.tier == "l2" and result.blocked == "none":
            self._remember_sticky(host)
        return result

    async def _fetch_l1_with_retry(
        self,
        url: str,
        *,
        headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
        timeout: float | None,
        bypass_cache: bool,
        allow_cache: bool = True,
        deadline: float | None = None,
        proxy: str | None = None,
    ) -> FetchOutcome:
        """网络异常/5xx 指数退避重试；429/503 带 Retry-After 时按配置做一次有界等待。

        有总预算时，每次尝试的超时都被压进剩余时间；剩余不够一次尝试或一次退避
        就直接返回已有结果，不再为空等买单。
        """
        attempts = max(1, int(self._settings.retry_times) + 1)
        retry_after_used = False
        # 每次重试都是一次新的 L1 结果，决策轨迹要跨尝试累积，否则调用方只看到最后一次
        trail: list[str] = []
        outcome: FetchOutcome | None = None
        for i in range(1, attempts + 1):
            left = budget.remaining(deadline)
            if left is not None and left < _MIN_ATTEMPT_S:
                return _deadline_outcome(url, trail)
            outcome = await self._fetch_l1(
                url,
                headers=headers,
                cookies=cookies,
                timeout=budget.cap(deadline, timeout or self._settings.request_timeout),
                bypass_cache=bypass_cache,
                allow_cache=allow_cache,
                proxy=proxy,
            )
            if trail:
                outcome.decisions = [*trail, *outcome.decisions]
            outcome.attempts = i
            if i >= attempts:
                return outcome

            # 服务端明确要求"稍后再来"：先看 Retry-After，比通用 5xx 退避更准确
            wait = outcome.retry_after
            cap = self._settings.retry_after_max_wait
            if (
                not retry_after_used
                and wait is not None
                and cap > 0
                and wait <= cap
                and (outcome.blocked == "rate_limit" or outcome.status == 503)
            ):
                retry_after_used = True
                if _budget_too_short(deadline, wait):
                    outcome.decisions.append("deadline:skip_retry")
                    return outcome
                outcome.decisions.append(f"retry:retry_after_{wait:g}s")
                trail = list(outcome.decisions)
                await asyncio.sleep(wait)
                continue

            if not _is_retryable(outcome):
                return outcome
            backoff = self._settings.retry_backoff * (2 ** (i - 1))
            if _budget_too_short(deadline, backoff):
                outcome.decisions.append("deadline:skip_retry")
                return outcome
            outcome.decisions.append("retry:transport" if outcome.error else "retry:5xx")
            trail = list(outcome.decisions)
            await asyncio.sleep(backoff)
        return outcome

    def _escalation_reason(self, outcome: FetchOutcome) -> str | None:
        """需要升级 L2 时返回原因标签；不需要返回 None。"""
        if not self._settings.l2_enabled or outcome.error:
            return None
        # PDF/纯文本没有 DOM，浏览器渲染不会带来更多信息，只会白付一次启动成本
        if outcome.kind != "html":
            return None
        if outcome.blocked == "none":
            # 没被判成拦截页，但大 HTML 榨不出可见文本时仍是骨架页/登录墙：
            # 这类页面（如 LinkedIn）只有浏览器把 JS 跑起来才有正文
            return "shell" if looks_like_shell(outcome.html) else None
        if outcome.blocked in _NO_ESCALATE:
            return None
        return outcome.blocked

    async def _fetch_l1(
        self,
        url: str,
        *,
        headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
        timeout: float | None = None,
        bypass_cache: bool = False,
        allow_cache: bool = True,
        proxy: str | None = None,
    ) -> FetchOutcome:
        resp = await self._http.get(
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            bypass_cache=bypass_cache,
            allow_cache=allow_cache,
            proxy=proxy,
        )
        if not resp.ok:
            return FetchOutcome(
                url=url,
                final_url=url,
                status=0,
                tier="l1",
                blocked="none",
                block_reason="",
                content_type="",
                html="",
                elapsed_ms=resp.elapsed_ms,
                error=resp.error,
            )
        kind = _content_kind(resp.content_type, resp.url or url)
        if kind != "html":
            return FetchOutcome(
                url=url,
                final_url=resp.url,
                status=resp.status,
                tier="l1",
                blocked="none",
                block_reason="",
                content_type=resp.content_type,
                html=resp.text,
                elapsed_ms=resp.elapsed_ms,
                cached=resp.cached,
                retry_after=resp.retry_after,
                kind=kind,
                content=resp.content,
            )
        blocked, reason = detect_block(resp.status, resp.text, resp.content_type)
        return FetchOutcome(
            url=url,
            final_url=resp.url,
            status=resp.status,
            tier="l1",
            blocked=blocked,
            block_reason=reason,
            content_type=resp.content_type,
            html=resp.text,
            elapsed_ms=resp.elapsed_ms,
            cached=resp.cached,
            retry_after=resp.retry_after,
        )

    async def _fetch_l2_sticky(
        self,
        url: str,
        *,
        wait_for: str | None,
        scroll: bool,
        click: str | None,
        session: str,
        deadline: float | None,
        proxy: str | None = None,
    ) -> FetchOutcome | None:
        """已知需要浏览器的主机：跳过 L1 直接渲染；渲染失败返回 None 交给 L1 兜底。"""
        placeholder = FetchOutcome(
            url=url,
            final_url=url,
            status=0,
            tier="l1",
            blocked="none",
            block_reason="",
            content_type="",
            html="",
            elapsed_ms=0,
            decisions=["skip_l1:sticky_l2"],
        )
        outcome = await self._fetch_l2(
            url,
            placeholder,
            wait_for=wait_for,
            scroll=scroll,
            click=click,
            session=session,
            deadline=deadline,
            proxy=proxy,
        )
        return None if outcome.tier != "l2" else outcome

    async def _fetch_l2(
        self,
        url: str,
        l1: FetchOutcome,
        *,
        wait_for: str | None = None,
        scroll: bool = False,
        click: str | None = None,
        session: str = "default",
        deadline: float | None = None,
        proxy: str | None = None,
    ) -> FetchOutcome:
        host = _host_of(l1.final_url or url)
        browser = self._get_browser()
        result = await browser.render(
            url,
            host,
            wait_for=wait_for,
            scroll=scroll,
            click=click,
            session=session,
            # 导航超时按剩余预算压缩，避免一页把整次调用的预算吃光
            timeout=budget.cap(deadline, self._settings.l2_navigate_timeout),
            proxy=proxy,
        )
        elapsed = l1.elapsed_ms + result.elapsed_ms
        l2_diag = {
            "wait_for_hit": result.wait_for_hit,
            "clicked": result.clicked,
            "scroll_rounds": result.scroll_rounds,
            "error": result.error,
        }

        if not result.ok:
            # 浏览器没跑起来/渲染失败：正文仍是 L1 的，只把原因如实挂上去，tier 保持 l1
            return replace(
                l1,
                block_reason=f"{l1.block_reason}；L2 渲染失败: {result.error}",
                elapsed_ms=elapsed,
                session_loaded=browser.has_session(host, session),
                notes=[*l1.notes, *result.notes],
                decisions=[*l1.decisions, "l2_failed"],
                l2=l2_diag,
            )

        blocked, reason = detect_block(result.status, result.html)
        # 只有页面上真的有登录入口时才提示用户去 login：裸 403（出口/IP 风控、WAF
        # 拒绝页）登录也救不了，按 blocked 如实上报，别把人往没用的操作上引
        login_required = blocked in _LOGIN_CANDIDATES and (
            blocked != "waf" or looks_like_login_wall(result.html)
        )
        return FetchOutcome(
            url=url,
            final_url=result.final_url or url,
            status=result.status,
            tier="l2",
            blocked=blocked,
            block_reason=reason,
            content_type="text/html",
            html=result.html,
            elapsed_ms=elapsed,
            session_loaded=browser.has_session(host, session),
            login_required=login_required,
            attempts=l1.attempts,
            notes=result.notes,
            retry_after=l1.retry_after,
            decisions=[*l1.decisions, "l2_ok"],
            l2=l2_diag,
        )

    async def login(self, url: str, timeout: float | None = None, session: str = "default") -> dict:
        """手动登录并持久化该站登录态。返回带登录结果的结构，供 MCP 工具直接透传。"""
        timeout = timeout or self._settings.login_timeout
        return await self._get_browser().login(url, timeout, session)

    def has_session(self, url: str, session: str = "default") -> bool:
        host = urlparse(url).netloc.lower()
        return self._get_browser().has_session(host, session)

    def browser_snapshot(self) -> dict[str, int] | None:
        """浏览器当前占用；还没启动过浏览器时返回 None（调用方按 0 处理）。"""
        return self._browser.snapshot() if self._browser is not None else None


def _host_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower().split(":")[0]


def _looks_like_asset(url: str) -> bool:
    return urlparse(url).path.lower().endswith(_ASSET_SUFFIXES)


def _content_kind(content_type: str, url: str) -> str:
    """按 Content-Type（必要时看 URL 后缀）区分 HTML / PDF / 纯文本。"""
    ctype = (content_type or "").lower()
    if "application/pdf" in ctype:
        return "pdf"
    if ctype in ("", "application/octet-stream") and url.lower().split("?")[0].endswith(".pdf"):
        return "pdf"
    if ctype.startswith("text/plain") or "text/markdown" in ctype or "text/x-markdown" in ctype:
        return "text"
    return "html"


def _is_retryable(outcome: FetchOutcome) -> bool:
    """可重试：传输层异常或服务端 5xx；429/403 是反爬信号，重试会加重封禁。"""
    if outcome.error:
        return True
    return 500 <= outcome.status < 600


# 整条链路走完仍命中的拦截：出口 IP 很可能已被目标站标记，交给代理池累计失败
_EGRESS_BAD_BLOCKS = ("rate_limit", "cloudflare", "captcha", "waf")


def _egress_failed(outcome: FetchOutcome) -> bool:
    """一次抓取是否说明该出口不可用；预算耗尽没发请求的情况不算在出口头上。"""
    if "deadline:exceeded" in outcome.decisions:
        return False
    return bool(outcome.error) or outcome.blocked in _EGRESS_BAD_BLOCKS


def _budget_too_short(deadline: float | None, wait: float) -> bool:
    """等完这段之后还放得下一次尝试吗；没有预算时永远放得下。"""
    left = budget.remaining(deadline)
    return left is not None and left < wait + _MIN_ATTEMPT_S


def _deadline_outcome(url: str, trail: list[str]) -> FetchOutcome:
    """预算在发起请求前就耗光：不发请求，直接给出可判定的失败。"""
    return FetchOutcome(
        url=url,
        final_url=url,
        status=0,
        tier="l1",
        blocked="none",
        block_reason="",
        content_type="",
        html="",
        elapsed_ms=0,
        error="超出总预算 deadline_ms，未发起请求",
        decisions=[*trail, "deadline:exceeded"],
    )
