"""场景 1：把一篇长文压成 token 预算内的正文。

用法：
    python examples/01_fetch_article.py
    python examples/01_fetch_article.py https://ruanyifeng.com/blog/ --max-tokens 1200 --out article.md

不加 --out 时只打印前 600 字符，便于快速确认正文质量。
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from scrape_mcp.sdk import fetch

DEFAULT_URL = "https://docs.python.org/3/library/asyncio-task.html"


async def main() -> int:
    parser = argparse.ArgumentParser(description="抓取单页正文（compact markdown）")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="目标网页 URL")
    parser.add_argument("--max-tokens", type=int, default=2000, help="正文 token 预算")
    parser.add_argument("--out", help="把正文写入该文件；缺省只打印摘要")
    args = parser.parse_args()

    page = await fetch(args.url, max_tokens=args.max_tokens)
    if not page.get("ok"):
        print(f"抓取失败: {page.get('error_code')} {page.get('error')}")
        print(f"是否值得重试: {page.get('retryable')}")
        return 1

    content = page.get("content") or ""
    heading = next((line for line in content.splitlines() if line.startswith("#")), "(无标题)")
    print(f"URL     : {page['final_url']}")
    print(f"层级    : {page['tier']} | 状态: {page['status']} | 耗时: {page['elapsed_ms']}ms")
    print(f"标题    : {heading.lstrip('# ').strip()}")
    print(
        f"正文    : {page['token_estimate']} tokens / {len(content)} 字符（截断: {page['truncated']}）"
    )

    if args.out:
        Path(args.out).write_text(content, encoding="utf-8")
        print(f"已写入  : {args.out}")
    else:
        print("-" * 60)
        print(content[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
