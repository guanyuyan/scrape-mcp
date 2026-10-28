"""同 key 在途请求合并（singleflight）。

并发的相同请求只让第一个真正执行，其余等待并复用同一结果，避免重复抓取同一个
URL（多租户同时读同一页面时尤其明显）。领头的请求被取消（客户端断开、预算回收）
时，等待者会自己接棒重新执行，而不是跟着一起失败。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import Any, TypeVar

T = TypeVar("T")


class SingleFlight:
    """按 key 合并并发调用；结果只在本次等待范围内共享，不做缓存。"""

    def __init__(self) -> None:
        self._inflight: dict[Hashable, asyncio.Future[Any]] = {}

    async def run(self, key: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
        while True:
            fut = self._inflight.get(key)
            if fut is None:
                return await self._lead(key, factory)
            try:
                # shield：等待者被取消时不要连带取消正在执行的领头请求
                return await asyncio.shield(fut)
            except asyncio.CancelledError:
                if not fut.cancelled():
                    raise
                # 领头者被取消：自己接棒，别把别人的取消当成自己的失败

    async def _lead(self, key: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            result = await factory()
        except BaseException:
            # 领头失败/被取消时唤醒等待者，让它们各自重试
            fut.cancel()
            raise
        else:
            fut.set_result(result)
            return result
        finally:
            self._inflight.pop(key, None)
