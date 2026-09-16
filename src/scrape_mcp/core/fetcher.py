"""抓取调度：L1（curl_cffi）打头阵，命中拦截才升级 L2（Playwright 渲染）。

上层只依赖 FetchOutcome，升级链路对 MCP 工具层透明。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    ) -> FetchOutcome:
        outcome = await self._fetch_l1(url, headers=headers, cookies=cookies)
        if not self._should_escalate(outcome):
            return outcome
        return await self._fetch_l2(url, outcome)

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
    ) -> FetchOutcome:
        resp = await self._http.get(url, headers=headers, cookies=cookies)
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

    async def _fetch_l2(self, url: str, l1: FetchOutcome) -> FetchOutcome:
        host = urlparse(l1.final_url or url).netloc.lower()
        browser = self._get_browser()
        result = await browser.render(url, host)
        elapsed = l1.elapsed_ms + result.elapsed_ms

        if not result.ok:
            # 浏览器没跑起来/渲染失败：正文仍是 L1 的，只把原因如实挂上去，tier 保持 l1
            return replace(
                l1,
                block_reason=f"{l1.block_reason}；L2 渲染失败: {result.error}",
                elapsed_ms=elapsed,
                session_loaded=browser.has_session(host),
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
        )

    async def login(self, url: str, timeout: float | None = None) -> dict:
        """手动登录并持久化该站登录态。返回带登录结果的结构，供 MCP 工具直接透传。"""
        timeout = timeout or self._settings.login_timeout
        return await self._get_browser().login(url, timeout)

    def has_session(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return self._get_browser().has_session(host)
