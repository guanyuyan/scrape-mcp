"""LlamaIndex Reader 适配：把一组 URL 抓成 ``Document`` 列表，直接进 ingestion pipeline。

需要可选依赖 ``mcp-scrape[llamaindex]``。``build_reader()`` 返回的实例同时支持
同步 ``load_data()`` 与异步 ``aload_data()``；失败页会被跳过，不产生空文档。
"""

from __future__ import annotations

import asyncio
from typing import Any

from .. import sdk

__all__ = ["build_reader", "document_kwargs"]


def document_kwargs(payload: dict[str, Any]) -> dict[str, Any] | None:
    """把 web_fetch 载荷转成 Document 构造参数；失败页返回 None。"""
    if not payload.get("ok"):
        return None
    text = payload.get("content") or ""
    metadata = {
        "url": payload.get("url") or "",
        "title": payload.get("title"),
        "status_code": payload.get("status_code"),
        "content_type": payload.get("content_type"),
    }
    clean = {key: value for key, value in metadata.items() if value is not None}
    return {"text": text, "metadata": clean}


def build_reader() -> Any:
    """构造 LlamaIndex Reader；未安装可选依赖时给出安装提示。"""
    try:
        from llama_index.core.readers.base import BaseReader
        from llama_index.core.schema import Document
    except ImportError as exc:
        raise RuntimeError(
            "使用 LlamaIndex 适配需要可选依赖，请先 pip install 'mcp-scrape[llamaindex]'"
        ) from exc

    class ScrapeMCPReader(BaseReader):
        """抓取 URL 列表并转成 Document，参数透传给 web_fetch。"""

        def load_data(self, urls: list[str], **options: Any) -> list[Any]:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(self.aload_data(urls, **options))
            raise RuntimeError("当前已在事件循环中，请改用 await reader.aload_data(urls)")

        async def aload_data(self, urls: list[str], **options: Any) -> list[Any]:
            documents: list[Any] = []
            for url in urls:
                kwargs = document_kwargs(await sdk.fetch(url, **options))
                if kwargs is not None:
                    documents.append(Document(**kwargs))
            return documents

    return ScrapeMCPReader()
