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
from . import captcha
from . import deadline as budget
from .browser import BrowserRenderer, ScreenshotOutcome
from .captcha_cache import CaptchaCookieCache
from .challenge import analyze as analyze_challenge
from .detector import detect_block, looks_like_login_wall, looks_like_shell
from .http_client import HttpClient, HttpResponse
from .proxy import (
    ProxyPool,
    build_provider_proxy,
    mask_proxy,
    parse_pool,
    parse_session_proxies,
)

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
    # 挑战页诊断：provider / kind / sitekey / action / status / retry_after / evidence。
    # 只在被判为拦截页时填充；工具层会去掉 sitekey 的后半段
    challenge: dict[str, Any] | None = None
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
            parse_pool(settings.proxy_pool),
            fail_threshold=settings.proxy_pool_fail_threshold,
            cooldown_s=settings.proxy_pool_cooldown,
            sticky_ttl_s=settings.proxy_pool_sticky_ttl,
        )
        # 会话绑定代理：name -> proxy；配置非法项会被忽略，该会话自然回落到池/全局
        self._session_proxies = parse_session_proxies(settings.proxy_sessions)
        # 求解结果缓存：默认关闭（cookie 属敏感数据，多租户共用进程时不应跨租户复用）
        self._captcha_cache = (
            CaptchaCookieCache(settings.data_dir, ttl=settings.captcha_cookie_ttl)
            if settings.captcha_cookie_cache
            else None
        )
        # 住宅出口模板：启动时用假值预展开一次，配置写错直接失败，而不是静默不生效
        self._residential_template = (settings.proxy_provider_template or "").strip()
        if self._residential_template:
            probe = build_provider_proxy(
                self._residential_template,
                user=settings.proxy_provider_user or "user",
                password=settings.proxy_provider_pass or "pass",
                host="example.com",
                region="us",
                sticky=True,
            )
            if probe is None:
                raise ValueError(
                    "SCRAPE_MCP_PROXY_PROVIDER_TEMPLATE 展开后不是合法代理地址，"
                    "请检查模板占位符与凭据"
                )

    def _get_browser(self) -> BrowserRenderer:
        if self._browser is None:
            self._browser = BrowserRenderer(self._settings)
        return self._browser

    def proxy_snapshot(self) -> dict[str, int] | None:
        """代理池状态（出口数/健康/冷却）；未配置代理池时返回 None。"""
        return self._pool.snapshot() if self._pool else None

    def proxy_regions(self) -> list[str]:
        """代理池中已标注的地区（排序），用于地区不可用时的提示。"""
        return self._pool.regions()

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.aclose()
            self._browser = None
        await self._http.aclose()

    async def post_json(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        proxy: str | None = None,
    ) -> HttpResponse:
        """发送 JSON POST（Tavily 等 API 用），失败不触发浏览器升级。"""
        return await self._http.post_json(
            url, payload=payload, headers=headers, timeout=timeout, proxy=proxy
        )

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
        actions: list[dict[str, Any]] | None = None,
        bypass_cache: bool = False,
        session: str = "default",
        deadline: float | None = None,
        proxy: str | None = None,
        region: str | None = None,
    ) -> FetchOutcome:
        """为本次抓取固定一个出口，再走分级链路。

        出口优先级：单请求 proxy > 会话绑定代理 > 代理池（region 过滤）> 全局
        SCRAPE_MCP_PROXY。池化出口按站点粘性分配，L1、重试与 L2 升级都走同一个 IP；
        结束后把结果回报给池用于冷却判定。只有池化出口可共享缓存：单请求与会话
        绑定的出口可能因身份/地区而异，结果不写入共享缓存。
        """
        proxy, source = self._resolve_egress(url, session=session, proxy=proxy, region=region)
        allow_cache = source in ("pool", "global")
        outcome = await self._fetch(
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            wait_for=wait_for,
            scroll=scroll,
            click=click,
            actions=actions,
            # 显式指定的出口可能让同一 URL 拿到不同内容，这类结果不进共享缓存
            bypass_cache=bypass_cache,
            allow_cache=allow_cache,
            session=session,
            deadline=deadline,
            proxy=proxy,
        )
        if source == "session":
            outcome.notes.append(f"出口：会话绑定代理（session={session}）")
        if region is not None:
            if source == "pool":
                outcome.notes.append(f"出口：代理池 region={region}")
            else:
                outcome.notes.append("region 未生效：本次使用显式代理、会话绑定代理或全局代理")
        # 有了求解 cookie 的缓存，同一站点第二次被拦时先重试而不是再次付费求解
        outcome = await self._try_captcha_cache(
            outcome,
            session=session,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            deadline=deadline,
            proxy=proxy,
        )
        outcome = await self._try_captcha_hook(
            outcome,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            deadline=deadline,
            proxy=proxy,
            session=session,
        )
        # 完整链路仍被拦：若配置了住宅出口，只升级一次再试，避免成本失控
        degraded = _egress_failed(outcome)
        escalated = await self._try_escalate_residential(
            outcome,
            url=url,
            session=session,
            region=region,
            source=source,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            wait_for=wait_for,
            scroll=scroll,
            click=click,
            actions=actions,
            deadline=deadline,
        )
        if proxy is not None and source == "pool":
            self._pool.report(proxy, ok=not degraded)
        return escalated if escalated is not None else outcome

    def _resolve_egress(
        self,
        url: str,
        *,
        session: str,
        proxy: str | None,
        region: str | None,
    ) -> tuple[str | None, str]:
        """解析本次出口，返回 (代理, 来源)。来源取值 request/session/pool/global。"""
        if proxy is not None:
            return proxy, "request"
        bound = self._session_proxies.get(session)
        if bound is not None:
            return bound, "session"
        picked = self._pool.pick(_host_of(url), region=region)
        if picked is not None:
            return picked, "pool"
        return None, "global"

    def region_available(self, region: str, *, session: str = "default") -> bool:
        """指定地区是否有可用出口；会话已绑定代理时 region 不参与分配。"""
        if session in self._session_proxies:
            return True
        return self._pool.has_region(region)

    async def _try_captcha_cache(
        self,
        outcome: FetchOutcome,
        *,
        session: str,
        headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
        timeout: float | None,
        deadline: float | None,
        proxy: str | None,
    ) -> FetchOutcome:
        """命中求解 cookie 缓存时先重试一次，成功则跳过再次求解。"""
        if self._captcha_cache is None or not _captcha_block(outcome):
            return outcome
        host = _host_of(outcome.final_url or outcome.url)
        cached = self._captcha_cache.get(session, host)
        if cached is None:
            return outcome
        outcome.decisions.append("captcha_cache:hit")
        outcome.notes.append("命中求解 cookie 缓存，已直接重试")
        retry = await self._retry_with_cookies(
            outcome.url,
            session=session,
            headers=headers,
            cookies={**(cookies or {}), **cached.cookies},
            user_agent=cached.user_agent,
            timeout=timeout,
            deadline=deadline,
            proxy=proxy,
        )
        if _recovered(retry):
            retry.decisions = [*outcome.decisions, "captcha_cache:solved", *retry.decisions]
            retry.notes = [*outcome.notes, *retry.notes]
            return retry
        outcome.decisions.append("captcha_cache:stale")
        return outcome

    async def _try_captcha_hook(
        self,
        outcome: FetchOutcome,
        *,
        headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
        timeout: float | None,
        deadline: float | None,
        proxy: str | None,
        session: str = "default",
    ) -> FetchOutcome:
        """检测到人机验证且配置了服务端点时，交给外部服务并按其 cookie 重试一次。

        未配置端点时是纯 no-op；钩子失败/不给 cookie 时只追加 notes，保留原有结果。
        请求体会带上挑战诊断（provider/kind/sitekey/action），但不含页面正文。
        """
        if not _captcha_block(outcome) or not captcha.enabled(self._settings):
            return outcome
        result = await captcha.request_solve(
            self._http,
            self._settings,
            url=outcome.final_url or outcome.url,
            status=outcome.status,
            blocked=outcome.blocked,
            challenge=outcome.challenge,
            deadline=deadline,
        )
        if result is None:  # 竞态下配置被清空，按未启用处理
            return outcome
        outcome.decisions.append("captcha_hook:called")
        if not result.ok:
            outcome.notes.append(result.note)
            return outcome
        if self._captcha_cache is not None:
            self._captcha_cache.put(
                session,
                _host_of(outcome.final_url or outcome.url),
                result.cookies,
                result.user_agent,
            )
        retry = await self._retry_with_cookies(
            outcome.url,
            session=session,
            headers=headers,
            # 原有 cookie 与求解服务返回的 cookie 合并；后者优先
            cookies={**(cookies or {}), **result.cookies},
            user_agent=result.user_agent,
            timeout=timeout,
            deadline=deadline,
            proxy=proxy,
        )
        if result.note:
            retry.notes.append(result.note)
        if _recovered(retry):
            retry.decisions = [*outcome.decisions, "captcha_hook:solved", *retry.decisions]
            retry.notes = [*outcome.notes, *retry.notes]
            return retry
        outcome.decisions.append("captcha_hook:still_blocked")
        return outcome

    async def _retry_with_cookies(
        self,
        url: str,
        *,
        session: str,
        headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
        user_agent: str,
        timeout: float | None,
        deadline: float | None,
        proxy: str | None,
    ) -> FetchOutcome:
        """带求解 cookie 先重试 L1；L1 仍被拦且预算允许时再渲染一次 L2。"""
        merged = dict(headers or {})
        if user_agent and not any(key.lower() == "user-agent" for key in merged):
            merged["User-Agent"] = user_agent
        retry = await self._fetch_l1_with_retry(
            url,
            headers=merged or None,
            cookies=cookies,
            timeout=timeout,
            bypass_cache=True,
            allow_cache=False,
            deadline=deadline,
            proxy=proxy,
        )
        if _recovered(retry) or retry.error is not None:
            return retry
        if not self._settings.l2_enabled or retry.blocked not in _LOGIN_CANDIDATES:
            return retry
        left = budget.remaining(deadline)
        if left is not None and left < _L2_MIN_BUDGET_S:
            retry.decisions.append("deadline:skip_l2")
            return retry
        l2 = await self._fetch_l2(
            url,
            retry,
            session=session,
            deadline=deadline,
            proxy=proxy,
            cookies=cookies,
        )
        return l2 if l2.tier == "l2" else retry

    async def _try_escalate_residential(
        self,
        outcome: FetchOutcome,
        *,
        url: str,
        session: str,
        region: str | None,
        source: str,
        headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
        timeout: float | None,
        wait_for: str | None,
        scroll: bool,
        click: str | None,
        actions: list[dict[str, Any]] | None,
        deadline: float | None,
    ) -> FetchOutcome | None:
        """被限流/挑战且还没走住宅出口时，只升级一次住宅出口重试。

        显式代理与会话绑定代理代表调用方自己的身份选择，不改写；
        预算不足以再跑一轮完整链路时也不升级，避免把一次调用拖成无底洞。
        """
        if source not in ("pool", "global"):
            return None
        if outcome.error or outcome.blocked not in _EGRESS_BAD_BLOCKS:
            return None
        if "egress:escalate_residential" in outcome.decisions:
            return None
        left = budget.remaining(deadline)
        if left is not None and left < _MIN_ATTEMPT_S:
            outcome.decisions.append("egress:skip_residential:deadline")
            return None
        residential = self._residential_egress(_host_of(url), session=session, region=region)
        if residential is None:
            return None
        outcome.decisions.append("egress:escalate_residential")
        outcome.notes.append(f"出口：升级住宅代理（{mask_proxy(residential)}）")
        retry = await self._fetch(
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            wait_for=wait_for,
            scroll=scroll,
            click=click,
            actions=actions,
            bypass_cache=True,
            allow_cache=False,
            session=session,
            deadline=deadline,
            proxy=residential,
        )
        retry = await self._try_captcha_hook(
            retry,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            deadline=deadline,
            proxy=residential,
            session=session,
        )
        retry.decisions = [*outcome.decisions, *retry.decisions]
        retry.notes = [*outcome.notes, *retry.notes]
        return retry

    def _residential_egress(self, host: str, *, session: str, region: str | None) -> str | None:
        """选一个住宅出口：优先服务商模板，其次池中标注 residential 的条目。"""
        template = self._residential_template
        if template:
            return build_provider_proxy(
                template,
                user=self._settings.proxy_provider_user,
                password=self._settings.proxy_provider_pass,
                host=host,
                region=region or "",
                sticky=self._settings.proxy_provider_sticky,
            )
        if not self._pool.has_kind("residential"):
            return None
        return self._pool.pick(host, region=region, kind="residential")

    def _attach_challenge(self, outcome: FetchOutcome) -> FetchOutcome:
        """给拦截页补上挑战诊断；正常页与纯错误页保持 ``challenge=None``。"""
        if outcome.blocked == "none":
            return outcome
        info = analyze_challenge(
            outcome.html,
            outcome.status,
            retry_after=outcome.retry_after,
        )
        if info is None:
            return outcome
        outcome.challenge = info.as_dict()
        outcome.decisions.append(f"challenge:{info.provider}:{info.kind}")
        if info.sitekey:
            outcome.decisions.append(f"challenge:sitekey={info.sitekey[:6]}")
        outcome.notes.append(f"检测到 {info.provider} 挑战（{info.kind}）")
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
        actions: list[dict[str, Any]] | None = None,
        bypass_cache: bool = False,
        allow_cache: bool = True,
        session: str = "default",
        deadline: float | None = None,
        proxy: str | None = None,
    ) -> FetchOutcome:
        host = _host_of(url)
        sticky_failed = False
        # 动作链只能在浏览器里执行：跳过 L1 直接渲染，避免"点完再抓"拿到未交互的页面
        if actions:
            left = budget.remaining(deadline)
            if left is None or left >= _L2_MIN_BUDGET_S:
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
                    decisions=["skip_l1:actions"],
                )
                l2 = await self._fetch_l2(
                    url,
                    placeholder,
                    wait_for=wait_for,
                    scroll=scroll,
                    click=click,
                    actions=actions,
                    session=session,
                    deadline=deadline,
                    proxy=proxy,
                )
                if l2.tier == "l2":
                    if l2.blocked == "none":
                        self._remember_sticky(host)
                    return l2
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
            outcome.notes = [*outcome.notes, "actions 未执行：浏览器渲染不可用或预算不足"]
            outcome.decisions.insert(0, "actions_skipped:l2_unavailable")
            return outcome

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
        # PDF/Office/纯文本没有 DOM，浏览器渲染不会带来更多信息，只会白付一次启动成本；
        # XML（sitemap/接口响应）同理，浏览器渲染不会多出正文
        if outcome.kind != "html":
            return None
        if "xml" in (outcome.content_type or "").lower():
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
        return self._attach_challenge(
            FetchOutcome(
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
                # XML/sitemap 可能是 gzip 压缩包，需要原始字节才能解压；普通 HTML 不额外留一份
                content=resp.content
                if _needs_raw_bytes(resp.content_type, resp.url or url)
                else b"",
            )
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
        actions: list[dict[str, Any]] | None = None,
        session: str = "default",
        deadline: float | None = None,
        proxy: str | None = None,
        cookies: dict[str, str] | None = None,
    ) -> FetchOutcome:
        host = _host_of(l1.final_url or url)
        browser = self._get_browser()
        result = await browser.render(
            url,
            host,
            wait_for=wait_for,
            scroll=scroll,
            click=click,
            actions=actions,
            session=session,
            # 导航超时按剩余预算压缩，避免一页把整次调用的预算吃光
            timeout=budget.cap(deadline, self._settings.l2_navigate_timeout),
            proxy=proxy,
            cookies=cookies,
        )
        elapsed = l1.elapsed_ms + result.elapsed_ms
        l2_diag = {
            "wait_for_hit": result.wait_for_hit,
            "clicked": result.clicked,
            "scroll_rounds": result.scroll_rounds,
            "error": result.error,
        }
        if result.actions:
            l2_diag["actions"] = list(result.actions)

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
        l2_outcome = self._attach_challenge(
            FetchOutcome(
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
        )
        # L2 页面特征不明显（如换成纯 403 拒绝页）时沿用 L1 的诊断，别让调用方丢失线索
        if l2_outcome.challenge is None and l2_outcome.blocked != "none":
            l2_outcome.challenge = l1.challenge
        return l2_outcome

    async def login(
        self,
        url: str,
        timeout: float | None = None,
        session: str = "default",
        *,
        headless: bool = False,
        qr_selector: str | None = None,
        cdp_url: str | None = None,
    ) -> dict:
        """登录并持久化该站登录态。返回带登录结果的结构，供 MCP 工具直接透传。"""
        timeout = timeout or self._settings.login_timeout
        return await self._get_browser().login(
            url,
            timeout,
            session,
            headless=headless,
            qr_selector=qr_selector,
            cdp_url=cdp_url,
        )

    async def screenshot(self, url: str, **kwargs) -> ScreenshotOutcome:
        """渲染并截图；与 fetch 共用浏览器池、登录态与并发页配额。"""
        return await self._get_browser().screenshot(url, **kwargs)

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


def _needs_raw_bytes(content_type: str, url: str) -> bool:
    """是否要把原始响应体一并带出（sitemap 可能是 .xml.gz，文本解码后无法还原）。"""
    ctype = (content_type or "").lower()
    path = url.lower().split("?")[0]
    return "xml" in ctype or path.endswith((".xml", ".xml.gz", ".gz"))


def _content_kind(content_type: str, url: str) -> str:
    """按 Content-Type（必要时看 URL 后缀）区分 HTML / PDF / Office / 纯文本。"""
    ctype = (content_type or "").lower()
    path = url.lower().split("?")[0]
    if "application/pdf" in ctype:
        return "pdf"
    if "wordprocessingml.document" in ctype:
        return "docx"
    if "spreadsheetml.sheet" in ctype:
        return "xlsx"
    if "presentationml.presentation" in ctype:
        return "pptx"
    if ctype in ("", "application/octet-stream"):
        for suffix in (".pdf", ".docx", ".xlsx", ".pptx"):
            if path.endswith(suffix):
                return suffix.lstrip(".")
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


def _recovered(outcome: FetchOutcome) -> bool:
    """带 cookie 重试后是否真的拿到了正文（求解成功判定）。"""
    return outcome.error is None and outcome.blocked == "none" and bool(outcome.html.strip())


def _captcha_block(outcome: FetchOutcome) -> bool:
    """是否值得走"人机验证"链路：detector 判 captcha，或挑战诊断识别出验证码。

    403 的 reCAPTCHA/hCaptcha 页面会被 detector 优先归为 waf（状态码优先），
    但页面本身就是验证码，交给求解钩子才有意义。
    """
    if outcome.blocked == "captcha":
        return True
    return (outcome.challenge or {}).get("kind") == "captcha"


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
