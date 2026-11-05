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

# 带工具白名单的租户：reader 只能抓取与搜索，full 不限制
TOOL_TENANTS = json.dumps(
    [
        {
            "name": "reader",
            "token": "reader-token",
            "qps": 0,
            "daily_quota": 0,
            "tools": ["web_fetch", "web_search"],
        },
        {"name": "full", "token": "full-token", "qps": 0, "daily_quota": 0},
    ]
)


def _tool_call(name: str) -> bytes:
    """构造一个最小的 tools/call JSON-RPC 请求体。"""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": {}},
        }
    ).encode()


async def _echo_body_app(scope, receive, send):
    """把下游收到的请求体记到 scope 上，用于验证中间件读取后仍可重放。"""
    scope["reached"] = True
    scope["body"] = (await receive())["body"]
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _ok_app(scope, receive, send):
    scope["reached"] = True
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _call(
    middleware: BearerAuthMiddleware,
    token: str | None,
    *,
    path: str = "/mcp",
    method: str | None = None,
    body: bytes | None = None,
):
    headers = [] if token is None else [(b"authorization", f"Bearer {token}".encode())]
    scope = {
        "type": "http",
        "path": path,
        "headers": headers,
        "client": ("10.0.0.5", 5000),
        "state": {},
    }
    if method is not None:
        scope["method"] = method
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": body or b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
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


def test_parse_tool_whitelist_and_star():
    raw = json.dumps(
        [
            {"name": "reader", "token": "aaa", "tools": ["web_fetch", "web_fetch", "web_search"]},
            {"name": "full", "token": "bbb", "tools": "*"},
            {"name": "all", "token": "ccc", "tools": ["*"]},
        ]
    )

    readers, full, star_list = parse_token_policies(raw)

    assert readers.tools == ("web_fetch", "web_search")
    assert full.tools == ()
    assert star_list.tools == ()
    assert readers.allows("web_fetch") is True
    assert readers.allows("web_crawl") is False


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
        ('[{"name": "a", "token": "x", "tools": "web_fetch"}]', "必须是工具名数组"),
        ('[{"name": "a", "token": "x", "tools": []}]', "tools 为空"),
        ('[{"name": "a", "token": "x", "tools": [""]}]', "非法条目"),
        ('[{"name": "a", "token": "x", "tools": ["web_fetch", 1]}]', "非法条目"),
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


# ---- 工具级权限与按租户用量 ----


def test_tool_call_outside_whitelist_is_rejected_with_403():
    middleware = BearerAuthMiddleware(_ok_app, tokens=parse_token_policies(TOOL_TENANTS))

    scope, status, body = _call(
        middleware, "reader-token", method="POST", body=_tool_call("web_crawl")
    )

    assert status == 403
    assert body == {"ok": False, "error": "tool_forbidden", "tool": "web_crawl"}
    assert scope.get("reached") is None
    values = _samples(metrics.render())
    assert values['scrape_mcp_http_rejections_total{reason="tool_forbidden"}'] == 1
    assert (
        values['scrape_mcp_tenant_rejections_total{tenant="reader",reason="tool_forbidden"}'] == 1
    )


def test_tool_call_inside_whitelist_passes_and_body_is_replayed():
    payload = _tool_call("web_fetch")
    middleware = BearerAuthMiddleware(_echo_body_app, tokens=parse_token_policies(TOOL_TENANTS))

    scope, status, _ = _call(middleware, "reader-token", method="POST", body=payload)

    assert status == 200
    assert scope["reached"] is True
    # 中间件读过 body 后必须重放，下游 MCP 才能正常解析
    assert scope["body"] == payload


def test_tools_without_whitelist_allow_any_tool():
    middleware = BearerAuthMiddleware(_ok_app, tokens=parse_token_policies(TOOL_TENANTS))

    _, status, _ = _call(middleware, "full-token", method="POST", body=_tool_call("web_crawl"))

    assert status == 200


def test_non_tool_requests_skip_whitelist():
    middleware = BearerAuthMiddleware(_ok_app, tokens=parse_token_policies(TOOL_TENANTS))
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()

    _, status, _ = _call(middleware, "reader-token", method="POST", body=payload)

    assert status == 200


def test_batch_with_one_forbidden_tool_is_rejected_as_a_whole():
    middleware = BearerAuthMiddleware(_ok_app, tokens=parse_token_policies(TOOL_TENANTS))
    batch = json.dumps(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "web_fetch"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "web_crawl"}},
        ]
    ).encode()

    scope, status, body = _call(middleware, "reader-token", method="POST", body=batch)

    assert status == 403
    assert body["tool"] == "web_crawl"
    assert scope.get("reached") is None


def test_tool_usage_is_counted_per_tenant():
    middleware = BearerAuthMiddleware(_ok_app, tokens=parse_token_policies(TOOL_TENANTS))

    for tool in ("web_fetch", "web_fetch", "web_search"):
        _call(middleware, "reader-token", method="POST", body=_tool_call(tool))
    _call(middleware, "full-token", method="POST", body=_tool_call("web_crawl"))

    values = _samples(metrics.render())
    assert values['scrape_mcp_tenant_tool_calls_total{tenant="reader",tool="web_fetch"}'] == 2
    assert values['scrape_mcp_tenant_tool_calls_total{tenant="reader",tool="web_search"}'] == 1
    assert values['scrape_mcp_tenant_tool_calls_total{tenant="full",tool="web_crawl"}'] == 1

    snapshot = metrics.tenant_snapshot()
    assert snapshot["reader"]["requests"] == 3
    assert snapshot["reader"]["tool_calls"] == {"web_fetch": 2, "web_search": 1}
    assert snapshot["full"]["tool_calls"] == {"web_crawl": 1}


def test_metrics_scrape_does_not_consume_tenant_quota():
    middleware = BearerAuthMiddleware(_ok_app, tokens=parse_token_policies(TENANTS))

    for _ in range(3):
        _, status, _ = _call(middleware, "token-a", path="/metrics")
        assert status == 200

    values = _samples(metrics.render())
    assert 'scrape_mcp_tenant_requests_total{tenant="team-a"}' not in values
    assert 'scrape_mcp_tenant_rejections_total{tenant="team-a",reason="rate_limited"}' not in values
