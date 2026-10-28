"""Prometheus 指标：抓取量/层级计数口径、拒绝计数、L2 占用与端点访问限制。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from scrape_mcp import __version__, metrics, server
from scrape_mcp.config import Settings
from scrape_mcp.core import stats
from scrape_mcp.http_auth import BearerAuthMiddleware, TokenPolicy

URL = "https://a.test/x"


def _request(host: str | None) -> SimpleNamespace:
    client = SimpleNamespace(host=host) if host is not None else None
    return SimpleNamespace(client=client)


def _samples(text: str) -> dict[str, float]:
    """把文本格式解析成 {样本名（含 label）: 值}，跳过 HELP/TYPE 行。"""
    values: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        values[name] = float(value)
    return values


# ---- 渲染 ----


def test_render_reports_fetch_volume_and_tiers():
    s = stats.get_stats()
    s.record_fetch(tier="l1", code="ok", kind="html", cached=False, html_bytes=120, elapsed_ms=30)
    s.record_fetch(tier="l2", code="ok", kind="html", cached=True, html_bytes=80, elapsed_ms=900)
    s.record_result_hit()
    s.record_tokens(300)

    body = metrics.render()
    values = _samples(body)

    assert values["scrape_mcp_fetches_total"] == 2
    assert values["scrape_mcp_requests_total"] == 3
    assert values["scrape_mcp_result_cache_hits_total"] == 1
    assert values["scrape_mcp_http_cache_hits_total"] == 1
    assert values["scrape_mcp_tokens_returned_total"] == 300
    assert values["scrape_mcp_bytes_in_total"] == 200
    assert values['scrape_mcp_fetch_tier_total{tier="l1"}'] == 1
    assert values['scrape_mcp_fetch_tier_total{tier="l2"}'] == 1
    assert values['scrape_mcp_fetch_code_total{code="ok"}'] == 2
    assert values['scrape_mcp_fetch_kind_total{kind="html"}'] == 2
    assert f'scrape_mcp_build_info{{version="{__version__}"}}' in values
    assert values[f'scrape_mcp_build_info{{version="{__version__}"}}'] == 1
    assert "# TYPE scrape_mcp_fetches_total counter" in body
    assert body.endswith("\n")


def test_render_reports_rejections_by_reason():
    metrics.record_rejection("unauthorized")
    metrics.record_rejection("unauthorized")
    metrics.record_rejection("rate_limited")

    values = _samples(metrics.render())

    assert values['scrape_mcp_http_rejections_total{reason="unauthorized"}'] == 2
    assert values['scrape_mcp_http_rejections_total{reason="rate_limited"}'] == 1
    # 关闭的维度集合始终给出 0 基线，便于告警与看板，不依赖“样本出现才有值”
    assert values['scrape_mcp_http_rejections_total{reason="forbidden"}'] == 0
    assert values['scrape_mcp_http_rejections_total{reason="quota_exceeded"}'] == 0


def test_render_reports_latency_histogram():
    """耗时按层级进直方图：桶值累计，+Inf 等于该层样本总数。"""
    metrics.observe_latency("l1", 30)
    metrics.observe_latency("l1", 250)
    metrics.observe_latency("l2", 1500)

    values = _samples(metrics.render())

    assert values['scrape_mcp_fetch_duration_seconds_bucket{tier="l1",le="0.1"}'] == 1
    assert values['scrape_mcp_fetch_duration_seconds_bucket{tier="l1",le="0.3"}'] == 2
    assert values['scrape_mcp_fetch_duration_seconds_bucket{tier="l1",le="1"}'] == 2
    assert values['scrape_mcp_fetch_duration_seconds_bucket{tier="l1",le="+Inf"}'] == 2
    assert values['scrape_mcp_fetch_duration_seconds_count{tier="l1"}'] == 2
    assert values['scrape_mcp_fetch_duration_seconds_sum{tier="l1"}'] == 0.28
    assert values['scrape_mcp_fetch_duration_seconds_bucket{tier="l2",le="3"}'] == 1
    assert values['scrape_mcp_fetch_duration_seconds_count{tier="l2"}'] == 1
    assert "# TYPE scrape_mcp_fetch_duration_seconds histogram" in metrics.render()


def test_render_reports_proxy_pool_health():
    values = _samples(metrics.render(proxy={"size": 3, "healthy": 2, "cooling": 1}))

    assert values["scrape_mcp_proxy_pool_size"] == 3
    assert values["scrape_mcp_proxy_pool_healthy"] == 2
    assert values["scrape_mcp_proxy_pool_cooling"] == 1


def test_render_omits_proxy_pool_when_unconfigured():
    """没配代理池时不输出这组 gauge，避免看板把“未配置”误读成 0 个可用出口。"""
    assert "scrape_mcp_proxy_pool" not in metrics.render()


def test_render_reports_l2_occupancy():
    snap = {
        "running": 1,
        "active_pages": 2,
        "queued": 1,
        "contexts": 3,
        "max_pages": 3,
        "max_contexts": 8,
    }

    values = _samples(metrics.render(snap))

    assert values["scrape_mcp_l2_browser_running"] == 1
    assert values["scrape_mcp_l2_active_pages"] == 2
    assert values["scrape_mcp_l2_queued_pages"] == 1
    assert values["scrape_mcp_l2_contexts"] == 3
    assert values["scrape_mcp_l2_max_pages"] == 3
    assert values["scrape_mcp_l2_max_contexts"] == 8


def test_render_without_browser_reports_zero_occupancy():
    """浏览器从未启动：占用类指标按 0 输出，而不是缺样本。"""
    values = _samples(metrics.render())

    assert values["scrape_mcp_l2_browser_running"] == 0
    assert values["scrape_mcp_l2_active_pages"] == 0
    assert values["scrape_mcp_l2_queued_pages"] == 0
    assert values["scrape_mcp_l2_contexts"] == 0


def test_render_escapes_label_values():
    metrics.record_rejection('bad"reason\nnext')

    body = metrics.render()

    assert '{reason="bad\\"reason\\nnext"}' in body
    assert body.count("\n") == body.strip("\n").count("\n") + 1  # 换行已被转义，未把样本拆成两行


# ---- /metrics 端点 ----


def test_metrics_endpoint_serves_loopback(monkeypatch):
    monkeypatch.setattr(server, "_fetcher", None)

    resp = asyncio.run(server.metrics_endpoint(_request("127.0.0.1")))

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert b"scrape_mcp_build_info" in resp.body
    assert b"scrape_mcp_l2_browser_running 0" in resp.body
    # 浏览器没启动过：占用为 0，但并发上限仍按配置输出（默认 3 / 8）
    assert b"scrape_mcp_l2_max_pages 3" in resp.body
    assert b"scrape_mcp_l2_max_contexts 8" in resp.body


def test_metrics_endpoint_rejects_remote_client(monkeypatch):
    monkeypatch.setattr(server, "_fetcher", None)

    resp = asyncio.run(server.metrics_endpoint(_request("10.1.2.3")))

    assert resp.status_code == 403
    assert json.loads(resp.body)["ok"] is False


def test_metrics_endpoint_exposes_fetcher_l2_snapshot(monkeypatch):
    class _Fetcher:
        def browser_snapshot(self) -> dict[str, int]:
            return {
                "running": 1,
                "active_pages": 1,
                "queued": 0,
                "contexts": 2,
                "max_pages": 3,
                "max_contexts": 8,
            }

        def proxy_snapshot(self) -> dict[str, int]:
            return {"size": 2, "healthy": 1, "cooling": 1}

    monkeypatch.setattr(server, "_fetcher", _Fetcher())

    resp = asyncio.run(server.metrics_endpoint(_request("127.0.0.1")))

    assert b"scrape_mcp_l2_active_pages 1" in resp.body
    assert b"scrape_mcp_l2_contexts 2" in resp.body
    assert b"scrape_mcp_proxy_pool_size 2" in resp.body
    assert b"scrape_mcp_proxy_pool_cooling 1" in resp.body


def test_metrics_endpoint_reports_pool_before_first_fetch(tmp_path, monkeypatch):
    """抓取器懒加载：首个请求前也要按配置显示代理池规模，而不是缺样本。"""
    settings = Settings(
        data_dir=str(tmp_path / "data"),
        cache_ttl=0,
        proxy_pool="http://127.0.0.1:9001,http://127.0.0.1:9002",
    )
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setattr(server, "_fetcher", None)

    resp = asyncio.run(server.metrics_endpoint(_request("127.0.0.1")))

    assert b"scrape_mcp_proxy_pool_size 2" in resp.body
    assert b"scrape_mcp_proxy_pool_healthy 2" in resp.body
    assert b"scrape_mcp_proxy_pool_cooling 0" in resp.body


def test_tenant_quota_remaining_is_exposed(monkeypatch):
    """配额余量由中间件回调渲染：只输出设了日配额的租户，用一次就减一。"""
    policies = [
        TokenPolicy(name="team-a", token="token-a", daily_quota=3),
        TokenPolicy(name="ci", token="token-ci", daily_quota=0),
    ]
    middleware = BearerAuthMiddleware(_ok_app, tokens=policies)
    authed = ((b"authorization", b"Bearer token-a"),)

    assert _call_auth(middleware, headers=authed) == 200
    assert _call_auth(middleware, headers=authed) == 200

    body = metrics.render()

    assert 'scrape_mcp_tenant_quota_remaining{tenant="team-a"} 1' in body
    assert 'scrape_mcp_tenant_quota_remaining{tenant="ci"' not in body


def test_metrics_endpoint_allows_remote_when_token_configured(tmp_path, monkeypatch):
    """配置令牌后由 Bearer 中间件把关，路由本身不再限制来源地址。"""
    settings = Settings(data_dir=str(tmp_path / "data"), cache_ttl=0, http_token="s3cret")
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    monkeypatch.setattr(server, "_fetcher", None)

    resp = asyncio.run(server.metrics_endpoint(_request("10.1.2.3")))

    assert resp.status_code == 200


# ---- 中间件拒绝计数 ----


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _call_auth(
    middleware: BearerAuthMiddleware,
    *,
    headers=(),
    client=("10.0.0.5", 5000),
    path: str = "/mcp",
):
    scope = {
        "type": "http",
        "path": path,
        "headers": list(headers),
        "client": client,
        "state": {},
    }
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    return next(item["status"] for item in sent if item["type"] == "http.response.start")


def test_auth_rejections_are_counted():
    token = "s3cret-token"
    middleware = BearerAuthMiddleware(_ok_app, token, qps=1.0, daily_quota=0)

    assert _call_auth(middleware) == 401
    assert _call_auth(BearerAuthMiddleware(_ok_app, None)) == 403

    authed = ((b"authorization", f"Bearer {token}".encode()),)
    assert _call_auth(middleware, headers=authed) == 200
    assert _call_auth(middleware, headers=authed) == 429

    values = _samples(metrics.render())
    assert values['scrape_mcp_http_rejections_total{reason="unauthorized"}'] == 1
    assert values['scrape_mcp_http_rejections_total{reason="forbidden"}'] == 1
    assert values['scrape_mcp_http_rejections_total{reason="rate_limited"}'] == 1


def test_metrics_path_skips_quota_but_still_requires_token():
    """Prometheus 按固定间隔抓取：/metrics 不计配额，但没有令牌仍然是 401。"""
    token = "s3cret-token"
    middleware = BearerAuthMiddleware(_ok_app, token, qps=1.0)
    authed = ((b"authorization", f"Bearer {token}".encode()),)

    assert _call_auth(middleware, path="/metrics") == 401
    assert _call_auth(middleware, headers=authed, path="/metrics") == 200
    assert _call_auth(middleware, headers=authed, path="/metrics") == 200

    values = _samples(metrics.render())
    assert values['scrape_mcp_http_rejections_total{reason="unauthorized"}'] == 1
    assert values['scrape_mcp_http_rejections_total{reason="rate_limited"}'] == 0
