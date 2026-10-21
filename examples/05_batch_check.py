"""场景 5：批量巡检一组 URL，输出每条的层级 / 状态 / token / 错误码。

适合定时任务或发版前的真实站点回归：单条失败不影响整批，最后按失败数返回退出码。

用法：
    python examples/05_batch_check.py
    python examples/05_batch_check.py https://example.com https://docs.python.org/3/
"""

from __future__ import annotations

import argparse
import asyncio

from scrape_mcp.sdk import aclose, batch

DEFAULT_URLS = [
    "https://example.com",
    "https://docs.python.org/3/library/asyncio.html",
    "https://github.com/trending",
]


async def main() -> int:
    parser = argparse.ArgumentParser(description="批量抓取并输出巡检摘要")
    parser.add_argument("urls", nargs="*", default=DEFAULT_URLS, help="待检查的 URL 列表")
    parser.add_argument("--max-tokens", type=int, default=300, help="每条正文 token 预算")
    args = parser.parse_args()

    result = await batch(args.urls, max_tokens=args.max_tokens)
    if not result.get("ok"):
        print(f"批量调用失败: {result.get('error_code')} {result.get('error')}")
        return 1

    print(f"{'URL':<52} {'结果':<6} {'层级':<4} {'状态':<5} {'tokens':>6}  错误码")
    for item in result.get("results") or []:
        url = (item.get("url") or "")[:50]
        ok = "OK" if item.get("ok") else "FAIL"
        print(
            f"{url:<52} {ok:<6} {item.get('tier') or '-':<4} "
            f"{item.get('status') or '-'!s:<5} {item.get('token_estimate') or 0:>6}  "
            f"{item.get('error_code') or ''}"
        )

    failed = result["total"] - result["ok_count"]
    print(f"\n共 {result['total']} 条，成功 {result['ok_count']}，失败 {failed}")
    # 本批可能触发过 L2 浏览器；退出前释放，避免子进程清理告警
    await aclose()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
