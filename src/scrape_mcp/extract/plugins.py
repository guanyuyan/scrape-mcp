"""自定义抽取类型（插件）：让第三方包往 ``web_extract`` 里加字段类型。

用法（插件包侧）::

    # pyproject.toml
    [project.entry-points."scrape_mcp.extractors"]
    meta_content = "my_pkg.extractors:meta_content"

    # my_pkg/extractors.py
    def meta_content(ctx):
        return (ctx.nodes[0].attributes or {}).get("content") if ctx.nodes else None

之后 ``web_extract`` 就能用 ``{"type": "meta_content", "selector": "meta[name=description]"}``。

约定：
- 名字必须是小写标识符，且不能占用内置类型（text/html/attr/count/list/object/jsonld）；
- 收到的是命中节点列表（可能为空）与原始字段规则，可返回任意 JSON 可序列化值；
- 返回的字符串/list 仍会走 regex / abs_url / strip / convert / required 等后处理；
- 抛异常不会让整条抽取失败，而是记进 ``errors`` 并回落到该字段的 ``default``。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

logger = logging.getLogger(__name__)

# 入口点组名：插件在 pyproject 里用 [project.entry-points."scrape_mcp.extractors"] 注册
ENTRY_POINT_GROUP = "scrape_mcp.extractors"

# 内置类型（schema.py 也以此为准），插件不得占用
BUILTIN_TYPES = ("text", "html", "attr", "count", "list", "object", "jsonld")

_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")


@dataclass
class ExtractorContext:
    """插件拿到的上下文。"""

    nodes: list[Any]  # 选择器命中的节点；未命中时为空列表
    rule: dict[str, Any]  # 原始字段规则，插件可读自定义键（如 "format"）
    base_url: str  # 页面最终地址，便于插件自己拼绝对链接
    root: Any  # 整页解析树（selectolax LexborHTMLParser），可读文档级信息


Extractor = Callable[[ExtractorContext], Any]

_REGISTRY: dict[str, Extractor] = {}
_loaded = False


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError(f"抽取器名字必须是小写标识符（如 meta_content），收到 {name!r}")
    if name in BUILTIN_TYPES:
        raise ValueError(f"抽取器名字 {name!r} 与内置类型冲突")


def register_extractor(name: str, func: Extractor) -> None:
    """注册（或覆盖）一个字段类型；供插件包与测试直接调用。"""
    _check_name(name)
    if not callable(func):
        raise TypeError(f"抽取器 {name} 必须是可调用对象")
    _REGISTRY[name] = func


def get_extractor(name: str) -> Extractor | None:
    """按名字取抽取器；第一次调用时顺便加载已安装插件的入口点。"""
    _ensure_loaded()
    return _REGISTRY.get(name)


def extractor_names() -> list[str]:
    """已注册的插件类型名（排序后），用于报错提示。"""
    _ensure_loaded()
    return sorted(_REGISTRY)


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        discovered = entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # 元数据损坏不该拖垮抓取
        logger.warning("读取抽取器入口点失败：%s", exc)
        return
    for entry in discovered:
        try:
            func = entry.load()
            register_extractor(entry.name, func)
        except Exception as exc:
            # 单个插件坏掉只记日志：其余插件与内置类型照常可用
            logger.warning("加载抽取器插件 %s 失败：%s", entry.name, exc)
