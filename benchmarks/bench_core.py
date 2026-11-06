"""核心路径性能基线：只测不依赖网络的热路径，用固定语料保证可复现。

与 ``scripts/bench.py``（真实网络延迟）和 ``scripts/bench_sites.py``（夜间站点矩阵）
互补：这里量化的是解析/分块/落盘的本地吞吐，可在 CI 上稳定复跑。

指标：
- ``compact_html``    HTML → Markdown（selectolax 解析 + 密度选正文 + 渲染）
- ``chunk_markdown``  Markdown → RAG 分块（标题路径 + 窗口滑动 + token 计数）
- ``build_record``    工具结果 → 数据集记录（纯内存）
- ``record_result``   数据集记录 → JSONL 追加写（含加锁与磁盘 I/O）
- ``recipe_validate`` 示例配方校验（选择器语法 + schema）

用法：
    python benchmarks/bench_core.py               # 跑一遍并打印表格
    python benchmarks/bench_core.py --check      # 低于 baseline.json 的下限时退出码 1
    python benchmarks/bench_core.py --json out.json

基准用「ops/sec 下限」而不是跨机器比绝对耗时：下限取参考机的 ~35%，只拦数量级
退化（例如误引入 O(n²)），不会因为 CI 机器慢而误报。数值随实现变化时用
``--update-baseline`` 重新生成，但请先确认变慢是预期内的。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).resolve().parent / "core_baseline.json"
RECIPE_DIR = ROOT / "examples" / "recipes"

WORDS = [
    "异步",
    "协程",
    "事件循环",
    "调度",
    "缓存",
    "索引",
    "抓取",
    "解析",
    "抽取",
    "代理",
    "配额",
    "租约",
    "并发",
    "超时",
    "重试",
    "限流",
    "渲染",
    "分块",
    "检索",
    "向量",
    "质量",
    "稳定",
    "观测",
    "回归",
    "基线",
    "吞吐",
    "延迟",
    "内存",
    "扩展",
    "边界",
]


def build_html(sections: int = 100) -> str:
    """固定种子的合成页面：标题、段落、列表、表格、链接齐全，约 100KB。"""
    rng = random.Random(20261106)
    parts = [
        "<!doctype html><html><head><title>性能基线页</title>",
        "<meta charset='utf-8'></head><body><main>",
        "<h1>性能基线页</h1>",
        "<p>本页由固定种子生成，仅用于基准测试。</p>",
    ]
    for index in range(sections):
        parts.append(f"<h2>第 {index + 1} 节</h2>")
        for _ in range(3):
            sentences = []
            for _ in range(10):
                words = [rng.choice(WORDS) for _ in range(rng.randint(12, 24))]
                sentences.append("".join(words) + "。")
            parts.append("<p>" + "".join(sentences) + "</p>")
        parts.append("<ul>" + "".join(f"<li>条目 {i}</li>" for i in range(5)) + "</ul>")
        parts.append(
            "<table><tr><th>指标</th><th>值</th></tr>"
            + "".join(f"<tr><td>行 {i}</td><td>{i * 3}</td></tr>" for i in range(5))
            + "</table>"
        )
    parts.append("<a href='/docs/next'>下一节</a><a href='https://external.example/x'>外部</a>")
    parts.append("</main></body></html>")
    return "".join(parts)


def measure(fn: Callable[[int], Any], iterations: int, repeats: int = 3) -> dict[str, Any]:
    """取多次重复里的最快一轮，降低 GC / 调度抖动。"""
    best = float("inf")
    for _ in range(repeats):
        started = time.perf_counter()
        for index in range(iterations):
            fn(index)
        best = min(best, time.perf_counter() - started)
    seconds_per_op = best / iterations
    return {
        "ops_per_sec": round(1 / seconds_per_op, 1),
        "avg_ms": round(seconds_per_op * 1000, 3),
        "iterations": iterations,
        "repeats": repeats,
    }


def run(scale: float = 1.0) -> dict[str, Any]:
    from scrape_mcp.config import get_settings
    from scrape_mcp.core.dataset import build_record, record_result
    from scrape_mcp.core.recipes import validate_recipe_file
    from scrape_mcp.extract.chunks import chunk_markdown
    from scrape_mcp.extract.compact import compact_html

    html = build_html()
    markdown = compact_html(html, "https://bench.local/").content
    payload = {
        "ok": True,
        "url": "https://bench.local/article",
        "final_url": "https://bench.local/article",
        "status": 200,
        "tier": "http",
        "title": "性能基线页",
        "token_estimate": 1200,
        "content": "基准正文。" * 700,
    }
    records = sorted(RECIPE_DIR.glob("*.json"))

    metrics: dict[str, Any] = {}

    metrics["compact_html"] = measure(
        lambda _: compact_html(html, "https://bench.local/article"),
        max(3, int(10 * scale)),
    )
    metrics["chunk_markdown"] = measure(
        lambda _: chunk_markdown(markdown),
        max(5, int(40 * scale)),
    )
    metrics["build_record"] = measure(
        lambda _: build_record(payload, tool="fetch"),
        max(50, int(2000 * scale)),
    )

    temp_dir = tempfile.mkdtemp(prefix="scrape-bench-")
    os.environ["SCRAPE_MCP_DATASET"] = str(Path(temp_dir) / "dataset.jsonl")
    get_settings.cache_clear()
    metrics["record_result"] = measure(
        lambda _: record_result(payload, tool="fetch"),
        max(20, int(400 * scale)),
    )
    os.environ.pop("SCRAPE_MCP_DATASET", None)
    get_settings.cache_clear()

    if records:
        metrics["recipe_validate"] = measure(
            lambda _: [validate_recipe_file(path) for path in records],
            max(3, int(20 * scale)),
        )

    return {
        "synthetic_html_kb": round(len(html.encode()) / 1024, 1),
        "synthetic_markdown_kb": round(len(markdown.encode()) / 1024, 1),
        "recipe_files": len(records),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "metrics": metrics,
    }


# 纯内存的微秒级操作噪声大，放宽到实测的 1/10，避免换机器就误报
RATIO_OVERRIDES = {"build_record": 0.1}


def load_floors(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {name: float(value) for name, value in data["floors"].items()}


def update_baseline(result: dict[str, Any], path: Path, ratio: float) -> None:
    """把实测值 × ratio 写成新的下限，供人工确认后提交。"""
    floors = {
        name: round(metric["ops_per_sec"] * RATIO_OVERRIDES.get(name, ratio), 1)
        for name, metric in result["metrics"].items()
    }
    data = {
        "schema": 1,
        "note": "ops/sec 下限；跨机器只比下限，不比绝对耗时。数值 = 参考机实测 × ratio（build_record 取 1/10）。",
        "ratio": ratio,
        "measured_on": result["platform"],
        "python": result["python"],
        "floors": floors,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已更新基线：{path}（ratio={ratio}）")


def print_table(result: dict[str, Any], floors: dict[str, float]) -> bool:
    print(
        f"环境：Python {result['python']} · {result['platform']}\n"
        f"语料：HTML {result['synthetic_html_kb']} KB → Markdown "
        f"{result['synthetic_markdown_kb']} KB · 配方 {result['recipe_files']} 份\n"
    )
    header = f"{'metric':<18}{'ops/sec':>10}{'avg ms':>10}{'floor':>10}  status"
    print(header)
    print("-" * len(header))
    ok = True
    for name, metric in result["metrics"].items():
        floor = floors.get(name)
        passed = floor is None or metric["ops_per_sec"] >= floor
        ok = ok and passed
        print(
            f"{name:<18}{metric['ops_per_sec']:>10.1f}{metric['avg_ms']:>10.3f}"
            f"{(f'{floor:.1f}' if floor is not None else '-'):>10}  "
            f"{'ok' if passed else 'REGRESSED'}"
        )
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="核心路径性能基线")
    parser.add_argument("--check", action="store_true", help="低于 baseline.json 下限时退出码 1")
    parser.add_argument("--json", default="", help="把结果写成 JSON")
    parser.add_argument(
        "--baseline", default=str(BASELINE_PATH), help="下限文件（默认 core_baseline.json）"
    )
    parser.add_argument("--update-baseline", action="store_true", help="用本次结果重建下限")
    parser.add_argument("--ratio", type=float, default=0.35, help="下限 = 实测 × ratio")
    parser.add_argument("--scale", type=float, default=1.0, help="迭代次数缩放（调试用）")
    args = parser.parse_args()

    result = run(scale=args.scale)
    baseline_path = Path(args.baseline)
    floors = load_floors(baseline_path) if baseline_path.exists() else {}
    for name in floors:
        if name not in result["metrics"]:
            print(f"[警告] 基线里的 {name} 没有对应指标", file=sys.stderr)

    ok = print_table(result, floors)
    if args.json:
        Path(args.json).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n结果已写入 {args.json}")
    if args.update_baseline:
        update_baseline(result, baseline_path, args.ratio)
        return 0
    if args.check and not ok:
        print(
            "\n[失败] 存在低于下限的指标；确认是实现退化而不是环境噪声后，再更新基线。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
