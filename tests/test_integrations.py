"""生态适配：LangChain 工具与 LlamaIndex Reader 的构造与载荷转换。"""

from __future__ import annotations

import asyncio
import builtins
import json
import sys
import types

import pytest

from scrape_mcp import sdk
from scrape_mcp.integrations import langchain as lc
from scrape_mcp.integrations import llamaindex as li


class _Tool:
    def __init__(self, coroutine, name, description, args_schema) -> None:
        self.coroutine = coroutine
        self.name = name
        self.description = description
        self.args_schema = args_schema


def _install_fake_langchain(monkeypatch) -> None:
    core = types.ModuleType("langchain_core")
    tools = types.ModuleType("langchain_core.tools")

    class StructuredTool:
        @classmethod
        def from_function(cls, *, coroutine, name, description, args_schema):
            return _Tool(coroutine, name, description, args_schema)

    tools.StructuredTool = StructuredTool
    core.tools = tools
    monkeypatch.setitem(sys.modules, "langchain_core", core)
    monkeypatch.setitem(sys.modules, "langchain_core.tools", tools)


def _install_fake_llamaindex(monkeypatch) -> None:
    root = types.ModuleType("llama_index")
    core = types.ModuleType("llama_index.core")
    readers = types.ModuleType("llama_index.core.readers")
    base = types.ModuleType("llama_index.core.readers.base")
    schema = types.ModuleType("llama_index.core.schema")

    class BaseReader:
        pass

    class Document:
        def __init__(self, text: str = "", metadata: dict | None = None) -> None:
            self.text = text
            self.metadata = metadata or {}

    base.BaseReader = BaseReader
    schema.Document = Document
    core.readers = readers
    core.schema = schema
    root.core = core
    readers.base = base
    for name, module in {
        "llama_index": root,
        "llama_index.core": core,
        "llama_index.core.readers": readers,
        "llama_index.core.readers.base": base,
        "llama_index.core.schema": schema,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


def test_langchain_build_tools_and_payloads(monkeypatch):
    _install_fake_langchain(monkeypatch)
    tools = lc.build_tools()

    assert [tool.name for tool in tools] == [
        "scrape_web_fetch",
        "scrape_web_extract",
        "scrape_web_search",
    ]
    assert lc.ExtractInput.model_json_schema()["properties"].keys() == {
        "url",
        "schema",
        "prompt",
    }

    async def fake_fetch(url: str, **options):
        return {"ok": True, "url": url, "content": "正文", "max_tokens": options}

    monkeypatch.setattr(sdk, "fetch", fake_fetch)
    payload = json.loads(asyncio.run(tools[0].coroutine("https://example.com")))
    assert payload["content"] == "正文"

    async def fake_extract(url: str, schema: dict, **options):
        return {"ok": True, "url": url, "data": schema}

    monkeypatch.setattr(sdk, "extract", fake_extract)
    result = json.loads(asyncio.run(tools[1].coroutine("https://example.com", schema_={"a": 1})))
    assert result["data"] == {"a": 1}


def test_langchain_build_tools_hint_when_missing(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("langchain_core"):
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match=r"mcp-scrape\[langchain\]"):
        lc.build_tools()


def test_document_kwargs_skips_failures_and_empty_metadata():
    assert li.document_kwargs({"ok": False, "error_code": "blocked"}) is None

    kwargs = li.document_kwargs({"ok": True, "url": "https://a.test", "content": "hi"})
    assert kwargs == {"text": "hi", "metadata": {"url": "https://a.test"}}

    assert li.document_kwargs({"ok": True, "content": None}) == {
        "text": "",
        "metadata": {"url": ""},
    }


def test_llamaindex_reader_skips_failed_pages(monkeypatch):
    _install_fake_llamaindex(monkeypatch)
    reader = li.build_reader()

    async def fake_fetch(url: str, **options):
        if "bad" in url:
            return {"ok": False, "url": url, "error_code": "blocked"}
        return {"ok": True, "url": url, "content": f"正文 {url}", "title": "标题"}

    monkeypatch.setattr(sdk, "fetch", fake_fetch)
    documents = asyncio.run(reader.aload_data(["https://a.test", "https://bad.test"]))

    assert len(documents) == 1
    assert documents[0].text == "正文 https://a.test"
    assert documents[0].metadata["title"] == "标题"


def test_llamaindex_reader_sync_wrapper(monkeypatch):
    _install_fake_llamaindex(monkeypatch)
    reader = li.build_reader()

    async def fake_fetch(url: str, **options):
        return {"ok": True, "url": url, "content": "正文"}

    monkeypatch.setattr(sdk, "fetch", fake_fetch)
    documents = reader.load_data(["https://a.test"])

    assert documents[0].text == "正文"


def test_llamaindex_reader_hint_when_missing(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("llama_index"):
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match=r"mcp-scrape\[llamaindex\]"):
        li.build_reader()
