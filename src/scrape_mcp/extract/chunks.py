"""RAG 分块：固定字符窗口 + overlap，并给每块带上标题路径。

输入是 compact 产出的 markdown（已按预算截断）；按标题切成小节，再在每个小节里
按窗口滑动。标题行本身不进正文（避免重复），由 ``heading_path`` 保留层级上下文，
这样单块被召回时仍知道自己在文档的哪一节。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from ..tokenizer import count_tokens

CHUNK_SIZE_DEFAULT = 1200
CHUNK_OVERLAP_DEFAULT = 200
CHUNK_SIZE_MIN = 100
CHUNK_SIZE_MAX = 8000

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^\s*```")


def chunk_markdown(
    markdown: str,
    *,
    size: int = CHUNK_SIZE_DEFAULT,
    overlap: int = CHUNK_OVERLAP_DEFAULT,
) -> list[dict[str, Any]]:
    """把 markdown 切成固定窗口的分块；每块含 text / heading_path / 偏移与 token 数。"""
    chunks: list[dict[str, Any]] = []
    for path, body, base in _sections((markdown or "").replace("\r\n", "\n")):
        for start, end in _windows(body, size, overlap):
            raw = body[start:end]
            lead = len(raw) - len(raw.lstrip())
            trail = len(raw) - len(raw.rstrip())
            text = raw.strip()
            if not text:
                continue
            begin = base + start + lead
            finish = base + end - trail
            chunks.append(
                {
                    "index": len(chunks),
                    "text": text,
                    "heading_path": list(path),
                    "start": begin,
                    "end": finish,
                    "tokens": count_tokens(text),
                }
            )
    return chunks


def _sections(text: str) -> Iterator[tuple[tuple[str, ...], str, int]]:
    """按标题切小节：返回 (标题路径, 小节正文, 正文在全文中的起始偏移)。"""
    stack: list[tuple[int, str]] = []
    path: tuple[str, ...] = ()
    body: list[str] = []
    body_start = 0
    pos = 0
    in_fence = False

    def flush(end: int) -> Iterator[tuple[tuple[str, ...], str, int]]:
        body_text = "".join(body)
        if body_text.strip():
            yield path, body_text, body_start

    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if _FENCE.match(stripped):
            in_fence = not in_fence
        match = None if in_fence else _HEADING.match(stripped)
        if match is not None:
            yield from flush(pos)
            level = len(match.group(1))
            title = match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            path = tuple(name for _level, name in stack)
            body = []
            body_start = pos + len(line)
        else:
            body.append(line)
        pos += len(line)
    yield from flush(pos)


def _windows(body: str, size: int, overlap: int) -> Iterator[tuple[int, int]]:
    """固定窗口滑动；尽量在换行处收口，并保证每轮都向前推进。"""
    length = len(body)
    if length <= size:
        yield 0, length
        return
    start = 0
    while start < length:
        end = min(length, start + size)
        if end < length:
            cut = _cut_at_boundary(body, start, end)
            if cut > start:
                end = cut
        if end <= start:
            end = min(length, start + size)
        yield start, end
        if end >= length:
            return
        # 重叠让相邻块共享上下文；窗口在换行处提前收口时也要保证下一步前进
        start = max(end - overlap, start + 1)


def _cut_at_boundary(body: str, start: int, end: int) -> int:
    """在窗口后半段找最近的换行，避免把词/句子切断；找不到就用原窗口。"""
    floor = start + (end - start) // 2
    for index in range(end, floor, -1):
        if body[index - 1] == "\n":
            return index
    return end
