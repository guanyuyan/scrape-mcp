"""代理：URL 校验与池化调度（轮询、按站点粘性、失败冷却）。

一次抓取只在入口选一次出口，L1、重试与 L2 升级共用同一个代理，
避免同一请求在多个 IP 之间跳来跳去（登录态与风控都会因此失效）。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

# curl_cffi 与 Playwright 都支持这几类代理协议
PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks5"})

# 代理列表分隔符：逗号、分号、空白都接受，方便从 .env 里一行写多个
_SPLIT = re.compile(r"[,;\s]+")


def valid_proxy(raw: object) -> str | None:
    """校验代理 URL 并返回去掉首尾空白的原文；非法返回 None。"""
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        return None
    parsed = urlparse(text)
    try:
        if parsed.scheme.lower() not in PROXY_SCHEMES or not parsed.hostname:
            return None
        # 端口写法非法（如 :abc）或越界时 urlparse 会在这里抛 ValueError
        if parsed.port is not None and parsed.port <= 0:
            return None
    except ValueError:
        return None
    return text


def split_pool(raw: str) -> list[str]:
    """解析代理列表：丢掉非法项，去重且保持输入顺序。"""
    urls: list[str] = []
    for item in _SPLIT.split(raw or ""):
        url = valid_proxy(item)
        if url is not None and url not in urls:
            urls.append(url)
    return urls


@dataclass
class _Entry:
    url: str
    fails: int = 0
    cooldown_until: float = 0.0


class ProxyPool:
    """按站点粘性的轮询池：失败达阈值进入冷却，冷却期内不再分配。

    全部出口都在冷却时回退到最早解冻的那个——调用方本来就要发请求，
    宁可冒一次已知风险，也不要因为"池里没有健康出口"直接拒绝服务。
    """

    def __init__(
        self,
        urls: list[str],
        *,
        fail_threshold: int = 2,
        cooldown_s: float = 60.0,
        sticky_ttl_s: float = 300.0,
    ) -> None:
        self._entries = [_Entry(u) for u in urls]
        self._fail_threshold = max(1, int(fail_threshold))
        self._cooldown_s = max(0.0, cooldown_s)
        self._sticky_ttl_s = max(0.0, sticky_ttl_s)
        # host -> (代理, 绑定到期时刻)；用单调时钟，不受系统时间调整影响
        self._bound: dict[str, tuple[str, float]] = {}
        self._cursor = 0

    def __bool__(self) -> bool:
        return bool(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def pick(self, host: str) -> str | None:
        """为本次请求选出口；池为空时返回 None，由调用方回落到全局代理。"""
        if not self._entries:
            return None
        now = time.monotonic()
        bound = self._bound.get(host)
        if bound is not None:
            url, expiry = bound
            entry = self._entry(url)
            if expiry > now and entry is not None and entry.cooldown_until <= now:
                return url
            self._bound.pop(host, None)
        entry = self._next_available(now)
        self._bound[host] = (entry.url, now + self._sticky_ttl_s)
        return entry.url

    def report(self, proxy: str, *, ok: bool) -> None:
        """回报一次出口结果：成功清零计数，连续失败达阈值则进入冷却。"""
        entry = self._entry(proxy)
        if entry is None:
            return
        if ok:
            entry.fails = 0
            return
        entry.fails += 1
        if entry.fails < self._fail_threshold:
            return
        entry.fails = 0
        entry.cooldown_until = time.monotonic() + self._cooldown_s
        # 冷却中的出口不再承接粘性站点，下次请求重新分配
        self._bound = {h: v for h, v in self._bound.items() if v[0] != proxy}

    def snapshot(self) -> dict[str, int]:
        """池状态摘要；不含代理地址，避免凭据进入统计与日志。"""
        now = time.monotonic()
        cooling = sum(1 for e in self._entries if e.cooldown_until > now)
        return {
            "size": len(self._entries),
            "healthy": len(self._entries) - cooling,
            "cooling": cooling,
        }

    def _entry(self, url: str) -> _Entry | None:
        for entry in self._entries:
            if entry.url == url:
                return entry
        return None

    def _next_available(self, now: float) -> _Entry:
        for i in range(len(self._entries)):
            idx = (self._cursor + i) % len(self._entries)
            entry = self._entries[idx]
            if entry.cooldown_until <= now:
                self._cursor = (idx + 1) % len(self._entries)
                return entry
        return min(self._entries, key=lambda e: e.cooldown_until)
