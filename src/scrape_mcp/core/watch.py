"""站点变更检测：把上次抓到的正文留一份快照，下次对比并报告差异。

与缓存刻意分开：缓存过了 TTL 就可以丢，快照要长期保留——否则"这个页面变了没有"
根本无从判断。只保存可比对的文本与哈希，失败抓取不会覆盖快照。
"""

from __future__ import annotations

import difflib
import hashlib
import time
from pathlib import Path
from typing import Any

from .objectstore import LocalSnapshotBackend, SnapshotBackend

# 快照保留的正文上限：仅用于生成 diff，超长页面截断，避免把 DATA_DIR 撑大
MAX_SNAPSHOT_CHARS = 200_000
# 单次返回的 diff 行数上限：变更检测要的是"变了哪几处"，不是整页重放
MAX_DIFF_LINES = 40


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


class WatchStore:
    """每个 URL（+ selector）一份快照，不随缓存过期。

    默认落在 ``DATA_DIR/watch``；传入 ``backend`` 可换成 S3 兼容对象存储，
    写入时统一补 ``saved_at``，读写失败都不影响抓取本身。
    """

    def __init__(self, root: str | Path, backend: SnapshotBackend | None = None) -> None:
        self._backend = backend or LocalSnapshotBackend(root)

    def load(self, key: str) -> dict[str, Any] | None:
        return self._backend.load(key)

    def save(self, key: str, data: dict[str, Any]) -> None:
        self._backend.save(key, {"saved_at": time.time(), **data})
