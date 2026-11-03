"""RAG 分块：标题路径、固定窗口、overlap 与偏移。"""

from __future__ import annotations

from itertools import pairwise

from scrape_mcp.extract.chunks import chunk_markdown


def test_heading_path_tracks_nesting():
    markdown = (
        "# 指南\n\n"
        "前言段落。\n\n"
        "## 安装\n\n"
        "安装步骤一。\n\n"
        "### 子步骤\n\n"
        "细节说明。\n\n"
        "## 使用\n\n"
        "用法说明。\n"
    )
    chunks = chunk_markdown(markdown, size=1200, overlap=0)
    paths = [c["heading_path"] for c in chunks]
    assert paths == [["指南"], ["指南", "安装"], ["指南", "安装", "子步骤"], ["指南", "使用"]]
    # 标题行不进正文，避免和 heading_path 重复
    assert all(not c["text"].startswith("#") for c in chunks)


def test_windows_use_fixed_size_with_overlap_and_offsets():
    body = "\n".join(f"第 {i} 行内容，用于验证窗口与重叠。" for i in range(200))
    markdown = f"# 长文\n\n{body}\n"
    chunks = chunk_markdown(markdown, size=400, overlap=100)
    assert len(chunks) > 4
    assert all(len(c["text"]) <= 400 for c in chunks)
    assert all(c["tokens"] > 0 for c in chunks)
    # 偏移能在原文中精确还原出该块的文本
    assert all(markdown[c["start"] : c["end"]] == c["text"] for c in chunks)
    # 相邻块有重叠，且窗口持续向前推进
    for prev, nxt in pairwise(chunks):
        assert nxt["start"] > prev["start"]
        assert nxt["start"] <= prev["end"]


def test_code_fence_headings_are_not_sections():
    markdown = "# 真标题\n\n```\n# 注释，不是标题\n```\n\n正文。\n"
    chunks = chunk_markdown(markdown, size=1200, overlap=0)
    assert [c["heading_path"] for c in chunks] == [["真标题"]]
    assert "注释" in chunks[0]["text"]


def test_empty_or_heading_only_markdown_yields_no_chunks():
    assert chunk_markdown("") == []
    assert chunk_markdown("# 只有标题\n\n## 小标题\n") == []
