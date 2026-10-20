"""输出格式转换：markdown → 纯文本 / 分块数组。"""

from scrape_mcp.extract.render import to_blocks, to_plain_text

_MD = """# 标题

正文一段，含 [站内链接](/a) 与 `行内代码`。

## 小节

- 要点一
- 要点二

> 引用

```python
print(1)
```
"""


def test_to_plain_text_strips_markup():
    text = to_plain_text(_MD)
    assert "#" not in text
    assert "[站内链接](/a)" not in text
    assert "站内链接" in text
    assert "`行内代码`" not in text
    assert "行内代码" in text
    assert "```" not in text
    assert "- 要点一" not in text
    assert "> 引用" not in text


def test_to_blocks_drops_blank_lines():
    blocks = to_blocks(_MD)
    assert all(b.strip() for b in blocks)
    assert "标题" in blocks[0]
    assert "print(1)" in blocks


def test_empty_input():
    assert to_plain_text("") == ""
    assert to_blocks("") == []
