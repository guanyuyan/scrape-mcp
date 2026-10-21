"""场景 3：沿"下一页"把一个栏目的前 N 页读进来，合并成一份 markdown。

两个要点：
  1. 顺序翻页（下一页地址依赖当前页），失败页不影响后续；
  2. 每页独立 token 预算，最后按页拼接写文件。

用法：
    python examples/03_crawl_docs.py
    python examples/03_crawl_docs.py <起始 URL> --selector 'a.next' --max-pages 5 --out docs.md
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from scrape_mcp.sdk import crawl

# Sphinx 文档站的"下一页"链接约定：带 accesskey=N
DEFAULT_URL = "https://docs.python.org/3/library/asyncio.html"
DEFAULT_SELECTOR = 'div.related a[accesskey="N"]'


async def main() -> int:
    parser = argparse.ArgumentParser(description="按下一页顺序翻页抓取并合并")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="起始页 URL")
    parser.add_argument("--selector", default=DEFAULT_SELECTOR, help="指向下一页的 CSS 选择器")
    parser.add_argument("--max-pages", type=int, default=3, help="最多翻多少页")
    parser.add_argument("--max-tokens", type=int, default=800, help="每页正文 token 预算")
    parser.add_argument("--out", default="crawl.md", help="合并结果写入的文件")
    args = parser.parse_args()

    result = await crawl(
        args.url,
        next_selector=args.selector,
        max_pages=args.max_pages,
        max_tokens=args.max_tokens,
    )
    if not result.get("ok"):
        print(f"爬取失败: {result.get('error_code')} {result.get('error')}")
        return 1

    pages = result.get("results") or []
    print(
        f"模式: {result['mode']} | 抓取: {result['pages_fetched']} 页 | 成功: {result['ok_count']}"
    )
    for index, page in enumerate(pages, 1):
        if page.get("ok"):
            print(f"  {index}. {page['url']}（{page['token_estimate']} tokens）")
        else:
            print(f"  {index}. {page['url']} 失败: {page['error_code']}")

    parts = [
        f"<!-- 第 {i} 页 -->\n\n{page.get('content') or ''}"
        for i, page in enumerate(pages, 1)
        if page.get("ok")
    ]
    text = f"# 抓取自 {args.url}\n\n" + "\n\n---\n\n".join(parts) + "\n"
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"已合并写入 {args.out}（{len(parts)} 页）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
