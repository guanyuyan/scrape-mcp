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


class CookieFileError(ValueError):
    """cookie 文件无法解析；消息可直接回给调用方。"""


def _cookies_from_json(text: str) -> list[dict[str, Any]]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CookieFileError(f"不是合法 JSON: {exc}") from exc
    cookies = raw.get("cookies") if isinstance(raw, dict) else raw
    if not isinstance(cookies, list):
        raise CookieFileError(
            "JSON 里没有 cookies 数组（支持 session_export 产物、storage_state 或 cookie 数组）"
        )
    return [c for c in cookies if isinstance(c, dict)]


def _cookies_from_netscape(text: str) -> list[dict[str, Any]]:
    """Netscape cookies.txt：domain, includeSubdomains, path, secure, expires, name, value。"""
    cookies: list[dict[str, Any]] = []
    for line in text.splitlines():
        raw = line.rstrip("\r")
        if not raw.strip():
            continue
        http_only = raw.startswith("#HttpOnly_")
        if http_only:
            raw = raw[len("#HttpOnly_") :]
        elif raw.lstrip().startswith("#"):
            continue
        fields = raw.split("\t")
        if len(fields) != 7:
            continue
        domain, _subdomains, path, secure, expires, name, value = fields
        if not name.strip():
            continue
        cookie: dict[str, Any] = {
            "name": name.strip(),
            "value": value,
            "domain": domain.strip(),
            "path": path.strip() or "/",
        }
        if secure.strip().upper() == "TRUE":
            cookie["secure"] = True
        if http_only:
            cookie["httpOnly"] = True
        exp = expires.strip()
        if exp.isdigit() and int(exp) > 0:
            cookie["expires"] = int(exp)
        cookies.append(cookie)
    if not cookies:
        raise CookieFileError("没有解析到 cookie（支持 Netscape cookies.txt 或 JSON）")
    return cookies


def parse_cookie_text(text: str) -> list[dict[str, Any]]:
    """按内容解析 cookie：JSON（导出对象 / storage_state / 数组）或 Netscape cookies.txt。"""
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped:
        raise CookieFileError("cookie 文件为空")
    if stripped.startswith(("{", "[")):
        return _cookies_from_json(stripped)
    return _cookies_from_netscape(text)


def load_cookie_file(path: Path) -> list[dict[str, Any]]:
    """读取 cookie 文件，格式由内容判断（浏览器扩展导出的 cookies.txt 也可直接用）。"""
    return parse_cookie_text(path.read_text(encoding="utf-8"))


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
