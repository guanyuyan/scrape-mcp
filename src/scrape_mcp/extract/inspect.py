"""页面交互地图：只读列出可点击/可输入元素与表单，供 Agent 规划动作。

输出的是"稳定 ref"，不是整页 HTML：
- ref 优先取自 id / data-testid / name / aria-label，缺失时用 nth-child 路径
- ref 本身是合法 CSS 选择器，可直接塞进 ``actions[].selector`` 由浏览器执行
- 文本截断 80 字符，元素数量有上限；不提供任意 JS 执行入口
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

TEXT_LIMIT = 80
MAX_FORMS = 10
MAX_FORM_FIELDS = 20
MAX_DEPTH = 8

_DROP = "script, style, noscript, svg, template"
_INTERACTIVE = (
    "a[href], button, input, select, textarea, "
    "[role=button], [role=link], [role=tab], [role=menuitem], "
    "[role=checkbox], [role=radio], [role=textbox], [role=searchbox], "
    "[contenteditable=true], [onclick]"
)
_ID_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_NEXT_TEXT = re.compile(
    r"^(下一页|下页|后一页|next|next page|older|more|more results|›|»|→)$",
    re.IGNORECASE,
)
_NEXT_REL = re.compile(r"(^|\s)next(\s|$)", re.IGNORECASE)
_INPUT_ROLES = {
    "button": "button",
    "submit": "button",
    "reset": "button",
    "image": "button",
    "checkbox": "checkbox",
    "radio": "radio",
    "search": "searchbox",
    "text": "textbox",
    "email": "textbox",
    "password": "textbox",
    "tel": "textbox",
    "url": "textbox",
    "number": "textbox",
    "file": "button",
}


def _norm(text: object) -> str:
    return " ".join(str(text or "").split())


def _clip(text: str) -> str:
    return text[:TEXT_LIMIT]


def _hidden(node: Node) -> bool:
    attrs = node.attributes
    if "hidden" in attrs or attrs.get("aria-hidden", "").lower() == "true":
        return True
    if node.tag == "input" and (attrs.get("type") or "").lower() == "hidden":
        return True
    style = (attrs.get("style") or "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


def _role(node: Node) -> str:
    role = (node.attributes.get("role") or "").strip().split()
    if role:
        return role[0].lower()
    tag = node.tag
    if tag == "a":
        return "link"
    if tag == "button":
        return "button"
    if tag == "select":
        return "combobox"
    if tag == "textarea":
        return "textbox"
    if tag == "input":
        return _INPUT_ROLES.get((node.attributes.get("type") or "text").lower(), "textbox")
    return tag


def _label(node: Node) -> str:
    attrs = node.attributes
    for key in ("aria-label", "placeholder", "title", "alt", "name"):
        value = _norm(attrs.get(key))
        if value:
            return value
    if node.tag == "input":
        value = _norm(attrs.get("value"))
        if value:
            return value
    return _norm(node.text(deep=True))


def _segment(node: Node) -> str:
    """相对父节点的 nth-child 片段；只数元素节点（文本/注释节点不计入）。"""
    index = 1
    prev = node.prev
    while prev is not None:
        if not prev.tag.startswith("-"):
            index += 1
        prev = prev.prev
    return f"{node.tag}:nth-child({index})" if index > 1 else node.tag


def _path(node: Node) -> str:
    parts: list[str] = []
    current: Node | None = node
    depth = 0
    while current is not None and depth < MAX_DEPTH:
        tag = current.tag
        if tag.startswith("-") or tag == "html":
            break
        node_id = current.attributes.get("id") or ""
        if _ID_OK.match(node_id) and tag != "body":
            parts.append(f"#{node_id}")
            break
        if tag == "body":
            parts.append("body")
            break
        parts.append(_segment(current))
        current = current.parent
        depth += 1
    return " > ".join(reversed(parts)) or node.tag


def _selector(node: Node) -> str:
    """生成稳定且合法的 CSS 选择器；优先语义属性，退化到位置路径。"""
    attrs = node.attributes
    node_id = attrs.get("id") or ""
    if _ID_OK.match(node_id):
        return f"#{node_id}"
    for key, template in (
        ("data-testid", '[data-testid="{value}"]'),
        ("name", '{tag}[name="{value}"]'),
        ("aria-label", '{tag}[aria-label="{value}"]'),
    ):
        value = _norm(attrs.get(key))
        if not value or '"' in value or len(value) > 64:
            continue
        return template.format(value=value, tag=node.tag)
    return _path(node)


def _element(node: Node) -> dict:
    attrs = node.attributes
    return {
        "ref": _selector(node),
        "role": _role(node),
        "tag": node.tag,
        "text": _clip(_label(node)),
        "name": _norm(attrs.get("name")),
        "type": _norm(attrs.get("type")),
        "href": _norm(attrs.get("href")),
        "disabled": "disabled" in attrs,
    }


def _forms(tree: HTMLParser, base_url: str) -> list[dict]:
    forms: list[dict] = []
    for form in tree.css("form"):
        if len(forms) >= MAX_FORMS:
            break
        attrs = form.attributes
        fields: list[dict] = []
        for node in form.css("input, select, textarea"):
            if len(fields) >= MAX_FORM_FIELDS:
                break
            if _hidden(node):
                continue
            item = _element(node)
            if not item["name"] and not item["text"]:
                continue
            fields.append(
                {
                    "name": item["name"],
                    "type": item["type"] or node.tag,
                    "ref": item["ref"],
                }
            )
        forms.append(
            {
                "action": urljoin(base_url, _norm(attrs.get("action")) or base_url),
                "method": (_norm(attrs.get("method")) or "get").lower(),
                "fields": fields,
            }
        )
    return forms


def _next_links(tree: HTMLParser, base_url: str) -> list[dict]:
    links: list[dict] = []
    seen: set[str] = set()
    for node in tree.css("a[href]"):
        attrs = node.attributes
        href = _norm(attrs.get("href"))
        text = _norm(node.text(deep=True))
        rel = _norm(attrs.get("rel"))
        if not href or href.startswith(("javascript:", "#")):
            continue
        if not (_NEXT_TEXT.match(text) or _NEXT_REL.search(rel)):
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        links.append({"text": _clip(text), "href": url})
        if len(links) >= 5:
            break
    return links


def inspect_html(html: str, url: str, max_elements: int = 120) -> dict:
    """解析已渲染的 HTML，返回 elements / forms / next 三部分。"""
    tree = HTMLParser(html or "")
    for node in tree.css(_DROP):
        node.decompose()

    elements: list[dict] = []
    seen_refs: set[str] = set()
    for node in tree.css(_INTERACTIVE):
        if len(elements) >= max_elements:
            break
        if _hidden(node):
            continue
        item = _element(node)
        if item["ref"] in seen_refs:
            continue
        seen_refs.add(item["ref"])
        elements.append(item)

    return {
        "elements": elements,
        "forms": _forms(tree, url),
        "next": _next_links(tree, url),
    }
