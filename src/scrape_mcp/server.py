"""MCP Server 入口：分级抓取 + compact 输出 + 结构化抽取 + 轻量爬取。"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from mcp.server.mcpserver import MCPServer
from starlette.responses import JSONResponse

from . import __version__
from .config import get_settings
from .core import errors
from .core.fetcher import Fetcher, FetchOutcome
from .core.hostlimit import HostLimiter
from .core.logging import configure, log_event
from .core.ratelimit import RateLimiter
from .core.robots import RobotsChecker
from .extract.compact import LINK_POLICIES, compact_html
from .extract.links import extract_links
from .extract.render import FORMATS, to_blocks, to_plain_text
from .tokenizer import count_tokens

_fetcher: Fetcher | None = None
_ratelimiter: RateLimiter | None = None
_robots: RobotsChecker | None = None
_hostlimiter: HostLimiter | None = None


def _get_fetcher() -> Fetcher:
    global _fetcher
    if _fetcher is None:
        _fetcher = Fetcher(get_settings())
    return _fetcher


def _get_ratelimiter() -> RateLimiter:
    global _ratelimiter
    if _ratelimiter is None:
        _ratelimiter = RateLimiter(get_settings().max_qps)
    return _ratelimiter


def _get_robots() -> RobotsChecker:
    global _robots
    if _robots is None:
        _robots = RobotsChecker(get_settings())
    return _robots


def _get_hostlimiter() -> HostLimiter:
    global _hostlimiter
    if _hostlimiter is None:
        _hostlimiter = HostLimiter(get_settings().host_max_concurrency)
    return _hostlimiter


def _host_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower().split(":")[0]


def _denied_payload(url: str, code: str, message: str) -> dict:
    """合规拒绝的统一载荷：不发请求，但同样给出稳定错误码与是否值得重试。"""
    log_event("denied", url=url, code=code)
    return {
        "ok": False,
        "url": url,
        "error_code": code,
        "error": message,
        "retryable": errors.is_retryable(code),
    }


def _diagnostics(outcome: FetchOutcome, *, detail: bool = False) -> dict:
    """分层决策轨迹。失败时给精简版，debug=true 时给完整版（不含页面内容）。"""
    diag: dict = {"decisions": list(outcome.decisions), "attempts": outcome.attempts}
    if detail:
        diag["html_bytes"] = len(outcome.html)
        diag["l2"] = outcome.l2
        if outcome.notes:
            diag["notes"] = list(outcome.notes)
        if outcome.retry_after is not None:
            diag["retry_after_s"] = outcome.retry_after
    return diag


async def _compliance_check(url: str) -> dict | None:
    """阶段 0 合规：白/黑名单 → 全局限流 → robots.txt。

    通过返回 None；被拦返回结构化错误 payload（拒绝抓取，不发起任何请求）。
    """
    settings = get_settings()
    host = _host_of(url)

    if host in settings.denied_host_set:
        return _denied_payload(url, errors.DENIED, f"主机 {host} 在黑名单内，已拒绝请求")
    if settings.allowed_host_set and host not in settings.allowed_host_set:
        return _denied_payload(url, errors.DENIED, f"主机 {host} 不在白名单内，已拒绝请求")

    await _get_ratelimiter().acquire()

    if settings.respect_robots and not await _get_robots().is_allowed(url):
        return _denied_payload(url, errors.ROBOTS_DISALLOWED, "目标路径被该站 robots.txt 禁止抓取")

    return None


@asynccontextmanager
async def _lifespan(_app):
    try:
        yield {}
    finally:
        global _fetcher
        if _fetcher is not None:
            await _fetcher.aclose()
            _fetcher = None


mcp = MCPServer("scrape-mcp", lifespan=_lifespan)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request) -> JSONResponse:
    """HTTP 传输模式下的存活探针（Docker/反向代理用），不触发任何抓取。"""
    settings = get_settings()
    return JSONResponse(
        {
            "ok": True,
            "name": "scrape-mcp",
            "version": __version__,
            "l2_enabled": settings.l2_enabled,
            "respect_robots": settings.respect_robots,
        }
    )


# 大页面 + 极小正文 → 疑似 JS 登录墙/骨架页，加 note 提示（不判失败，避免误杀超短正文）。
# 与 detector._SHELL_MIN_HTML 对齐到 30KB：60KB 澎湃首页这类"挺大却榨不出正文"也要能提示。
LOW_CONTENT_HTML_BYTES = 30_000
LOW_CONTENT_TOKENS = 250


def _payload(**kwargs) -> str:
    # 失败载荷统一补 retryable，避免调用方为了一个布尔值去解析错误文本
    if kwargs.get("ok") is False and "error_code" in kwargs and "retryable" not in kwargs:
        kwargs["retryable"] = errors.is_retryable(kwargs["error_code"])
    return json.dumps(kwargs, ensure_ascii=False, indent=None)


async def _fetch_html(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: float | None = None,
    wait_for: str | None = None,
    scroll: bool = False,
    click: str | None = None,
    bypass_cache: bool = False,
) -> tuple[FetchOutcome | None, dict | None]:
    """抓取单个 URL，返回 (outcome, 错误 payload)。

    web_fetch 与 web_extract 共用：先做合规检查，再走分级抓取链路；
    拦截/登录墙统一如实上报，避免把挑战页当正文/当数据。成功时 error 为 None。
    """
    denied = await _compliance_check(url)
    if denied is not None:
        return None, denied

    # 同域并发闸门：批量/爬取时避免瞬间对同一站点打多个请求
    async with _get_hostlimiter().slot(_host_of(url)):
        outcome: FetchOutcome = await _get_fetcher().fetch(
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            wait_for=wait_for,
            scroll=scroll,
            click=click,
            bypass_cache=bypass_cache,
        )

    log_event(
        "fetch",
        url=url,
        tier=outcome.tier,
        status=outcome.status,
        ms=outcome.elapsed_ms,
        attempts=outcome.attempts,
        cached=outcome.cached,
        code=errors.classify_outcome(outcome),
    )

    if outcome.error:
        code = errors.classify_outcome(outcome)
        return None, {
            "ok": False,
            "url": url,
            "tier": outcome.tier,
            "elapsed_ms": outcome.elapsed_ms,
            "error_code": code,
            "error": outcome.error,
            "retryable": errors.is_retryable(code),
            "diagnostics": _diagnostics(outcome),
        }

    if outcome.blocked != "none":
        code = errors.LOGIN_REQUIRED if outcome.login_required else errors.BLOCKED
        hint = (
            "命中反爬拦截"
            if outcome.blocked in ("cloudflare", "captcha", "waf", "rate_limit")
            else "页面无有效正文（疑似 JS 渲染）"
        )
        explain = (
            "已升级 L2 浏览器渲染后仍被判为拦截页"
            if outcome.tier == "l2"
            else "未取得 L2 渲染结果（限流类判定不升级，或浏览器渲染失败，详见 block_reason）"
        )
        payload = {
            "ok": False,
            "url": url,
            "final_url": outcome.final_url,
            "status": outcome.status,
            "tier": outcome.tier,
            "blocked": outcome.blocked,
            "block_reason": outcome.block_reason,
            "elapsed_ms": outcome.elapsed_ms,
            "error_code": code,
            "error": f"{hint}；{explain}",
            "retryable": errors.is_retryable(code),
            "diagnostics": _diagnostics(outcome, detail=True),
        }
        if outcome.retry_after is not None:
            payload["retry_after_s"] = outcome.retry_after
        if outcome.login_required:
            state = (
                "已带登录态仍失败，登录态可能过期，请重新 login"
                if outcome.session_loaded
                else "该站还无登录态"
            )
            payload["login_required"] = True
            payload["error"] = f"{payload['error']}；{state}，可调用 login(url=...) 手动登录后重试"
            payload["login_hint"] = f'可调用 login(url="{url}", timeout=180) 打开浏览器手动登录'
        return None, payload

    # 4xx（401/403/429 已在上面按拦截/限流处理）：404/410 这类不是有效内容，
    # 不当作成功返回，否则调用方会把错误页当正文/数据。
    if outcome.status >= 400:
        code = errors.HTTP_5XX if outcome.status >= 500 else errors.HTTP_4XX
        return None, {
            "ok": False,
            "url": url,
            "final_url": outcome.final_url,
            "status": outcome.status,
            "tier": outcome.tier,
            "elapsed_ms": outcome.elapsed_ms,
            "error_code": code,
            "error": f"目标返回 HTTP {outcome.status}，该页不是有效内容",
            "retryable": errors.is_retryable(code),
            "diagnostics": _diagnostics(outcome),
        }

    return outcome, None


async def _fetch_dict(
    url: str,
    max_tokens: int,
    link_policy: str,
    *,
    fmt: str = "markdown",
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: float | None = None,
    wait_for: str | None = None,
    scroll: bool = False,
    click: str | None = None,
    bypass_cache: bool = False,
    debug: bool = False,
) -> dict:
    """单个 URL 的完整抓取闭环，返回可序列化的 payload dict（web_fetch 与 web_batch 共用）。"""
    settings = get_settings()

    if link_policy not in LINK_POLICIES:
        return {
            "ok": False,
            "error_code": errors.INVALID_ARGUMENT,
            "error": f"link_policy 必须是 {LINK_POLICIES} 之一",
            "retryable": False,
        }
    if fmt not in FORMATS:
        return {
            "ok": False,
            "error_code": errors.INVALID_ARGUMENT,
            "error": f"format 必须是 {FORMATS} 之一",
            "retryable": False,
        }

    outcome, error = await _fetch_html(
        url,
        headers=headers,
        cookies=cookies,
        timeout=timeout,
        wait_for=wait_for,
        scroll=scroll,
        click=click,
        bypass_cache=bypass_cache,
    )
    if error is not None:
        return error

    budget = max(200, min(int(max_tokens), settings.hard_max_tokens))
    result = compact_html(
        outcome.html,
        outcome.final_url or url,
        link_policy=link_policy,
        max_tokens=budget,
    )

    payload = {
        "ok": True,
        "url": url,
        "final_url": outcome.final_url,
        "status": outcome.status,
        "tier": outcome.tier,
        "title": result.title,
        "content": result.content,
        "token_estimate": count_tokens(result.content),
        "truncated": result.truncated,
        "blocks": result.blocks,
        "links_kept": result.links_kept,
        "links_dropped": result.links_dropped,
        "html_bytes": len(outcome.html),
        "elapsed_ms": outcome.elapsed_ms,
        "cached": outcome.cached,
        "attempts": outcome.attempts,
    }
    if fmt != "markdown":
        payload["format"] = fmt
        if fmt == "text":
            payload["content"] = to_plain_text(result.content)
            payload["token_estimate"] = count_tokens(payload["content"])
        else:  # json：按行分块，方便调用方逐段入库/切片
            payload["content"] = to_blocks(result.content)
            payload["token_estimate"] = sum(count_tokens(b) for b in payload["content"])
    if outcome.notes:
        payload["notes"] = outcome.notes
    if debug:
        diag = _diagnostics(outcome, detail=True)
        diag.update(
            {
                "root": result.root,
                "selector_hits": result.selector_hits,
                "content_chars": len(result.content),
            }
        )
        payload["diagnostics"] = diag

    # 大页面却榨不出正文，多半是 JS 登录墙/骨架页：正文本身可能是真实内容，故不判失败，只如实提示
    if (
        len(outcome.html) >= LOW_CONTENT_HTML_BYTES
        and payload["token_estimate"] < LOW_CONTENT_TOKENS
    ):
        payload["note"] = (
            "正文极少，疑似 JS 登录墙或骨架页；浏览器已渲染过，公开区域确实只有这些内容"
            if outcome.tier == "l2"
            else "正文极少，疑似 JS 登录墙或骨架页；当前仅 L1 抓取（未执行 JS）"
        )

    return payload


@mcp.tool()
async def web_fetch(
    url: str,
    max_tokens: int = 4000,
    link_policy: str = "internal",
    format: str = "markdown",
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: float | None = None,
    wait_for: str | None = None,
    scroll: bool = False,
    click: str | None = None,
    bypass_cache: bool = False,
    debug: bool = False,
) -> str:
    """抓取网页并返回极省 token 的紧凑正文（自动辨别反爬拦截页并如实上报）。

    Args:
        url: 目标网页完整 URL，需带 http/https 协议头。
        max_tokens: 正文 token 预算上限，超出时保留头尾、省略中段。
        link_policy: 链接处理策略。internal=站内链接保留为相对路径、站外降级为纯文本（默认，最省）；
            all=站内外链接全保留；none=全部降级为纯文本。
        format: 输出格式。markdown=保留标题/列表/链接标记（默认）；
            text=剥掉 markdown 标记的纯文本；json=按行切分的块数组（content 为字符串列表）。
        headers: 自定义请求头，如 {"Referer": "...", "User-Agent": "..."}；传入时不走共享缓存。
        cookies: 自定义 cookie 字典；传入时不走共享缓存。
        timeout: 单次请求超时秒数（默认取配置 request_timeout）。
        wait_for: 仅 L2 生效，等待该 CSS 选择器出现后再取内容（适合 SPA）。
        scroll: 仅 L2 生效，滚动页面触发懒加载/无限滚动。
        click: 仅 L2 生效，渲染后点击一次该 CSS 选择器（如"加载更多"）。
        bypass_cache: 跳过并绕过响应缓存，强制走网络。
        debug: 返回 diagnostics（分层决策、L2 交互命中情况、正文根与候选命中数），
            只含计数与决策、不含页面内容；失败时默认也会带精简版决策轨迹。
    """
    if not url.startswith(("http://", "https://")):
        return _payload(
            ok=False,
            error_code=errors.INVALID_URL,
            error="url 必须以 http:// 或 https:// 开头",
        )
    return _payload(
        **await _fetch_dict(
            url,
            max_tokens,
            link_policy,
            fmt=format,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            wait_for=wait_for,
            scroll=scroll,
            click=click,
            bypass_cache=bypass_cache,
            debug=debug,
        )
    )


@mcp.tool()
async def web_extract(
    url: str,
    schema: dict,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: float | None = None,
    wait_for: str | None = None,
    scroll: bool = False,
    click: str | None = None,
    bypass_cache: bool = False,
    debug: bool = False,
) -> str:
    """抓取网页并按字段 schema 抽取结构化 JSON（字段级数据，可入库）。

    适合需要"数据而非整页正文"的场景：给 URL 和字段规则，直接返回字段值，
    而不是一段 compact 文本。抓取链路（分级反爬、登录态、合规）与 web_fetch 一致。

    字段规则示例：
      {"fields": {
        "title": {"selector": "h1"},
        "price": {"selector": ".price", "regex": "(\\d+(?:\\.\\d+)?)", "group": 1},
        "main_link": {"selector": "a", "type": "attr", "attr": "href", "abs_url": true},
        "link_count": {"selector": "a", "type": "count"},
        "tags": {"selector": ".tag", "type": "list", "list_key": "text"},
        "items": {"selector": ".item", "type": "list", "fields": {
            "name": {"selector": ".name"},
            "url": {"selector": "a", "type": "attr", "attr": "href", "abs_url": true}
        }},
        "author": {"selector": ".author", "type": "object", "fields": {
            "name": {"selector": ".name"},
            "bio": {"selector": ".bio"}
        }}
      }}
    字段类型：text（默认，节点归一化文本）/ html / attr（需配 attr）/
        count（匹配节点数）/ list（配 fields 返回对象数组，否则按 list_key 取值）/
        object（在第一个匹配节点内部继续按 fields 抽取）。
    后处理：regex + group（正则提取，未命中回落 default）、abs_url（相对链接转绝对）、
        strip（去首尾空白，默认 true）。

    Args:
        url: 目标网页完整 URL。
        schema: 字段抽取规则字典，见上方示例。
        headers: 自定义请求头；传入时不走共享缓存。
        cookies: 自定义 cookie 字典；传入时不走共享缓存。
        timeout: 单次请求超时秒数。
        wait_for: 仅 L2 生效，等待该 CSS 选择器出现后再取内容。
        scroll: 仅 L2 生效，滚动页面触发懒加载。
        click: 仅 L2 生效，渲染后点击一次该 CSS 选择器。
        bypass_cache: 跳过并绕过响应缓存，强制走网络。
        debug: 返回 diagnostics（分层决策与 L2 交互命中情况），不含页面内容。
    """
    if not url.startswith(("http://", "https://")):
        return _payload(
            ok=False,
            error_code=errors.INVALID_URL,
            error="url 必须以 http:// 或 https:// 开头",
        )

    outcome, error = await _fetch_html(
        url,
        headers=headers,
        cookies=cookies,
        timeout=timeout,
        wait_for=wait_for,
        scroll=scroll,
        click=click,
        bypass_cache=bypass_cache,
    )
    if error is not None:
        return _payload(**error)

    try:
        from .extract.schema import SchemaValidationError, extract_fields, parse_schema

        fields = parse_schema(schema)
    except SchemaValidationError as exc:
        return _payload(
            ok=False,
            url=url,
            error_code=errors.INVALID_ARGUMENT,
            error=f"schema 非法: {exc}",
        )

    result = extract_fields(outcome.html, fields, base_url=outcome.final_url or url)
    payload = {
        "ok": True,
        "url": url,
        "final_url": outcome.final_url,
        "status": outcome.status,
        "tier": outcome.tier,
        "cached": outcome.cached,
        "elapsed_ms": outcome.elapsed_ms,
        "attempts": outcome.attempts,
        "data": result.fields,
        "missing": [k for k, v in result.fields.items() if v is None],
    }
    if outcome.notes:
        payload["notes"] = outcome.notes
    if result.errors:
        payload["schema_notes"] = result.errors
    if debug:
        payload["diagnostics"] = _diagnostics(outcome, detail=True)
    return _payload(**payload)


@mcp.tool()
async def web_batch(
    urls: list[str],
    max_tokens: int = 3000,
    link_policy: str = "internal",
    format: str = "markdown",
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: float | None = None,
    bypass_cache: bool = False,
    debug: bool = False,
) -> str:
    """并发抓取一批 URL，各自走完整分级链路，返回逐个结果。

    Args:
        urls: 目标 URL 列表（最多 batch_max_urls 个，默认 20）。
        max_tokens: 每个 URL 的正文 token 预算上限。
        link_policy: 同 web_fetch。
        format: 同 web_fetch（markdown / text / json）。
        headers: 所有 URL 共用的自定义请求头；传入时不走共享缓存。
        cookies: 所有 URL 共用的自定义 cookie；传入时不走共享缓存。
        timeout: 单次请求超时秒数。
        bypass_cache: 跳过并绕过响应缓存。
        debug: 每条结果附带 diagnostics（决策轨迹与计数，不含页面内容）。
    """
    settings = get_settings()
    if link_policy not in LINK_POLICIES:
        return _payload(
            ok=False,
            error_code=errors.INVALID_ARGUMENT,
            error=f"link_policy 必须是 {LINK_POLICIES} 之一",
        )
    urls = [u for u in (urls or []) if isinstance(u, str) and u.startswith(("http://", "https://"))]
    if not urls:
        return _payload(
            ok=False,
            error_code=errors.INVALID_URL,
            error="urls 需为至少一个 http/https 的 URL 列表",
        )
    if len(urls) > settings.batch_max_urls:
        return _payload(
            ok=False,
            error_code=errors.INVALID_ARGUMENT,
            error=f"urls 数量超过上限 {settings.batch_max_urls}，请分批",
        )

    sem = asyncio.Semaphore(settings.batch_max_concurrency)

    async def one(u: str) -> dict:
        async with sem:
            try:
                return await _fetch_dict(
                    u,
                    max_tokens,
                    link_policy,
                    fmt=format,
                    headers=headers,
                    cookies=cookies,
                    timeout=timeout,
                    bypass_cache=bypass_cache,
                    debug=debug,
                )
            except Exception as exc:  # 单条意外异常不应拖垮整批
                code = errors.classify_exception(exc)
                return {
                    "ok": False,
                    "url": u,
                    "error_code": code,
                    "error": f"{type(exc).__name__}: {exc}",
                    "retryable": errors.is_retryable(code),
                }

    results = await asyncio.gather(*(one(u) for u in urls))
    ok_count = sum(1 for r in results if r.get("ok"))
    return _payload(ok=True, total=len(results), ok_count=ok_count, results=results)


async def _crawl_page(
    url: str,
    *,
    max_tokens: int,
    link_policy: str,
    same_host: bool,
    link_limit: int,
    headers: dict[str, str] | None,
    cookies: dict[str, str] | None,
    timeout: float | None,
    bypass_cache: bool,
) -> tuple[dict, list[str]]:
    """抓一页并顺手抽链接；失败页返回错误 payload 和空链接列表。"""
    outcome, error = await _fetch_html(
        url,
        headers=headers,
        cookies=cookies,
        timeout=timeout,
        bypass_cache=bypass_cache,
    )
    if error is not None:
        return error, []

    base = outcome.final_url or url
    budget = max(200, min(int(max_tokens), get_settings().hard_max_tokens))
    result = compact_html(outcome.html, base, link_policy=link_policy, max_tokens=budget)
    payload = {
        "ok": True,
        "url": url,
        "final_url": outcome.final_url,
        "status": outcome.status,
        "tier": outcome.tier,
        "title": result.title,
        "content": result.content,
        "token_estimate": count_tokens(result.content),
        "truncated": result.truncated,
        "cached": outcome.cached,
    }
    if outcome.notes:
        payload["notes"] = outcome.notes
    links = extract_links(outcome.html, base, same_host=same_host, limit=link_limit)
    return payload, links


@mcp.tool()
async def web_crawl(
    url: str,
    depth: int = 1,
    limit: int = 10,
    same_host: bool = True,
    max_tokens: int = 2000,
    link_policy: str = "internal",
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: float | None = None,
    bypass_cache: bool = False,
) -> str:
    """从起始页按层发现站内链接并批量抓取，适合"把一个栏目的前 N 篇读进来"。

    每页都走完整的分级抓取与合规链路（robots/黑白名单/限流/同域并发），
    单页失败只影响该页，不中断整次爬取；静态资源链接（图片/压缩包等）自动跳过。

    Args:
        url: 起始页 URL。
        depth: 跳转层数。0=只抓起始页；1=再抓起始页上的链接（默认）；最大 3。
        limit: 最多抓取的页面总数（含起始页），受配置 crawl_max_pages 上限约束。
        same_host: 只跟进同主机链接（默认 True）。
        max_tokens: 每页正文 token 预算上限。
        link_policy: 同 web_fetch。
        headers: 所有页面共用的自定义请求头；传入时不走共享缓存。
        cookies: 所有页面共用的自定义 cookie；传入时不走共享缓存。
        timeout: 单次请求超时秒数。
        bypass_cache: 跳过并绕过响应缓存。
    """
    if not url.startswith(("http://", "https://")):
        return _payload(
            ok=False,
            error_code=errors.INVALID_URL,
            error="url 必须以 http:// 或 https:// 开头",
        )
    settings = get_settings()
    if depth < 0 or depth > 3:
        return _payload(
            ok=False,
            error_code=errors.INVALID_ARGUMENT,
            error="depth 需在 0~3 之间",
        )
    cap = min(max(1, int(limit)), settings.crawl_max_pages)
    sem = asyncio.Semaphore(settings.batch_max_concurrency)

    async def one(u: str) -> tuple[dict, list[str]]:
        async with sem:
            try:
                return await _crawl_page(
                    u,
                    max_tokens=max_tokens,
                    link_policy=link_policy,
                    same_host=same_host,
                    link_limit=max(cap * 5, 50),
                    headers=headers,
                    cookies=cookies,
                    timeout=timeout,
                    bypass_cache=bypass_cache,
                )
            except Exception as exc:  # 单页异常不中断整次爬取
                return {
                    "ok": False,
                    "url": u,
                    "error_code": errors.classify_exception(exc),
                    "error": f"{type(exc).__name__}: {exc}",
                }, []

    visited: set[str] = {url}
    frontier: list[str] = [url]
    results: list[dict] = []
    level = 0

    while frontier and len(results) < cap:
        frontier = frontier[: cap - len(results)]
        pairs = await asyncio.gather(*(one(u) for u in frontier))
        next_frontier: list[str] = []
        for payload, links in pairs:
            results.append(payload)
            if level >= depth:
                continue
            for link in links:
                if link in visited or len(visited) >= cap:
                    continue
                visited.add(link)
                next_frontier.append(link)
        frontier = next_frontier
        level += 1

    ok_count = sum(1 for r in results if r.get("ok"))
    return _payload(
        ok=True,
        start=url,
        depth=depth,
        limit=cap,
        pages_fetched=len(results),
        ok_count=ok_count,
        links_discovered=len(visited) - 1,
        results=results,
    )


@mcp.tool()
async def login(url: str, timeout: float = 180.0) -> str:
    """打开带界面的浏览器手动登录，并把该站登录态持久化到磁盘。

    用于 web_fetch 报告 login_required=true 的站点：浏览器会以非无头方式打开目标页，
    请在弹出的窗口里完成登录；检测到登录成功或超时后，登录态（cookie 等）会被保存，
    之后该站的 web_fetch 会自动带上登录态。

    Args:
        url: 目标站点任一页面 URL（按域名区分登录态）。
        timeout: 等待手动登录完成的秒数，超时也会保存当前状态便于下次继续。
    """
    if not url.startswith(("http://", "https://")):
        return _payload(
            ok=False,
            error_code=errors.INVALID_URL,
            error="url 必须以 http:// 或 https:// 开头",
        )

    result = await _get_fetcher().login(url, timeout)
    result["host"] = urlparse(url).netloc.lower()
    if result.get("ok"):
        result["message"] = (
            "登录态已保存，后续 web_fetch 会自动带上"
            if result["logged_in"]
            else "未检测到登录完成，但已保存当前状态；等你登录完成后可重新调用 login 覆盖"
        )
    return _payload(**result)


def main(argv: list[str] | None = None) -> None:
    """CLI 入口。

    零配置默认即用：不传参按 stdio 运行，可作为 MCP 服务被 Claude/Cursor 等接管。
    仅当显式指定 --transport=streamable-http 时才走 HTTP。任意目录下
    ``pip install mcp-scrape`` 后运行 ``scrape-mcp`` 即可，无需 clone/venv/PYTHONPATH。
    """
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["--transport", "stdio"]
    elif argv in (["-h"], ["--help"]):
        msg = (
            "scrape-mcp — 反爬感知抓取 + 极省 Token 内容精简的 MCP Server\n\n"
            "用法: scrape-mcp [--transport stdio|streamable-http] [--port N] [--version]\n\n"
            "零配置默认作为 stdio MCP 服务运行，供 Claude/Cursor 等客户端接管。\n"
            "如需 HTTP 直连（Postman/curl），加 --transport streamable-http [--port 8000]。\n"
            "配置详见 .env / SCRAPE_MCP_* 环境变量。"
        )
        print(msg)
        return
    if "--version" in argv:
        import importlib.metadata

        try:
            version = importlib.metadata.version("mcp-scrape")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown (source tree)"
        print(version)
        return

    transport = ("--transport" in argv and argv[argv.index("--transport") + 1]) or "stdio"
    settings = get_settings()
    # 日志只写 stderr：stdio 模式下 stdout 是 MCP 协议通道
    configure(settings.log_level)
    # CLI 优先级高于 env/.env，便于一键切 HTTP
    if transport == "streamable-http":
        if "--port" in argv:
            settings.http_port = int(argv[argv.index("--port") + 1])
        base = settings.http_port if settings.http_port else 8000
        mcp.run(
            transport="streamable-http",
            host=settings.http_host,
            port=base,
            json_response=settings.http_json,
            stateless_http=settings.http_stateless,
        )
        return
    # 默认 stdio
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
