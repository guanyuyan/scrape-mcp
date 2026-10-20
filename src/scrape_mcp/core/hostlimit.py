"""按主机隔离的并发闸门。

全局并发上限挡不住"同一站点被并发打穿"：batch 里 4 个同域 URL 会瞬间发 4 个请求，
比单页抓取更容易触发风控。这里给每个 host 一个独立信号量，跨 host 仍可并行。

超过 max_hosts 时按最久未用淘汰（不关闭正在等待的槽位，只从字典里移除映射）。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class HostLimiter:
    def __init__(self, per_host: int = 2, max_hosts: int = 256) -> None:
        self._per_host = max(1, int(per_host))
        self._max_hosts = max(1, int(max_hosts))
        self._slots: OrderedDict[str, asyncio.Semaphore] = OrderedDict()

    def reconfigure(self, per_host: int) -> None:
        self._per_host = max(1, int(per_host))

    def _semaphore(self, host: str) -> asyncio.Semaphore:
        sem = self._slots.get(host)
        if sem is not None:
            self._slots.move_to_end(host)
            return sem
        sem = asyncio.Semaphore(self._per_host)
        self._slots[host] = sem
        if len(self._slots) > self._max_hosts:
            self._slots.popitem(last=False)
        return sem

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        sem = self._semaphore(host or "")
        async with sem:
            yield
