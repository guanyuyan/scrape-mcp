"""结构化抽取深化：嵌套字段、对象数组、regex/abs_url 后处理。"""

from __future__ import annotations

import pytest

from scrape_mcp.extract.schema import (
    SchemaValidationError,
    extract_from_html,
    parse_schema,
)

_HTML = """
<html><body>
  <div class="product">
    <h2 class="name">机械键盘</h2>
    <span class="price">到手价 ￥299.00 元</span>
    <a class="detail" href="/p/1">详情</a>
  </div>
  <div class="product">
    <h2 class="name">无线鼠标</h2>
    <span class="price">到手价 ￥89 元</span>
    <a class="detail" href="/p/2">详情</a>
  </div>
  <div class="author">
    <span class="name">张三</span>
    <p class="bio">写代码的人</p>
  </div>
</body></html>
"""


def test_regex_and_group_extract_number():
    res = extract_from_html(
        _HTML,
        {"fields": {"price": {"selector": ".price", "regex": r"￥(\d+(?:\.\d+)?)", "group": 1}}},
    )
    assert res.fields["price"] == "299.00"


def test_regex_miss_falls_back_to_default():
    res = extract_from_html(
        _HTML,
        {
            "fields": {
                "price": {
                    "selector": ".price",
                    "regex": r"USD(\d+)",
                    "default": "无报价",
                }
            }
        },
    )
    assert res.fields["price"] == "无报价"


def test_abs_url_resolves_relative_link():
    res = extract_from_html(
        _HTML,
        {
            "fields": {
                "detail": {"selector": "a.detail", "type": "attr", "attr": "href", "abs_url": True}
            }
        },
        base_url="https://shop.example.com/list/1",
    )
    assert res.fields["detail"] == "https://shop.example.com/p/1"


def test_list_of_objects_with_nested_fields():
    res = extract_from_html(
        _HTML,
        {
            "fields": {
                "products": {
                    "selector": ".product",
                    "type": "list",
                    "fields": {
                        "name": {"selector": ".name"},
                        "price": {"selector": ".price", "regex": r"￥([\d.]+)", "group": 1},
                        "url": {
                            "selector": "a.detail",
                            "type": "attr",
                            "attr": "href",
                            "abs_url": True,
                        },
                    },
                }
            }
        },
        base_url="https://shop.example.com/list/1",
    )
    assert res.errors == []
    assert res.fields["products"] == [
        {
            "name": "机械键盘",
            "price": "299.00",
            "url": "https://shop.example.com/p/1",
        },
        {"name": "无线鼠标", "price": "89", "url": "https://shop.example.com/p/2"},
    ]


def test_object_type_extracts_subtree():
    res = extract_from_html(
        _HTML,
        {
            "fields": {
                "author": {
                    "selector": ".author",
                    "type": "object",
                    "fields": {"name": {"selector": ".name"}, "bio": {"selector": ".bio"}},
                }
            }
        },
    )
    assert res.fields["author"] == {"name": "张三", "bio": "写代码的人"}


def test_missing_object_gives_none():
    res = extract_from_html(
        _HTML,
        {"fields": {"nope": {"selector": ".nope", "type": "object", "fields": {"x": "span"}}}},
    )
    assert res.fields["nope"] is None


def test_nested_invalid_selector_reports_path():
    res = extract_from_html(
        _HTML,
        {
            "fields": {
                "products": {
                    "selector": ".product",
                    "type": "list",
                    "fields": {"bad": {"selector": "a["}},
                }
            }
        },
    )
    assert res.fields["products"][0]["bad"] is None
    assert any("products.bad" in e for e in res.errors)


def test_object_requires_fields():
    with pytest.raises(SchemaValidationError):
        parse_schema({"fields": {"a": {"selector": "div", "type": "object"}}})


def test_invalid_regex_rejected():
    with pytest.raises(SchemaValidationError):
        parse_schema({"fields": {"a": {"selector": "div", "regex": "("}}})


def test_negative_group_rejected():
    with pytest.raises(SchemaValidationError):
        parse_schema({"fields": {"a": {"selector": "div", "regex": "x", "group": -1}}})


def test_html_type_returns_fragment():
    res = extract_from_html(_HTML, {"fields": {"snippet": {"selector": ".name", "type": "html"}}})
    assert "机械键盘" in res.fields["snippet"]
