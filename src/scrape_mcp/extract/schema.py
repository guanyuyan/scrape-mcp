"""结构化抽取：按用户提供的字段 schema，从 HTML 提取结构化 JSON。

这是从"省 token 的文本工具"走向"按需取字段值"的升级——给 URL 和字段，
拿到的是可直接使用的字段值，而不是一大段正文。

字段语法（对 LLM / 使用者友好的 dict）：
    {
      "fields": {
        "title": {"selector": "h1"},
        "price": {"selector": ".price", "regex": "(\\d+(?:\\.\\d+)?)", "group": 1},
        "url":   {"selector": "a.btn", "type": "attr", "attr": "href", "abs_url": true},
        "count": {"selector": "ul li", "type": "count"},
        "tags":  {"selector": ".tag", "type": "list", "list_key": "text"},
        "items": {"selector": ".item", "type": "list", "fields": {
            "name": {"selector": ".name"},
            "link": {"selector": "a", "type": "attr", "attr": "href", "abs_url": true}
        }},
        "author": {"selector": ".author", "type": "object", "fields": {
            "name": {"selector": ".name"},
            "bio":  {"selector": ".bio"}
        }}
      }
    }

字段类型：
    text   —— 取匹配节点归一化文本（默认）
    html   —— 取匹配节点 HTML 片段
    attr   —— 取匹配节点的某属性值，需配合 attr
    count  —— 取匹配节点数量
    list   —— 取所有匹配节点；配 fields 时返回对象数组，否则按 list_key 取值
    object —— 取第一个匹配节点的子树，按 fields 在其内部继续抽取

后处理（对字符串结果生效，按 regex → abs_url → strip 顺序）：
    regex / group —— 正则提取，未命中时回落到 default（无 default 则 None）
    abs_url       —— 把相对链接拼成绝对 URL（以页面 final_url 为基准）
    strip         —— 是否去除首尾空白，默认 true
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dcfield
from typing import Any
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

_TYPES = ("text", "html", "attr", "count", "list", "object")
_LIST_KEYS = ("text", "text_trimmed", "attr", "html")


@dataclass
class SchemaField:
    key: str
    selector: str
    type: str = "text"
    attr: str | None = None
    list_key: str = "text"
    default: Any = None
    fields: list[SchemaField] | None = None
    abs_url: bool = False
    regex: str | None = None
    group: int = 0
    strip: bool = True


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
        raise SchemaValidationError(
            "schema 需为 {'fields': {字段名: 规则}} 或字段名的 dict，且非空"
        )
    return _parse_fields(raw, path="")


def _parse_fields(raw: dict, *, path: str) -> list[SchemaField]:
    fields: list[SchemaField] = []
    seen: set[str] = set()
    for key, rule in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise SchemaValidationError("字段名必须是非空字符串")
        if key in seen:
            raise SchemaValidationError(f"字段名重复: {path}{key}")
        seen.add(key)
        fields.append(_parse_rule(key, rule, path=path))
    return fields


def _parse_rule(key: str, rule: Any, *, path: str) -> SchemaField:
    label = f"{path}{key}"
    if isinstance(rule, str):
        # 简写：字段名对应一个 CSS 选择器
        return SchemaField(key=key, selector=rule)
    if not isinstance(rule, dict):
        raise SchemaValidationError(f"字段 {label} 的规则必须是 dict 或字符串选择器")

    ftype = rule.get("type", "text")
    if ftype not in _TYPES:
        raise SchemaValidationError(f"字段 {label} 的 type 必须是 {_TYPES} 之一")

    selector = rule.get("selector") or rule.get("select")
    if not selector:
        raise SchemaValidationError(f"字段 {label} 缺少 selector")
    if not isinstance(selector, str):
        raise SchemaValidationError(f"字段 {label} 的 selector 必须是字符串")

    attr = rule.get("attr")
    if ftype == "attr" and not attr:
        raise SchemaValidationError(f"字段 {label} 的 type=attr 必须提供 attr")
    if attr is not None and not isinstance(attr, str):
        raise SchemaValidationError(f"字段 {label} 的 attr 必须是字符串")

    list_key = rule.get("list_key", "text")
    if ftype == "list" and not rule.get("fields") and list_key not in _LIST_KEYS:
        raise SchemaValidationError(f"字段 {label} 的 list_key 必须是 {_LIST_KEYS} 之一")

    sub_raw = rule.get("fields") if "fields" in rule else rule.get("item_fields")
    if ftype in ("object", "list") and sub_raw is not None:
        if not isinstance(sub_raw, dict) or not sub_raw:
            raise SchemaValidationError(f"字段 {label} 的 fields 必须是非空 dict")
    elif ftype == "object":
        raise SchemaValidationError(f"字段 {label} 的 type=object 必须提供 fields")
    sub_fields = _parse_fields(sub_raw, path=f"{label}.") if sub_raw else None

    regex = rule.get("regex")
    if regex is not None:
        if not isinstance(regex, str):
            raise SchemaValidationError(f"字段 {label} 的 regex 必须是字符串")
        try:
            re.compile(regex)
        except re.error as exc:
            raise SchemaValidationError(f"字段 {label} 的 regex 非法: {exc}") from exc

    group = rule.get("group", 0)
    if not isinstance(group, int) or isinstance(group, bool) or group < 0:
        raise SchemaValidationError(f"字段 {label} 的 group 必须是非负整数")

    return SchemaField(
        key=key,
        selector=selector,
        type=ftype,
        attr=attr,
        list_key=list_key,
        default=rule.get("default"),
        fields=sub_fields,
        abs_url=bool(rule.get("abs_url", False)),
        regex=regex,
        group=group,
        strip=bool(rule.get("strip", True)),
    )


def _norm(text: str) -> str:
    return " ".join(text.split())


def _node_text(node: Any) -> str:
    return _norm(node.text(deep=True, separator=" ") or "")


def _html_of(node: Any) -> str:
    return (node.html or "").strip()


def _raw_value(node: Any, f: SchemaField) -> Any:
    """按字段类型取节点原始值，尚未做 regex/abs_url 等后处理。"""
    if f.type == "attr":
        return (node.attributes or {}).get(f.attr or "")
    if f.type == "html" or (f.type == "list" and f.list_key == "html"):
        return _html_of(node)
    if f.type == "list" and f.list_key == "attr":
        return (node.attributes or {}).get(f.attr or "")
    if f.type == "list" and f.list_key == "text_trimmed":
        return _norm(node.text(deep=True, separator=" ") or "")
    return _node_text(node)


def _apply(value: Any, f: SchemaField, base_url: str) -> Any:
    """对单个值做后处理；正则未命中时回落 default。"""
    if not isinstance(value, str):
        return value
    if f.regex:
        match = re.search(f.regex, value)
        if match is None:
            return f.default
        try:
            value = match.group(f.group)
        except (IndexError, re.error):
            return f.default
    if f.abs_url and value:
        value = urljoin(base_url, value)
    return value.strip() if f.strip else value


def _run(scope: Any, schema: list[SchemaField], base_url: str, path: str = "") -> SchemaResult:
    fields: dict[str, Any] = {}
    errors: list[str] = []

    for f in schema:
        label = f"{path}{f.key}"
        try:
            nodes = scope.css(f.selector)
        except Exception as exc:  # selectolax 对非法 CSS 的兜底
            errors.append(f"字段 {label}: 选择器非法: {exc}")
            fields[f.key] = f.default
            continue

        if f.type == "count":
            fields[f.key] = len(nodes)
            continue

        if not nodes:
            fields[f.key] = f.default
            continue

        if f.type == "object":
            sub = _run(nodes[0], f.fields or [], base_url, path=f"{label}.")
            fields[f.key] = sub.fields if sub.fields else None
            errors.extend(sub.errors)
            continue

        if f.type == "list":
            if f.fields:
                items: list[dict[str, Any]] = []
                for node in nodes:
                    sub = _run(node, f.fields, base_url, path=f"{label}.")
                    items.append(sub.fields)
                    errors.extend(sub.errors)
                fields[f.key] = items
            else:
                fields[f.key] = [_apply(_raw_value(node, f), f, base_url) for node in nodes]
            continue

        fields[f.key] = _apply(_raw_value(nodes[0], f), f, base_url)

    return SchemaResult(fields=fields, errors=errors)


def extract_fields(html: str, schema: list[SchemaField], *, base_url: str = "") -> SchemaResult:
    """在 HTML 上执行 schema，返回字段值 dict 与抽样失败的错误清单。"""
    tree = HTMLParser(html or "")
    return _run(tree, schema, base_url)


def extract_from_html(html: str, schema: Any, *, base_url: str = "") -> SchemaResult:
    """便捷入口：接收原始 schema（dict 或字段 dict），内部完成校验与抽取。"""
    parsed = parse_schema(schema)
    return extract_fields(html, parsed, base_url=base_url)
