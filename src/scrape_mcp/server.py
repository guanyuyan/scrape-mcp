"""MCP Server 入口：分级抓取 + compact 输出 + 结构化抽取 + 轻量爬取。"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.mcpserver import Context, MCPServer
from starlette.responses import JSONResponse

from . import __version__
from .config import get_settings
from .core import deadline as dl
from .core import errors
from .core.cache import ResultCache
from .core.fetcher import Fetcher, FetchOutcome
from .core.hostlimit import HostLimiter
from .core.logging import configure, log_event
from .core.ratelimit import RateLimiter
from .core.recipes import Recipe, get_recipe_store
from .core.robots import RobotsChecker
from .core.session import SessionStore, normalize_session
from .core.stats import get_stats
from .extract.compact import LINK_POLICIES, compact_html
from .extract.links import extract_links, next_link
from .extract.pdf import PdfError, pdf_pages
from .extract.render import FORMATS, to_blocks, to_plain_text
from .http_auth import BearerAuthMiddleware, ensure_http_access, is_loopback
from .tokenizer import count_tokens, truncate_to_budget

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


def _recipe_for(url: str) -> Recipe | None:
    """按 host 取站点 recipe；没配就是 None（所有行为保持默认）。"""
    return get_recipe_store().for_url(url)


def _result_cache(namespace: str) -> ResultCache:
    """正文/抽取结果缓存：与 HTTP 缓存共用 DATA_DIR 与 CACHE_TTL。"""
    settings = get_settings()
    return ResultCache(Path(settings.data_dir) / "results", settings.cache_ttl, namespace)


def _result_key(**parts: object) -> str:
    """把影响最终结果的全部参数拼成稳定 key（排序 JSON，参数顺序不影响命中）。"""
    return json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)


def _cacheable(
    *,
    headers: dict[str, str] | None,
    cookies: dict[str, str] | None,
    bypass_cache: bool,
    debug: bool,
) -> bool:
    """带自定义头/凭证的结果可能因人而异；debug 要现算诊断，都不走结果缓存。"""
    return (
        get_settings().cache_ttl > 0
        and not bypass_cache
        and not debug
        and not headers
        and not cookies
    )


def _cache_hit(cache: ResultCache, key: str) -> dict | None:
    """命中时把 cached / elapsed_ms 换成本次读缓存的真实耗时。"""
    started = time.perf_counter()
    payload = cache.get(key)
    if payload is None:
        return None
    get_stats().record_result_hit()
    payload["cached"] = True
    payload["elapsed_ms"] = max(1, int((time.perf_counter() - started) * 1000))
    return payload


def _session_store() -> SessionStore:
    return SessionStore(get_settings().data_dir)


def _session_error(session: str) -> dict | None:
    """会话名非法时返回统一错误载荷，避免路径穿越等意外写入。"""
    try:
        normalize_session(session)
    except ValueError as exc:
        return {
            "ok": False,
            "error_code": errors.INVALID_ARGUMENT,
            "error": str(exc),
            "retryable": False,
        }
    return None


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


def _deadline_arg(deadline_ms: int | None) -> tuple[float | None, dict | None]:
    """校验 deadline_ms 并换算成绝对截止时间；非法时给出统一错误载荷。"""
    if deadline_ms is None:
        return None, None
    if isinstance(deadline_ms, bool) or not isinstance(deadline_ms, int) or deadline_ms <= 0:
        return None, {
            "ok": False,
            "error_code": errors.INVALID_ARGUMENT,
            "error": "deadline_ms 必须是正整数（毫秒）；不传表示不限",
            "retryable": False,
        }
    return dl.start(deadline_ms), None


def _deadline_payload(url: str, note: str = "") -> dict:
    """预算在抓取开始前就耗尽：不发请求，沿用 timeout 错误码并标明原因。"""
    message = "超出总预算 deadline_ms，未完成抓取"
    if note:
        message = f"{message}；{note}"
    return {
        "ok": False,
        "url": url,
        "error_code": errors.TIMEOUT,
        "error": message,
        "retryable": True,
        "diagnostics": {"decisions": ["deadline:exceeded"], "attempts": 0},
    }


def _deadline_limited(outcome: FetchOutcome) -> bool:
    """因预算降级得到的结果不进缓存：后续预算更充足的调用不该拿到残缺内容。"""
    return any(d.startswith("deadline:") for d in outcome.decisions)


def _budget_below(deadline: float | None, need: float) -> bool:
    """剩余预算是否已经不够做下一步（没有预算时永远够）。"""
    left = dl.remaining(deadline)
    return left is not None and left < need


async def _notify_progress(ctx: Context | None, done: int, total: int, message: str) -> None:
    """向客户端汇报进度。

    客户端没请求进度时 SDK 本身是 no-op；直接调用工具函数（SDK/测试）时没有
    Context，也不该为了通知多发一次请求，所以这里静默跳过。
    """
    if ctx is None or ctx.request_context is None:
        return
    await ctx.report_progress(done, total, message)


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


# 合规检查至少要留出的预算：限流等待 + robots.txt 抓取 + 真正发请求
_COMPLIANCE_MIN_S = 0.5


async def _compliance_check(url: str, deadline: float | None = None) -> dict | None:
    """阶段 0 合规：白/黑名单 → 全局限流 → robots.txt。

    通过返回 None；被拦返回结构化错误 payload（拒绝抓取，不发起任何请求）。
    有总预算时合规检查同样受约束：剩余时间不够走完就先返回超时，
    不让准备阶段把预算吃光，robots 抓取也按剩余时间压缩超时。
    """
    settings = get_settings()
    host = _host_of(url)

    if host in settings.denied_host_set:
        return _denied_payload(url, errors.DENIED, f"主机 {host} 在黑名单内，已拒绝请求")
    if settings.allowed_host_set and host not in settings.allowed_host_set:
        return _denied_payload(url, errors.DENIED, f"主机 {host} 不在白名单内，已拒绝请求")

    if _budget_below(deadline, _COMPLIANCE_MIN_S):
        return _deadline_payload(url, "预算不足以完成限流与 robots.txt 检查")

    left = dl.remaining(deadline)
    if left is None:
        await _get_ratelimiter().acquire()
    else:
        try:
            await asyncio.wait_for(_get_ratelimiter().acquire(), left)
        except asyncio.TimeoutError:
            return _deadline_payload(url, "等待全局限流配额时预算耗尽")

    if settings.respect_robots:
        if _budget_below(deadline, _COMPLIANCE_MIN_S):
            return _deadline_payload(url, "预算不足以完成 robots.txt 检查")
        robots = _get_robots()
        if not await robots.is_allowed(url, timeout=dl.remaining(deadline)):
            return _denied_payload(
                url, errors.ROBOTS_DISALLOWED, "目标路径被该站 robots.txt 禁止抓取"
            )

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


@mcp.custom_route("/stats", methods=["GET"])
async def stats_endpoint(request) -> JSONResponse:
    """进程内统计：请求数、分层/错误码分布、缓存命中率。

    未配置 ``SCRAPE_MCP_HTTP_TOKEN`` 时只接受本机回环访问；配置令牌后
    由 Bearer 鉴权保护，可远程读取。
    """
    if not (get_settings().http_token or "").strip():
        client = getattr(request, "client", None)
        host = (getattr(client, "host", "") or "").lower()
        if not is_loopback(host):
            return JSONResponse({"ok": False, "error": "仅允许本机访问"}, status_code=403)
    return JSONResponse(get_stats().snapshot())


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
    scroll: bool | None = None,
    click: str | None = None,
    bypass_cache: bool = False,
    session: str = "default",
    deadline: float | None = None,
) -> tuple[FetchOutcome | None, dict | None]:
    """抓取单个 URL，返回 (outcome, 错误 payload)。

    web_fetch 与 web_extract 共用：先做合规检查，再走分级抓取链路；
    拦截/登录墙统一如实上报，避免把挑战页当正文/当数据。成功时 error 为 None。
    wait_for/scroll/click 为 None 时套用该站 recipe，显式传值优先。
    deadline 是绝对截止时间（见 core.deadline），预算耗尽时不再发起新请求。
    """
    recipe = _recipe_for(url)
    if recipe is not None:
        wait_for = wait_for if wait_for is not None else recipe.wait_for
        click = click if click is not None else recipe.click
        scroll = scroll if scroll is not None else recipe.scroll

    if dl.expired(deadline):
        return None, _deadline_payload(url)

    denied = await _compliance_check(url, deadline)
    if denied is not None:
        return None, denied

    # 同域并发闸门：批量/爬取时避免瞬间对同一站点打多个请求
    async with _get_hostlimiter().slot(_host_of(url)):
        if dl.expired(deadline):
            return None, _deadline_payload(url, "排队等待同域配额时预算耗尽")
        outcome: FetchOutcome = await _get_fetcher().fetch(
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            wait_for=wait_for,
            scroll=bool(scroll),
            click=click,
            bypass_cache=bypass_cache,
            session=session,
            deadline=deadline,
        )

    code = errors.classify_outcome(outcome)
    log_event(
        "fetch",
        url=url,
        tier=outcome.tier,
        status=outcome.status,
        ms=outcome.elapsed_ms,
        attempts=outcome.attempts,
        cached=outcome.cached,
        code=code,
    )
    get_stats().record_fetch(
        tier=outcome.tier,
        code=code,
        kind=outcome.kind,
        cached=outcome.cached,
        html_bytes=len(outcome.html),
        elapsed_ms=outcome.elapsed_ms,
    )

    if outcome.error:
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
        if "deadline:skip_l2" in outcome.decisions:
            explain = "总预算剩余不足，未升级 L2 渲染；可提高 deadline_ms 后重试"
        elif outcome.tier == "l2":
            explain = "已升级 L2 浏览器渲染后仍被判为拦截页"
        else:
            explain = "未取得 L2 渲染结果（限流类判定不升级，或浏览器渲染失败，详见 block_reason）"
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


def _plain_payload(outcome: FetchOutcome, url: str, budget: int, fmt: str) -> dict:
    """PDF / 纯文本的返回载荷：主键与 HTML 路径保持一致，便于调用方统一处理。"""
    extra: dict = {}
    if outcome.kind == "pdf":
        try:
            pages, total, parsed = pdf_pages(
                outcome.content, max_pages=get_settings().pdf_max_pages
            )
        except PdfError as exc:
            return {
                "ok": False,
                "url": url,
                "status": outcome.status,
                "tier": outcome.tier,
                "error_code": errors.UNSUPPORTED_CONTENT,
                "error": str(exc),
                "retryable": False,
                "diagnostics": _diagnostics(outcome),
            }
        text = "\n\n".join(pages)
        extra = {"pages": total, "pages_parsed": parsed}
        if not text.strip():
            return {
                "ok": False,
                "url": url,
                "status": outcome.status,
                "tier": outcome.tier,
                "error_code": errors.EMPTY,
                "error": "PDF 未提取到文本（可能是扫描件，需要 OCR）",
                "retryable": False,
                "pages": total,
                "diagnostics": _diagnostics(outcome),
            }
    else:
        text = outcome.html

    truncated = False
    if budget > 0:
        text, truncated, _ = truncate_to_budget(text, budget)

    blocks = to_blocks(text)
    payload = {
        "ok": True,
        "url": url,
        "final_url": outcome.final_url,
        "status": outcome.status,
        "tier": outcome.tier,
        "title": "",
        "content": text,
        "token_estimate": count_tokens(text),
        "truncated": truncated,
        "blocks": len(blocks),
        "links_kept": 0,
        "links_dropped": 0,
        "html_bytes": len(outcome.html.encode("utf-8")) if outcome.html else len(outcome.content),
        "elapsed_ms": outcome.elapsed_ms,
        "cached": outcome.cached,
        "attempts": outcome.attempts,
        "content_type": outcome.content_type,
        "kind": outcome.kind,
    }
    payload.update(extra)
    if fmt != "markdown":
        payload["format"] = fmt
        if fmt == "text":
            payload["content"] = to_plain_text(text)
            payload["token_estimate"] = count_tokens(payload["content"])
        else:  # json：按行分块，PDF 也是逐行切，调用方按需再按页聚合
            payload["content"] = blocks
            payload["token_estimate"] = sum(count_tokens(b) for b in blocks)
    if outcome.notes:
        payload["notes"] = outcome.notes
    return payload


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
    scroll: bool | None = None,
    click: str | None = None,
    bypass_cache: bool = False,
    debug: bool = False,
    session: str = "default",
    deadline: float | None = None,
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

    budget = max(200, min(int(max_tokens), settings.hard_max_tokens))
    recipe = _recipe_for(url)
    cache: ResultCache | None = None
    key = ""
    if _cacheable(headers=headers, cookies=cookies, bypass_cache=bypass_cache, debug=debug):
        cache = _result_cache("fetch")
        # 命中 key 必须覆盖所有会改变正文的参数：recipe 的正文根与 L2 交互也算在内
        key = _result_key(
            kind="fetch",
            version=__version__,
            url=url,
            session=session,
            format=fmt,
            link_policy=link_policy,
            budget=budget,
            root=recipe.root_selector if recipe else None,
            wait_for=wait_for if wait_for is not None else (recipe.wait_for if recipe else None),
            click=click if click is not None else (recipe.click if recipe else None),
            scroll=scroll if scroll is not None else (recipe.scroll if recipe else None),
            pdf_max_pages=settings.pdf_max_pages,
        )
        if (hit := _cache_hit(cache, key)) is not None:
            return hit

    outcome, error = await _fetch_html(
        url,
        headers=headers,
        cookies=cookies,
        timeout=timeout,
        wait_for=wait_for,
        scroll=scroll,
        click=click,
        bypass_cache=bypass_cache,
        session=session,
        deadline=deadline,
    )
    if error is not None:
        return error

    budget = max(200, min(int(max_tokens), settings.hard_max_tokens))
    if outcome.kind != "html":
        payload = _plain_payload(outcome, url, budget, fmt)
        if payload.get("ok"):
            get_stats().record_tokens(payload.get("token_estimate", 0))
            if cache is not None and not _deadline_limited(outcome):
                cache.put(key, payload)
        return payload
    result = compact_html(
        outcome.html,
        outcome.final_url or url,
        link_policy=link_policy,
        max_tokens=budget,
        root_selector=recipe.root_selector if recipe else None,
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

    if cache is not None and payload.get("ok") and not _deadline_limited(outcome):
        cache.put(key, payload)
    if payload.get("ok"):
        get_stats().record_tokens(payload["token_estimate"])
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
    deadline_ms: int | None = None,
    wait_for: str | None = None,
    scroll: bool | None = None,
    click: str | None = None,
    bypass_cache: bool = False,
    debug: bool = False,
    session: str = "default",
) -> str:
    """抓取网页并返回极省 token 的紧凑正文（自动辨别反爬拦截页并如实上报）。

    支持 HTML 页面、PDF（需安装 ``mcp-scrape[pdf]``）与纯文本（``.txt``/``.md``）；
    后两者直接返回文本内容，不做 DOM 精简。

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
        deadline_ms: 整次调用的总预算（毫秒），不传表示不限。预算不够时逐级降级：
            压缩单步超时 → 跳过重试（diagnostics 里是 deadline:skip_retry）→
            不升级 L2（deadline:skip_l2）→ 未开始就耗尽时直接返回 timeout。
            预算内拿到的正文照常返回，但降级结果不会写入结果缓存。
        wait_for: 仅 L2 生效，等待该 CSS 选择器出现后再取内容（适合 SPA）；不传则套用站点 recipe。
        scroll: 仅 L2 生效，滚动页面触发懒加载/无限滚动；不传则套用站点 recipe。
        click: 仅 L2 生效，渲染后点击一次该 CSS 选择器（如"加载更多"）；不传则套用站点 recipe。
        bypass_cache: 跳过并绕过响应缓存，强制走网络。
        debug: 返回 diagnostics（分层决策、L2 交互命中情况、正文根与候选命中数），
            只含计数与决策、不含页面内容；失败时默认也会带精简版决策轨迹。
        session: 登录态会话名（默认 default）；需与 login 时使用的会话名一致，多身份互不干扰。
    """
    if not url.startswith(("http://", "https://")):
        return _payload(
            ok=False,
            error_code=errors.INVALID_URL,
            error="url 必须以 http:// 或 https:// 开头",
        )
    if (session_err := _session_error(session)) is not None:
        return _payload(**session_err)
    deadline, deadline_err = _deadline_arg(deadline_ms)
    if deadline_err is not None:
        return _payload(**deadline_err)
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
            session=session,
            deadline=deadline,
        )
    )


@mcp.tool()
async def web_extract(
    url: str,
    schema: dict | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: float | None = None,
    deadline_ms: int | None = None,
    wait_for: str | None = None,
    scroll: bool | None = None,
    click: str | None = None,
    bypass_cache: bool = False,
    debug: bool = False,
    session: str = "default",
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
        object（在第一个匹配节点内部继续按 fields 抽取）/
        jsonld（不写 selector，用 path 从 JSON-LD 取值，如 {"type": "jsonld", "path": "author.name"}）。
    后处理：regex + group（正则提取，未命中回落 default）、abs_url（相对链接转绝对）、
        strip（去首尾空白，默认 true）、convert（int / float / date，date 输出 ISO 8601）、
        required（必填，缺失时整体 ok=false 并返回 missing_required）。

    Args:
        url: 目标网页完整 URL。
        schema: 字段抽取规则字典，见上方示例；不传则套用该站 recipe 的 extract 规则。
        headers: 自定义请求头；传入时不走共享缓存。
        cookies: 自定义 cookie 字典；传入时不走共享缓存。
        timeout: 单次请求超时秒数。
        deadline_ms: 整次调用的总预算（毫秒），不传表示不限；降级规则同 web_fetch。
        wait_for: 仅 L2 生效，等待该 CSS 选择器出现后再取内容；不传则套用站点 recipe。
        scroll: 仅 L2 生效，滚动页面触发懒加载；不传则套用站点 recipe。
        click: 仅 L2 生效，渲染后点击一次该 CSS 选择器；不传则套用站点 recipe。
        bypass_cache: 跳过并绕过响应缓存，强制走网络。
        debug: 返回 diagnostics（分层决策与 L2 交互命中情况），不含页面内容。
        session: 登录态会话名（默认 default）；需与 login 时使用的会话名一致。
    """
    if not url.startswith(("http://", "https://")):
        return _payload(
            ok=False,
            error_code=errors.INVALID_URL,
            error="url 必须以 http:// 或 https:// 开头",
        )
    if (session_err := _session_error(session)) is not None:
        return _payload(**session_err)
    deadline, deadline_err = _deadline_arg(deadline_ms)
    if deadline_err is not None:
        return _payload(**deadline_err)

    recipe = _recipe_for(url)
    if schema is None:
        schema = recipe.extract if recipe is not None else None
        if schema is None:
            return _payload(
                ok=False,
                url=url,
                error_code=errors.INVALID_ARGUMENT,
                error="缺少 schema，且该站未配置 recipe 抽取规则",
            )

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

    cache: ResultCache | None = None
    key = ""
    if _cacheable(headers=headers, cookies=cookies, bypass_cache=bypass_cache, debug=debug):
        cache = _result_cache("extract")
        # schema 决定字段结果，recipe 的 L2 交互决定渲染后的 DOM，都进 key
        key = _result_key(
            kind="extract",
            version=__version__,
            url=url,
            session=session,
            schema=schema,
            wait_for=wait_for if wait_for is not None else (recipe.wait_for if recipe else None),
            click=click if click is not None else (recipe.click if recipe else None),
            scroll=scroll if scroll is not None else (recipe.scroll if recipe else None),
        )
        if (hit := _cache_hit(cache, key)) is not None:
            return _payload(**hit)

    outcome, error = await _fetch_html(
        url,
        headers=headers,
        cookies=cookies,
        timeout=timeout,
        wait_for=wait_for,
        scroll=scroll,
        click=click,
        bypass_cache=bypass_cache,
        session=session,
        deadline=deadline,
    )
    if error is not None:
        return _payload(**error)

    if outcome.kind != "html":
        return _payload(
            ok=False,
            url=url,
            status=outcome.status,
            tier=outcome.tier,
            error_code=errors.UNSUPPORTED_CONTENT,
            error="该 URL 不是 HTML（PDF/纯文本不支持 CSS 字段抽取），请改用 web_fetch",
            retryable=False,
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
        "required_missing": result.required_missing,
    }
    if outcome.notes:
        payload["notes"] = outcome.notes
    if result.errors:
        payload["schema_notes"] = result.errors
    if debug:
        payload["diagnostics"] = _diagnostics(outcome, detail=True)
    if result.required_missing:
        payload["ok"] = False
        payload["error_code"] = errors.MISSING_REQUIRED
        payload["error"] = "必填字段缺失: " + ", ".join(result.required_missing)
        payload["retryable"] = False
    if cache is not None and payload.get("ok") and not _deadline_limited(outcome):
        cache.put(key, payload)
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
    deadline_ms: int | None = None,
    bypass_cache: bool = False,
    debug: bool = False,
    session: str = "default",
    ctx: Context | None = None,
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
        deadline_ms: 整批共用的总预算（毫秒），不传表示不限。预算耗尽后还没开始的
            子任务直接返回 timeout（diagnostics 为 deadline:exceeded），不再占用网络与浏览器。
        bypass_cache: 跳过并绕过响应缓存。
        debug: 每条结果附带 diagnostics（决策轨迹与计数，不含页面内容）。
        session: 登录态会话名（默认 default）；对该批所有 URL 生效。
        ctx: MCP 上下文，由框架注入，用于逐条上报进度（客户端请求进度时才发通知）。
    """
    settings = get_settings()
    if (session_err := _session_error(session)) is not None:
        return _payload(**session_err)
    deadline, deadline_err = _deadline_arg(deadline_ms)
    if deadline_err is not None:
        return _payload(**deadline_err)
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
    total = len(urls)
    done = 0

    async def one(u: str) -> dict:
        nonlocal done
        async with sem:
            if dl.expired(deadline):
                # 排队期间预算耗尽：不再发起抓取，直接给出可判定的失败
                result = _deadline_payload(u, "批量子任务排队时预算耗尽")
            else:
                try:
                    result = await _fetch_dict(
                        u,
                        max_tokens,
                        link_policy,
                        fmt=format,
                        headers=headers,
                        cookies=cookies,
                        timeout=timeout,
                        bypass_cache=bypass_cache,
                        debug=debug,
                        session=session,
                        deadline=deadline,
                    )
                except Exception as exc:  # 单条意外异常不应拖垮整批
                    code = errors.classify_exception(exc)
                    result = {
                        "ok": False,
                        "url": u,
                        "error_code": code,
                        "error": f"{type(exc).__name__}: {exc}",
                        "retryable": errors.is_retryable(code),
                    }
        done += 1
        await _notify_progress(ctx, done, total, f"已抓取 {done}/{total} 个 URL")
        return result

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
    next_selector: str | None = None,
    session: str = "default",
    deadline: float | None = None,
) -> tuple[dict, list[str], str | None]:
    """抓一页并顺手抽链接/下一页；失败页返回错误 payload 和空链接列表。"""
    outcome, error = await _fetch_html(
        url,
        headers=headers,
        cookies=cookies,
        timeout=timeout,
        bypass_cache=bypass_cache,
        session=session,
        deadline=deadline,
    )
    if error is not None:
        return error, [], None

    if outcome.kind != "html":
        return (
            {
                "ok": False,
                "url": url,
                "status": outcome.status,
                "tier": outcome.tier,
                "error_code": errors.UNSUPPORTED_CONTENT,
                "error": "该页不是 HTML（PDF/纯文本不支持爬取正文），已跳过",
                "retryable": False,
            },
            [],
            None,
        )

    base = outcome.final_url or url
    budget = max(200, min(int(max_tokens), get_settings().hard_max_tokens))
    recipe = _recipe_for(url)
    result = compact_html(
        outcome.html,
        base,
        link_policy=link_policy,
        max_tokens=budget,
        root_selector=recipe.root_selector if recipe else None,
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
        "cached": outcome.cached,
    }
    if outcome.notes:
        payload["notes"] = outcome.notes
    links = extract_links(outcome.html, base, same_host=same_host, limit=link_limit)
    nxt = (
        next_link(outcome.html, base, next_selector, same_host=same_host) if next_selector else None
    )
    return payload, links, nxt


@mcp.tool()
async def web_crawl(
    url: str,
    depth: int = 1,
    limit: int = 10,
    same_host: bool = True,
    max_tokens: int = 2000,
    link_policy: str = "internal",
    next_selector: str | None = None,
    max_pages: int | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: float | None = None,
    deadline_ms: int | None = None,
    bypass_cache: bool = False,
    session: str = "default",
    ctx: Context | None = None,
) -> str:
    """从起始页发现站内链接并批量抓取，或按"下一页"顺序翻页，适合把一个栏目的前 N 篇读进来。

    每页都走完整的分级抓取与合规链路（robots/黑白名单/限流/同域并发），
    单页失败只影响该页，不中断整次爬取；静态资源链接（图片/压缩包等）自动跳过。

    两种模式：
      - 链接模式（默认）：按 depth 分层发现站内链接，limit 控制总页数。
      - 分页模式：传 next_selector（或站点 recipe 配置），顺序跟进"下一页"，max_pages 控制页数。

    Args:
        url: 起始页 URL。
        depth: 链接模式的跳转层数。0=只抓起始页；1=再抓起始页上的链接（默认）；最大 3。
        limit: 链接模式最多抓取的页面总数（含起始页），受配置 crawl_max_pages 上限约束。
        same_host: 只跟进同主机链接（默认 True）。
        max_tokens: 每页正文 token 预算上限。
        link_policy: 同 web_fetch。
        next_selector: 分页模式：指向"下一页"的 CSS 选择器；有值时忽略 depth。
        max_pages: 分页模式最多翻多少页（默认 5），受 crawl_max_pages 上限约束。
        headers: 所有页面共用的自定义请求头；传入时不走共享缓存。
        cookies: 所有页面共用的自定义 cookie；传入时不走共享缓存。
        timeout: 单次请求超时秒数。
        deadline_ms: 整次爬取的总预算（毫秒），不传表示不限。预算耗尽后不再抓新页，
            已抓到的页面照常返回；排队中的子任务直接返回 timeout。
        bypass_cache: 跳过并绕过响应缓存。
        session: 登录态会话名（默认 default）；对本次所有页面生效。
        ctx: MCP 上下文，由框架注入，用于逐页上报进度（客户端请求进度时才发通知）。
    """
    if not url.startswith(("http://", "https://")):
        return _payload(
            ok=False,
            error_code=errors.INVALID_URL,
            error="url 必须以 http:// 或 https:// 开头",
        )
    if (session_err := _session_error(session)) is not None:
        return _payload(**session_err)
    deadline, deadline_err = _deadline_arg(deadline_ms)
    if deadline_err is not None:
        return _payload(**deadline_err)
    if dl.expired(deadline):
        return _payload(**_deadline_payload(url))
    settings = get_settings()
    if depth < 0 or depth > 3:
        return _payload(
            ok=False,
            error_code=errors.INVALID_ARGUMENT,
            error="depth 需在 0~3 之间",
        )
    if next_selector is not None and (
        not isinstance(next_selector, str) or not next_selector.strip()
    ):
        return _payload(
            ok=False,
            error_code=errors.INVALID_ARGUMENT,
            error="next_selector 必须是非空字符串",
        )
    if max_pages is not None and (
        not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1
    ):
        return _payload(
            ok=False,
            error_code=errors.INVALID_ARGUMENT,
            error="max_pages 必须是正整数",
        )

    recipe = _recipe_for(url)
    if next_selector is None and recipe is not None:
        next_selector = recipe.next_selector
    if max_pages is None and recipe is not None:
        max_pages = recipe.max_pages

    # 分页模式：必须顺序翻页（下一页依赖当前页），因此不复用并发 BFS
    if next_selector:
        cap = min(max_pages or 5, settings.crawl_max_pages)
        results: list[dict] = []
        visited: set[str] = {url}
        current: str | None = url
        while current is not None and len(results) < cap and not dl.expired(deadline):
            try:
                payload, _links, nxt = await _crawl_page(
                    current,
                    max_tokens=max_tokens,
                    link_policy=link_policy,
                    same_host=same_host,
                    link_limit=50,
                    headers=headers,
                    cookies=cookies,
                    timeout=timeout,
                    bypass_cache=bypass_cache,
                    next_selector=next_selector,
                    session=session,
                    deadline=deadline,
                )
            except Exception as exc:  # 单页异常不中断整次爬取
                code = errors.classify_exception(exc)
                payload = {
                    "ok": False,
                    "url": current,
                    "error_code": code,
                    "error": f"{type(exc).__name__}: {exc}",
                    "retryable": errors.is_retryable(code),
                }
                nxt = None
            results.append(payload)
            await _notify_progress(ctx, len(results), cap, f"已翻到第 {len(results)}/{cap} 页")
            if not payload.get("ok") or nxt is None or nxt in visited:
                break
            visited.add(nxt)
            current = nxt

        ok_count = sum(1 for r in results if r.get("ok"))
        return _payload(
            ok=True,
            start=url,
            depth=0,
            limit=cap,
            mode="paginate",
            pages_fetched=len(results),
            ok_count=ok_count,
            links_discovered=len(visited) - 1,
            results=results,
        )

    cap = min(max(1, int(limit)), settings.crawl_max_pages)
    sem = asyncio.Semaphore(settings.batch_max_concurrency)
    fetched = 0

    async def one(u: str) -> tuple[dict, list[str], str | None]:
        nonlocal fetched
        async with sem:
            if dl.expired(deadline):
                result = (_deadline_payload(u, "爬取子任务排队时预算耗尽"), [], None)
            else:
                try:
                    result = await _crawl_page(
                        u,
                        max_tokens=max_tokens,
                        link_policy=link_policy,
                        same_host=same_host,
                        link_limit=max(cap * 5, 50),
                        headers=headers,
                        cookies=cookies,
                        timeout=timeout,
                        bypass_cache=bypass_cache,
                        session=session,
                        deadline=deadline,
                    )
                except Exception as exc:  # 单页异常不中断整次爬取
                    code = errors.classify_exception(exc)
                    result = (
                        {
                            "ok": False,
                            "url": u,
                            "error_code": code,
                            "error": f"{type(exc).__name__}: {exc}",
                            "retryable": errors.is_retryable(code),
                        },
                        [],
                        None,
                    )
        fetched += 1
        await _notify_progress(ctx, fetched, cap, f"已抓取 {fetched}/{cap} 页")
        return result

    visited: set[str] = {url}
    frontier: list[str] = [url]
    results: list[dict] = []
    level = 0

    while frontier and len(results) < cap and not dl.expired(deadline):
        frontier = frontier[: cap - len(results)]
        pairs = await asyncio.gather(*(one(u) for u in frontier))
        next_frontier: list[str] = []
        for payload, links, _nxt in pairs:
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
        mode="links",
        pages_fetched=len(results),
        ok_count=ok_count,
        links_discovered=len(visited) - 1,
        results=results,
    )


@mcp.tool()
async def login(url: str, timeout: float = 180.0, session: str = "default") -> str:
    """打开带界面的浏览器手动登录，并把该站登录态持久化到磁盘。

    用于 web_fetch 报告 login_required=true 的站点：浏览器会以非无头方式打开目标页，
    请在弹出的窗口里完成登录；检测到登录成功或超时后，登录态（cookie 等）会被保存，
    之后该站的 web_fetch 会自动带上登录态。

    Args:
        url: 目标站点任一页面 URL（按域名区分登录态）。
        timeout: 等待手动登录完成的秒数，超时也会保存当前状态便于下次继续。
        session: 会话名（默认 default）。需要多账号/多身份时分别用不同名字登录，
            web_fetch 时传同一个 session 即可，互不覆盖。
    """
    if not url.startswith(("http://", "https://")):
        return _payload(
            ok=False,
            error_code=errors.INVALID_URL,
            error="url 必须以 http:// 或 https:// 开头",
        )
    if (session_err := _session_error(session)) is not None:
        return _payload(**session_err)

    result = await _get_fetcher().login(url, timeout, session)
    result["host"] = urlparse(url).netloc.lower()
    if result.get("ok"):
        result["message"] = (
            "登录态已保存，后续 web_fetch 会自动带上"
            if result["logged_in"]
            else "未检测到登录完成，但已保存当前状态；等你登录完成后可重新调用 login 覆盖"
        )
    return _payload(**result)


def _url_host(url: str) -> str | None:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    return (urlparse(url).netloc or "").lower().split(":")[0] or None


@mcp.tool()
async def session_list() -> str:
    """列出本地已保存的登录态（会话名 + 主机 + 更新时间），不含 cookie 内容。"""
    items = _session_store().list_sessions()
    return _payload(ok=True, total=len(items), sessions=items)


@mcp.tool()
async def session_export(url: str, session: str = "default", path: str | None = None) -> str:
    """导出某站登录态里的 cookie，便于备份或在另一台机器上导入。

    Args:
        url: 目标站点任一页面 URL（按域名匹配登录态）。
        session: 会话名（默认 default）。
        path: 可选本地文件路径；给出时把导出 JSON 写入该文件（目录需已存在），
            不传则直接在返回里给出 cookies。
    """
    host = _url_host(url)
    if host is None:
        return _payload(
            ok=False,
            error_code=errors.INVALID_URL,
            error="url 必须以 http:// 或 https:// 开头",
        )
    if (session_err := _session_error(session)) is not None:
        return _payload(**session_err)

    data = _session_store().export_cookies(host, session)
    if data is None:
        return _payload(
            ok=False,
            url=url,
            error_code=errors.INVALID_ARGUMENT,
            error=f"{host} 在会话 {session} 下还没有登录态，请先调用 login",
        )

    payload = {"ok": True, "url": url, **data, "cookie_count": len(data["cookies"])}
    if path:
        target = Path(path).expanduser()
        try:
            target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            return _payload(
                ok=False,
                url=url,
                error_code=errors.INVALID_ARGUMENT,
                error=f"写入 {target} 失败: {exc}",
            )
        payload["path"] = str(target)
    return _payload(**payload)


@mcp.tool()
async def session_import(
    url: str,
    path: str,
    session: str = "default",
    replace: bool = False,
) -> str:
    """从本地 JSON 文件导入 cookie 到某站登录态（仅本地文件，不发起网络请求）。

    文件内容支持三种形态：session_export 导出的对象（含 ``cookies``）、
    Playwright storage_state（含 ``cookies``），或直接是一个 cookie 数组。

    Args:
        url: 目标站点任一页面 URL（按域名匹配登录态）。
        path: 本地 JSON 文件路径。
        session: 会话名（默认 default）；与 web_fetch 使用的会话名保持一致。
        replace: True 时用文件里的 cookie 整体替换该会话；默认 False 为按
            （name, domain, path）合并，同名以导入值为准。
    """
    host = _url_host(url)
    if host is None:
        return _payload(
            ok=False,
            error_code=errors.INVALID_URL,
            error="url 必须以 http:// 或 https:// 开头",
        )
    if (session_err := _session_error(session)) is not None:
        return _payload(**session_err)

    source = Path(path).expanduser()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return _payload(
            ok=False,
            url=url,
            error_code=errors.INVALID_ARGUMENT,
            error=f"读取 {source} 失败: {exc}",
        )
    except json.JSONDecodeError as exc:
        return _payload(
            ok=False,
            url=url,
            error_code=errors.INVALID_ARGUMENT,
            error=f"{source} 不是合法 JSON: {exc}",
        )

    cookies = raw.get("cookies") if isinstance(raw, dict) else raw
    if not isinstance(cookies, list):
        return _payload(
            ok=False,
            url=url,
            error_code=errors.INVALID_ARGUMENT,
            error="文件里没有 cookies 数组（支持 session_export 产物、storage_state 或 cookie 数组）",
        )

    count = _session_store().import_cookies(host, cookies, session, replace=replace)
    if count == 0:
        return _payload(
            ok=False,
            url=url,
            error_code=errors.INVALID_ARGUMENT,
            error="没有可用的 cookie（每项至少需要 name 字段）",
        )
    return _payload(
        ok=True,
        url=url,
        host=host,
        session=normalize_session(session),
        imported=count,
        path=str(source),
        message="cookie 已写入本地登录态，后续 web_fetch 传同一 session 即可生效",
    )


def _run_http(settings, port: int, token: str | None) -> None:  # pragma: no cover
    """构建带鉴权的 StreamableHTTP 应用并交给 uvicorn。"""
    import uvicorn

    app = mcp.streamable_http_app(
        json_response=settings.http_json,
        stateless_http=settings.http_stateless,
        host=settings.http_host,
    )
    uvicorn.run(
        BearerAuthMiddleware(app, token=token),
        host=settings.http_host,
        port=port,
        log_level=settings.log_level.lower(),
    )


def main(argv: list[str] | None = None) -> None:
    """CLI 入口。

    零配置默认即用：不传参按 stdio 运行，可作为 MCP 服务被 Claude/Cursor 等接管。
    仅当显式指定 --transport=streamable-http 时才走 HTTP。任意目录下
    ``pip install mcp-scrape`` 后运行 ``scrape-mcp`` 即可，无需 clone/venv/PYTHONPATH。
    """
    if argv is None:
        argv = sys.argv[1:]
    # 子命令（init / schema）走独立模块，不启动服务；其余参数保持原有零配置行为
    if argv and argv[0] in ("init", "schema"):
        from .cli import run as run_subcommand

        raise SystemExit(run_subcommand(argv))
    if not argv:
        argv = ["--transport", "stdio"]
    elif argv in (["-h"], ["--help"]):
        msg = (
            "scrape-mcp — 反爬感知抓取 + 极省 Token 内容精简的 MCP Server\n\n"
            "用法: scrape-mcp [--transport stdio|streamable-http] [--port N] [--version]\n"
            "      scrape-mcp init [--client claude|cursor|vscode|json] [--write]\n"
            "      scrape-mcp schema [--tool NAME] [--output FILE]\n\n"
            "零配置默认作为 stdio MCP 服务运行，供 Claude/Cursor 等客户端接管。\n"
            "init 生成客户端配置（默认只预览 diff），schema 导出工具定义。\n"
            "如需 HTTP 直连（Postman/curl），加 --transport streamable-http [--port 8000]；\n"
            "绑定非回环地址时必须设置 SCRAPE_MCP_HTTP_TOKEN。\n"
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
        token = (settings.http_token or "").strip() or None
        ensure_http_access(settings.http_host, token)
        _run_http(settings, base, token)
        return
    # 默认 stdio
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
