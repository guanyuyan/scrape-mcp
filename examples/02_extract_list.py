"""场景 2：列表页只要数据，不要整页正文。

用 web_extract 按字段规则抽取，直接拿到结构化 JSON；示例目标是 GitHub Trending，
把每条仓库抽成 name / url / stars，也可换成任意列表页改 SCHEMA 即可。

用法：
    python examples/02_extract_list.py
    python examples/02_extract_list.py <列表页 URL> --json
    python examples/02_extract_list.py <列表页 URL> --csv repos.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from pathlib import Path

from scrape_mcp.sdk import extract

DEFAULT_URL = "https://github.com/trending"

# 字段规则：type=list 表示按 item_selector 收集数组；regex + group 从 href 取 owner/repo
SCHEMA = {
    "fields": {
        "page_title": {"selector": "h1"},
        "repos": {
            "type": "list",
            "selector": "article.Box-row",
            "fields": {
                "name": {
                    "selector": "h2 a",
                    "type": "attr",
                    "attr": "href",
                    "regex": "(?:https?://[^/]+)?/([^/?]+/[^/?]+)/?$",
                    "group": 1,
                },
                "url": {"selector": "h2 a", "type": "attr", "attr": "href", "abs_url": True},
                "stars": {"selector": "span.d-inline-block.float-sm-right"},
            },
        },
    }
}


async def main() -> int:
    parser = argparse.ArgumentParser(description="按字段规则抽取列表页数据")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="列表页 URL")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON（缺省打印表格）")
    parser.add_argument("--csv", help="把列表字段写入 CSV 文件")
    args = parser.parse_args()

    result = await extract(args.url, SCHEMA)
    if not result.get("ok"):
        print(f"抽取失败: {result.get('error_code')} {result.get('error')}")
        return 1

    data = result.get("data") or {}
    repos = data.get("repos") or []
    print(f"标题: {data.get('page_title') or '(空)'}")
    print(
        f"层级: {result['tier']} | 条目: {len(repos)} | 缺失字段: {len(result.get('missing') or [])}"
    )

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    for repo in repos[:10]:
        print(f"  {repo.get('name') or '-':<45} {repo.get('stars') or ''}")

    if args.csv:
        path = Path(args.csv)
        rows = [
            {"name": r.get("name"), "url": r.get("url"), "stars": r.get("stars")} for r in repos
        ]
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["name", "url", "stars"])
            writer.writeheader()
            writer.writerows(rows)
        print(f"已写入 {path}（{len(rows)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
