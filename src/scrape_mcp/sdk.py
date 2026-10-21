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

__all__ = ["batch", "crawl", "extract", "fetch"]

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


async def batch(urls: list[str], **options: Any) -> dict[str, Any]:
    """并发抓取一组 URL；参数同 ``web_batch``。"""
    _ensure_logging()
    return _parse(await server.web_batch(urls, **options))
