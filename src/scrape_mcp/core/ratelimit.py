"""全局限流（阶段 0）：为 web_fetch 与 web_batch 提供统一的每秒请求上限。

令牌桶模型：以固定速率补充令牌，请求消耗一个令牌，不足则等待补充后再放行，
从而把整体请求速率平滑限制到 max_qps 以内。max_qps<=0 表示不限速。
"""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    def __init__(self, max_qps: float = 0.0) -> None:
        self._qps = max(float(max_qps or 0), 0.0)
        self._tokens = 0.0
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    def reconfigure(self, max_qps: float) -> None:
        self._qps = max(float(max_qps or 0), 0.0)

    async def acquire(self) -> None:
        if self._qps <= 0:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(self._qps, self._tokens + (now - self._last) * self._qps)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._qps
            # 在锁外等待，允许其它等待者公平抢令牌
            await asyncio.sleep(wait)
