"""真实站点质量基准：成功率、正文完整度、token 与延迟。

与 `bench.py`（冷热缓存加速）互补：这里回答"在真实站点上取到的内容对不对、全不全、贵不贵"。

用法：
    python scripts/bench_sites.py                          # 跑全部站点
    python scripts/bench_sites.py --nightly                # 只跑夜间回归子集
    python scripts/bench_sites.py --category docs,list     # 按分类过滤
    python scripts/bench_sites.py --json out.json --md out.md
    python scripts/bench_sites.py --baseline baseline.json # 与上次结果对比
    python scripts/bench_sites.py --reference trafilatura  # 额外对照纯正文抽取器（需自行安装）

指标口径：
    - 期望：expect=ok 的站点应取到正文；expect 为错误码的站点应被如实拦截。
    - 完整度：站点配置的 checks 命中比例（正文中必须出现的稳定片段）。
    - 通过：结果符合期望，且正文字符数达标、全部 checks 命中。
    - 延迟：单次冷调用（bypass_cache）的 elapsed_ms，不含结果缓存命中。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scrape_mcp import __version__
from scrape_mcp.config import get_settings
from scrape_mcp.sdk import aclose, fetch
from scrape_mcp.tokenizer import count_tokens

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SITES = ROOT / "benchmarks" / "sites.json"

CATEGORY_LABELS = {
    "docs": "文档站",
    "blog": "博客",
    "news": "资讯",
    "list": "列表页",
    "spa": "前端渲染",
    "anti_bot": "登录墙/反爬",
    "pdf": "PDF",
}


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(p * (len(ordered) - 1))))
    return ordered[idx]


def load_sites(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    sites = data.get("sites") or []
    if not sites:
        raise SystemExit(f"站点集为空: {path}")

    seen: set[str] = set()
    for site in sites:
        sid = site.get("id", "")
        if not sid:
            raise SystemExit(f"站点缺少 id: {site}")
        if sid in seen:
            raise SystemExit(f"站点 id 重复: {sid}")
        seen.add(sid)
        if not str(site.get("url", "")).startswith(("http://", "https://")):
            raise SystemExit(f"{sid} 的 url 不合法: {site.get('url')}")
        if site.get("category") not in CATEGORY_LABELS:
            raise SystemExit(f"{sid} 的 category 不合法: {site.get('category')}")
        if not isinstance(site.get("checks", []), list):
            raise SystemExit(f"{sid} 的 checks 必须是数组")
    return sites


def select_sites(
    sites: list[dict[str, Any]],
    categories: list[str] | None,
    only: list[str] | None,
    nightly: bool,
) -> list[dict[str, Any]]:
    picked = sites
    if categories:
        wanted = {c.strip() for c in categories if c.strip()}
        picked = [s for s in picked if s["category"] in wanted]
    if only:
        wanted_ids = {i.strip() for i in only if i.strip()}
        picked = [s for s in picked if s["id"] in wanted_ids]
    if nightly:
        picked = [s for s in picked if s.get("nightly")]
    return picked


def _missing_checks(content: str, checks: list[str]) -> list[str]:
    folded = content.casefold()
    return [c for c in checks if c.casefold() not in folded]


def _expect_codes(expect: str | list[str]) -> list[str]:
    """把站点期望归一化成候选结果集合；列表表示任一结果都算符合预期。"""
    return list(expect) if isinstance(expect, list) else [expect]


async def run_site(site: dict[str, Any], max_tokens: int, deadline_ms: int) -> dict[str, Any]:
    started = time.perf_counter()
    payload = await fetch(
        url=site["url"],
        max_tokens=max_tokens,
        bypass_cache=True,
        deadline_ms=deadline_ms,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    content = payload.get("content") or ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    ok = bool(payload.get("ok"))
    error_code = payload.get("error_code")
    expect = site.get("expect", "ok")
    # expect 可以是单个值，也可以是列表：某些站在不同出口表现不同（如澎湃首页在
    # GitHub Actions 出口被 WAF 403、在住宅出口正常取正文），列表内任一结果都算符合预期
    codes = _expect_codes(expect)
    matched = (ok and "ok" in codes) or (not ok and error_code in codes)

    checks = site.get("checks") or []
    missing = _missing_checks(content, checks) if ok else list(checks)
    checks_hit = len(checks) - len(missing)
    chars = len(content)
    passed = matched and (not ok or chars >= int(site.get("min_chars", 0))) and not missing
    return {
        "id": site["id"],
        "url": site["url"],
        "category": site["category"],
        "expect": expect,
        "ok": ok,
        "matched": matched,
        "passed": passed,
        "tier": payload.get("tier"),
        "kind": payload.get("kind"),
        "status": payload.get("status"),
        "error_code": error_code,
        "error": payload.get("error"),
        "blocked": payload.get("blocked"),
        "chars": chars,
        "tokens": int(payload.get("token_estimate") or 0),
        "html_bytes": payload.get("html_bytes"),
        "elapsed_ms": elapsed_ms,
        "payload_ms": payload.get("elapsed_ms"),
        "checks_total": len(checks),
        "checks_hit": checks_hit,
        "missing_checks": missing,
        "truncated": bool(payload.get("truncated")),
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    expected_ok = [r for r in records if "ok" in _expect_codes(r["expect"])]
    expected_block = [r for r in records if "ok" not in _expect_codes(r["expect"])]
    fetched = [r for r in expected_ok if r["ok"]]
    intercepted = [r for r in expected_block if r["matched"]]
    latencies = [float(r["elapsed_ms"]) for r in fetched]
    checks_total = sum(r["checks_total"] for r in records)
    checks_hit = sum(r["checks_hit"] for r in records)

    by_category: dict[str, dict[str, Any]] = {}
    for cat, label in CATEGORY_LABELS.items():
        rows = [r for r in records if r["category"] == cat]
        if not rows:
            continue
        fetched_rows = [r for r in rows if r["ok"]]
        by_category[cat] = {
            "label": label,
            "sites": len(rows),
            "passed": sum(1 for r in rows if r["passed"]),
            "ok": len(fetched_rows),
            "median_chars": int(statistics.median([r["chars"] for r in fetched_rows]))
            if fetched_rows
            else 0,
            "median_tokens": int(statistics.median([r["tokens"] for r in fetched_rows]))
            if fetched_rows
            else 0,
        }

    tiers: dict[str, int] = {}
    for r in fetched:
        tier = r.get("tier") or "unknown"
        tiers[tier] = tiers.get(tier, 0) + 1

    return {
        "sites": len(records),
        "passed": sum(1 for r in records if r["passed"]),
        "pass_rate": round(sum(1 for r in records if r["passed"]) / len(records), 4)
        if records
        else 0.0,
        "expected_ok": len(expected_ok),
        "fetch_ok": len(fetched),
        "fetch_rate": round(len(fetched) / len(expected_ok), 4) if expected_ok else None,
        "expected_block": len(expected_block),
        "intercepted": len(intercepted),
        "intercept_rate": round(len(intercepted) / len(expected_block), 4)
        if expected_block
        else None,
        "check_total": checks_total,
        "check_hit": checks_hit,
        "check_rate": round(checks_hit / checks_total, 4) if checks_total else None,
        "tokens_total": sum(r["tokens"] for r in fetched),
        "chars_total": sum(r["chars"] for r in fetched),
        "latency_p50_ms": round(_pct(latencies, 0.50), 1),
        "latency_p95_ms": round(_pct(latencies, 0.95), 1),
        "tiers": tiers,
        "by_category": by_category,
    }


def _fmt_rate(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _fmt_status(record: dict[str, Any]) -> str:
    if record["passed"]:
        return "通过"
    if record["matched"] and not record["ok"]:
        return "按预期拦截"
    if record["ok"] and record["expect"] != "ok":
        return "未按预期拦截"
    if record["ok"]:
        return "内容不达标"
    return record["error_code"] or "失败"


def _fmt_checks(record: dict[str, Any]) -> str:
    if not record["checks_total"]:
        return "-"
    return f"{record['checks_hit']}/{record['checks_total']}"


def _short(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def print_reference(records: list[dict[str, Any]], reference: dict[str, dict[str, Any]]) -> None:
    rows = [r for r in records if r["ok"] and reference.get(r["id"], {}).get("tokens")]
    if not rows:
        return
    print("\n对照（trafilatura，纯正文）")
    for r in rows:
        ref = reference[r["id"]]
        print(f"  {r['id']}: 本工具 {r['tokens']} token / 对照 {ref['tokens']} token")


def print_console(records: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    header = (
        f"{'站点':<24}{'分类':<10}{'层级':<6}{'结果':<12}"
        f"{'字符':>8}{'token':>8}{'完整度':>8}{'耗时':>9}"
    )
    print(header)
    print("-" * len(header))
    for r in records:
        tier = r.get("tier") or "-"
        print(
            f"{_short(r['id'], 22):<24}{r['category']:<10}{tier:<6}{_fmt_status(r):<12}"
            f"{r['chars']:>8}{r['tokens']:>8}{_fmt_checks(r):>8}{r['elapsed_ms']:>8}ms"
        )
        if r["missing_checks"]:
            print(f"    缺少检查项: {', '.join(r['missing_checks'])}")

    print("\n汇总")
    print(
        f"  通过 {summary['passed']}/{summary['sites']}（{_fmt_rate(summary['pass_rate'])}）"
        f"；取正文 {summary['fetch_ok']}/{summary['expected_ok']}"
        f"（{_fmt_rate(summary['fetch_rate'])}）"
    )
    if summary["expected_block"]:
        print(
            f"  按预期拦截 {summary['intercepted']}/{summary['expected_block']}"
            f"（{_fmt_rate(summary['intercept_rate'])}）"
        )
    print(
        f"  完整度 {summary['check_hit']}/{summary['check_total']}"
        f"（{_fmt_rate(summary['check_rate'])}）"
        f"；延迟 p50={summary['latency_p50_ms']}ms p95={summary['latency_p95_ms']}ms"
    )
    tiers = ", ".join(f"{k}:{v}" for k, v in sorted(summary["tiers"].items())) or "-"
    print(f"  分层分布 {tiers}")
    for cat, row in summary["by_category"].items():
        print(
            f"  {row['label']}({cat}): {row['passed']}/{row['sites']} 通过"
            f"，中位字符 {row['median_chars']}，中位 token {row['median_tokens']}"
        )


def render_markdown(
    records: list[dict[str, Any]],
    summary: dict[str, Any],
    reference: dict[str, dict[str, Any]] | None = None,
) -> str:
    lines = [
        "### 真实站点质量基准",
        "",
        f"- 运行时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- 版本：scrape-mcp {__version__}",
        f"- 通过：{summary['passed']}/{summary['sites']}（{_fmt_rate(summary['pass_rate'])}）",
        f"- 取正文：{summary['fetch_ok']}/{summary['expected_ok']}"
        f"（{_fmt_rate(summary['fetch_rate'])}）",
        f"- 完整度：{summary['check_hit']}/{summary['check_total']}"
        f"（{_fmt_rate(summary['check_rate'])}）",
        f"- 延迟：p50 {summary['latency_p50_ms']} ms / p95 {summary['latency_p95_ms']} ms",
        f"- 分层分布：{', '.join(f'{k} {v}' for k, v in sorted(summary['tiers'].items())) or '-'}",
        "",
    ]
    if reference:
        lines += [
            "| 站点 | 本工具 token | 对照 token | 相对用量 |",
            "| --- | ---: | ---: | ---: |",
        ]
        for r in records:
            ref = reference.get(r["id"])
            if not ref or not r["ok"] or not ref["tokens"]:
                continue
            ratio = r["tokens"] / ref["tokens"]
            lines.append(f"| {r['id']} | {r['tokens']} | {ref['tokens']} | {ratio:.2f}× |")
        lines.append("")

    header = "| 站点 | 分类 | 层级 | 结果 | 字符 | token | 完整度 | 耗时 |"
    sep = "| --- | --- | --- | --- | ---: | ---: | ---: | ---: |"
    lines += [header, sep]
    for r in records:
        lines.append(
            f"| {r['id']} | {r['category']} | {r.get('tier') or '-'} | {_fmt_status(r)} "
            f"| {r['chars']} | {r['tokens']} | {_fmt_checks(r)} | {r['elapsed_ms']} ms |"
        )
    return "\n".join(lines) + "\n"


def compare_baseline(
    records: list[dict[str, Any]], baseline: dict[str, Any]
) -> list[dict[str, Any]]:
    previous = {r["id"]: r for r in baseline.get("records", [])}
    rows: list[dict[str, Any]] = []
    for r in records:
        old = previous.get(r["id"])
        if not old or not old.get("ok") or not r.get("ok"):
            continue
        if not old.get("chars") or not old.get("tokens"):
            continue
        char_delta = (r["chars"] - old["chars"]) / old["chars"]
        token_delta = (r["tokens"] - old["tokens"]) / old["tokens"]
        rows.append(
            {
                "id": r["id"],
                "char_delta": char_delta,
                "token_delta": token_delta,
                "regression": char_delta < -0.2,
            }
        )
    return rows


def print_baseline(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("\n基线对比：没有可对比的成功站点")
        return
    print("\n基线对比（正文字符变化）")
    for row in rows:
        flag = "退化" if row["regression"] else "正常"
        print(
            f"  {row['id']}: 字符 {row['char_delta']:+.1%}，token {row['token_delta']:+.1%} [{flag}]"
        )


async def reference_stats(url: str, timeout: float, proxy: str | None) -> dict[str, Any] | None:
    """用第三方正文抽取器做同页对照；未安装或失败时返回 None。"""
    try:
        import trafilatura
    except ImportError:
        return None
    from curl_cffi.requests import AsyncSession

    session = AsyncSession(impersonate=get_settings().impersonate)
    try:
        response = await session.get(url, timeout=timeout, proxy=proxy)
        text = trafilatura.extract(response.text) or ""
    except Exception:
        return None
    finally:
        await session.close()
    return {"chars": len(text), "tokens": count_tokens(text)}


async def main() -> int:
    parser = argparse.ArgumentParser(description="scrape-mcp 真实站点质量基准")
    parser.add_argument("--sites", type=Path, default=DEFAULT_SITES, help="站点集 JSON 路径")
    parser.add_argument("--category", action="append", help="按分类过滤，可重复或用逗号分隔")
    parser.add_argument("--only", action="append", help="只跑指定站点 id，可重复")
    parser.add_argument("--nightly", action="store_true", help="只跑夜间回归子集")
    parser.add_argument("--max-tokens", type=int, default=8000, help="正文 token 预算")
    parser.add_argument("--deadline-ms", type=int, default=60000, help="单站总预算（毫秒）")
    parser.add_argument("--json", type=Path, help="把完整结果写入 JSON")
    parser.add_argument("--md", type=Path, help="把结果表格写入 Markdown")
    parser.add_argument("--baseline", type=Path, help="与上次 JSON 结果对比")
    parser.add_argument(
        "--reference",
        choices=["trafilatura"],
        help="额外调用第三方正文抽取器做对照（需已安装）",
    )
    parser.add_argument("--fail-under", type=float, default=None, help="通过率低于该值时退出码非零")
    args = parser.parse_args()

    categories: list[str] = []
    for item in args.category or []:
        categories.extend(part for part in item.split(",") if part)

    sites = select_sites(load_sites(args.sites), categories, args.only, args.nightly)
    if not sites:
        raise SystemExit("过滤后没有可跑的站点")

    reference: dict[str, dict[str, Any]] | None = {} if args.reference else None
    if args.reference == "trafilatura":
        try:
            import trafilatura  # noqa: F401
        except ImportError:
            raise SystemExit("未安装 trafilatura，无法做对照：pip install trafilatura") from None

    records: list[dict[str, Any]] = []
    try:
        for index, site in enumerate(sites, 1):
            print(f"[{index}/{len(sites)}] {site['id']} ...", file=sys.stderr)
            record = await run_site(site, args.max_tokens, args.deadline_ms)
            if reference is not None and record["ok"] and record["kind"] in (None, "html"):
                stats = await reference_stats(
                    site["url"], get_settings().request_timeout, get_settings().proxy
                )
                if stats is None:
                    print("    对照抽取失败，已跳过", file=sys.stderr)
                else:
                    reference[site["id"]] = stats
            records.append(record)
    finally:
        await aclose()

    summary = summarize(records)
    print_console(records, summary)
    if reference:
        print_reference(records, reference)

    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        print_baseline(compare_baseline(records, baseline))

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": __version__,
        "sites_file": str(args.sites),
        "summary": summary,
        "records": records,
        "reference": reference or None,
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON 已写入 {args.json}")
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(render_markdown(records, summary, reference), encoding="utf-8")
        print(f"Markdown 已写入 {args.md}")

    if args.fail_under is not None and summary["pass_rate"] < args.fail_under:
        print(
            f"\n通过率 {summary['pass_rate']:.1%} 低于阈值 {args.fail_under:.1%}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
