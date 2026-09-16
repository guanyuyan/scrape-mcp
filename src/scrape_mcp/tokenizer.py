"""Token 估算与按预算截断。

优先用 tiktoken 精确计数；离线/加载失败时退化为启发式估算
（CJK 约 1 字 1 token，其余约 4 字符 1 token）。
"""

from __future__ import annotations

import re
from functools import lru_cache

_WS = re.compile(r"\s+")
_CJK = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


@lru_cache(maxsize=1)
def _encoding():
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _encoding()
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:
            pass
    return _heuristic(text)


def _heuristic(text: str) -> int:
    cjk = len(_CJK.findall(text))
    other = len(_WS.sub("", text[0:])) - cjk
    return max(1, int(cjk + other / 4))


def truncate_to_budget(text: str, max_tokens: int) -> tuple[str, bool, int]:
    """超预算时保留头尾、省略中段。返回 (文本, 是否截断, 最终 token 数)。"""
    total = count_tokens(text)
    if total <= max_tokens:
        return text, False, total

    blocks = [b.strip() for b in text.split("\n") if b.strip()]
    if len(blocks) < 3:
        keep = max(1, max_tokens * 4)
        clipped = text[:keep] + "\n…(已截断)…"
        return clipped, True, count_tokens(clipped)

    # 头多留、尾少留：门户页尾部常是页脚/合作媒体等低价值块，真实内容集中在头部。
    head_budget = int(max_tokens * 0.7)
    tail_budget = int(max_tokens * 0.25)

    head: list[str] = []
    used = 0
    i = 0
    while i < len(blocks):
        cost = count_tokens(blocks[i]) + 1
        if used + cost > head_budget:
            break
        head.append(blocks[i])
        used += cost
        i += 1

    tail: list[str] = []
    used = 0
    j = len(blocks) - 1
    while j > i:
        cost = count_tokens(blocks[j]) + 1
        if used + cost > tail_budget:
            break
        tail.append(blocks[j])
        used += cost
        j -= 1

    if i == 0:
        head = [blocks[0][: max(1, head_budget * 3)]]
        i = 1

    omitted = j - i + 1
    marker = f"…(省略 {omitted} 段)…" if omitted > 0 else ""
    parts = head + ([marker] if marker else []) + list(reversed(tail))
    out = "\n".join(p for p in parts if p)
    return out, True, count_tokens(out)
