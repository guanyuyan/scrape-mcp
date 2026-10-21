"""进程内统计与 /stats 端点：计数口径、命中率、回环访问限制。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from scrape_mcp import server
from scrape_mcp.config import Settings
from scrape_mcp.core import stats
from scrape_mcp.core.fetcher import FetchOutcome

URL = "https://a.test/x"
HTML = (
    "<html><head><title>示例标题</title></head>"
    "<body><article><h1>示例标题</h1><p>正文内容，用于统计测试。</p></article></body></html>"
)


def _request(host: str | None) -> SimpleNamespace:
    client = SimpleNamespace(host=host) if host is not None else None
    return SimpleNamespace(client=client)


def _outcome() -> FetchOutcome:
    return FetchOutcome(
        url=URL,
        final_url=URL,
        status=200,
        tier="l1",
        blocked="none",
        block_reason="",
        content_type="text/html",
        html=HTML,
        elapsed_ms=8,
    )


def _patch_fetch(monkeypatch) -> None:
    class _Fetcher:
        async def fetch(self, url: str, **_kwargs) -> FetchOutcome:
            return _outcome()

        async def aclose(self) -> None:
            pass

    async def allowed(_url: str, *_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(server, "_compliance_check", allowed)
    monkeypatch.setattr(server, "_get_fetcher", lambda: _Fetcher())


def _run(coro) -> dict:
    return json.loads(asyncio.run(coro))


# ---- Stats 本体 ----


def test_snapshot_counts_and_hit_rate():
    s = stats.Stats()
    s.record_fetch(tier="l1", code="ok", kind="html", cached=False, html_bytes=100)
    s.record_fetch(tier="l2", code="blocked", kind="html", cached=True, html_bytes=0)
    s.record_result_hit()
    s.record_tokens(120)

    snap = s.snapshot()
    assert snap["ok"] is True
    assert snap["requests"] == 3
    assert snap["fetches"] == 2
    assert snap["result_cache_hits"] == 1
    assert snap["http_cache_hits"] == 1
    assert snap["cache_hit_rate"] == round(1 / 3, 3)
    assert snap["by_tier"] == {"l1": 1, "l2": 1}
    assert snap["by_code"] == {"blocked": 1, "ok": 1}
    assert snap["by_kind"] == {"html": 2}
    assert snap["tokens_returned"] == 120
    assert snap["bytes_in"] == 100
    assert snap["uptime_s"] >= 0


def test_snapshot_of_empty_stats():
    snap = stats.Stats().snapshot()
    assert snap["requests"] == 0
    assert snap["cache_hit_rate"] == 0.0
    assert snap["by_tier"] == {}


# ---- /stats 端点 ----


def test_stats_endpoint_serves_loopback():
    resp = asyncio.run(server.stats_endpoint(_request("127.0.0.1")))
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["ok"] is True
    assert set(body) == {
        "ok",
        "uptime_s",
        "requests",
        "fetches",
        "result_cache_hits",
        "http_cache_hits",
        "cache_hit_rate",
        "by_tier",
        "by_code",
        "by_kind",
        "tokens_returned",
        "bytes_in",
    }


def test_stats_endpoint_accepts_ipv6_loopback():
    resp = asyncio.run(server.stats_endpoint(_request("::1")))
    assert resp.status_code == 200


def test_stats_endpoint_rejects_remote_client():
    resp = asyncio.run(server.stats_endpoint(_request("10.1.2.3")))
    assert resp.status_code == 403
    assert json.loads(resp.body)["ok"] is False


def test_stats_endpoint_rejects_missing_client():
    """拿不到来源地址时按"非本机"处理，避免容器暴露到公网时泄露统计。"""
    resp = asyncio.run(server.stats_endpoint(_request(None)))
    assert resp.status_code == 403


# ---- 与抓取链路联动 ----


def test_fetch_and_result_cache_hit_are_counted(tmp_path, monkeypatch):
    settings = Settings(data_dir=str(tmp_path / "data"), cache_ttl=300)
    monkeypatch.setattr(server, "get_settings", lambda: settings)
    _patch_fetch(monkeypatch)

    first = _run(server.web_fetch(URL))
    second = _run(server.web_fetch(URL))
    snap = stats.get_stats().snapshot()

    assert first["cached"] is False and second["cached"] is True
    assert snap["fetches"] == 1
    assert snap["result_cache_hits"] == 1
    assert snap["requests"] == 2
    assert snap["by_tier"] == {"l1": 1}
    assert snap["by_code"] == {"ok": 1}
    assert snap["tokens_returned"] == first["token_estimate"]


def test_blocked_fetch_counts_error_code(tmp_path, monkeypatch):
    class _Fetcher:
        async def fetch(self, url: str, **_kwargs) -> FetchOutcome:
            return FetchOutcome(
                url=URL,
                final_url=URL,
                status=200,
                tier="l2",
                blocked="cloudflare",
                block_reason="cf",
                content_type="text/html",
                html=HTML,
                elapsed_ms=12,
            )

        async def aclose(self) -> None:
            pass

    async def allowed(_url: str, *_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(server, "_compliance_check", allowed)
    monkeypatch.setattr(server, "_get_fetcher", lambda: _Fetcher())

    out = _run(server.web_fetch(URL))
    snap = stats.get_stats().snapshot()
    assert out["ok"] is False
    assert snap["by_code"] == {"blocked": 1}
    assert snap["tokens_returned"] == 0
