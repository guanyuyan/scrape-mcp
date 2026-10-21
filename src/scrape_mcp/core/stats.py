"""进程内统计：给 /stats 用，只记计数与耗时，不记 URL、不记页面内容。"""

from __future__ import annotations

import math
import time
from collections import Counter, deque
from typing import Any

# 耗时只保留最近 N 条样本：够算分位数，又不会让内存随请求数无限增长
_LATENCY_WINDOW = 1024

# 分桶边界（毫秒）：键固定，方便客户端直接拿去画分布
_LATENCY_BUCKETS: tuple[tuple[str, float, float | None], ...] = (
    ("lt_100", 0, 100),
    ("lt_300", 100, 300),
    ("lt_1000", 300, 1000),
    ("lt_3000", 1000, 3000),
    ("lt_10000", 3000, 10000),
    ("gte_10000", 10000, None),
)


def _percentile(sorted_values: list[int], q: float) -> int:
    """最近秩法分位数：样本少时也不会插值出一个从未出现过的耗时。"""
    if not sorted_values:
        return 0
    idx = min(len(sorted_values) - 1, max(0, math.ceil(q * len(sorted_values)) - 1))
    return sorted_values[idx]


def _bucket_counts(values: list[int]) -> dict[str, int]:
    counts = {name: 0 for name, _lo, _hi in _LATENCY_BUCKETS}
    for ms in values:
        for name, lo, hi in _LATENCY_BUCKETS:
            if ms >= lo and (hi is None or ms < hi):
                counts[name] += 1
                break
    return counts


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
        # 耗时样本（毫秒）：全部抓取与按层分开，后者用来观察 L2 的真实代价
        self._latency: deque[int] = deque(maxlen=_LATENCY_WINDOW)
        self._latency_by_tier: dict[str, deque[int]] = {}

    def record_fetch(
        self,
        *,
        tier: str,
        code: str,
        kind: str,
        cached: bool,
        html_bytes: int = 0,
        elapsed_ms: int = 0,
    ) -> None:
        self.fetches += 1
        self.by_tier[tier or "unknown"] += 1
        self.by_code[code or "unknown"] += 1
        self.by_kind[kind or "html"] += 1
        if cached:
            self.http_cache_hits += 1
        self.bytes_in += max(0, html_bytes)
        # 与 payload 里的 elapsed_ms 同源，保证 /stats 和调用方看到的是同一个口径
        ms = max(0, int(elapsed_ms))
        self._latency.append(ms)
        name = tier or "unknown"
        if name not in self._latency_by_tier:
            self._latency_by_tier[name] = deque(maxlen=_LATENCY_WINDOW)
        self._latency_by_tier[name].append(ms)

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
            "latency_ms": self._latency_summary(),
        }

    def _latency_summary(self) -> dict[str, Any]:
        """最近窗口内的耗时分布：分位数看整体，分桶看形态，分层看 L2 代价。"""
        values = sorted(self._latency)
        by_tier = {}
        for tier, samples in sorted(self._latency_by_tier.items()):
            vals = sorted(samples)
            by_tier[tier] = {
                "samples": len(vals),
                "p50": _percentile(vals, 0.5),
                "p95": _percentile(vals, 0.95),
            }
        return {
            "window": _LATENCY_WINDOW,
            "samples": len(values),
            "p50": _percentile(values, 0.5),
            "p95": _percentile(values, 0.95),
            "p99": _percentile(values, 0.99),
            "buckets": _bucket_counts(values),
            "by_tier": by_tier,
        }


_stats = Stats()


def get_stats() -> Stats:
    return _stats


def reset_stats() -> Stats:
    """换一个新实例；测试用，避免用例之间互相累加。"""
    global _stats
    _stats = Stats()
    return _stats
