"""一键演示：真实站点上跑 web_fetch 与 web_extract，3 分钟看到效果。

用法（免手工起服务，stdout 走 stdio 子进程拉起）：
    python scripts/demo.py

演示内容：
    1. web_fetch  对文档站/门户页压缩，打印压缩比与 tier
    2. web_extract 按 schema 抽取字段 JSON
    3. 覆盖 文章页 / 文档站 / 列表页 三类代表性站点

依赖：项目依赖（mcp）已安装；复用 examples 的 stdio 消费方式。
"""

from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters, stdio_client

# 可按需替换为你想演示的真实页面。默认用国内直连可达的站点
DEMO_PAGES = [
    {
        "label": "文档站",
        "kind": "web_fetch",
        "url": "https://developer.mozilla.org/zh-CN/docs/Web/HTTP/CORS",
    },
    {
        "label": "门户/文章页",
        "kind": "web_fetch",
        "url": "https://www.geekbang.org/",
    },
    {
        "label": "列表页",
        "kind": "web_fetch",
        "url": "https://news.qq.com/",
    },
]


async def call(session: ClientSession, name: str, arguments: dict) -> dict:
    res = await session.call_tool(name, arguments)
    return json.loads(res.content[0].text)


async def run_fetch(session, url: str) -> None:
    r = await call(session, "web_fetch", {"url": url, "max_tokens": 4000})
    if not r.get("ok"):
        print(f"  [错误] {r.get('error')} blocked={r.get('blocked')}")
        return
    ratio = 0.0
    if r.get("html_bytes"):
        ratio = 1.0 * r.get("token_estimate", 0) * 4 / r["html_bytes"]
    print(
        f"  ok={r.get('ok')} tier={r.get('tier')} status={r.get('status')} "
        f"html={r.get('html_bytes')}B → tokens={r.get('token_estimate')} (约 {ratio:.1%} 压缩)"
    )
    title = (r.get("title") or "").strip()
    if title:
        print(f"  标题: {title}")
    snippet = (r.get("content") or "").replace("\n", " ").strip()
    print(f"  正文预览: {snippet[:90]}...")


async def run_extract(session, url: str) -> None:
    schema = {
        "fields": {
            "heading": {"selector": "h1", "type": "text"},
            "h2_count": {"selector": "h2", "type": "count"},
            "links": {"selector": "a", "type": "count"},
        }
    }
    r = await call(session, "web_extract", {"url": url, "schema": schema})
    if not r.get("ok"):
        print(f"  [错误] {r.get('error')}")
        return
    print(f"  ok={r.get('ok')} tier={r.get('tier')} data={json.dumps(r.get('data'), ensure_ascii=False)}")


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "scrape_mcp.server"],
        env={"PYTHONPATH": "src", "SCRAPE_MCP_TRANSPORT": "stdio"},  # stdio，无需手工起 HTTP 服务
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        for page in DEMO_PAGES:
            print(f"\n=== {page['label']} | {page['url']}")
            await run_fetch(session, page["url"])
            # 顺便演示 web_extract（同一页面）
            await run_extract(session, page["url"])


if __name__ == "__main__":
    asyncio.run(main())
