"""多令牌与租户级配额：配置解析、互斥校验、按租户限流与指标。"""

from __future__ import annotations

import asyncio
import json

import pytest

from scrape_mcp import metrics
from scrape_mcp.http_auth import (
    BearerAuthMiddleware,
    TokenConfigError,
    TokenPolicy,
    ensure_http_access,
    load_token_policies,
    parse_token_policies,
)

TENANTS = json.dumps(
    [
        {"name": "team-a", "token": "token-a", "qps": 1.0},
        {"name": "team-b", "token": "token-b", "qps": 0.0, "daily_quota": 1},
    ]
)


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _ok_app(scope, receive, send):
    scope["reached"] = True
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _call(middleware: BearerAuthMiddleware, token: str | None, *, path: str = "/mcp"):
    headers = [] if token is None else [(b"authorization", f"Bearer {token}".encode())]
    scope = {
        "type": "http",
        "path": path,
        "headers": headers,
        "client": ("10.0.0.5", 5000),
        "state": {},
    }
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, _receive, send))
    start = next(item for item in sent if item["type"] == "http.response.start")
    raw_body = next(item["body"] for item in sent if item["type"] == "http.response.body")
    try:
        body = json.loads(raw_body)
    except ValueError:
        body = {}
    return scope, start["status"], body


def _samples(text: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        values[name] = float(value)
    return values


# ---- 配置解析 ----


def test_parse_json_array_inherits_global_quotas():
    raw = json.dumps(
        [
            {"name": "team-a", "token": "aaa"},
            {"name": "ci", "token": "bbb", "qps": 2, "daily_quota": 30},
        ]
    )

    policies = parse_token_policies(raw, default_qps=5.0, default_daily=10000)

    assert policies == [
        TokenPolicy(name="team-a", token="aaa", qps=5.0, daily_quota=10000),
        TokenPolicy(name="ci", token="bbb", qps=2.0, daily_quota=30),
    ]


def test_parse_accepts_tokens_wrapper_and_zero_means_unlimited():
    raw = json.dumps({"tokens": [{"name": "ci", "token": "bbb", "qps": 0, "daily_quota": 0}]})

    assert parse_token_policies(raw, default_qps=5.0, default_daily=100) == [
        TokenPolicy(name="ci", token="bbb", qps=0.0, daily_quota=0)
    ]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not-json", "不是合法 JSON"),
        ('{"foo": 1}', "数组"),
        ('[{"token": "x"}]', "缺少 name"),
        ('[{"name": "a"}]', "缺少 token"),
        ('[{"name": "a", "token": "x"}, {"name": "a", "token": "y"}]', "租户名重复"),
        ('[{"name": "a", "token": "x"}, {"name": "b", "token": "x"}]', "令牌重复"),
        ('[{"name": "a", "token": "x", "qps": "fast"}]', "必须是数字"),
        ('[{"name": "a", "token": "x", "daily_quota": "many"}]', "必须是数字"),
        ("[]", "空数组"),
    ],
)
def test_parse_rejects_invalid_config(raw, message):
    with pytest.raises(TokenConfigError) as exc:
        parse_token_policies(raw)

    assert message in str(exc.value)


def test_load_prefers_file_and_reads_json(tmp_path):
    source = tmp_path / "tokens.json"
    source.write_text(json.dumps([{"name": "ci", "token": "bbb"}]), encoding="utf-8")

    policies = load_token_policies(file=str(source), default_qps=5.0, default_daily=1000)

    assert policies == [TokenPolicy(name="ci", token="bbb", qps=5.0, daily_quota=1000)]


def test_load_single_token_becomes_default_tenant():
    policies = load_token_policies(single="solo", default_qps=5.0, default_daily=100)

    assert policies == [TokenPolicy(name="default", token="solo", qps=5.0, daily_quota=100)]


def test_load_without_credentials_returns_empty():
    assert load_token_policies() == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"raw": "[]", "file": "tokens.json"}, "HTTP_TOKENS 与 HTTP_TOKENS_FILE"),
        ({"single": "solo", "raw": "[]"}, "单个 HTTP_TOKEN 与多令牌"),
        ({"single": "solo", "file": "tokens.json"}, "单个 HTTP_TOKEN 与多令牌"),
    ],
)
def test_load_rejects_conflicting_sources(kwargs, message):
    with pytest.raises(TokenConfigError) as exc:
        load_token_policies(**kwargs)

    assert message in str(exc.value)


def test_load_reports_unreadable_file(tmp_path):
    missing = tmp_path / "nope.json"

    with pytest.raises(TokenConfigError) as exc:
        load_token_policies(file=str(missing))

    assert "读取" in str(exc.value)


def test_binding_public_address_with_multi_tokens_is_allowed():
    ensure_http_access("0.0.0.0", None, tokens=parse_token_policies(TENANTS))


def test_binding_public_address_without_any_token_is_rejected():
    with pytest.raises(SystemExit) as exc:
        ensure_http_access("0.0.0.0", None, tokens=[])

    message = str(exc.value)
    assert "SCRAPE_MCP_HTTP_TOKEN" in message
    assert "HTTP_TOKENS_FILE" in message


# ---- 按租户鉴权与配额 ----


def test_each_tenant_has_independent_quota():
    middleware = BearerAuthMiddleware(_ok_app, tokens=parse_token_policies(TENANTS))

    _, status_a1, _ = _call(middleware, "token-a")
    _, status_a2, body_a2 = _call(middleware, "token-a")
    _, status_b1, _ = _call(middleware, "token-b")
    _, status_b2, body_b2 = _call(middleware, "token-b")

    assert status_a1 == 200
    assert status_a2 == 429
    assert body_a2["error"] == "rate_limited"
    # B 的配额与 A 完全独立：A 被限流不影响 B 的首次调用
    assert status_b1 == 200
    assert status_b2 == 429
    assert body_b2["error"] == "quota_exceeded"


def test_authenticated_request_carries_tenant_name():
    middleware = BearerAuthMiddleware(_ok_app, tokens=parse_token_policies(TENANTS))

    scope, status, _ = _call(middleware, "token-b")

    assert status == 200
    assert scope["state"]["scrape_mcp_tenant"] == "team-b"
    assert scope["state"]["scrape_mcp_authenticated"] is True


@pytest.mark.parametrize("token", [None, "token-aX", "unknown-token"])
def test_unknown_or_missing_token_is_rejected(token):
    middleware = BearerAuthMiddleware(_ok_app, tokens=parse_token_policies(TENANTS))

    scope, status, body = _call(middleware, token)

    assert status == 401
    assert body["error"] == "unauthorized"
    assert scope.get("reached") is None


def test_tenant_metrics_count_accepted_and_rejected():
    middleware = BearerAuthMiddleware(_ok_app, tokens=parse_token_policies(TENANTS))

    _call(middleware, "token-a")
    _call(middleware, "token-a")
    _call(middleware, "token-b")

    values = _samples(metrics.render())

    assert values['scrape_mcp_tenant_requests_total{tenant="team-a"}'] == 1
    assert values['scrape_mcp_tenant_rejections_total{tenant="team-a",reason="rate_limited"}'] == 1
    assert values['scrape_mcp_tenant_requests_total{tenant="team-b"}'] == 1


def test_metrics_scrape_does_not_consume_tenant_quota():
    middleware = BearerAuthMiddleware(_ok_app, tokens=parse_token_policies(TENANTS))

    for _ in range(3):
        _, status, _ = _call(middleware, "token-a", path="/metrics")
        assert status == 200

    values = _samples(metrics.render())
    assert 'scrape_mcp_tenant_requests_total{tenant="team-a"}' not in values
    assert 'scrape_mcp_tenant_rejections_total{tenant="team-a",reason="rate_limited"}' not in values
