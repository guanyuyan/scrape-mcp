"""HTTP 响应磁盘缓存（M4）：按 URL 缓存 L1 明文响应，命中即跳过网络。

只为 GET 类公开页加速重复抓取；detector + compact 依旧重跑（毫秒级），
保证反爬判定与精简逻辑始终基于最新页面结构。
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

    def get(self, url: str) -> dict[str, Any] | None:
        if self._ttl <= 0:
            return None
        rec = self._read(url)
        if not rec:
            return None
        # 过期即视作未命中（不做主动清理，靠写入覆盖）
        if time.time() - rec["ts"] > self._ttl:
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
