"""站点 recipe：把"这个站怎么抓、取哪块、抽哪些字段"沉淀成 JSON。

目录默认 ``<data_dir>/recipes``（即 ``~/.scrape_mcp/recipes``），文件名即主机名
（如 ``example.com.json``），也可在文件内用 ``host`` 字段覆盖。命中时按最长后缀
匹配（``www.example.com`` 可命中 ``example.com``）；单次调用的显式参数优先于 recipe。

示例::

    {
      "host": "example.com",
      "wait_for": "article",
      "scroll": true,
      "root_selector": "main article",
      "extract": {"fields": {"title": "h1", "items": {"selector": ".item", "type": "list"}}},
      "crawl": {"next_selector": "a.next", "max_pages": 5}
    }
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import get_close_matches
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import get_settings
from ..extract.schema import SchemaValidationError, parse_schema


@dataclass
class Recipe:
    host: str
    wait_for: str | None = None
    scroll: bool | None = None
    click: str | None = None
    root_selector: str | None = None
    extract: dict[str, Any] | None = None
    next_selector: str | None = None
    max_pages: int | None = None
    note: str = ""


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _recipe_from(data: dict, stem: str) -> Recipe | None:
    host = (_str_or_none(data.get("host")) or stem).lower().lstrip(".")
    if not host:
        return None
    crawl = data.get("crawl") if isinstance(data.get("crawl"), dict) else {}
    scroll = data.get("scroll")
    return Recipe(
        host=host,
        wait_for=_str_or_none(data.get("wait_for")),
        scroll=scroll if isinstance(scroll, bool) else None,
        click=_str_or_none(data.get("click")),
        root_selector=_str_or_none(data.get("root_selector")),
        extract=data.get("extract") if isinstance(data.get("extract"), dict) else None,
        next_selector=_str_or_none(crawl.get("next_selector")),
        max_pages=_positive_int(crawl.get("max_pages")),
        note=_str_or_none(data.get("note")) or "",
    )


class RecipeStore:
    """按目录扫描 recipe，文件变化（新增/修改）后自动重载。"""

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory).expanduser()
        self._cache: dict[str, Recipe] = {}
        self._sig: tuple = ()
        self._loaded = False

    @property
    def directory(self) -> Path:
        return self._dir

    def reload(self) -> None:
        self._loaded = False

    def _signature(self) -> tuple:
        try:
            files = sorted(self._dir.glob("*.json"))
        except OSError:
            return ()
        sig = []
        for path in files:
            try:
                sig.append((path.name, path.stat().st_mtime_ns))
            except OSError:
                continue
        return tuple(sig)

    def _ensure(self) -> None:
        sig = self._signature()
        if self._loaded and sig == self._sig:
            return
        recipes: dict[str, Recipe] = {}
        for name, _mtime in sig:
            path = self._dir / name
            try:
                # utf-8-sig：Windows 记事本等编辑器常写出带 BOM 的文件，直接 utf-8 会解析失败
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if isinstance(data, dict):
                recipe = _recipe_from(data, path.stem)
                if recipe is not None:
                    recipes[recipe.host] = recipe
        self._cache = recipes
        self._sig = sig
        self._loaded = True

    def all(self) -> list[Recipe]:
        self._ensure()
        return list(self._cache.values())

    def for_host(self, host: str) -> Recipe | None:
        self._ensure()
        host = (host or "").lower().lstrip(".")
        if not host:
            return None
        best: Recipe | None = None
        for key, recipe in self._cache.items():
            if (host == key or host.endswith("." + key)) and (
                best is None or len(key) > len(best.host)
            ):
                best = recipe
        return best

    def for_url(self, url: str) -> Recipe | None:
        host = (urlparse(url).netloc or "").lower().split(":")[0]
        return self.for_host(host)


def default_recipe_dir() -> Path:
    settings = get_settings()
    if settings.recipes_dir:
        return Path(settings.recipes_dir).expanduser()
    return Path(settings.data_dir).expanduser() / "recipes"


@lru_cache(maxsize=1)
def get_recipe_store() -> RecipeStore:
    return RecipeStore(default_recipe_dir())


# ---- recipe 校验（供 `scrape-mcp recipe validate` 使用）----

# 顶层与 crawl 段认的键；写错键不会报错、只会被静默忽略，所以校验要主动指出来
_RECIPE_KEYS = ("host", "wait_for", "scroll", "click", "root_selector", "extract", "crawl", "note")
_CRAWL_KEYS = ("next_selector", "max_pages")
_SELECTOR_KEYS = ("wait_for", "click", "root_selector")
_HOST_RE = re.compile(r"[a-z0-9.-]+")


@dataclass
class RecipeIssue:
    level: str  # "error"（会失效）或 "warning"（能用但有隐患）
    message: str

    def __str__(self) -> str:
        return f"[{'错误' if self.level == 'error' else '警告'}] {self.message}"


def _error(message: str) -> RecipeIssue:
    return RecipeIssue("error", message)


def _warning(message: str) -> RecipeIssue:
    return RecipeIssue("warning", message)


def _check_selector(value: Any, label: str, issues: list[RecipeIssue]) -> None:
    """用真实解析器验选择器：selectolax 解析不了的选择器运行期同样选不中。"""
    if not isinstance(value, str) or not value.strip():
        issues.append(_error(f"{label} 必须是非空字符串选择器"))
        return
    from selectolax.lexbor import LexborHTMLParser

    try:
        LexborHTMLParser("<html><body></body></html>").css(value)
    except Exception as exc:  # selectolax 抛的是 SelectolaxError，别依赖具体类型
        issues.append(_error(f"{label} 的选择器语法有误（{exc}）: {value!r}"))


def validate_recipe_data(data: Any, stem: str = "") -> list[RecipeIssue]:
    """校验一份 recipe 的 JSON 内容；``stem`` 是文件名（不含 .json），用于提示 host 口径。"""
    issues: list[RecipeIssue] = []
    if not isinstance(data, dict):
        return [_error("顶层必须是 JSON 对象")]

    for key in data:
        if key not in _RECIPE_KEYS:
            hint = get_close_matches(str(key), _RECIPE_KEYS, n=1)
            tail = f"（是不是 {hint[0]}？）" if hint else "，加载时会被忽略"
            issues.append(_warning(f"未知字段 {key}{tail}"))

    host = data.get("host")
    if host is not None:
        if not isinstance(host, str) or not host.strip():
            issues.append(_error("host 必须是非空字符串"))
        else:
            normalized = host.strip().lower().lstrip(".")
            if any(ch in normalized for ch in "/: "):
                issues.append(_error(f"host 应为纯主机名（不带协议/端口/路径），收到 {host!r}"))
            elif not _HOST_RE.fullmatch(normalized):
                issues.append(_error(f"host 含非法字符: {host!r}"))
            elif stem and normalized != stem.lower():
                issues.append(
                    _warning(f"host={normalized} 与文件名 {stem}.json 不一致；生效以 host 为准")
                )

    for key in _SELECTOR_KEYS:
        if key in data:
            _check_selector(data[key], key, issues)

    if "scroll" in data and not isinstance(data["scroll"], bool):
        issues.append(_error("scroll 必须是 true/false"))
    if "note" in data and not isinstance(data["note"], str):
        issues.append(_warning("note 建议写成字符串"))

    crawl = data.get("crawl")
    if crawl is not None:
        if not isinstance(crawl, dict):
            issues.append(_error("crawl 必须是对象"))
        else:
            for key in crawl:
                if key not in _CRAWL_KEYS:
                    hint = get_close_matches(str(key), _CRAWL_KEYS, n=1)
                    tail = f"（是不是 {hint[0]}？）" if hint else ""
                    issues.append(_warning(f"crawl 里的未知字段 {key}{tail}"))
            if "next_selector" in crawl:
                _check_selector(crawl["next_selector"], "crawl.next_selector", issues)
            if "max_pages" in crawl:
                pages = crawl["max_pages"]
                if not isinstance(pages, int) or isinstance(pages, bool) or pages <= 0:
                    issues.append(_error("crawl.max_pages 必须是正整数"))

    extract = data.get("extract")
    if extract is not None:
        if not isinstance(extract, dict):
            issues.append(_error("extract 必须是对象"))
        else:
            try:
                parse_schema(extract)
            except SchemaValidationError as exc:
                issues.append(_error(f"extract 规则非法: {exc}"))

    return issues


def validate_recipe_file(path: Path) -> list[RecipeIssue]:
    """读文件并校验；JSON/编码错误也归到问题列表，方便一次列全。"""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return [_error("文件不是 UTF-8 文本")]
    except OSError as exc:
        return [_error(f"读取失败: {exc}")]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [_error(f"JSON 解析失败: {exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）")]
    return validate_recipe_data(data, path.stem)


def recipe_host(data: dict, stem: str) -> str:
    """与加载器一致地算出生效主机：文件里有 host 就优先，否则用文件名。"""
    host = data.get("host") if isinstance(data, dict) else None
    value = host if isinstance(host, str) and host.strip() else stem
    return value.strip().lower().lstrip(".")
