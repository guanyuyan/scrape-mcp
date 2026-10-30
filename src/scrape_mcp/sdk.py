"""Python SDK 薄封装：与 MCP 工具共用同一套实现，不复制抓取逻辑。

```python
import asyncio
from scrape_mcp.sdk import fetch, extract

data = asyncio.run(fetch("https://docs.python.org/3/library/asyncio.html", max_tokens=2000))
fields = asyncio.run(extract("https://example.com", schema={"fields": {"title": {"selector": "h1"}}}))
```

返回值就是 MCP 工具的 JSON 载荷（已解析为 dict），字段与错误码完全一致。
"""

from __future__ import annotations

import json
from typing import Any

from . import server
from .config import get_settings
from .core.logging import configure

__all__ = [
    "aclose",
    "batch",
    "crawl",
    "crawl_cancel",
    "crawl_list",
    "crawl_start",
    "crawl_status",
    "extract",
    "fetch",
    "index",
    "screenshot",
    "search",
    "site_map",
    "watch",
]

_logging_ready = False


def _ensure_logging() -> None:
    """SDK 调用时装一次 stderr JSON 日志，避免事件日志冒泡到宿主应用的 root logger。"""
    global _logging_ready
    if not _logging_ready:
        configure(get_settings().log_level)
        _logging_ready = True


def _parse(payload: str) -> dict[str, Any]:
    return json.loads(payload)


async def fetch(url: str, **options: Any) -> dict[str, Any]:
    """抓取单个页面并按 compact 规则精简；参数同 ``web_fetch``。"""
    _ensure_logging()
    return _parse(await server.web_fetch(url, **options))


async def extract(url: str, schema: dict[str, Any] | None = None, **options: Any) -> dict[str, Any]:
    """按字段 schema 抽取结构化数据；参数同 ``web_extract``。"""
    _ensure_logging()
    return _parse(await server.web_extract(url, schema, **options))


async def crawl(url: str, **options: Any) -> dict[str, Any]:
    """批量抓取（链接模式）或按下一页顺序翻页；参数同 ``web_crawl``。"""
    _ensure_logging()
    return _parse(await server.web_crawl(url, **options))


async def crawl_start(url: str, **options: Any) -> dict[str, Any]:
    """启动异步爬取任务并返回 job_id；参数同 ``web_crawl_start``。"""
    _ensure_logging()
    return _parse(await server.web_crawl_start(url, **options))


async def crawl_status(job_id: str, **options: Any) -> dict[str, Any]:
    """读取异步任务状态与部分结果；参数同 ``web_crawl_status``。"""
    _ensure_logging()
    return _parse(await server.web_crawl_status(job_id, **options))


async def crawl_cancel(job_id: str) -> dict[str, Any]:
    """取消异步爬取任务；参数同 ``web_crawl_cancel``。"""
    _ensure_logging()
    return _parse(await server.web_crawl_cancel(job_id))


async def crawl_list(**options: Any) -> dict[str, Any]:
    """列出最近的异步爬取任务；参数同 ``web_crawl_list``。"""
    _ensure_logging()
    return _parse(await server.web_crawl_list(**options))


async def batch(urls: list[str], **options: Any) -> dict[str, Any]:
    """并发抓取一组 URL；参数同 ``web_batch``。"""
    _ensure_logging()
    return _parse(await server.web_batch(urls, **options))


async def watch(url: str, **options: Any) -> dict[str, Any]:
    """对比页面自上次抓取以来的变化；参数同 ``web_watch``。"""
    _ensure_logging()
    return _parse(await server.web_watch(url, **options))


async def search(query: str, **options: Any) -> dict[str, Any]:
    """搜索关键词并返回结构化结果；参数同 ``web_search``。"""
    _ensure_logging()
    return _parse(await server.web_search(query, **options))


async def screenshot(url: str, **options: Any) -> dict[str, Any]:
    """浏览器渲染并截图，返回本地 PNG 路径；参数同 ``web_screenshot``。"""
    _ensure_logging()
    return _parse(await server.web_screenshot(url, **options))


async def index(url: str, **options: Any) -> dict[str, Any]:
    """读取 sitemap 并与上次清单对比；参数同 ``web_index``。"""
    _ensure_logging()
    return _parse(await server.web_index(url, **options))


async def site_map(url: str, **options: Any) -> dict[str, Any]:
    """发现站点 URL（sitemap + 入口页链接）；参数同 ``web_map``。"""
    _ensure_logging()
    return _parse(await server.web_map(url, **options))


async def aclose() -> None:
    """释放全局抓取器（含 Playwright 浏览器）。

    脚本在事件循环结束前调用一次，可避免浏览器子进程在解释器退出时清理告警；
    长驻进程可复用同一个抓取器，无需每次调用。
    """
    fetcher = server._fetcher
    if fetcher is not None:
        await fetcher.aclose()
        server._fetcher = None
