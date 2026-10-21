"""场景 4：PDF 直接读成文本（需要可选依赖）。

先安装：pip install "mcp-scrape[pdf]"
未安装时调用会返回 unsupported_content，并提示安装命令。

PDF 走 L1 直取字节后本地解析，不渲染、不升级浏览器；扫描件没有文字层时返回 empty。

用法：
    python examples/04_pdf_to_text.py
    python examples/04_pdf_to_text.py <PDF URL> --out paper.txt --max-tokens 20000
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from scrape_mcp.sdk import fetch

DEFAULT_URL = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"


async def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 PDF 并抽取文本")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="PDF URL")
    parser.add_argument("--max-tokens", type=int, default=8000, help="文本 token 预算")
    parser.add_argument("--out", default="pdf.txt", help="文本写入的文件")
    args = parser.parse_args()

    page = await fetch(args.url, max_tokens=args.max_tokens)
    if not page.get("ok"):
        code = page.get("error_code")
        print(f"抽取失败: {code} {page.get('error')}")
        if code == "unsupported_content":
            print('提示: 执行 pip install "mcp-scrape[pdf]" 后重试')
        elif code == "empty":
            print("提示: 该 PDF 没有文字层（扫描件），需要 OCR")
        return 1

    if page.get("kind") != "pdf":
        print(f"该地址返回的不是 PDF（kind={page.get('kind')}），请改用 01 示例")
        return 1

    text = page.get("content") or ""
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"URL    : {page['url']}")
    print(f"页数   : {page.get('pages')}（解析到文字: {page.get('pages_parsed')}）")
    print(
        f"文本   : {page['token_estimate']} tokens / {len(text)} 字符（截断: {page['truncated']}）"
    )
    print(f"已写入 : {args.out}")
    head = "\n".join(text.splitlines()[:3])
    if head.strip():
        print("-" * 60)
        print(head)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
