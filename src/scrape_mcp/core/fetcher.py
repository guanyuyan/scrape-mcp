"""抓取调度：L1（curl_cffi）打头阵，命中拦截才升级 L2（Playwright 渲染）。

上层只依赖 FetchOutcome，升级链路对 MCP 工具层透明。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
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
    ) -> FetchOutcome:
        outcome = await self._fetch_l1_with_retry(
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            bypass_cache=bypass_cache,
        )
        if not self._should_escalate(outcome):
            return outcome
        return await self._fetch_l2(url, outcome, wait_for=wait_for, scroll=scroll, click=click)

    async def _fetch_l1_with_retry(
        self,
        url: str,
        *,
        headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
        timeout: float | None,
        bypass_cache: bool,
    ) -> FetchOutcome:
        """网络异常/5xx 指数退避重试；429/403 属反爬信号，同层重试无效故不重试。"""
        attempts = max(1, int(self._settings.retry_times) + 1)
        outcome: FetchOutcome | None = None
        for i in range(1, attempts + 1):
            outcome = await self._fetch_l1(
                url,
                headers=headers,
                cookies=cookies,
                timeout=timeout,
                bypass_cache=bypass_cache,
            )
            outcome.attempts = i
            if i >= attempts or not _is_retryable(outcome):
                return outcome
            await asyncio.sleep(self._settings.retry_backoff * (2 ** (i - 1)))
        return outcome

    def _should_escalate(self, outcome: FetchOutcome) -> bool:
        if not self._settings.l2_enabled or outcome.error:
            return False
        if outcome.blocked == "none":
            # 没被判成拦截页，但大 HTML 榨不出可见文本时仍是骨架页/登录墙：
            # 这类页面（如 LinkedIn）只有浏览器把 JS 跑起来才有正文
            return looks_like_shell(outcome.html)
        return outcome.blocked not in _NO_ESCALATE

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
        )

    async def _fetch_l2(
        self,
        url: str,
        l1: FetchOutcome,
        *,
        wait_for: str | None = None,
        scroll: bool = False,
        click: str | None = None,
    ) -> FetchOutcome:
        host = urlparse(l1.final_url or url).netloc.lower()
        browser = self._get_browser()
        result = await browser.render(url, host, wait_for=wait_for, scroll=scroll, click=click)
        elapsed = l1.elapsed_ms + result.elapsed_ms

        if not result.ok:
            # 浏览器没跑起来/渲染失败：正文仍是 L1 的，只把原因如实挂上去，tier 保持 l1
            return replace(
                l1,
                block_reason=f"{l1.block_reason}；L2 渲染失败: {result.error}",
                elapsed_ms=elapsed,
                session_loaded=browser.has_session(host),
                notes=[*l1.notes, *result.notes],
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
            session_loaded=browser.has_session(host),
            login_required=login_required,
            notes=result.notes,
        )

    async def login(self, url: str, timeout: float | None = None) -> dict:
        """手动登录并持久化该站登录态。返回带登录结果的结构，供 MCP 工具直接透传。"""
        timeout = timeout or self._settings.login_timeout
        return await self._get_browser().login(url, timeout)

    def has_session(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return self._get_browser().has_session(host)


def _is_retryable(outcome: FetchOutcome) -> bool:
    """可重试：传输层异常或服务端 5xx；429/403 是反爬信号，重试会加重封禁。"""
    if outcome.error:
        return True
    return 500 <= outcome.status < 600
