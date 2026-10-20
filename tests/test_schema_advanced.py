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


# ---------- JSON-LD ----------

_LD_HTML = """
<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "NewsArticle", "headline": "示例新闻",
 "author": {"name": "张三"}, "keywords": ["AI", "抓取"],
 "datePublished": "2024-01-02T03:04:05+08:00"}
</script>
<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "BreadcrumbList", "name": "首页"},
  {"@type": "Organization", "name": "示例站"}
]}
</script>
</head><body><p>正文</p></body></html>
"""


def test_jsonld_nested_path():
    res = extract_from_html(
        _LD_HTML,
        {
            "fields": {
                "headline": {"type": "jsonld", "path": "headline"},
                "author": {"type": "jsonld", "path": "author.name"},
            }
        },
    )
    assert res.fields == {"headline": "示例新闻", "author": "张三"}


def test_jsonld_flattens_across_documents_and_graph():
    res = extract_from_html(_LD_HTML, {"fields": {"names": {"type": "jsonld", "path": "name"}}})
    assert res.fields["names"] == ["首页", "示例站"]


def test_jsonld_array_value_and_index():
    res = extract_from_html(
        _LD_HTML,
        {
            "fields": {
                "keywords": {"type": "jsonld", "path": "keywords"},
                "first": {"type": "jsonld", "path": "keywords.0"},
            }
        },
    )
    assert res.fields["keywords"] == ["AI", "抓取"]
    assert res.fields["first"] == "AI"


def test_jsonld_invalid_script_falls_back_to_default_and_required():
    html = '<html><body><script type="application/ld+json">{not json}</script></body></html>'
    res = extract_from_html(
        html,
        {"fields": {"title": {"type": "jsonld", "path": "headline", "default": "无"}}},
    )
    assert res.fields["title"] == "无"

    res = extract_from_html(
        html,
        {"fields": {"title": {"type": "jsonld", "path": "headline", "required": True}}},
    )
    assert res.required_missing == ["title"]


def test_jsonld_requires_path():
    with pytest.raises(SchemaValidationError):
        parse_schema({"fields": {"a": {"type": "jsonld"}}})


# ---------- convert / required ----------


def test_convert_float_strips_currency():
    res = extract_from_html(
        _HTML,
        {
            "fields": {
                "price": {
                    "selector": ".price",
                    "regex": r"￥([\d.]+)",
                    "group": 1,
                    "convert": "float",
                }
            }
        },
    )
    assert res.fields["price"] == 299.0


def test_convert_int_handles_thousands_separator():
    html = '<span class="stock">库存 1,234 件</span>'
    res = extract_from_html(html, {"fields": {"stock": {"selector": ".stock", "convert": "int"}}})
    assert res.fields["stock"] == 1234


def test_convert_date_normalizes_chinese_and_iso():
    html = '<span class="d">2024年1月2日</span><span class="t">2024-01-02T03:04:05+08:00</span>'
    res = extract_from_html(
        html,
        {
            "fields": {
                "d": {"selector": ".d", "convert": "date"},
                "t": {"selector": ".t", "convert": "date"},
            }
        },
    )
    assert res.fields["d"] == "2024-01-02"
    assert res.fields["t"] == "2024-01-02T03:04:05+08:00"


def test_convert_failure_records_note():
    res = extract_from_html(
        '<p class="x">暂无</p>',
        {"fields": {"x": {"selector": ".x", "convert": "int"}}},
    )
    assert res.fields["x"] is None
    assert any("无法转换为 int" in e for e in res.errors)


def test_required_reports_missing_fields():
    res = extract_from_html(
        _HTML,
        {
            "fields": {
                "name": {"selector": ".name", "required": True},
                "absent": {"selector": ".nope", "required": True},
                "optional": {"selector": ".nope"},
            }
        },
    )
    assert res.fields["name"] == "机械键盘"
    assert res.required_missing == ["absent"]


def test_required_nested_field_uses_path():
    html = '<div class="item"><span class="other">x</span></div>'
    res = extract_from_html(
        html,
        {
            "fields": {
                "items": {
                    "selector": ".item",
                    "type": "list",
                    "fields": {"name": {"selector": ".name", "required": True}},
                }
            }
        },
    )
    assert res.required_missing == ["items.name"]


def test_invalid_convert_rejected():
    with pytest.raises(SchemaValidationError):
        parse_schema({"fields": {"a": {"selector": "div", "convert": "money"}}})


def test_required_must_be_bool():
    with pytest.raises(SchemaValidationError):
        parse_schema({"fields": {"a": {"selector": "div", "required": "yes"}}})
