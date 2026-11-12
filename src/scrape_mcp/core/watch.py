"""站点变更检测：把上次抓到的正文留一份快照，下次对比并报告差异。

与缓存刻意分开：缓存过了 TTL 就可以丢，快照要长期保留——否则"这个页面变了没有"
根本无从判断。只保存可比对的文本与哈希，失败抓取不会覆盖快照。
"""

from __future__ import annotations

import difflib
import hashlib
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .objectstore import LocalSnapshotBackend, SnapshotBackend

# 快照保留的正文上限：仅用于生成 diff，超长页面截断，避免把 DATA_DIR 撑大
MAX_SNAPSHOT_CHARS = 200_000
# 单次返回的 diff 行数上限：变更检测要的是"变了哪几处"，不是整页重放
MAX_DIFF_LINES = 40
# 字段级变更单次最多返回的条数（通知正文要能直接读，不做整页重放）
MAX_CHANGES = 20


class SnapshotError(ValueError):
    """版本标识不合法（非 latest / prev / 序号 / ISO 时间点）。"""


def watch_key(url: str, selector: str | None) -> str:
    """同一 URL 的不同区块分开记快照，避免互相覆盖。"""
    raw = f"{url}\n{selector or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def normalized_lines(text: str) -> list[str]:
    """行尾空白与空行不参与对比：站点每次渲染的换行差异不该算"变了"。"""
    return [line.rstrip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]


def content_hash(text: str) -> str:
    joined = "\n".join(normalized_lines(text))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def diff_summary(previous: str, current: str) -> dict[str, Any]:
    """给出增删行数与截断后的 diff 预览。"""
    old = normalized_lines(previous)
    new = normalized_lines(current)
    added = removed = 0
    lines: list[str] = []
    for line in difflib.unified_diff(old, new, lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        marker = line[:1]
        if marker == "+":
            added += 1
        elif marker == "-":
            removed += 1
        if len(lines) < MAX_DIFF_LINES:
            lines.append(line)
    return {
        "added_lines": added,
        "removed_lines": removed,
        "diff": lines,
        "diff_truncated": added + removed > len(lines),
    }


def _parse_time(text: str) -> float:
    """ISO 8601 → 时间戳；无时区按本机时区理解（调用方写的是"我看的时间"）。"""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotError(
            f"version 需为 latest / prev / 序号 / ISO 时间点，当前为 {text!r}"
        ) from exc
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.timestamp()


def flatten_fields(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """把嵌套字段摊平成路径 → 值，list 用下标（items[0].price），与 data 路径一致。"""
    flat: dict[str, Any] = {}
    for key, value in (data or {}).items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten_fields(value, f"{path}."))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                item_path = f"{path}[{index}]"
                if isinstance(item, dict):
                    flat.update(flatten_fields(item, f"{item_path}."))
                else:
                    flat[item_path] = item
        else:
            flat[path] = value
    return flat


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def field_changes(
    previous: dict[str, Any] | None,
    current: dict[str, Any] | None,
    *,
    limit: int = MAX_CHANGES,
) -> dict[str, list]:
    """按字段路径比较两份抽取结果，数值字段额外给出 delta / pct。"""
    old = flatten_fields(previous or {})
    new = flatten_fields(current or {})
    added = [path for path in new if path not in old]
    removed = [path for path in old if path not in new]
    modified: list[dict[str, Any]] = []
    for path, value in new.items():
        if path not in old:
            continue
        before = old[path]
        if before == value:
            continue
        item: dict[str, Any] = {"path": path, "from": before, "to": value}
        left, right = _number(before), _number(value)
        if left is not None and right is not None:
            item["delta"] = round(right - left, 6)
            item["pct"] = round((right - left) / abs(left) * 100, 1) if left else None
        modified.append(item)
    return {
        "added": added[: max(1, limit)],
        "removed": removed[: max(1, limit)],
        "modified": modified[: max(1, limit)],
    }


def change_summary(changes: dict[str, list], *, limit: int = MAX_CHANGES) -> list[str]:
    """生成可直接放进通知正文的行，如 ``价格 299→249（-16.7%）``。"""
    lines: list[str] = []
    for item in changes.get("modified", []):
        line = f"{item['path']} {item['from']}→{item['to']}"
        pct = item.get("pct")
        if pct is not None:
            sign = "+" if pct > 0 else ""
            line += f"（{sign}{pct}%）"
        lines.append(line)
    for path in changes.get("added", []):
        lines.append(f"新增 {path}")
    for path in changes.get("removed", []):
        lines.append(f"移除 {path}")
    return lines[: max(1, limit)]


class WatchStore:
    """每个 URL（+ selector）一份快照序列，不随缓存过期。

    默认落在 ``DATA_DIR/snapshots``（兼容读取旧版 ``DATA_DIR/watch/<key>.json``）；
    传入 ``backend`` 可换成 S3 兼容对象存储，写入时统一补 ``saved_at``，
    读写失败都不影响抓取本身。
    """

    def __init__(self, root: str | Path, backend: SnapshotBackend | None = None) -> None:
        self._backend = backend or LocalSnapshotBackend(root)

    def load(self, key: str) -> dict[str, Any] | None:
        return self._backend.load(key)

    def save(self, key: str, data: dict[str, Any]) -> None:
        self._backend.save(key, {"saved_at": time.time(), **data})

    def history(self, key: str, limit: int | None = None) -> list[dict[str, Any]]:
        return self._backend.versions(key, limit=limit)

    def resolve(self, key: str, target: Any = "latest") -> dict[str, Any] | None:
        """按 latest / prev / 序号（1=最新）/ ISO 时间点取一份快照。"""
        records = self._backend.versions(key)
        if not records:
            return None
        if target is None:
            return records[0]
        text = str(target).strip()
        if not text or text.lower() == "latest":
            return records[0]
        if text.lower() == "prev":
            return records[1] if len(records) > 1 else None
        if text.isdigit():
            index = int(text)
            if index < 1:
                raise SnapshotError("version 序号从 1 开始")
            return records[index - 1] if index <= len(records) else None
        moment = _parse_time(text)
        # captured_at 精度到微秒，回读时可能比原始 ts 略小，留 0.1ms 容差
        for record in records:  # 新 → 旧，第一个不晚于目标时间的即所要版本
            if float(record.get("ts") or 0) <= moment + 1e-4:
                return record
        return None
