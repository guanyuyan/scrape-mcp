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
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import get_settings


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
                data = json.loads(path.read_text(encoding="utf-8"))
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
