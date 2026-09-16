"""结构化抽取（阶段 1-①）：按用户提供的字段 schema，从 HTML 提取结构化 JSON。

这是从"省 token 的文本工具"走向"按需取字段值"的升级——给 URL 和字段，
拿到的是可直接使用的字段值，而不是一大段正文。

字段语法（对 LLM / 使用者友好的 dict）：
    {
      "fields": {
        "title": {"selector": "h1", "type": "text"},
        "price": {"selector": ".price", "type": "text"},
        "link":  {"selector": "a.btn", "type": "attr", "attr": "href"},
        "count": {"selector": "ul li", "type": "count"},
        "tags":  {"selector": ".tag", "type": "list", "list_key": "text"}
      }
    }

字段类型：
    text  —— 取匹配节点归一化文本（默认）
    attr  —— 取匹配节点的某属性值，需配合 attr
    count —— 取匹配节点数量
    list  —— 取所有匹配节点，每个节点按 list_key 取值，返回数组
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dcfield
from typing import Any

from selectolax.parser import HTMLParser

_TYPES = ("text", "attr", "count", "list")
_LIST_KEYS = ("text", "text_trimmed", "attr", "html")


@dataclass
class SchemaField:
    key: str
    selector: str
    type: str = "text"
    attr: str | None = None
    list_key: str = "text"
    default: Any = None


@dataclass
class SchemaResult:
    fields: dict[str, Any]
    errors: list[str] = dcfield(default_factory=list)


class SchemaValidationError(ValueError):
    pass


def parse_schema(schema: Any) -> list[SchemaField]:
    """校验并规范化用户 schema，非法则抛 SchemaValidationError。"""
    raw = schema["fields"] if isinstance(schema, dict) and "fields" in schema else schema
    if not isinstance(raw, dict) or not raw:
        raise SchemaValidationError("schema 需为 {'fields': {字段名: 规则}} 或字段名的 dict，且非空")

    fields: list[SchemaField] = []
    seen: set[str] = set()
    for key, rule in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise SchemaValidationError("字段名必须是非空字符串")
        if key in seen:
            raise SchemaValidationError(f"字段名重复: {key}")
        seen.add(key)
        if isinstance(rule, str):
            # 简写：字段名对应一个 CSS 选择器
            fields.append(SchemaField(key=key, selector=rule))
            continue
        if not isinstance(rule, dict):
            raise SchemaValidationError(f"字段 {key} 的规则必须是 dict 或字符串选择器")
        selector = rule.get("selector") or rule.get("select")
        if not selector or not isinstance(selector, str):
            raise SchemaValidationError(f"字段 {key} 缺少 selector")
        ftype = rule.get("type", "text")
        if ftype not in _TYPES:
            raise SchemaValidationError(f"字段 {key} 的 type 必须是 {_TYPES} 之一")
        attr = rule.get("attr")
        if ftype == "attr" and not attr:
            raise SchemaValidationError(f"字段 {key} 的 type=attr 必须提供 attr")
        if ftype == "attr" and not isinstance(attr, str):
            raise SchemaValidationError(f"字段 {key} 的 attr 必须是字符串")
        list_key = rule.get("list_key", "text")
        if ftype == "list" and list_key not in _LIST_KEYS:
            raise SchemaValidationError(f"字段 {key} 的 list_key 必须是 {_LIST_KEYS} 之一")
        fields.append(
            SchemaField(
                key=key,
                selector=selector,
                type=ftype,
                attr=attr,
                list_key=list_key,
                default=rule.get("default"),
            )
        )
    return fields


def _norm(text: str) -> str:
    return " ".join(text.split())


def _node_text(node: Any, trimmed: bool = False) -> str:
    text = _norm(node.text(deep=True, separator=" ") or "")
    if trimmed:
        return text
    return text


def _html_of(node: Any) -> str:
    return _norm(node.html or "")


def _extract_node(node: Any, f: SchemaField) -> Any:
    if f.type == "attr":
        return (node.attributes or {}).get(f.attr or "")
    if f.type == "html":
        return _html_of(node)
    return _node_text(node, trimmed=(f.list_key == "text_trimmed"))


def extract_fields(html: str, schema: list[SchemaField]) -> SchemaResult:
    """在 HTML 上执行 schema，返回字段值 dict 与抽样失败的错误清单。"""
    tree = HTMLParser(html or "")
    fields: dict[str, Any] = {}
    errors: list[str] = []

    for f in schema:
        try:
            nodes = tree.css(f.selector)
        except Exception as exc:  # selectolax 对非法 CSS 的兜底
            errors.append(f"字段 {f.key}: 选择器非法: {exc}")
            continue

        if f.type == "count":
            fields[f.key] = len(nodes)
            continue

        if not nodes:
            if f.default is not None:
                fields[f.key] = f.default
            else:
                fields[f.key] = None
            continue

        if f.type == "list":
            values = []
            for node in nodes:
                if f.list_key == "html":
                    values.append(_html_of(node))
                elif f.list_key == "attr" and f.attr:
                    values.append((node.attributes or {}).get(f.attr))
                else:
                    values.append(_node_text(node, trimmed=(f.list_key == "text_trimmed")))
            fields[f.key] = values
        else:
            fields[f.key] = _extract_node(nodes[0], f)

    return SchemaResult(fields=fields, errors=errors)


def extract_from_html(html: str, schema: Any) -> SchemaResult:
    """便捷入口：接收原始 schema（dict 或字段 dict），内部完成校验与抽取。"""
    parsed = parse_schema(schema)
    return extract_fields(html, parsed)
