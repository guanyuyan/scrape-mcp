"""基准套件：量化抓取延迟、正文缓存收益与 L2 冷启动开销。

用法：
    python scripts/bench.py                                  # 默认两个公开文档页
    python scripts/bench.py --url https://x.test --rounds 5
    python scripts/bench.py --l2 https://news.qq.com/        # 额外测 L2 冷启动/复用
    python scripts/bench.py --json                           # 机器可读输出

"冷" = `bypass_cache=True`（跳过两层缓存，强制走网络 + 解析）
"热" = 普通调用（命中正文缓存，跳过网络与精简）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from scrape_mcp.config import get_settings
from scrape_mcp.sdk import aclose, fetch

DEFAULT_URLS = [
    "https://example.com",
    "https://docs.python.org/3/library/asyncio-task.html",
]


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(p * (len(ordered) - 1))))
    return ordered[idx]


def _summary(values: list[float]) -> dict:
    return {
        "p50_ms": round(_pct(values, 0.50), 1),
        "p95_ms": round(_pct(values, 0.95), 1),
        "min_ms": round(min(values), 1) if values else 0.0,
    }


async def _timed(**kwargs) -> tuple[dict, float]:
    started = time.perf_counter()
    payload = await fetch(**kwargs)
    return payload, (time.perf_counter() - started) * 1000


async def bench_url(url: str, rounds: int, max_tokens: int) -> dict:
    cold: list[float] = []
    tokens = 0
    tier = ""
    for _ in range(rounds):
        payload, ms = await _timed(url=url, max_tokens=max_tokens, bypass_cache=True)
        if not payload.get("ok"):
            return {
                "url": url,
                "ok": False,
                "error_code": payload.get("error_code"),
                "error": payload.get("error"),
            }
        cold.append(ms)
        tokens = payload.get("token_estimate", 0)
        tier = payload.get("tier", "")

    # 先正常抓一次把结果缓存写下来，再计时热调用
    await fetch(url=url, max_tokens=max_tokens)
    warm: list[float] = []
    for _ in range(rounds):
        payload, ms = await _timed(url=url, max_tokens=max_tokens)
        warm.append(ms)

    row = {"url": url, "ok": True, "tier": tier, "tokens": tokens, "cold": _summary(cold)}
    if get_settings().cache_ttl > 0:
        row["warm"] = _summary(warm)
        row["speedup"] = round(_pct(cold, 0.50) / max(_pct(warm, 0.50), 0.1), 1)
    else:
        row["warm"] = None
        row["speedup"] = None
        row["note"] = "CACHE_TTL=0，热调用未命中结果缓存"
    return row


async def bench_l2(url: str, rounds: int, max_tokens: int) -> dict:
    """L2 的第一次调用包含浏览器启动，后续复用同一实例；两次都强制走网络。"""
    payload, cold_ms = await _timed(url=url, max_tokens=max_tokens, bypass_cache=True)
    if not payload.get("ok"):
        return {
            "url": url,
            "ok": False,
            "error_code": payload.get("error_code"),
            "error": payload.get("error"),
        }
    warm: list[float] = []
    for _ in range(rounds):
        _, ms = await _timed(url=url, max_tokens=max_tokens, bypass_cache=True)
        warm.append(ms)
    row = {
        "url": url,
        "ok": True,
        "tier": payload.get("tier"),
        "cold_ms": round(cold_ms, 1),
        "warm": _summary(warm),
    }
    if row["tier"] != "l2":
        row["note"] = "该页由 L1 完成，未触发浏览器，本次数据不反映 L2 开销"
    return row


def _short(url: str, width: int = 48) -> str:
    return url if len(url) <= width else url[: width - 3] + "..."


def _print_table(rows: list[dict]) -> None:
    header = f"{'URL':<50}{'tier':<6}{'cold p50':>10}{'warm p50':>10}{'加速':>8}{'tokens':>8}"
    print(header)
    print("-" * len(header))
    for row in rows:
        if not row["ok"]:
            print(f"{_short(row['url']):<50}  失败 {row.get('error_code')}: {row.get('error')}")
            continue
        warm = row.get("warm") or {}
        warm_p50 = f"{warm.get('p50_ms')}ms" if warm else "-"
        speedup = f"{row.get('speedup')}x" if row.get("speedup") else "-"
        print(
            f"{_short(row['url']):<50}{row.get('tier', '-'):<6}"
            f"{row['cold']['p50_ms']:>9}ms{warm_p50:>10}{speedup:>8}{row['tokens']:>8}"
        )


def _print_l2(rows: list[dict]) -> None:
    print("\nL2（浏览器）冷启动 / 复用")
    print("  注：同进程里若先跑过 L2 页面，“首次”不包含浏览器启动开销；")
    print("      要测真实冷启动，单独执行 python scripts/bench.py --url <L2 页面>。")
    for row in rows:
        if not row["ok"]:
            print(f"  {row['url']}: 失败 {row.get('error_code')} {row.get('error')}")
            continue
        print(
            f"  {row['url']} tier={row['tier']} 首次={row['cold_ms']}ms "
            f"复用 p50={row['warm']['p50_ms']}ms p95={row['warm']['p95_ms']}ms"
        )
        if row.get("note"):
            print(f"    注意: {row['note']}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="scrape-mcp 抓取基准")
    parser.add_argument("--url", action="append", help="被测 URL，可重复；默认两个文档页")
    parser.add_argument("--rounds", type=int, default=3, help="每个指标重复次数（默认 3）")
    parser.add_argument("--max-tokens", type=int, default=4000, help="正文 token 预算")
    parser.add_argument("--l2", action="append", default=[], help="额外测 L2 冷启动的 URL")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    urls = args.url or DEFAULT_URLS
    rounds = max(1, args.rounds)
    try:
        rows = [await bench_url(url, rounds, args.max_tokens) for url in urls]
        l2_rows = [await bench_l2(url, rounds, args.max_tokens) for url in args.l2]
    finally:
        await aclose()

    failed = [r for r in rows + l2_rows if not r["ok"]]
    if args.json:
        print(json.dumps({"fetch": rows, "l2": l2_rows}, ensure_ascii=False, indent=2))
    else:
        print(f"rounds={rounds} max_tokens={args.max_tokens}\n")
        _print_table(rows)
        if l2_rows:
            _print_l2(l2_rows)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
