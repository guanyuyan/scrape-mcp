"""插件式抽取器：自定义字段类型。"""

from __future__ import annotations

import pytest

from scrape_mcp.extract import plugins
from scrape_mcp.extract.plugins import (
    ExtractorContext,
    extractor_names,
    get_extractor,
    register_extractor,
)
from scrape_mcp.extract.schema import SchemaValidationError, extract_from_html


@pytest.fixture(autouse=True)
def clean_registry():
    """每条用例前后都清干净，避免插件注册泄漏到别的用例。"""
    saved = dict(plugins._REGISTRY)
    plugins._REGISTRY.clear()
    yield
    plugins._REGISTRY.clear()
    plugins._REGISTRY.update(saved)


def _meta_content(ctx: ExtractorContext):
    return (ctx.nodes[0].attributes or {}).get("content") if ctx.nodes else None


def test_plugin_type_extracts_value():
    register_extractor("meta_content", _meta_content)
    html = '<html><head><meta name="description" content="整洁正文"></head></html>'
    result = extract_from_html(
        html,
        {"desc": {"selector": 'meta[name="description"]', "type": "meta_content"}},
        base_url="https://example.com/",
    )
    assert result.fields == {"desc": "整洁正文"}
    assert result.errors == []


def test_plugin_reads_custom_rule_key_and_gets_all_nodes():
    def joined(ctx):
        sep = ctx.rule.get("sep", "/")
        return sep.join((node.text() or "").strip() for node in ctx.nodes)

    register_extractor("joined_text", joined)
    html = "<ul><li>a</li><li>b</li><li>c</li></ul>"
    result = extract_from_html(
        html, {"tags": {"selector": "li", "type": "joined_text", "sep": "-"}}
    )
    assert result.fields["tags"] == "a-b-c"


def test_plugin_result_still_goes_through_postprocessing():
    def first_href(ctx):
        return (ctx.nodes[0].attributes or {}).get("href") if ctx.nodes else None

    register_extractor("first_href", first_href)
    html = '<p><a href="/docs/a">doc</a></p>'
    result = extract_from_html(
        html,
        {
            "link": {
                "selector": "a",
                "type": "first_href",
                "abs_url": True,
            }
        },
        base_url="https://example.com/base/",
    )
    assert result.fields["link"] == "https://example.com/docs/a"


def test_plugin_failure_isolated_to_field_and_required_reported():
    def boom(ctx):
        raise RuntimeError("插件炸了")

    register_extractor("boom", boom)
    html = "<p>x</p>"
    result = extract_from_html(
        html,
        {
            "bad": {"selector": "p", "type": "boom", "default": "fallback"},
            "good": {"selector": "p"},
        },
    )
    assert result.fields == {"bad": "fallback", "good": "x"}
    assert any("插件 boom 执行失败" in item for item in result.errors)
    assert result.required_missing == []


def test_plugin_returning_empty_falls_back_to_default():
    register_extractor("nothing", lambda ctx: None)
    result = extract_from_html(
        "<p>x</p>", {"v": {"selector": "p", "type": "nothing", "default": "d"}}
    )
    assert result.fields["v"] == "d"


def test_unknown_type_error_lists_registered_plugins():
    register_extractor("meta_content", _meta_content)
    with pytest.raises(SchemaValidationError) as excinfo:
        extract_from_html("<p>x</p>", {"v": {"selector": "p", "type": "nope"}})
    assert "meta_content" in str(excinfo.value)


def test_plugin_type_rejects_nested_fields():
    register_extractor("meta_content", _meta_content)
    with pytest.raises(SchemaValidationError) as excinfo:
        extract_from_html(
            "<p>x</p>",
            {"v": {"selector": "p", "type": "meta_content", "fields": {"a": "b"}}},
        )
    assert "不支持 fields" in str(excinfo.value)


@pytest.mark.parametrize("name", ["text", "list", "Bad-Name", "", "1abc"])
def test_register_rejects_builtin_or_invalid_names(name):
    with pytest.raises(ValueError):
        register_extractor(name, _meta_content)


def test_get_extractor_and_names():
    assert get_extractor("missing") is None
    register_extractor("meta_content", _meta_content)
    assert extractor_names() == ["meta_content"]
    assert get_extractor("meta_content") is _meta_content


def test_entry_points_are_discovered(monkeypatch):
    class FakeEntry:
        name = "from_entry_point"

        @staticmethod
        def load():
            return _meta_content

    monkeypatch.setattr(plugins, "entry_points", lambda group=None: [FakeEntry()])
    plugins._loaded = False
    try:
        assert get_extractor("from_entry_point") is _meta_content
    finally:
        plugins._loaded = False


def test_broken_entry_point_does_not_break_others(monkeypatch):
    class Broken:
        name = "broken"

        @staticmethod
        def load():
            raise ImportError("缺依赖")

    class Good:
        name = "good_one"

        @staticmethod
        def load():
            return _meta_content

    monkeypatch.setattr(plugins, "entry_points", lambda group=None: [Broken(), Good()])
    plugins._loaded = False
    try:
        assert get_extractor("good_one") is _meta_content
        assert get_extractor("broken") is None
    finally:
        plugins._loaded = False
