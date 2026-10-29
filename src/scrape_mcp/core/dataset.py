"""数据集导出：把成功抓取的结果追加成 JSONL，按需转 Parquet。

默认关闭；设置 ``SCRAPE_MCP_DATASET=<path>.jsonl`` 后生效：

- 一次抓取写一行，字段固定（见 ``build_record``），可直接被 jq / pandas / 向量库消费；
- 只记成功结果：拦截页、登录墙、超时属于噪声，混进数据集只会污染后续检索；
- 追加写 + 进程内锁，并发抓取也不会把两行写串。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import get_settings

logger = logging.getLogger(__name__)

# 记录格式版本：字段增删时递增，下游按版本兼容
RECORD_VERSION = 1

_lock = threading.Lock()


class DatasetError(RuntimeError):
    """数据集读写失败（配置缺失、文件损坏、缺少 pyarrow 等）。"""


def dataset_path() -> Path | None:
    """配置里的数据集文件；未配置返回 None。"""
    raw = (get_settings().dataset or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def build_record(payload: dict[str, Any], *, tool: str) -> dict[str, Any]:
    """把工具返回的 payload 压成一条数据集记录。"""
    content = payload.get("content")
    if isinstance(content, str):
        chars: int | None = len(content)
    elif isinstance(content, list):
        chars = sum(len(str(item)) for item in content)
    else:
        chars = None
    record: dict[str, Any] = {
        "v": RECORD_VERSION,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": tool,
        "url": payload.get("url"),
        "final_url": payload.get("final_url"),
        "status": payload.get("status"),
        "tier": payload.get("tier"),
        "title": payload.get("title"),
        "tokens": payload.get("token_estimate"),
        "chars": chars,
        "content": content,
        "error_code": payload.get("error_code"),
    }
    if "data" in payload:  # web_extract：字段抽取结果
        record["data"] = payload.get("data")
    return record


def record_result(payload: dict[str, Any], *, tool: str = "fetch") -> None:
    """把一次成功结果追加到数据集；未配置或结果失败时什么都不做。

    写盘失败只记日志：导出是旁路能力，不能因为磁盘问题让抓取本身失败。
    """
    path = dataset_path()
    if path is None or not payload.get("ok"):
        return
    line = json.dumps(build_record(payload, tool=tool), ensure_ascii=False)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock, path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        logger.warning("写入数据集失败（%s）：%s", path, exc)


def load_records(path: Path) -> tuple[list[dict[str, Any]], int]:
    """读回 JSONL；返回 (记录列表, 损坏行数)，坏行跳过而不是整份失败。"""
    if not path.is_file():
        raise DatasetError(f"找不到数据集文件：{path}")
    records: list[dict[str, Any]] = []
    bad = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(item, dict):
                records.append(item)
            else:
                bad += 1
    return records, bad


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """给 CLI 的概况：条数、按工具分布、时间跨度、平均体量。"""
    tools: dict[str, int] = {}
    for record in records:
        name = str(record.get("tool") or "unknown")
        tools[name] = tools.get(name, 0) + 1
    tokens = [r["tokens"] for r in records if isinstance(r.get("tokens"), (int, float))]
    chars = [r["chars"] for r in records if isinstance(r.get("chars"), (int, float))]
    stamps = sorted(str(r["ts"]) for r in records if r.get("ts"))
    return {
        "records": len(records),
        "by_tool": dict(sorted(tools.items())),
        "first_ts": stamps[0] if stamps else None,
        "last_ts": stamps[-1] if stamps else None,
        "avg_tokens": round(sum(tokens) / len(tokens)) if tokens else 0,
        "avg_chars": round(sum(chars) / len(chars)) if chars else 0,
    }


def to_parquet(source: Path, target: Path | None = None) -> Path:
    """把 JSONL 转成 Parquet（需要 pyarrow，属 ``mcp-scrape[dataset]`` 可选依赖）。"""
    if importlib.util.find_spec("pyarrow") is None:
        raise DatasetError('导出 Parquet 需要 pyarrow，先安装：pip install "mcp-scrape[dataset]"')
    import pyarrow as pa
    import pyarrow.parquet as pq

    records, bad = load_records(source)
    if not records:
        raise DatasetError(f"{source} 里没有可导出的记录（损坏行 {bad}）")
    rows: list[dict[str, Any]] = []
    for record in records:
        row = dict(record)
        content = row.get("content")
        if isinstance(content, list):
            row["content"] = "\n".join(str(item) for item in content)
        if isinstance(row.get("data"), (dict, list)):
            row["data"] = json.dumps(row["data"], ensure_ascii=False)
        rows.append(row)
    columns = sorted({key for row in rows for key in row})
    table = pa.table({name: [row.get(name) for row in rows] for name in columns})
    out = target or source.with_suffix(".parquet")
    pq.write_table(table, out)
    return out
