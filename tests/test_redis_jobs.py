"""Redis 任务后端：用内存假客户端覆盖存储、队列与分布式取消语义，不依赖真实 Redis。"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from scrape_mcp.core.jobs import CrawlJobManager, JobStore, job_id_for
from scrape_mcp.core.jobstore_redis import RedisJobStore


class FakeRedis:
    """够用的内存实现，只覆盖 RedisJobStore 用到的命令。"""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.lists: dict[str, list[str]] = {}
        self.expires: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self.strings.get(key)

    def set(self, key: str, value: str) -> None:
        self.strings[key] = value

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.strings[key] = value
        self.expires[key] = ttl

    def expire(self, key: str, ttl: int) -> bool:
        self.expires[key] = ttl
        return True

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.zsets.setdefault(key, {}).update(mapping)

    def zremrangebyrank(self, key: str, start: int, end: int) -> None:
        return None

    def zrevrange(self, key: str, start: int, end: int) -> list[str]:
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1], reverse=True)
        keys = [name for name, _ in items]
        return keys[start:] if end == -1 else keys[start : end + 1]

    def rpush(self, key: str, value: str) -> int:
        queue = self.lists.setdefault(key, [])
        queue.append(value)
        return len(queue)

    def blpop(self, key: str, timeout: int = 0) -> tuple[str, str] | None:
        queue = self.lists.get(key) or []
        if not queue:
            return None
        return key, queue.pop(0)

    def ping(self) -> bool:
        return True


def _store(ttl: float = 0) -> RedisJobStore:
    return RedisJobStore(FakeRedis(), "test", ttl)


def _job(job_id: str = "crawl-abc", **overrides) -> dict:
    now = time.time()
    job = {
        "job_id": job_id,
        "kind": "crawl",
        "state": "queued",
        "created_at": now,
        "updated_at": now,
        "request": {"url": "https://example.com", "depth": 0},
        "progress": {"pages_fetched": 0, "ok_count": 0, "limit": 1},
        "queue": [["https://example.com", 0]],
        "visited": ["https://example.com"],
        "results": [],
        "error": None,
    }
    job.update(overrides)
    return job


def test_store_roundtrip_and_list_order():
    store = _store()
    older = _job("crawl-a", updated_at=100)
    newer = _job("crawl-b", updated_at=200)

    store.save(older)
    store.save(newer)

    assert store.load("crawl-a") == older
    assert [item["job_id"] for item in store.list(2)] == ["crawl-b", "crawl-a"]


def test_store_sets_ttl_and_rejects_bad_ids():
    store = _store(ttl=120)
    store.save(_job())

    assert store._client.expires[store._key("crawl-abc")] == 120
    assert store.load("not/a/job") is None
    with pytest.raises(ValueError):
        store.save({"job_id": "bad id"})


def test_store_without_ttl_uses_plain_set():
    store = _store(ttl=10)
    store.save(_job())

    assert store._client.expires == {}


def test_queue_push_and_pop_in_order():
    store = _store()
    store.enqueue("crawl-a")
    store.enqueue("crawl-b")

    assert store.dequeue(1) == "crawl-a"
    assert store.dequeue(1) == "crawl-b"
    assert store.dequeue(1) is None


async def _fetch(url: str, request: dict) -> tuple[dict, list[str], str | None]:
    return {"ok": True, "url": url}, [], None


def test_producer_mode_enqueues_without_local_task():
    store = _store()
    pushed: list[str] = []
    manager = CrawlJobManager(lambda: store, _fetch, enqueue=pushed.append)
    request = {"url": "https://example.com", "depth": 0, "limit": 1}

    job, existing = manager.start(request, cap=1)
    again, second = manager.start(request, cap=1)

    assert job["job_id"] == again["job_id"]
    assert existing is False and second is True
    # 同一参数只投递一次，避免队列里出现重复任务
    assert pushed == [job["job_id"]]
    assert manager.status(job["job_id"])["state"] == "queued"


def test_worker_run_completes_job():
    store = _store()
    job = _job()
    store.save(job)
    manager = CrawlJobManager(lambda: store, _fetch)

    asyncio.run(manager.run(job["job_id"]))

    done = store.load(job["job_id"])
    assert done["state"] == "done"
    assert done["progress"] == {"pages_fetched": 1, "ok_count": 1, "limit": 1}
    assert done["results"][0]["url"] == "https://example.com"


def test_cancel_from_another_process_is_seen_by_run():
    store = _store()
    job = _job(state="cancelled", finished_at=time.time())
    store.save(job)
    manager = CrawlJobManager(lambda: store, _fetch)

    asyncio.run(manager.run(job["job_id"]))

    assert store.load(job["job_id"])["state"] == "cancelled"
    assert store.load(job["job_id"])["results"] == []


def test_recover_stale_running_jobs_only():
    store = _store()
    now = time.time()
    store.save(_job("crawl-old", state="running", updated_at=now - 700))
    store.save(_job("crawl-new", state="running", updated_at=now - 10))
    store.save(_job("crawl-done", state="done", updated_at=now - 700))

    assert store.recover_stale(stale_after=600) == 1

    assert store.load("crawl-old")["state"] == "queued"
    assert store.load("crawl-new")["state"] == "running"
    assert store.load("crawl-done")["state"] == "done"
    assert store.dequeue(1) == "crawl-old"


def test_job_id_is_stable_and_fits_redis_key():
    request = {"url": "https://example.com", "depth": 1, "limit": 3}
    job_id = job_id_for(request)

    assert job_id == job_id_for(json.loads(json.dumps(request)))
    store = _store()
    store.save(_job(job_id))
    assert store.load(job_id)["job_id"] == job_id


def test_local_store_still_rejects_bad_ids(tmp_path):
    store = JobStore(tmp_path)
    assert store.load("ok-id") is None
    with pytest.raises(ValueError):
        store.save({"job_id": "../escape"})
