"""阶段 1-① 结构化抽取测试：schema 校验与字段抽取。"""

import pytest

from scrape_mcp.extract.schema import (
    SchemaValidationError,
    extract_from_html,
    parse_schema,
)

_HTML = """
<html><body>
  <h1>示例文章标题</h1>
  <p class="teaser">这是导语。</p>
  <a class="btn" href="/download">下载</a>
  <ul>
    <li class="tag">Python</li>
    <li class="tag">MCP</li>
  </ul>
  <div class="item"><span class="name">商品A</span></div>
  <div class="item"><span class="name">商品B</span></div>
  <p>正文第一段</p>
</body></html>
"""


def test_parse_schema_shorthand_string_selector():
    fields = parse_schema({"fields": {"title": "h1"}})
    assert len(fields) == 1
    assert fields[0].selector == "h1"
    assert fields[0].type == "text"


def test_parse_schema_rejects_empty():
    with pytest.raises(SchemaValidationError):
        parse_schema({})
    with pytest.raises(SchemaValidationError):
        parse_schema({"fields": {}})


def test_parse_schema_requires_selector():
    with pytest.raises(SchemaValidationError):
        parse_schema({"fields": {"a": {"type": "text"}}})


def test_parse_schema_attr_requires_attr():
    with pytest.raises(SchemaValidationError):
        parse_schema({"fields": {"a": {"selector": "a", "type": "attr"}}})


def test_parse_schema_bad_type():
    with pytest.raises(SchemaValidationError):
        parse_schema({"fields": {"a": {"selector": "a", "type": "nope"}}})


def test_extract_text_attr_count_list():
    res = extract_from_html(
        _HTML,
        {
            "fields": {
                "title": {"selector": "h1", "type": "text"},
                "link": {"selector": "a.btn", "type": "attr", "attr": "href"},
                "tag_count": {"selector": ".tag", "type": "count"},
                "tags": {"selector": ".tag", "type": "list", "list_key": "text"},
            }
        },
    )
    assert res.errors == []
    assert res.fields["title"] == "示例文章标题"
    assert res.fields["link"] == "/download"
    assert res.fields["tag_count"] == 2
    assert res.fields["tags"] == ["Python", "MCP"]


def test_extract_missing_selectors_none():
    res = extract_from_html(_HTML, {"fields": {"nope": {"selector": ".missing"}}})
    assert res.fields["nope"] is None


def test_extract_default_for_missing():
    res = extract_from_html(
        _HTML, {"fields": {"nope": {"selector": ".missing", "default": "fallback"}}}
    )
    assert res.fields["nope"] == "fallback"


def test_nested_list_of_objects():
    res = extract_from_html(
        _HTML,
        {
            "fields": {
                "items": {
                    "selector": ".item",
                    "type": "list",
                    "list_key": "text",
                }
            }
        },
    )
    assert res.fields["items"] == ["商品A", "商品B"]
