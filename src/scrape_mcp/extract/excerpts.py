"""query 相关摘录：把正文切块后按 BM25 选出与问题最相关的几段。

不依赖模型与分词库：拉丁词按符号/空白切分，中文按 bigram 兜底；标题命中加权，
打分相同按原文位置排序，保证同一输入输出稳定（可缓存、可测试）。

摘录只是"按问题选段"，不是摘要——返回的每段都是原文连续片段，调用方可直接引用。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# 标题行：只认 markdown 的 # 前缀（compact 后的正文已经统一成 markdown）
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_LATIN = re.compile(r"[a-z0-9_]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
# 太短的块多是导航残渣，参与打分只会把预算浪费在噪声上
_MIN_BLOCK_CHARS = 4
# 标题比正文更能说明"这段讲什么"，命中时按两份计入词频
_HEADING_WEIGHT = 2
_BM25_K1 = 1.5
_BM25_B = 0.75
# 召回判定：摘录总长不足正文的 5% 且一个关键词都没命中时，视为没找到答案
_FALLBACK_RATIO = 0.05


@dataclass
class Excerpt:
    """一段命中摘录；start/end 是相对整页正文的字符偏移。"""

    text: str
    heading: str
    start: int
    end: int
    score: float

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "heading": self.heading,
            "start": self.start,
            "end": self.end,
            "score": round(self.score, 4),
        }


@dataclass
class _Block:
    text: str
    heading: str
    start: int
    end: int


def tokenize(text: str) -> list[str]:
    """拉丁词 + 中文 bigram；中文不做词典分词，避免引入必装依赖。"""
    lowered = (text or "").lower()
    tokens = _LATIN.findall(lowered)
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def split_blocks(content: str) -> list[_Block]:
    """按标题与空行切块，并记录每块对应的标题与字符偏移。"""
    blocks: list[_Block] = []
    heading = ""
    buf: list[str] = []
    buf_start = 0
    offset = 0

    def flush(end: int) -> None:
        nonlocal buf, buf_start
        if buf:
            text = "\n".join(buf).strip()
            if len(text) >= _MIN_BLOCK_CHARS:
                blocks.append(_Block(text=text, heading=heading, start=buf_start, end=end))
        buf = []

    for raw in (content or "").splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        start = offset
        offset += len(raw)
        stripped = line.strip()
        match = _HEADING.match(stripped)
        if match:
            flush(start)
            heading = match.group(1).strip()
            continue
        if not stripped:
            flush(start)
            continue
        if not buf:
            buf_start = start
        buf.append(line)
    flush(offset)
    return blocks


def _bm25(docs: list[list[str]], query_tokens: list[str]) -> list[float]:
    total = len(docs)
    if not total:
        return []
    lengths = [len(doc) for doc in docs]
    avgdl = sum(lengths) / total
    df: dict[str, int] = {}
    for doc in docs:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1

    scores = [0.0] * total
    for term in set(query_tokens):
        idf = math.log(1 + (total - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
        for index, doc in enumerate(docs):
            tf = doc.count(term)
            if not tf:
                continue
            norm = _BM25_K1 * (1 - _BM25_B + _BM25_B * (lengths[index] / avgdl if avgdl else 0))
            scores[index] += idf * tf * (_BM25_K1 + 1) / (tf + norm)
    return scores


def select_excerpts(
    content: str,
    query: str,
    *,
    max_chunks: int = 3,
    max_chars: int = 600,
) -> list[Excerpt]:
    """返回得分最高的若干段（按原文顺序），每段截断到 max_chars 字符。"""
    blocks = split_blocks(content)
    query_tokens = tokenize(query)
    if not blocks or not query_tokens:
        return []

    docs: list[list[str]] = []
    for block in blocks:
        tokens = tokenize(block.text)
        if block.heading:
            tokens.extend(tokenize(block.heading) * _HEADING_WEIGHT)
        docs.append(tokens)
    scores = _bm25(docs, query_tokens)

    ranked = sorted(range(len(blocks)), key=lambda i: (-scores[i], blocks[i].start))
    picked = [i for i in ranked[: max(1, max_chunks)] if scores[i] > 0]
    picked.sort(key=lambda i: blocks[i].start)

    excerpts: list[Excerpt] = []
    for index in picked:
        block = blocks[index]
        text = block.text[: max(1, max_chars)]
        start = content.find(block.text, block.start)
        start = start if start >= 0 else block.start
        excerpts.append(
            Excerpt(
                text=text,
                heading=block.heading,
                start=start,
                end=start + len(text),
                score=scores[index],
            )
        )
    return excerpts


def query_hits(query: str, content: str) -> int:
    """query 中出现的词有几个能在正文里找到（中文按 bigram 计数）。"""
    terms = set(tokenize(query))
    if not terms:
        return 0
    haystack = (content or "").lower()
    return sum(1 for term in terms if term in haystack)


def should_fallback(*, excerpt_chars: int, content_chars: int, hits: int) -> bool:
    """没选出段落，或量极少且关键词完全没命中时，退回整页而不是给空片段。"""
    if excerpt_chars <= 0:
        return True
    return hits == 0 and excerpt_chars < max(1, int(content_chars * _FALLBACK_RATIO))
