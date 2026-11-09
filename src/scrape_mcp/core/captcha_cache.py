"""求解结果缓存：把一次成功求解得到的 cookie 复用到同一站点的后续请求。

动机：外部求解服务（人工值守/已授权 solver）返回的 cookie 通常有几分钟到
几十分钟的有效期，但一次批量抓取里同一站点会被反复打开。每次都去求解既慢
又贵，还会放大风控。这里按 ``(session, host)`` 记住 cookie 与配套 UA：

- 默认关闭（``SCRAPE_MCP_CAPTCHA_COOKIE_CACHE=false``）：cookie 属敏感数据，
  多租户共用一个进程时开启会跨租户泄露，必须由部署方显式承担；
- LRU 上限 256、TTL 默认 900 秒，过期即视为未命中；
- 落盘 ``DATA_DIR/captcha_cookies.json``，原子替换 + 权限 600，进程重启后仍可复用；
- 任何读写失败都静默降级为"未命中"，不影响抓取主链路。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

# 单条缓存最多保留的 cookie 数量与单值长度：与 captcha 钩子的入库口径一致
_MAX_COOKIES = 20
_MAX_COOKIE_LEN = 1000
_MAX_UA_LEN = 256
_FILE_VERSION = 1
_FILE_NAME = "captcha_cookies.json"


@dataclass
class CachedSolve:
    """一次可复用的求解结果；``expires_at`` 用墙钟时间戳，便于跨进程复用。"""

    cookies: dict[str, str] = field(default_factory=dict)
    user_agent: str = ""
    expires_at: float = 0.0


class CaptchaCookieCache:
    """按 (session, host) 索引的求解 cookie LRU 缓存。"""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        ttl: float = 900.0,
        max_entries: int = 256,
        persist: bool = True,
    ) -> None:
        self._path = Path(os.path.expanduser(str(data_dir))) / _FILE_NAME
        self._ttl = max(0.0, float(ttl))
        self._max = max(1, int(max_entries))
        self._persist = persist
        self._entries: OrderedDict[str, CachedSolve] = OrderedDict()
        if persist:
            self._load()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, session: str, host: str) -> CachedSolve | None:
        """取未过期的缓存；命中时刷新 LRU 位置。"""
        key = _key(session, host)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.time():
            self._entries.pop(key, None)
            self._save()
            return None
        self._entries.move_to_end(key)
        return entry

    def put(
        self,
        session: str,
        host: str,
        cookies: dict[str, str],
        user_agent: str = "",
    ) -> None:
        """写入一条缓存；空 cookie 直接忽略，写盘失败不影响内存命中。"""
        clean = _clean_cookies(cookies)
        if not clean:
            return
        key = _key(session, host)
        self._entries[key] = CachedSolve(
            cookies=clean,
            user_agent=(user_agent or "")[:_MAX_UA_LEN],
            expires_at=time.time() + self._ttl,
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
        self._save()

    def clear(self) -> None:
        """清空缓存；显式登出/换身份时可由上层调用。"""
        self._entries.clear()
        self._save()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        entries = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(entries, dict):
            return
        now = time.time()
        for key, item in entries.items():
            if not isinstance(key, str) or not isinstance(item, dict):
                continue
            cookies = _clean_cookies(item.get("cookies"))
            expires_at = item.get("expires_at")
            if not cookies or not isinstance(expires_at, (int, float)) or expires_at <= now:
                continue
            self._entries[key] = CachedSolve(
                cookies=cookies,
                user_agent=str(item.get("user_agent") or "")[:_MAX_UA_LEN],
                expires_at=float(expires_at),
            )
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def _save(self) -> None:
        if not self._persist:
            return
        payload = {
            "version": _FILE_VERSION,
            "entries": {
                key: {
                    "cookies": entry.cookies,
                    "user_agent": entry.user_agent,
                    "expires_at": entry.expires_at,
                }
                for key, entry in self._entries.items()
            },
        }
        tmp: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".captcha_", suffix=".json"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            # 先收紧权限再替换：碰撞窗口里也不会留下可被其他用户读取的明文 cookie
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
            tmp = None
        except OSError:
            return
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)


def _key(session: str, host: str) -> str:
    return f"{(session or 'default').strip() or 'default'}::{host.strip().lower()}"


def _clean_cookies(raw: object) -> dict[str, str]:
    """只接受扁平字符串字典，并做数量/长度截断。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for name, value in raw.items():
        if len(out) >= _MAX_COOKIES:
            break
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        key = name.strip()
        if not key or len(key) > 128 or len(value) > _MAX_COOKIE_LEN:
            continue
        out[key] = value
    return out


__all__ = ["CachedSolve", "CaptchaCookieCache"]
