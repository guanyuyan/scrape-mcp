"""MCP 客户端示例：调用 web_fetch / web_batch。

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


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,  # 复用当前解释器（需已安装依赖，如 .venv）
        args=["-m", "scrape_mcp.server"],
        env={"PYTHONPATH": "src", "SCRAPE_MCP_TRANSPORT": "stdio"},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        # 单页抓取
        r = await call(session, "web_fetch", {"url": "https://example.com"})
        print("=== web_fetch ===")
        print("ok:", r.get("ok"), "| tier:", r.get("tier"), "| tokens:", r.get("token_estimate"))
        print((r.get("content") or "")[:200])

        # 批量抓取
        r = await call(
            session,
            "web_batch",
            {"urls": ["https://example.com", "https://developer.mozilla.org"]},
        )
        print("=== web_batch ===")
        print("total:", r.get("total"), "| ok_count:", r.get("ok_count"))


if __name__ == "__main__":
    asyncio.run(main())
