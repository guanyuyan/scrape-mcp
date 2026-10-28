"""同 key 在途合并：并发只执行一次、不同 key 互不影响、取消不会误伤等待者。"""

from __future__ import annotations

import asyncio

import pytest

from scrape_mcp.core.singleflight import SingleFlight


async def test_concurrent_calls_share_one_execution():
    flight = SingleFlight()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def work() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "body"

    first = asyncio.create_task(flight.run("k", work))
    await started.wait()
    second = asyncio.create_task(flight.run("k", work))
    await asyncio.sleep(0.01)
    release.set()

    assert await first == "body"
    assert await second == "body"
    assert calls == 1


async def test_different_keys_run_independently():
    flight = SingleFlight()
    calls: list[str] = []

    async def work(key: str) -> str:
        calls.append(key)
        await asyncio.sleep(0.01)
        return key

    assert await asyncio.gather(
        flight.run("a", lambda: work("a")), flight.run("b", lambda: work("b"))
    )
    assert sorted(calls) == ["a", "b"]


async def test_leader_cancellation_hands_over_to_waiter():
    """领头请求被取消（客户端断开/预算回收）时，等待者自己接棒而不是一起失败。"""
    flight = SingleFlight()
    started = asyncio.Event()
    calls = 0

    async def work() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.Event().wait()  # 挂住，等被取消
        return "ok"

    leader = asyncio.create_task(flight.run("k", work))
    await started.wait()
    waiter = asyncio.create_task(flight.run("k", work))
    await asyncio.sleep(0.01)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader

    assert await waiter == "ok"
    assert calls == 2


async def test_waiter_retries_after_leader_failure():
    flight = SingleFlight()
    release = asyncio.Event()
    calls = 0

    async def work() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            await release.wait()
            raise RuntimeError("boom")
        return "ok"

    leader = asyncio.create_task(flight.run("k", work))
    await asyncio.sleep(0.01)
    waiter = asyncio.create_task(flight.run("k", work))
    await asyncio.sleep(0.01)
    release.set()

    with pytest.raises(RuntimeError):
        await leader
    assert await waiter == "ok"
    assert calls == 2


async def test_inflight_cleared_after_completion():
    flight = SingleFlight()

    async def work() -> str:
        return "done"

    assert await flight.run("k", work) == "done"
    assert flight._inflight == {}
