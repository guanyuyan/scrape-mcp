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
    jsonld —— 从页面 <script type="application/ld+json"> 里按 path 取值，
              路径用点号分隔（如 "author.name"），遇到数组自动展开，无需 selector

后处理（对字符串结果生效，按 regex → abs_url → strip → convert 顺序）：
    regex / group —— 正则提取，未命中时回落到 default（无 default 则 None）
    abs_url       —— 把相对链接拼成绝对 URL（以页面 final_url 为基准）
    strip         —— 是否去除首尾空白，默认 true
    convert       —— 类型转换：int / float / date（date 统一输出 ISO 8601）
    required      —— 必填字段；最终值为空时记入 required_missing，由上层整体判失败
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from dataclasses import field as dcfield
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

_TYPES = ("text", "html", "attr", "count", "list", "object", "jsonld")
_LIST_KEYS = ("text", "text_trimmed", "attr", "html")
_CONVERT_TYPES = ("int", "float", "date")
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y年%m月%d日",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


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
    # jsonld 专用：点号分隔的取值路径，数组自动展开
    path: str | None = None
    convert: str | None = None
    required: bool = False


@dataclass
class SchemaResult:
    fields: dict[str, Any]
    errors: list[str] = dcfield(default_factory=list)
    required_missing: list[str] = dcfield(default_factory=list)


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
    path = rule.get("path")
    if ftype == "jsonld":
        if not isinstance(path, str) or not path.strip():
            raise SchemaValidationError(f"字段 {label} 的 type=jsonld 必须提供 path")
        selector = selector or ""
    else:
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

    convert = rule.get("convert")
    if convert is not None and convert not in _CONVERT_TYPES:
        raise SchemaValidationError(f"字段 {label} 的 convert 必须是 {_CONVERT_TYPES} 之一")

    required = rule.get("required", False)
    if not isinstance(required, bool):
        raise SchemaValidationError(f"字段 {label} 的 required 必须是布尔值")

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
        path=path,
        convert=convert,
        required=required,
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


# ---------- JSON-LD ----------


def _collect_jsonld(data: Any, out: list[Any]) -> None:
    """把单个 script 的内容展开成对象列表（含 @graph）。"""
    if isinstance(data, list):
        for item in data:
            _collect_jsonld(item, out)
    elif isinstance(data, dict):
        out.append(data)
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                _collect_jsonld(item, out)


def _walk_path(value: Any, parts: list[str]) -> Any:
    """按点号路径取值；数组自动展开（数字段表示下标）。"""
    if not parts:
        return value
    head, rest = parts[0], parts[1:]
    if isinstance(value, list):
        if head.isdigit():
            index = int(head)
            return _walk_path(value[index], rest) if 0 <= index < len(value) else None
        collected: list[Any] = []
        for item in value:
            sub = _walk_path(item, parts)
            if isinstance(sub, list):
                collected.extend(sub)
            elif sub is not None:
                collected.append(sub)
        return collected or None
    if isinstance(value, dict):
        if head not in value:
            return None
        return _walk_path(value[head], rest)
    return None


def _jsonld_value(scope: Any, f: SchemaField) -> Any:
    docs: list[Any] = []
    try:
        nodes = scope.css('script[type="application/ld+json"]')
    except Exception:
        nodes = []
    for node in nodes:
        try:
            data = json.loads(node.text() or "")
        except (json.JSONDecodeError, ValueError):
            continue
        _collect_jsonld(data, docs)
    if not docs:
        return f.default

    results: list[Any] = []
    for doc in docs:
        value = _walk_path(doc, (f.path or "").split("."))
        if value is None:
            continue
        if isinstance(value, list):
            results.extend(item for item in value if item is not None)
        else:
            results.append(value)
    if not results:
        return f.default
    return results[0] if len(results) == 1 else results


# ---------- 类型转换与必填 ----------


def _clean_number(text: str) -> str | None:
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
    return match.group(0).replace(",", "") if match else None


def _to_date(text: str) -> str | None:
    raw = text.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for fmt in _DATE_FORMATS:
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None and (dt.hour, dt.minute, dt.second) == (0, 0, 0):
        return dt.date().isoformat()
    return dt.isoformat()


def _convert_value(value: Any, target: str) -> Any:
    if target == "date":
        return _to_date(value) if isinstance(value, str) else None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if target == "int" else float(value)
    if not isinstance(value, str):
        return None
    raw = _clean_number(value)
    if raw is None:
        return None
    try:
        return int(float(raw)) if target == "int" else float(raw)
    except ValueError:
        return None


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _finalize(value: Any, f: SchemaField, base_url: str, errors: list[str], label: str) -> Any:
    if isinstance(value, list):
        return [_finalize_one(item, f, base_url, errors, label) for item in value]
    return _finalize_one(value, f, base_url, errors, label)


def _finalize_one(value: Any, f: SchemaField, base_url: str, errors: list[str], label: str) -> Any:
    value = _apply(value, f, base_url)
    if not f.convert or _is_missing(value):
        return value
    converted = _convert_value(value, f.convert)
    if converted is None:
        errors.append(f"字段 {label}: 无法转换为 {f.convert}: {value!r}")
        return f.default
    return converted


def _run(scope: Any, schema: list[SchemaField], base_url: str, path: str = "") -> SchemaResult:
    fields: dict[str, Any] = {}
    errors: list[str] = []
    required_missing: list[str] = []

    for f in schema:
        label = f"{path}{f.key}"

        if f.type == "jsonld":
            value = _finalize(_jsonld_value(scope, f), f, base_url, errors, label)
            fields[f.key] = value
            if f.required and _is_missing(value):
                required_missing.append(label)
            continue

        try:
            nodes = scope.css(f.selector)
        except Exception as exc:  # selectolax 对非法 CSS 的兜底
            errors.append(f"字段 {label}: 选择器非法: {exc}")
            fields[f.key] = f.default
            if f.required:
                required_missing.append(label)
            continue

        if f.type == "count":
            fields[f.key] = len(nodes)
            continue

        if not nodes:
            fields[f.key] = f.default
            if f.required:
                required_missing.append(label)
            continue

        if f.type == "object":
            sub = _run(nodes[0], f.fields or [], base_url, path=f"{label}.")
            fields[f.key] = sub.fields if sub.fields else None
            errors.extend(sub.errors)
            required_missing.extend(sub.required_missing)
            continue

        if f.type == "list":
            if f.fields:
                items: list[dict[str, Any]] = []
                for node in nodes:
                    sub = _run(node, f.fields, base_url, path=f"{label}.")
                    items.append(sub.fields)
                    errors.extend(sub.errors)
                    required_missing.extend(sub.required_missing)
                fields[f.key] = items
            else:
                fields[f.key] = [
                    _finalize(_raw_value(node, f), f, base_url, errors, label) for node in nodes
                ]
            if f.required and _is_missing(fields[f.key]):
                required_missing.append(label)
            continue

        value = _finalize(_raw_value(nodes[0], f), f, base_url, errors, label)
        fields[f.key] = value
        if f.required and _is_missing(value):
            required_missing.append(label)

    return SchemaResult(fields=fields, errors=errors, required_missing=required_missing)


def extract_fields(html: str, schema: list[SchemaField], *, base_url: str = "") -> SchemaResult:
    """在 HTML 上执行 schema，返回字段值 dict 与抽样失败的错误清单。"""
    tree = HTMLParser(html or "")
    return _run(tree, schema, base_url)


def extract_from_html(html: str, schema: Any, *, base_url: str = "") -> SchemaResult:
    """便捷入口：接收原始 schema（dict 或字段 dict），内部完成校验与抽取。"""
    parsed = parse_schema(schema)
    return extract_fields(html, parsed, base_url=base_url)
