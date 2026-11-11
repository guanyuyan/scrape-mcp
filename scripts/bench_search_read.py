"""搜索即阅读基准：对比「搜索 + 逐个 web_fetch」与「web_search(read=true)」。

用法：
    python scripts/bench_search_read.py                          # 默认查询，top-3
    python scripts/bench_search_read.py --query "async python" --top-k 5
    python scripts/bench_search_read.py --json                   # 机器可读输出

两条路径使用同一批结果（同一查询、同样条数、同样 token 预算）：
  A. search(max_results=top_k) 后顺序 fetch 每条 URL（各自 max_tokens=max_tokens）
  B. search(max_results=top_k, read=True, top_k=top_k, max_tokens=max_tokens*top_k)

B 的预算按"整批"给，等价于 A 每条各给 max_tokens。输出对比耗时与 token 总量。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from scrape_mcp.sdk import aclose, fetch, search

DEFAULT_QUERY = "python asyncio tutorial"


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(p * (len(ordered) - 1))))
    return ordered[idx]


def _summary(values: list[float]) -> dict:
    return {
        "p50_ms": round(_pct(values, 0.50), 1),
        "p95": round(_pct(values, 0.95), 1),
        "min_ms": round(min(values), 1) if values else 0.0,
    }


async def run_sequential(query: str, top_k: int, max_tokens: int, options: dict) -> dict:
    """A：先搜索，再顺序抓取每条结果的正文。"""
    started = time.perf_counter()
    found = await search(query, max_results=top_k, **options)
    if not found.get("ok"):
        return {"ok": False, "stage": "search", "error_code": found.get("error_code")}

    hits = found.get("results", [])[:top_k]
    tokens = 0
    read = 0
    failed = 0
    for hit in hits:
        payload = await fetch(hit["url"], max_tokens=max_tokens)
        if payload.get("ok"):
            read += 1
            tokens += int(payload.get("token_estimate") or 0)
        else:
            failed += 1
    return {
        "ok": True,
        "mode": "search_then_fetch",
        "results": len(hits),
        "read": read,
        "failed": failed,
        "tokens": tokens,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


async def run_read(query: str, top_k: int, max_tokens: int, options: dict) -> dict:
    """B：一次 read=true 调用完成搜索与阅读。"""
    started = time.perf_counter()
    payload = await search(
        query,
        max_results=top_k,
        read=True,
        top_k=top_k,
        max_tokens=max_tokens * top_k,
        **options,
    )
    elapsed = (time.perf_counter() - started) * 1000
    if not payload.get("ok"):
        return {"ok": False, "stage": "search", "error_code": payload.get("error_code")}
    meta = payload.get("tokens") or {}
    hits = payload.get("results", [])[:top_k]
    return {
        "ok": True,
        "mode": "search_read",
        "results": len(hits),
        "read": int(meta.get("results_read") or 0),
        "failed": int(meta.get("results_skipped") or 0),
        "tokens": int(meta.get("used") or 0),
        "elapsed_ms": round(elapsed, 1),
    }


def _print_compare(a: dict, b: dict) -> None:
    print(f"{'路径':<20}{'耗时':>10}{'tokens':>10}{'读到':>8}{'失败':>6}")
    print("-" * 56)
    for row in (a, b):
        if not row["ok"]:
            print(f"{row.get('mode', '?'):<20}  失败 {row.get('error_code')} ({row['stage']})")
            continue
        print(
            f"{row['mode']:<20}{row['elapsed_ms']:>9}ms{row['tokens']:>10}"
            f"{row['read']:>8}{row['failed']:>6}"
        )
    if a["ok"] and b["ok"]:
        if a["tokens"] and b["tokens"]:
            saved = (1 - b["tokens"] / a["tokens"]) * 100
            print(f"\ntoken 变化: {saved:+.1f}%（正数=read=true 更省）")
        if a["elapsed_ms"] and b["elapsed_ms"]:
            speedup = a["elapsed_ms"] / max(b["elapsed_ms"], 0.1)
            print(f"耗时加速: {speedup:.2f}x")


async def main() -> int:
    parser = argparse.ArgumentParser(description="搜索即阅读基准")
    parser.add_argument("--query", default=DEFAULT_QUERY, help=f"查询词（默认 {DEFAULT_QUERY}）")
    parser.add_argument("--top-k", type=int, default=3, help="读取的结果条数 1~10（默认 3）")
    parser.add_argument("--max-tokens", type=int, default=4000, help="每条正文的 token 预算")
    parser.add_argument("--provider", default=None, help="搜索后端，默认取配置")
    parser.add_argument("--site", default=None, help="限定站点（site: 运算符）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    top_k = max(1, min(10, args.top_k))
    options: dict = {}
    if args.provider:
        options["provider"] = args.provider
    if args.site:
        options["site"] = args.site

    try:
        sequential = await run_sequential(args.query, top_k, args.max_tokens, options)
        read = await run_read(args.query, top_k, args.max_tokens, options)
    finally:
        await aclose()

    if args.json:
        print(
            json.dumps(
                {"query": args.query, "top_k": top_k, "sequential": sequential, "read": read},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"query={args.query!r} top_k={top_k} max_tokens={args.max_tokens}/条\n")
        _print_compare(sequential, read)

    return 0 if sequential.get("ok") and read.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
