"""登录态持久化：按站点（及可选会话名）保存浏览器 storage_state。

M3 的核心：首次手动登录后，把 Playwright context 的 storage_state（含 cookie/localStorage）
以 JSON 落盘到数据目录；之后的抓取在开 context 时自动加载，跳过登录墙。

v0.5 P1 起支持命名会话（``session="work"``）：
- 默认会话沿用旧路径 ``sessions/<host>.json``，旧文件无需迁移；
- 命名会话放在 ``sessions/<session>/<host>.json``，多身份互不干扰；
- 额外提供 cookie 导入/导出，便于在机器之间搬运登录态（仅本地文件）。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SESSION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HOST_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_COOKIE_KEYS = ("expires", "httpOnly", "secure", "sameSite")


def normalize_session(name: str | None) -> str:
    """校验并归一化会话名；非法名直接抛 ValueError（由工具层转成 invalid_argument）。"""
    name = (name or "").strip() or "default"
    if name == "default" or _SESSION_NAME.match(name):
        return name
    raise ValueError("session 只能包含字母、数字、点、下划线和连字符，长度 1-64")


def _normalize_cookie(raw: Any, host: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    cookie: dict[str, Any] = {
        "name": name,
        "value": str(raw.get("value") or ""),
        "domain": str(raw.get("domain") or host),
        "path": str(raw.get("path") or "/"),
    }
    for key in _COOKIE_KEYS:
        if raw.get(key) is not None:
            cookie[key] = raw[key]
    return cookie


def _cookie_key(cookie: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(cookie.get("name") or ""),
        str(cookie.get("domain") or ""),
        str(cookie.get("path") or "/"),
    )


class SessionStore:
    def __init__(self, data_dir: str) -> None:
        # 支持 ~ 展开，默认 ~/.scrape_mcp/sessions
        self._root = Path(os.path.expanduser(data_dir)).resolve()

    def _path_for(self, host: str, session: str = "default") -> Path:
        name = normalize_session(session)
        safe = _HOST_SAFE.sub("_", (host or "").lower()).strip("._-") or "unknown"
        base = self._root / "sessions"
        if name == "default":
            return base / f"{safe}.json"
        return base / name / f"{safe}.json"

    def has(self, host: str, session: str = "default") -> bool:
        return self._path_for(host, session).is_file()

    def load(self, host: str, session: str = "default") -> dict[str, Any] | None:
        path = self._path_for(host, session)
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, host: str, storage_state: dict[str, Any], session: str = "default") -> None:
        path = self._path_for(host, session)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(storage_state, f, ensure_ascii=False, indent=2)

    def delete(self, host: str, session: str = "default") -> bool:
        path = self._path_for(host, session)
        if path.is_file():
            path.unlink()
            return True
        return False

    # ---------- 多会话 ----------

    def list_sessions(self) -> list[dict[str, str]]:
        """列出本地已保存的登录态（不含 cookie 内容）。"""
        base = self._root / "sessions"
        out: list[dict[str, str]] = []
        try:
            paths = list(base.glob("*.json")) + list(base.glob("*/*.json"))
        except OSError:
            return out
        for path in paths:
            session = "default" if path.parent == base else path.parent.name
            try:
                updated = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            out.append(
                {
                    "session": session,
                    "host": path.stem,
                    "updated_at": updated.isoformat(timespec="seconds"),
                }
            )
        return sorted(out, key=lambda item: (item["session"], item["host"]))

    # ---------- cookie 导入 / 导出 ----------

    def export_cookies(self, host: str, session: str = "default") -> dict[str, Any] | None:
        state = self.load(host, session)
        if state is None:
            return None
        cookies = [c for c in state.get("cookies") or [] if isinstance(c, dict)]
        return {
            "host": host,
            "session": normalize_session(session),
            "cookies": cookies,
        }

    def import_cookies(
        self,
        host: str,
        cookies: list[Any],
        session: str = "default",
        *,
        replace: bool = False,
    ) -> int:
        """把 cookie 列表合并进该会话（同名同域同路径以导入值为准），返回有效条数。"""
        incoming = [c for c in (_normalize_cookie(raw, host) for raw in cookies) if c is not None]
        if not incoming:
            return 0
        state = self.load(host, session) or {}
        existing = [
            _normalize_cookie(c, host) for c in (state.get("cookies") or []) if isinstance(c, dict)
        ]
        existing = [c for c in existing if c is not None]
        merged: dict[tuple[str, str, str], dict[str, Any]] = {}
        if not replace:
            merged = {_cookie_key(c): c for c in existing}
        for cookie in incoming:
            merged[_cookie_key(cookie)] = cookie
        self.save(
            host,
            {"cookies": list(merged.values()), "origins": state.get("origins") or []},
            session,
        )
        return len(incoming)
