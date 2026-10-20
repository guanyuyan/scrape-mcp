"""抓取调度：L1（curl_cffi）打头阵，命中拦截才升级 L2（Playwright 渲染）。

上层只依赖 FetchOutcome，升级链路对 MCP 工具层透明。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlparse

from ..config import Settings
from .browser import BrowserRenderer
from .detector import detect_block, looks_like_shell
from .http_client import HttpClient

# 这些判定不值得升级浏览器：同一 IP 下换浏览器通常仍被限流，
# 只是白付一次浏览器启动成本。
_NO_ESCALATE = ("rate_limit",)

# 升级后仍未放行、很可能需要登录态的判定类型
_LOGIN_CANDIDATES = ("captcha", "waf", "empty_spa", "empty", "cloudflare")


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

    def _get_browser(self) -> BrowserRenderer:
        if self._browser is None:
            self._browser = BrowserRenderer(self._settings)
        return self._browser

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.aclose()
            self._browser = None

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
    ) -> FetchOutcome:
        outcome = await self._fetch_l1_with_retry(
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            bypass_cache=bypass_cache,
        )
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
        outcome.decisions.append(f"escalate_l2:{reason}")
        return await self._fetch_l2(
            url, outcome, wait_for=wait_for, scroll=scroll, click=click, session=session
        )

    async def _fetch_l1_with_retry(
        self,
        url: str,
        *,
        headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
        timeout: float | None,
        bypass_cache: bool,
    ) -> FetchOutcome:
        """网络异常/5xx 指数退避重试；429/503 带 Retry-After 时按配置做一次有界等待。"""
        attempts = max(1, int(self._settings.retry_times) + 1)
        retry_after_used = False
        # 每次重试都是一次新的 L1 结果，决策轨迹要跨尝试累积，否则调用方只看到最后一次
        trail: list[str] = []
        outcome: FetchOutcome | None = None
        for i in range(1, attempts + 1):
            outcome = await self._fetch_l1(
                url,
                headers=headers,
                cookies=cookies,
                timeout=timeout,
                bypass_cache=bypass_cache,
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
                outcome.decisions.append(f"retry:retry_after_{wait:g}s")
                trail = list(outcome.decisions)
                await asyncio.sleep(wait)
                continue

            if not _is_retryable(outcome):
                return outcome
            outcome.decisions.append("retry:transport" if outcome.error else "retry:5xx")
            trail = list(outcome.decisions)
            await asyncio.sleep(self._settings.retry_backoff * (2 ** (i - 1)))
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
    ) -> FetchOutcome:
        resp = await self._http.get(
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            bypass_cache=bypass_cache,
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

    async def _fetch_l2(
        self,
        url: str,
        l1: FetchOutcome,
        *,
        wait_for: str | None = None,
        scroll: bool = False,
        click: str | None = None,
        session: str = "default",
    ) -> FetchOutcome:
        host = urlparse(l1.final_url or url).netloc.lower()
        browser = self._get_browser()
        result = await browser.render(
            url, host, wait_for=wait_for, scroll=scroll, click=click, session=session
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
        login_required = blocked in _LOGIN_CANDIDATES
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
