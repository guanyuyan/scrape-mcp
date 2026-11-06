"""云端控制面 broker：上游解析、租约分发、TTL、并发与配额、/cloud/* 路由。"""

from __future__ import annotations

import asyncio
import json

import pytest

import scrape_mcp.server as server
from scrape_mcp.cloud.broker import (
    CLOUD_DISABLED,
    CLOUD_LEASE_LIMIT,
    CLOUD_QUOTA_EXCEEDED,
    CLOUD_UNAVAILABLE,
    BrokerError,
    CloudConfigError,
    LeaseBroker,
    parse_browser_upstreams,
    parse_proxy_upstreams,
)
from scrape_mcp.http_auth import BearerAuthMiddleware

TOKEN = "cloud-secret"


class _Clock:
    """可注入的墙上时钟：租约到期与跨日清零都靠它稳定复现。"""

    def __init__(self, wall: float = 86400.0 * 100) -> None:
        self.wall = wall

    def __call__(self) -> float:
        return self.wall


class _Request:
    """路由处理器只用到 method 与 scope state，无需完整 Starlette Request。"""

    def __init__(self, method: str = "GET", tenant: str | None = None) -> None:
        self.method = method
        self.scope = {
            "type": "http",
            "state": {} if tenant is None else {"scrape_mcp_tenant": tenant},
        }


def _body(response) -> dict:
    return json.loads(response.body)


def _call(coro) -> tuple[int, dict]:
    response = asyncio.run(coro)
    return response.status_code, _body(response)


def _broker(**kwargs) -> LeaseBroker:
    kwargs.setdefault("clock", _Clock())
    return LeaseBroker(**kwargs)


# ---- 配置解析 ----


def test_parse_browser_upstreams_dedupes_and_keeps_order():
    upstreams = parse_browser_upstreams(
        "http://b1:3000, ws://b2:3000;http://b1:3000\n wss://b3:3000"
    )

    assert [item.url for item in upstreams] == ["http://b1:3000", "ws://b2:3000", "wss://b3:3000"]


@pytest.mark.parametrize(
    "raw", ["ftp://b1:3000", "http:///missing-host", "http://b1:0", "not-a-url"]
)
def test_parse_browser_upstreams_rejects_invalid(raw):
    with pytest.raises(CloudConfigError):
        parse_browser_upstreams(raw)


def test_parse_proxy_upstreams_reads_region_label():
    upstreams = parse_proxy_upstreams("socks5://p1:1080|us, http://p2:8080, http://p3:8080|HK")

    assert [(item.url, item.region) for item in upstreams] == [
        ("socks5://p1:1080", "us"),
        ("http://p2:8080", ""),
        ("http://p3:8080", "hk"),
    ]


@pytest.mark.parametrize("raw", ["ftp://p1:1080", "http://p1:0", "http://p1:8080|中文"])
def test_parse_proxy_upstreams_rejects_invalid(raw):
    with pytest.raises(CloudConfigError):
        parse_proxy_upstreams(raw)


def test_empty_config_is_allowed():
    """启用但没配上游是合法状态：/cloud/* 返回 503，而不是拒绝启动。"""
    broker = _broker()

    assert broker.upstream_count("browser") == 0
    assert broker.upstream_count("proxy") == 0


# ---- 租约分发 ----


def test_round_robin_across_upstreams():
    broker = _broker(browser_upstreams="http://b1:3000,http://b2:3000", max_leases_per_tenant=0)

    first = broker.acquire("browser", "team-a")
    second = broker.acquire("browser", "team-a")
    third = broker.acquire("browser", "team-a")

    assert [first.url, second.url, third.url] == [
        "http://b1:3000",
        "http://b2:3000",
        "http://b1:3000",
    ]


def test_proxy_lease_carries_region():
    broker = _broker(proxy_upstreams="http://p1:8080|us")

    lease = broker.acquire("proxy", "team-a")

    assert lease.region == "us"
    assert lease.payload(broker.now())["region"] == "us"


def test_get_reuses_active_lease_but_post_creates_new_one():
    broker = _broker(browser_upstreams="http://b1:3000", max_leases_per_tenant=2)

    first = broker.acquire("browser", "team-a", reuse=True)
    reused = broker.acquire("browser", "team-a", reuse=True)
    second = broker.acquire("browser", "team-a")

    assert reused.lease_id == first.lease_id
    assert second.lease_id != first.lease_id


def test_concurrent_lease_limit_returns_429():
    broker = _broker(browser_upstreams="http://b1:3000", max_leases_per_tenant=1)
    broker.acquire("browser", "team-a")

    with pytest.raises(BrokerError) as exc:
        broker.acquire("browser", "team-a")

    assert exc.value.status == 429
    assert exc.value.code == CLOUD_LEASE_LIMIT
    assert exc.value.retry_after is not None and exc.value.retry_after > 0


def test_leases_expire_and_free_the_slot():
    clock = _Clock()
    broker = _broker(browser_upstreams="http://b1:3000", max_leases_per_tenant=1, clock=clock)
    first = broker.acquire("browser", "team-a")

    clock.wall += broker.ttl + 1
    second = broker.acquire("browser", "team-a")

    assert second.lease_id != first.lease_id
    assert broker.usage("team-a")["browser"]["active"] == 1


def test_daily_quota_is_separate_and_resets_at_utc_midnight():
    clock = _Clock()
    broker = _broker(
        browser_upstreams="http://b1:3000",
        max_leases_per_tenant=0,
        daily_leases=1,
        clock=clock,
    )
    broker.acquire("browser", "team-a")

    with pytest.raises(BrokerError) as exc:
        broker.acquire("browser", "team-a")
    assert exc.value.code == CLOUD_QUOTA_EXCEEDED

    clock.wall += 86400
    broker.acquire("browser", "team-a")


def test_daily_quota_does_not_count_other_kind():
    broker = _broker(
        browser_upstreams="http://b1:3000",
        proxy_upstreams="http://p1:8080",
        max_leases_per_tenant=0,
        daily_leases=1,
    )

    broker.acquire("browser", "team-a")
    broker.acquire("proxy", "team-a")


# ---- 用量 ----


def test_usage_only_reports_own_tenant():
    broker = _broker(
        browser_upstreams="http://b1:3000",
        proxy_upstreams="http://p1:8080|us",
        max_leases_per_tenant=2,
        daily_leases=5,
    )
    broker.acquire("browser", "team-a")
    broker.acquire("browser", "team-b")

    usage = broker.usage("team-a")

    assert usage["tenant"] == "team-a"
    assert usage["browser"]["active"] == 1
    assert usage["browser"]["limit"] == 2
    assert usage["browser"]["daily_used"] == 1
    assert usage["browser"]["daily_remaining"] == 4
    assert usage["proxy"]["active"] == 0
    assert usage["upstreams"] == {"browser": 1, "proxy": 1}
    assert "team-b" not in json.dumps(usage, ensure_ascii=False)


def test_usage_without_daily_limit_reports_none():
    broker = _broker(browser_upstreams="http://b1:3000")

    assert broker.usage("team-a")["browser"]["daily_remaining"] is None


# ---- REST 路由 ----


def test_routes_return_404_when_disabled(monkeypatch):
    monkeypatch.setattr(server, "get_cloud_broker", lambda: None)

    status, body = _call(server._cloud_lease(_Request("POST"), "browser"))
    assert status == 404
    assert body["error_code"] == CLOUD_DISABLED
    assert body["retryable"] is False

    status, body = _call(server._cloud_usage(_Request()))
    assert status == 404


def test_route_returns_503_without_upstream(monkeypatch):
    broker = _broker(proxy_upstreams="http://p1:8080")
    monkeypatch.setattr(server, "get_cloud_broker", lambda: broker)

    status, body = _call(server._cloud_lease(_Request("POST", "team-a"), "browser"))

    assert status == 503
    assert body["error_code"] == CLOUD_UNAVAILABLE
    assert body["retryable"] is True
    assert body["request_id"]


def test_route_uses_tenant_from_auth_state(monkeypatch):
    broker = _broker(browser_upstreams="http://b1:3000", max_leases_per_tenant=1)
    monkeypatch.setattr(server, "get_cloud_broker", lambda: broker)

    status, body = _call(server._cloud_lease(_Request("POST", "team-a"), "browser"))
    assert status == 200
    assert body["ok"] is True
    assert body["lease_id"]

    # 同租户再次申请触发并发上限；另一个租户不受影响
    status, body = _call(server._cloud_lease(_Request("POST", "team-a"), "browser"))
    assert status == 429
    assert body["error_code"] == CLOUD_LEASE_LIMIT
    assert body["retryable"] is True
    assert body["retry_after_s"] > 0

    status, body = _call(server._cloud_lease(_Request("POST", "team-b"), "browser"))
    assert status == 200


def test_route_get_reuses_lease(monkeypatch):
    broker = _broker(browser_upstreams="http://b1:3000")
    monkeypatch.setattr(server, "get_cloud_broker", lambda: broker)

    _, first = _call(server._cloud_lease(_Request("GET", "team-a"), "browser"))
    _, second = _call(server._cloud_lease(_Request("GET", "team-a"), "browser"))

    assert first["lease_id"] == second["lease_id"]
    assert first["expires_at"].endswith("Z")
    assert first["expires_in_s"] > 0


def test_usage_route_returns_own_counts(monkeypatch):
    broker = _broker(browser_upstreams="http://b1:3000", daily_leases=2)
    monkeypatch.setattr(server, "get_cloud_broker", lambda: broker)
    _call(server._cloud_lease(_Request("POST", "team-a"), "browser"))

    status, body = _call(server._cloud_usage(_Request("GET", "team-a")))

    assert status == 200
    assert body["ok"] is True
    assert body["tenant"] == "team-a"
    assert body["browser"]["active"] == 1
    assert body["browser"]["daily_remaining"] == 1


def test_cloud_routes_require_bearer_token():
    """Bearer 中间件同样保护 /cloud/*；缺令牌返回 401，不进入 broker。"""
    reached = False

    async def _app(scope, receive, send):
        nonlocal reached
        reached = True

    scope = {
        "type": "http",
        "path": "/cloud/usage",
        "headers": [],
        "client": ("10.0.0.5", 5000),
        "state": {},
    }
    sent: list[dict] = []

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        sent.append(message)

    asyncio.run(BearerAuthMiddleware(_app, TOKEN)(scope, _receive, _send))

    start = next(item for item in sent if item["type"] == "http.response.start")
    assert start["status"] == 401
    assert not reached
