"""站点索引：从 sitemap.xml 收集 URL 清单，并与上次的清单对比。

只清点页面地址，不抓正文：先从 robots.txt 的 ``Sitemap:`` 指向找入口，
没有就回退 ``/sitemap.xml``；sitemap index 会递归展开（深度有限），
``.gz`` 压缩包原样解压。清单落在 ``DATA_DIR/index`` 下，供下次对比新增/变更/移除。
"""

from __future__ import annotations

import gzip
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

# 单次最多展开多少个 sitemap 文件（递归展开 + 防御超大站点）
MAX_SITEMAPS = 20
# sitemap index 的递归层数上限
MAX_DEPTH = 2
# 载荷里每个清单（new/changed/removed）最多列多少条 URL
MAX_LISTED = 50
# check_content 单次最多回抓多少个已变更页面（避免一次索引把目标站打穿）
MAX_CONTENT_CHECKS = 20


class SitemapError(ValueError):
    """sitemap 解压/解析失败（结构不对或压缩包损坏）。"""


def sitemap_candidates(robots_text: str, base_url: str) -> list[str]:
    """从 robots.txt 里取出全部 ``Sitemap:`` 地址，按出现顺序去重。"""
    found: list[str] = []
    for line in (robots_text or "").splitlines():
        key, sep, value = line.partition(":")
        if not sep or key.strip().lower() != "sitemap":
            continue
        loc = value.strip()
        if loc and loc not in found:
            found.append(urljoin(base_url, loc))
    return found


def parse_sitemap(text: str, *, base_url: str = "") -> tuple[list[dict[str, str]], list[str]]:
    """解析 sitemap：返回 (页面条目, 子 sitemap 地址)。

    两类根节点都支持：``<urlset>`` 是页面清单，``<sitemapindex>`` 是 sitemap 清单。
    命名空间前缀不影响匹配，按 local-name 取值。
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SitemapError(f"sitemap 解析失败: {exc}") from exc

    kind = _local(root.tag)
    if kind == "sitemapindex":
        children = []
        for node in root:
            if _local(node.tag) != "sitemap":
                continue
            loc = _child_text(node, "loc")
            if loc:
                children.append(urljoin(base_url, loc))
        return [], children

    if kind == "urlset":
        entries: list[dict[str, str]] = []
        for node in root:
            if _local(node.tag) != "url":
                continue
            loc = _child_text(node, "loc")
            if not loc:
                continue
            entries.append({"url": urljoin(base_url, loc), "lastmod": _child_text(node, "lastmod")})
        return entries, []

    raise SitemapError(f"不是 sitemap（根节点为 {kind or '空'}）")


def decode_sitemap(raw: bytes, text: str = "") -> str:
    """把响应还原成 XML 文本；gzip（按魔数判断，不限后缀）先解压。"""
    data = raw or text.encode("utf-8", "replace")
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError as exc:
            raise SitemapError(f"gzip 解压失败: {exc}") from exc
    return data.decode("utf-8", "replace")


def diff_index(
    previous: dict[str, Any] | None, current: list[dict[str, str]]
) -> dict[str, list[str]]:
    """把本次清单与上次的记录对比成四组 URL。

    上次记录形如 ``{"entries": {url: {"lastmod": ..., "hash": ...}}}``；
    缺 lastmod 时按"没有时间戳"处理，只有两边都为空才算未变。
    """
    prev_entries = (previous or {}).get("entries") or {}
    if not isinstance(prev_entries, dict):
        prev_entries = {}
    now = {entry["url"]: entry.get("lastmod") or "" for entry in current}

    new: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    for url, lastmod in now.items():
        old = prev_entries.get(url)
        if old is None:
            new.append(url)
            continue
        old_lastmod = (old.get("lastmod") if isinstance(old, dict) else str(old)) or ""
        if old_lastmod == lastmod:
            unchanged.append(url)
        else:
            changed.append(url)

    removed = [url for url in prev_entries if url not in now]
    return {"new": new, "changed": changed, "unchanged": unchanged, "removed": removed}


def _local(tag: str) -> str:
    """去掉 XML 命名空间前缀，只留 local-name。"""
    return tag.rsplit("}", 1)[-1] if tag else ""


def _child_text(node: ET.Element, name: str) -> str:
    for child in node:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


class SiteIndexStore:
    """每个 host 一份索引状态，落在 ``DATA_DIR/index`` 下，不随缓存过期。"""

    def __init__(self, root: str | Path) -> None:
        self._path = Path(os.path.expanduser(str(root))).resolve() / "index"

    def _file(self, host: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", host or "unknown")[:120]
        return self._path / f"{safe}.json"

    def load(self, host: str) -> dict[str, Any] | None:
        try:
            with self._file(host).open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, host: str, data: dict[str, Any]) -> None:
        # 与快照一致：写失败不影响本次结果
        try:
            fp = self._file(host)
            fp.parent.mkdir(parents=True, exist_ok=True)
            with fp.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except (OSError, TypeError):
            pass
