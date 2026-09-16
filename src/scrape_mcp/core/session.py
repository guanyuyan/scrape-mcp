"""登录态持久化：按站点保存浏览器 storage_state。

M3 的核心：首次手动登录后，把 Playwright context 的 storage_state（含 cookie/localStorage）
以 JSON 落盘到数据目录；之后的抓取在开 context 时自动加载，跳过登录墙。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class SessionStore:
    def __init__(self, data_dir: str) -> None:
        # 支持 ~ 展开，默认 ~/.scrape_mcp/sessions
        self._root = Path(os.path.expanduser(data_dir)).resolve()

    def _path_for(self, host: str) -> Path:
        safe = host.replace(":", "_")
        return self._root / "sessions" / f"{safe}.json"

    def has(self, host: str) -> bool:
        return self._path_for(host).is_file()

    def load(self, host: str) -> dict[str, Any] | None:
        path = self._path_for(host)
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, host: str, storage_state: dict[str, Any]) -> None:
        path = self._path_for(host)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(storage_state, f, ensure_ascii=False, indent=2)

    def delete(self, host: str) -> bool:
        path = self._path_for(host)
        if path.is_file():
            path.unlink()
            return True
        return False
