"""同域并发闸门：同 host 受限，跨 host 互不影响。"""

from __future__ import annotations

import asyncio

from scrape_mcp.core.hostlimit import HostLimiter


async def _run(limiter: HostLimiter, hosts: list[str]) -> int:
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def work(host: str) -> None:
        nonlocal active, peak
        async with limiter.slot(host):
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.01)
            async with lock:
                active -= 1

    await asyncio.gather(*(work(h) for h in hosts))
    return peak


def test_same_host_concurrency_is_capped():
    assert asyncio.run(_run(HostLimiter(per_host=2), ["a.test"] * 6)) == 2


def test_different_hosts_run_in_parallel():
    hosts = ["a.test", "b.test", "c.test", "d.test"]
    assert asyncio.run(_run(HostLimiter(per_host=1), hosts)) == 4


def test_reconfigure_changes_limit():
    limiter = HostLimiter(per_host=4)
    limiter.reconfigure(1)
    assert asyncio.run(_run(limiter, ["a.test"] * 4)) == 1
