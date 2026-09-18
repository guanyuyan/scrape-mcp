"""MCP 客户端示例：调用 web_fetch / web_batch / web_extract。

用法（stdout 走 stdio，由本脚本以子进程方式拉起服务）：
    python examples/example_client.py

返回均为 JSON 字符串，这里演示如何解析 content[0].text。
"""

from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters, stdio_client


async def call(session: ClientSession, name: str, arguments: dict) -> dict:
    res = await session.call_tool(name, arguments)
    # 工具统一返回 JSON 字符串，落在 content[0].text
    text = res.content[0].text
    return json.loads(text)


async def demo_fetch(session: ClientSession) -> None:
    """web_fetch：拿到极省 token 的正文。"""
    r = await call(session, "web_fetch", {"url": "https://developer.mozilla.org/zh-CN/docs/Web/HTTP/CORS"})
    print("=== web_fetch ===")
    print("ok:", r.get("ok"), "| tier:", r.get("tier"), "| tokens:", r.get("token_estimate"))
    print((r.get("content") or "")[:200])


async def demo_batch(session: ClientSession) -> None:
    """web_batch：并发抓一批 URL。"""
    r = await call(
        session,
        "web_batch",
        {"urls": ["https://developer.mozilla.org/zh-CN/docs/Web/HTTP/CORS", "https://developer.mozilla.org/zh-CN/docs/Web/HTTP"]},
    )
    print("=== web_batch ===")
    print("total:", r.get("total"), "| ok_count:", r.get("ok_count"))


async def demo_extract(session: ClientSession) -> None:
    """web_extract：按字段 schema 抽结构化 JSON（适合要数据而非整页正文的场景）。

    字段类型：text(默认) / attr(取属性) / count(计数) / list(取数组)。
    """
    schema = {
        "title": {"selector": "h1", "type": "text"},
        "h2_count": {"selector": "h2", "type": "count"},
    }
    r = await call(
        session,
        "web_extract",
        {"url": "https://developer.mozilla.org/zh-CN/docs/Web/HTTP/CORS", "schema": schema},
    )
    print("=== web_extract ===")
    print("ok:", r.get("ok"), "| data:", json.dumps(r.get("data"), ensure_ascii=False))


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,  # 复用当前解释器（需已安装依赖，如 .venv）
        args=["-m", "scrape_mcp.server"],
        env={"PYTHONPATH": "src", "SCRAPE_MCP_TRANSPORT": "stdio"},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        await demo_fetch(session)
        await demo_batch(session)
        await demo_extract(session)


if __name__ == "__main__":
    asyncio.run(main())
