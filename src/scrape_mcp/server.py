"""MCP Server 入口（M1：L1 抓取 + compact 输出；M2：命中拦截升级 L2 浏览器渲染）。"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from .config import get_settings
from .core.fetcher import FetchOutcome, Fetcher
from .extract.compact import LINK_POLICIES, compact_html
from .tokenizer import count_tokens

_fetcher: Fetcher | None = None


def _get_fetcher() -> Fetcher:
    global _fetcher
    if _fetcher is None:
        _fetcher = Fetcher(get_settings())
    return _fetcher


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

# 大页面 + 极小正文 → 疑似 JS 登录墙/骨架页，加 note 提示（不判失败，避免误杀超短正文）。
# 与 detector._SHELL_MIN_HTML 对齐到 30KB：60KB 澎湃首页这类"挺大却榨不出正文"也要能提示。
LOW_CONTENT_HTML_BYTES = 30_000
LOW_CONTENT_TOKENS = 250


def _payload(**kwargs) -> str:
    return json.dumps(kwargs, ensure_ascii=False, indent=None)


async def _fetch_dict(url: str, max_tokens: int, link_policy: str) -> dict:
    """单个 URL 的完整抓取闭环，返回可序列化的 payload dict（web_fetch 与 web_batch 共用）。"""
    settings = get_settings()

    if link_policy not in LINK_POLICIES:
        return {"ok": False, "error": f"link_policy 必须是 {LINK_POLICIES} 之一"}

    budget = max(200, min(int(max_tokens), settings.hard_max_tokens))

    outcome: FetchOutcome = await _get_fetcher().fetch(url)

    if outcome.error:
        return dict(
            ok=False,
            url=url,
            tier=outcome.tier,
            elapsed_ms=outcome.elapsed_ms,
            error=outcome.error,
        )

    if outcome.blocked != "none":
        # 已尝试的分级如实上报，避免把挑战页/空壳页当正文喂给模型
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
        payload = dict(
            ok=False,
            url=url,
            final_url=outcome.final_url,
            status=outcome.status,
            tier=outcome.tier,
            blocked=outcome.blocked,
            block_reason=outcome.block_reason,
            elapsed_ms=outcome.elapsed_ms,
            error=f"{hint}；{explain}",
        )
        if outcome.login_required:
            # 渲染过后正文仍极薄，很可能该内容需要登录才可见：明确提示可用的登录路径
            state = "已带登录态仍失败，登录态可能过期，请重新 login" if outcome.session_loaded else "该站还无登录态"
            payload["login_required"] = True
            payload["error"] = f"{payload['error']}；{state}，可调用 login(url=...) 手动登录后重试"
            payload["login_hint"] = f'可调用 login(url="{url}", timeout=180) 打开浏览器手动登录'
        return payload

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
    }

    # 大页面却榨不出正文，多半是 JS 登录墙/骨架页：正文本身可能是真实内容，故不判失败，只如实提示
    if len(outcome.html) >= LOW_CONTENT_HTML_BYTES and payload["token_estimate"] < LOW_CONTENT_TOKENS:
        payload["note"] = (
            "正文极少，疑似 JS 登录墙或骨架页；浏览器已渲染过，公开区域确实只有这些内容"
            if outcome.tier == "l2"
            else "正文极少，疑似 JS 登录墙或骨架页；当前仅 L1 抓取（未执行 JS）"
        )

    return payload


@mcp.tool()
async def web_fetch(url: str, max_tokens: int = 4000, link_policy: str = "internal") -> str:
    """抓取网页并返回极省 token 的紧凑正文（自动辨别反爬拦截页并如实上报）。

    Args:
        url: 目标网页完整 URL，需带 http/https 协议头。
        max_tokens: 正文 token 预算上限，超出时保留头尾、省略中段。
        link_policy: 链接处理策略。internal=站内链接保留为相对路径、站外降级为纯文本（默认，最省）；
            all=站内外链接全保留；none=全部降级为纯文本。
    """
    if not url.startswith(("http://", "https://")):
        return _payload(ok=False, error="url 必须以 http:// 或 https:// 开头")
    return _payload(**await _fetch_dict(url, max_tokens, link_policy))


@mcp.tool()
async def web_batch(urls: list[str], max_tokens: int = 3000, link_policy: str = "internal") -> str:
    """并发抓取一批 URL，各自走完整分级链路，返回逐个结果。

    Args:
        urls: 目标 URL 列表（最多 batch_max_urls 个，默认 20）。
        max_tokens: 每个 URL 的正文 token 预算上限。
        link_policy: 同 web_fetch。
    """
    import asyncio

    settings = get_settings()
    if link_policy not in LINK_POLICIES:
        return _payload(ok=False, error=f"link_policy 必须是 {LINK_POLICIES} 之一")
    urls = [u for u in (urls or []) if isinstance(u, str) and u.startswith(("http://", "https://"))]
    if not urls:
        return _payload(ok=False, error="urls 需为至少一个 http/https 的 URL 列表")
    if len(urls) > settings.batch_max_urls:
        return _payload(ok=False, error=f"urls 数量超过上限 {settings.batch_max_urls}，请分批")

    sem = asyncio.Semaphore(settings.batch_max_concurrency)

    async def one(u: str) -> dict:
        async with sem:
            try:
                return await _fetch_dict(u, max_tokens, link_policy)
            except Exception as exc:  # 单条意外异常不应拖垮整批
                return {"ok": False, "url": u, "error": f"{type(exc).__name__}: {exc}"}

    results = await asyncio.gather(*(one(u) for u in urls))
    ok_count = sum(1 for r in results if r.get("ok"))
    return _payload(ok=True, total=len(results), ok_count=ok_count, results=results)


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
    from urllib.parse import urlparse

    if not url.startswith(("http://", "https://")):
        return _payload(ok=False, error="url 必须以 http:// 或 https:// 开头")

    result = await _get_fetcher().login(url, timeout)
    result["host"] = urlparse(url).netloc.lower()
    if result.get("ok"):
        result["message"] = (
            "登录态已保存，后续 web_fetch 会自动带上"
            if result["logged_in"]
            else "未检测到登录完成，但已保存当前状态；等你登录完成后可重新调用 login 覆盖"
        )
    return _payload(**result)


def main() -> None:
    transport = os.getenv("SCRAPE_MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        # HTTP 入口（Postman/curl 友好）：JSON 响应 + 无状态会话，免 session 头
        mcp.run(
            transport="streamable-http",
            host=os.getenv("SCRAPE_MCP_HTTP_HOST", "127.0.0.1"),
            port=int(os.getenv("SCRAPE_MCP_HTTP_PORT", "8000")),
            json_response=os.getenv("SCRAPE_MCP_HTTP_JSON", "1") not in ("0", "false", "False"),
            stateless_http=os.getenv("SCRAPE_MCP_HTTP_STATELESS", "1") not in ("0", "false", "False"),
        )
    else:
        mcp.run(transport=transport)


if __name__ == "__main__":
    main()
