"""LangChain 工具适配：把 web_fetch / web_extract / web_search 包成 StructuredTool。

需要可选依赖 ``mcp-scrape[langchain]``；未安装时导入本模块不受影响，
只有调用 ``build_tools()`` 才会报带安装提示的错误。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .. import sdk

__all__ = ["ExtractInput", "FetchInput", "SearchInput", "build_tools"]


class FetchInput(BaseModel):
    """web_fetch 的入参：URL + 正文预算。"""

    url: str = Field(..., description="要抓取的完整 URL（必须带 http/https 协议）")
    max_tokens: int = Field(4000, ge=1, description="正文 token 预算，超出按截断")


class ExtractInput(BaseModel):
    """web_extract 的入参：URL + 字段 schema。

    字段名用 ``schema_`` 避开 pydantic 的 ``schema`` 属性，对外别名仍是 ``schema``。
    """

    model_config = ConfigDict(populate_by_name=True)

    url: str = Field(..., description="要抽取的完整 URL")
    schema_: dict[str, Any] = Field(
        ...,
        alias="schema",
        description='字段选择器，如 {"fields": {"title": {"selector": "h1"}}}',
    )
    prompt: str | None = Field(None, description="可选的抽取要求，需服务端配置 LLM")


class SearchInput(BaseModel):
    """web_search 的入参：查询词 + 返回条数。"""

    query: str = Field(..., description="搜索关键词")
    max_results: int = Field(10, ge=1, le=20, description="返回结果条数上限")


async def _fetch(url: str, max_tokens: int = 4000) -> str:
    payload = await sdk.fetch(url, max_tokens=max_tokens)
    return json.dumps(payload, ensure_ascii=False)


async def _extract(url: str, schema_: dict[str, Any], prompt: str | None = None) -> str:
    options: dict[str, Any] = {"prompt": prompt} if prompt else {}
    payload = await sdk.extract(url, schema_, **options)
    return json.dumps(payload, ensure_ascii=False)


async def _search(query: str, max_results: int = 10) -> str:
    payload = await sdk.search(query, max_results=max_results)
    return json.dumps(payload, ensure_ascii=False)


def build_tools() -> list[Any]:
    """构造 LangChain 工具列表；返回 JSON 字符串，与 MCP 载荷一致。"""
    try:
        from langchain_core.tools import StructuredTool
    except ImportError as exc:
        raise RuntimeError(
            "使用 LangChain 适配需要可选依赖，请先 pip install 'mcp-scrape[langchain]'"
        ) from exc

    return [
        StructuredTool.from_function(
            coroutine=_fetch,
            name="scrape_web_fetch",
            description="抓取单个网页并按 compact 规则精简正文，返回 JSON 字符串。",
            args_schema=FetchInput,
        ),
        StructuredTool.from_function(
            coroutine=_extract,
            name="scrape_web_extract",
            description="按字段 schema 从网页抽取结构化数据，返回 JSON 字符串。",
            args_schema=ExtractInput,
        ),
        StructuredTool.from_function(
            coroutine=_search,
            name="scrape_web_search",
            description="搜索关键词并返回结构化结果（标题、URL、摘要），返回 JSON 字符串。",
            args_schema=SearchInput,
        ),
    ]
