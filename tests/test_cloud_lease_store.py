"""云端 broker 的 Redis 共享状态：两个 broker 实例共用一份假 Redis 验证跨实例语义。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import scrape_mcp.cloud.lease_store as lease_store
from scrape_mcp.cloud.broker import (
    CLOUD_LEASE_LIMIT,
    CLOUD_QUOTA_EXCEEDED,
    CLOUD_UNAVAILABLE,
    BrokerError,
    LeaseBroker,
    build_broker,
)
from scrape_mcp.cloud.lease_store import RedisLeaseStore


class FakeRedis:
    """只实现云端租约状态用到的命令，语义对齐 redis-py。"""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.expires: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self.strings.get(key)

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        if ex:
            self.expires[key] = ex
        return True

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.strings[key] = value
        self.expires[key] = ttl

    def delete(self, key: str) -> int:
        self.strings.pop(key, None)
        self.sets.pop(key, None)
        return 1

    def sadd(self, key: str, *values: str) -> int:
        self.sets.setdefault(key, set()).update(values)
        return len(values)

    def srem(self, key: str, *values: str) -> int:
        bucket = self.sets.get(key, set())
        removed = [value for value in values if value in bucket]
        bucket.difference_update(removed)
        return len(removed)

    def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    def mget(self, keys) -> list[str | None]:
        return [self.strings.get(key) for key in keys]

    def incr(self, key: str) -> int:
        value = int(self.strings.get(key, 0)) + 1
        self.strings[key] = str(value)
        return value

    def expire(self, key: str, ttl: int) -> bool:
        self.expires[key] = ttl
        return True

    def ping(self) -> bool:
        return True


class _Clock:
    def __init__(self, wall: float = 86400.0 * 100) -> None:
        self.wall = wall

    def __call__(self) -> float:
        return self.wall


def _brokers(redis: FakeRedis, clock: _Clock, instances: int = 2, **kwargs) -> list[LeaseBroker]:
    """模拟多实例：每个 broker 各自持有一个指向同一 Redis 的 store。"""
    kwargs.setdefault("browser_upstreams", "http://b1:3000")
    return [
        LeaseBroker(store=RedisLeaseStore(redis, "test"), clock=clock, **kwargs)
        for _ in range(instances)
    ]


def test_shared_lease_is_visible_across_instances():
    redis, clock = FakeRedis(), _Clock()
    a, b = _brokers(redis, clock)

    first = a.acquire("browser", "team-a", reuse=True)
    reused = b.acquire("browser", "team-a", reuse=True)

    assert reused.lease_id == first.lease_id
    assert b.usage("team-a")["browser"]["active"] == 1


def test_concurrent_limit_is_enforced_across_instances():
    redis, clock = FakeRedis(), _Clock()
    a, b = _brokers(redis, clock, max_leases_per_tenant=1)
    a.acquire("browser", "team-a")

    with pytest.raises(BrokerError) as exc:
        b.acquire("browser", "team-a")

    assert exc.value.status == 429
    assert exc.value.code == CLOUD_LEASE_LIMIT


def test_daily_quota_is_shared_across_instances():
    redis, clock = FakeRedis(), _Clock()
    a, b = _brokers(redis, clock, max_leases_per_tenant=0, daily_leases=1)
    a.acquire("browser", "team-a")

    with pytest.raises(BrokerError) as exc:
        b.acquire("browser", "team-a")

    assert exc.value.code == CLOUD_QUOTA_EXCEEDED


def test_round_robin_cursor_is_shared():
    redis, clock = FakeRedis(), _Clock()
    a, b = _brokers(
        redis, clock, browser_upstreams="http://b1:3000,http://b2:3000", max_leases_per_tenant=0
    )

    first = a.acquire("browser", "team-a")
    second = b.acquire("browser", "team-b")

    assert [first.url, second.url] == ["http://b1:3000", "http://b2:3000"]


def test_expired_remote_lease_is_reclaimed():
    redis, clock = FakeRedis(), _Clock()
    a, b = _brokers(redis, clock, max_leases_per_tenant=1)
    first = a.acquire("browser", "team-a")

    clock.wall += a.ttl + 1
    second = b.acquire("browser", "team-a")

    assert second.lease_id != first.lease_id
    assert b.usage("team-a")["browser"]["active"] == 1


def test_corrupt_lease_entry_is_dropped():
    redis, clock = FakeRedis(), _Clock()
    store = RedisLeaseStore(redis, "test")
    set_key = store._set_key("team-a", "browser")
    redis.sadd(set_key, "broken")
    redis.set(store._lease_key("team-a", "browser", "broken"), "{not-json")

    assert store.list_active("team-a", "browser", clock.wall) == []
    assert "broken" not in redis.smembers(set_key)


def test_lock_contention_returns_retryable_503(monkeypatch):
    redis, clock = FakeRedis(), _Clock()
    broker = _brokers(redis, clock, instances=1)[0]
    monkeypatch.setattr(lease_store, "LOCK_RETRIES", 1)
    monkeypatch.setattr(lease_store, "LOCK_SLEEP_S", 0)
    store = broker._store
    redis.set(store._lock_key("team-a", "browser"), "other-instance", nx=True, ex=5)

    with pytest.raises(BrokerError) as exc:
        broker.acquire("browser", "team-a")

    assert exc.value.status == 503
    assert exc.value.code == CLOUD_UNAVAILABLE
    assert exc.value.retry_after == 0.5


def test_build_broker_uses_redis_only_when_url_set(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(lease_store, "create_client", lambda url: redis)
    base = {
        "cloud_browser_upstreams": "http://b1:3000",
        "cloud_proxy_upstreams": "",
        "cloud_lease_ttl": 300.0,
        "cloud_max_leases_per_tenant": 2,
        "cloud_daily_leases": 0,
        "redis_prefix": "test",
    }

    local = build_broker(SimpleNamespace(cloud_broker_redis_url="", **base))
    shared = build_broker(
        SimpleNamespace(cloud_broker_redis_url="redis://127.0.0.1:6379/0", **base)
    )

    assert local.shared is False
    assert shared.shared is True
    assert shared.acquire("browser", "team-a").lease_id
    lease_key = next(key for key in redis.strings if ":lease:" in key)
    assert json.loads(redis.strings[lease_key])["tenant"] == "team-a"
