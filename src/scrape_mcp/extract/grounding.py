"""字段级溯源：为 web_extract 抽到的每个字段标出它在页面里的位置与原文片段。

不做第二次抽取：schema 解析阶段顺手记下命中的节点，键路径与 ``data`` 完全一致
（含 list 下标，如 ``items[0].price``），因此不会出现"data 有值、grounding 对不上"。

片段（snippet）截断到配置长度；偏移（offset）优先相对 compact 后的 markdown，
找不到时再回退原始 HTML，两者都找不到说明是动态渲染页，只保证 selector 与 snippet。
"""

from __future__ import annotations

import re
from typing import Any

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .inspect import _selector

_WS = re.compile(r"\s+")
_CLASS = re.compile(r"\.([A-Za-z_][\w-]{1,})")
_ID = re.compile(r"#([A-Za-z_][\w-]{1,})")
_TAG = re.compile(r"(?:^|[\s>+~])([a-zA-Z][\w-]*)")
# 让候选抽取得出有意义文本的常见内容容器；html/body/script 之类跳过
_CONTENT_TAGS = frozenset(
    {"div", "span", "p", "td", "th", "li", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "em"}
)


def _norm(text: Any) -> str:
    return _WS.sub(" ", str(text or "")).strip()


def _clip(text: str, limit: int) -> str:
    return text[: max(1, limit)]


def locate(text: str, haystack: str) -> int | None:
    """在 basis 文本里定位片段；返回字符偏移，找不到返回 None（动态页常见）。"""
    if not text or not haystack:
        return None
    direct = haystack.find(text)
    if direct >= 0:
        return direct
    # 节点文本可能跨标签拼接，整段找不到时退到更短的前缀（仍要求足够长，避免误命中）
    for size in (40, 20, 10, 6, 4):
        if size >= len(text):
            continue
        found = haystack.find(text[:size])
        if found >= 0:
            return found
    return None


def build(
    hits: dict[str, dict],
    *,
    markdown: str,
    html: str,
    snippet_chars: int,
) -> dict[str, dict]:
    """把抽取阶段记录的命中节点转成 ``{path: {selector, snippet, offset, basis}}``。"""
    markdown = markdown or ""
    html = html or ""
    grounding: dict[str, dict] = {}
    for path, hit in hits.items():
        node = hit.get("node")
        kind = hit.get("type")
        if node is not None and kind != "attr":
            # 片段取自页面原文而不是转换后的值（10 而不是 10.0），才能按原文回查
            raw = node.html if kind == "html" else node.text(deep=True)
            snippet = _clip(_norm(raw), snippet_chars)
        else:
            snippet = _clip(_norm(hit.get("value")), snippet_chars)
        selector = _selector(node) if node is not None else str(hit.get("selector") or "")
        basis = "markdown"
        offset = locate(snippet, markdown)
        if offset is None:
            basis = "html"
            offset = locate(snippet, html)
        grounding[path] = {
            "selector": selector,
            "snippet": snippet,
            "offset": offset,
            "basis": basis,
        }
    return grounding


def _tokens(selector: str) -> list[tuple[str, str]]:
    """按 class → id → tag 的顺序给出可尝试的近似线索（class 最具体）。

    class 会再按 ``-`` / ``_`` 拆出子词（``product-price`` → ``price``），
    站点改版常只改前缀或后缀，子词仍能命中。
    """
    classes: list[str] = []
    for token in _CLASS.findall(selector):
        classes.append(token)
        for part in re.split(r"[-_]", token):
            if len(part) >= 3 and part != token:
                classes.append(part)
    found = [("class", token) for token in classes]
    found += [("id", token) for token in _ID.findall(selector)]
    found += [("tag", token) for token in _TAG.findall(selector)]
    return found


def _css(kind: str, token: str) -> str | None:
    if kind == "class":
        return f'[class*="{token}"]'
    if kind == "id":
        return f'[id*="{token}"]'
    return token if token.lower() in _CONTENT_TAGS else None


def candidates(
    html: str,
    selector: str,
    *,
    limit: int = 3,
    snippet_chars: int = 120,
) -> list[dict]:
    """字段选择器没命中时，按其中的 class/id/tag 词找近似节点。

    目的不是自动修复，而是让调用方判断"页面改版"还是"选择器写错"。
    """
    if not selector:
        return []
    tree = HTMLParser(html or "")
    for kind, token in _tokens(selector):
        css = _css(kind, token)
        if not css:
            continue
        try:
            nodes = tree.css(css)
        except Exception:  # lexbor 对极端选择器可能直接抛错，跳过这条线索即可
            continue
        found: list[dict] = []
        for node in nodes:
            if node.tag.startswith("-"):
                continue
            snippet = _clip(_norm(node.text(deep=True)), snippet_chars)
            if not snippet:
                continue
            found.append({"selector": _selector(node), "snippet": snippet})
            if len(found) >= max(1, limit):
                break
        if found:
            return found
    return []
