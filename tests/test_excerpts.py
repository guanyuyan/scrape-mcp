"""query 摘录：切块、BM25 选段、标题加权、偏移与召回回退判定。"""

from __future__ import annotations

from scrape_mcp.extract.excerpts import (
    query_hits,
    select_excerpts,
    should_fallback,
    split_blocks,
    tokenize,
)

CONTENT = """# 产品说明

这是一款面向开发者的抓取工具，支持 markdown 输出和分页抓取。

## 价格

基础版每月 29 元，专业版每月 99 元，年付可享八折优惠。

## 常见问题

问：支持自定义代理吗？答：支持按会话绑定代理，也支持代理池。

## 更新日志

- 0.34.0 新增搜索即阅读与页面交互地图。
"""


def test_tokenize_mixes_latin_words_and_cjk_bigrams():
    tokens = tokenize("Redis 缓存 TTL")
    assert "redis" in tokens and "ttl" in tokens
    assert "缓存" in tokens and "存t" not in tokens


def test_split_blocks_keeps_heading_and_offsets():
    blocks = split_blocks(CONTENT)
    headings = [block.heading for block in blocks]
    assert "价格" in headings and "常见问题" in headings
    price = next(block for block in blocks if block.heading == "价格")
    assert price.text.startswith("基础版每月")
    assert CONTENT[price.start : price.start + len(price.text)] == price.text


def test_select_excerpts_finds_relevant_paragraph():
    excerpts = select_excerpts(CONTENT, "专业版多少钱")
    assert excerpts
    best = excerpts[0]
    assert best.heading == "价格"
    assert "99" in best.text
    assert 0 <= best.start < len(CONTENT)


def test_heading_weight_beats_body_only_match():
    # "价格"只出现在标题里，正文块靠标题加权被选中
    excerpts = select_excerpts(CONTENT, "价格", max_chunks=1)
    assert len(excerpts) == 1
    assert excerpts[0].heading == "价格"


def test_excerpts_respect_chunk_and_char_limits():
    excerpts = select_excerpts(CONTENT, "代理", max_chunks=2, max_chars=8)
    assert len(excerpts) == 1
    assert len(excerpts[0].text) == 8
    assert excerpts[0].end - excerpts[0].start == 8


def test_excerpts_keep_document_order():
    excerpts = select_excerpts(CONTENT, "价格 代理 更新日志", max_chunks=3)
    assert len(excerpts) >= 2
    assert [item.start for item in excerpts] == sorted(item.start for item in excerpts)


def test_select_excerpts_returns_empty_without_match_or_query():
    assert select_excerpts(CONTENT, "量子计算") == []
    assert select_excerpts(CONTENT, "   ") == []
    assert select_excerpts("", "价格") == []


def test_query_hits_and_fallback_rules():
    assert query_hits("价格", CONTENT) > 0
    assert query_hits("量子计算", CONTENT) == 0

    assert should_fallback(excerpt_chars=0, content_chars=1000, hits=0) is True
    # 命中关键词时不回退，哪怕选出来的段落很短
    assert should_fallback(excerpt_chars=10, content_chars=1000, hits=2) is False
    # 段落长度已超过正文 5% 时保留摘录
    assert should_fallback(excerpt_chars=200, content_chars=1000, hits=0) is False
