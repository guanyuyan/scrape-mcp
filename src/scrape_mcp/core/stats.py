"""进程内统计：给 /stats 用，只记计数与耗时，不记 URL、不记页面内容。"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any


class Stats:
    def __init__(self) -> None:
        self.started_at = time.time()
        # 走到抓取链路的次数（含 HTTP 缓存命中，不含结果缓存命中）
        self.fetches = 0
        # 结果缓存命中次数：这部分请求没有产生网络与解析
        self.result_cache_hits = 0
        # HTTP 响应缓存命中（含 304 复用）
        self.http_cache_hits = 0
        self.by_tier: Counter[str] = Counter()
        self.by_code: Counter[str] = Counter()
        self.by_kind: Counter[str] = Counter()
        self.tokens_returned = 0
        self.bytes_in = 0

    def record_fetch(
        self,
        *,
        tier: str,
        code: str,
        kind: str,
        cached: bool,
        html_bytes: int = 0,
    ) -> None:
        self.fetches += 1
        self.by_tier[tier or "unknown"] += 1
        self.by_code[code or "unknown"] += 1
        self.by_kind[kind or "html"] += 1
        if cached:
            self.http_cache_hits += 1
        self.bytes_in += max(0, html_bytes)

    def record_result_hit(self) -> None:
        self.result_cache_hits += 1

    def record_tokens(self, tokens: int) -> None:
        self.tokens_returned += max(0, tokens)

    def snapshot(self) -> dict[str, Any]:
        requests = self.fetches + self.result_cache_hits
        return {
            "ok": True,
            "uptime_s": int(time.time() - self.started_at),
            "requests": requests,
            "fetches": self.fetches,
            "result_cache_hits": self.result_cache_hits,
            "http_cache_hits": self.http_cache_hits,
            "cache_hit_rate": round(self.result_cache_hits / requests, 3) if requests else 0.0,
            "by_tier": dict(sorted(self.by_tier.items())),
            "by_code": dict(sorted(self.by_code.items())),
            "by_kind": dict(sorted(self.by_kind.items())),
            "tokens_returned": self.tokens_returned,
            "bytes_in": self.bytes_in,
        }


_stats = Stats()


def get_stats() -> Stats:
    return _stats


def reset_stats() -> Stats:
    """换一个新实例；测试用，避免用例之间互相累加。"""
    global _stats
    _stats = Stats()
    return _stats
