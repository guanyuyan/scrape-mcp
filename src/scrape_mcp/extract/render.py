"""输出格式：把 compact 的 markdown 风格正文转成纯文本或分块数组。

compact 默认输出 markdown（标题 #、列表 -、链接 [text](url)、代码围栏），
对模型友好；但有些调用方只要纯文本或结构化分块，这里做无损转换。
"""

from __future__ import annotations

import re

FORMATS = ("markdown", "text", "json")

_HEADING = re.compile(r"^#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s{0,2}[-*]\s+", re.M)
_QUOTE = re.compile(r"^\s{0,2}>\s?", re.M)
_CODE_FENCE = re.compile(r"^```.*$", re.M)
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MULTI_NL = re.compile(r"\n{2,}")


def to_plain_text(markdown: str) -> str:
    """剥掉 markdown 标记，保留链接文字与代码内容。"""
    text = _CODE_FENCE.sub("", markdown or "")
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    text = _QUOTE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _MULTI_NL.sub("\n", text)
    return text.strip()


def to_blocks(markdown: str) -> list[str]:
    """按行切成非空块，便于调用方逐段处理（如写入表格/向量库）。"""
    return [line.strip() for line in (markdown or "").splitlines() if line.strip()]
