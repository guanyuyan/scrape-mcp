"""磁盘缓存：HTTP 响应（M4）+ 最终结果（正文/抽取）。

HTTP 层按 URL 缓存 L1 明文响应，命中即跳过网络；结果层缓存的是 compact/extract
之后的载荷，命中后连网络与解析一起跳过。两层都用 `cache_ttl` 统一控制
（0 表示全部关闭），调用方的 `bypass_cache=True` 会同时绕过两层。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any


class HttpCache:
    def __init__(self, data_dir: str, ttl: float) -> None:
        self._path = Path(os.path.expanduser(data_dir)).resolve() / "http"
        self._ttl = ttl

    def get(self, url: str, *, allow_stale: bool = False) -> dict[str, Any] | None:
        """读取缓存。allow_stale=True 时忽略 TTL，用于拿过期条目的 ETag/Last-Modified 做条件请求。"""
        if self._ttl <= 0:
            return None
        rec = self._read(url)
        if not rec:
            return None
        # 过期即视作未命中（不做主动清理，靠写入覆盖）
        if not allow_stale and time.time() - rec["ts"] > self._ttl:
            return None
        return rec["data"]

    def put(self, url: str, data: dict[str, Any]) -> None:
        if self._ttl <= 0:
            return
        try:
            entry = {"ts": time.time(), "data": data}
            fp = self._file_for(url)
            fp.parent.mkdir(parents=True, exist_ok=True)
            with fp.open("w", encoding="utf-8") as f:
                json.dump(entry, f, ensure_ascii=False)
        except (OSError, TypeError):
            pass  # 缓存写失败不影响主流程

    def _read(self, url: str) -> dict[str, Any] | None:
        try:
            with self._file_for(url).open("r", encoding="utf-8") as f:
                rec = json.load(f)
            return rec if isinstance(rec, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _file_for(self, url: str) -> Path:
        # 目录按 host 分，文件名用 URL 的优先级散列，避免把整段 URL 当文件名（易超长）
        from urllib.parse import urlparse

        host = urlparse(url).netloc or "misc"
        safe_host = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in host)
        digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
        return self._path / safe_host / f"{digest}.json"


class ResultCache:
    """最终结果缓存：正文（compact 后）与字段抽取结果。

    key 由调用方拼成，必须包含全部影响结果的参数（URL、格式、预算、schema 等），
    否则会跨参数串味。命中后直接返回载荷，网络、渲染、解析全部跳过。
    """

    def __init__(self, root: str | Path, ttl: float, namespace: str) -> None:
        self._path = Path(os.path.expanduser(str(root))).resolve() / namespace
        self._ttl = ttl

    def get(self, key: str) -> dict[str, Any] | None:
        if self._ttl <= 0:
            return None
        try:
            with self._file_for(key).open("r", encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(rec, dict) or not isinstance(rec.get("data"), dict):
            return None
        try:
            fresh = time.time() - float(rec["ts"]) <= self._ttl
        except (KeyError, TypeError, ValueError):
            return None
        return rec["data"] if fresh else None

    def put(self, key: str, data: dict[str, Any]) -> None:
        if self._ttl <= 0:
            return
        try:
            fp = self._file_for(key)
            fp.parent.mkdir(parents=True, exist_ok=True)
            with fp.open("w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "data": data}, f, ensure_ascii=False)
        except (OSError, TypeError):
            pass  # 缓存写失败不影响主流程

    def _file_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self._path / f"{digest}.json"
