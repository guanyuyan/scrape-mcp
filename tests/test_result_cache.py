"""结果缓存：正文（compact 后）与字段抽取结果的命中、失效与隔离。"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp import server
from scrape_mcp.config import Settings
from scrape_mcp.core.cache import ResultCache
from scrape_mcp.core.fetcher import FetchOutcome

URL = "https://a.test/x"
HTML = (
    "<html><head><title>示例标题</title></head>"
    "<body><article><h1>示例标题</h1><p>正文内容，用于缓存测试。</p></article></body></html>"
)
SCHEMA = {"fields": {"title": {"selector": "h1"}}}


def _outcome(**overrides) -> FetchOutcome:
    base = {
        "url": URL,
        "final_url": URL,
        "status": 200,
        "tier": "l1",
        "blocked": "none",
        "block_reason": "",
        "content_type": "text/html",
        "html": HTML,
        "elapsed_ms": 8,
    }
    base.update(overrides)
    return FetchOutcome(**base)


class _CountingFetcher:
    def __init__(self, outcome: FetchOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    async def fetch(self, url: str, **_kwargs) -> FetchOutcome:
        self.calls += 1
        return self.outcome

    async def aclose(self) -> None:
        pass


def _patch(monkeypatch, outcome: FetchOutcome) -> _CountingFetcher:
    fetcher = _CountingFetcher(outcome)

    async def allowed(_url: str) -> None:
        return None

    monkeypatch.setattr(server, "_compliance_check", allowed)
    monkeypatch.setattr(server, "_get_fetcher", lambda: fetcher)
    return fetcher


def _enable_cache(tmp_path, monkeypatch, ttl: float = 300) -> None:
    """打开结果缓存：conftest 默认把 cache_ttl 设为 0。"""
    settings = Settings(data_dir=str(tmp_path / "data"), cache_ttl=ttl)
    monkeypatch.setattr(server, "get_settings", lambda: settings)


def _run(coro) -> dict:
    return json.loads(asyncio.run(coro))


# ---- ResultCache 本体 ----


def test_result_cache_roundtrip_and_ttl(tmp_path):
    cache = ResultCache(tmp_path, 300, "fetch")
    assert cache.get("k") is None
    cache.put("k", {"ok": True, "content": "正文"})
    assert cache.get("k") == {"ok": True, "content": "正文"}

    fp = cache._file_for("k")
    entry = json.loads(fp.read_text(encoding="utf-8"))
    entry["ts"] -= 301
    fp.write_text(json.dumps(entry), encoding="utf-8")
    assert cache.get("k") is None


def test_result_cache_namespaces_and_ttl_zero(tmp_path):
    ResultCache(tmp_path, 300, "fetch").put("k", {"ok": True})
    assert ResultCache(tmp_path, 300, "extract").get("k") is None
    assert ResultCache(tmp_path, 0, "fetch").get("k") is None


def test_result_cache_ignores_broken_file(tmp_path):
    cache = ResultCache(tmp_path, 300, "fetch")
    cache._file_for("k").parent.mkdir(parents=True, exist_ok=True)
    cache._file_for("k").write_text("{not json", encoding="utf-8")
    assert cache.get("k") is None


# ---- web_fetch ----


def test_web_fetch_second_call_hits_result_cache(tmp_path, monkeypatch):
    _enable_cache(tmp_path, monkeypatch)
    fetcher = _patch(monkeypatch, _outcome())

    first = _run(server.web_fetch(URL))
    second = _run(server.web_fetch(URL))

    assert fetcher.calls == 1
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["content"] == first["content"]
    assert second["token_estimate"] == first["token_estimate"]


def test_result_cache_hit_keeps_payload_contract(tmp_path, monkeypatch):
    """命中缓存返回的键集合必须与现抓一致，调用方不必为缓存分支写两套解析。"""
    _enable_cache(tmp_path, monkeypatch)
    _patch(monkeypatch, _outcome())
    fresh = _run(server.web_fetch(URL))
    hit = _run(server.web_fetch(URL))
    assert set(hit) == set(fresh)


def test_different_budget_misses_cache(tmp_path, monkeypatch):
    _enable_cache(tmp_path, monkeypatch)
    fetcher = _patch(monkeypatch, _outcome())
    _run(server.web_fetch(URL, max_tokens=1000))
    _run(server.web_fetch(URL, max_tokens=2000))
    assert fetcher.calls == 2


def test_bypass_cache_skips_result_cache(tmp_path, monkeypatch):
    _enable_cache(tmp_path, monkeypatch)
    fetcher = _patch(monkeypatch, _outcome())
    _run(server.web_fetch(URL))
    out = _run(server.web_fetch(URL, bypass_cache=True))
    assert fetcher.calls == 2
    assert out["cached"] is False


def test_debug_skips_result_cache(tmp_path, monkeypatch):
    _enable_cache(tmp_path, monkeypatch)
    fetcher = _patch(monkeypatch, _outcome())
    _run(server.web_fetch(URL))
    out = _run(server.web_fetch(URL, debug=True))
    assert fetcher.calls == 2
    assert "diagnostics" in out


def test_custom_headers_and_cookies_skip_result_cache(tmp_path, monkeypatch):
    _enable_cache(tmp_path, monkeypatch)
    fetcher = _patch(monkeypatch, _outcome())
    _run(server.web_fetch(URL, headers={"X-Test": "1"}))
    _run(server.web_fetch(URL, cookies={"sid": "1"}))
    assert fetcher.calls == 2


def test_failed_fetch_is_not_cached(tmp_path, monkeypatch):
    _enable_cache(tmp_path, monkeypatch)
    fetcher = _patch(monkeypatch, _outcome(blocked="cloudflare", block_reason="cf"))
    first = _run(server.web_fetch(URL))
    second = _run(server.web_fetch(URL))
    assert fetcher.calls == 2
    assert first["ok"] is False and second["ok"] is False


def test_cache_off_when_ttl_zero(tmp_path, monkeypatch):
    """conftest 已把 cache_ttl 设为 0：同一 URL 每次都真的抓取。"""
    fetcher = _patch(monkeypatch, _outcome())
    _run(server.web_fetch(URL))
    _run(server.web_fetch(URL))
    assert fetcher.calls == 2


# ---- web_extract ----


def test_web_extract_second_call_hits_result_cache(tmp_path, monkeypatch):
    _enable_cache(tmp_path, monkeypatch)
    fetcher = _patch(monkeypatch, _outcome())

    first = _run(server.web_extract(URL, SCHEMA))
    second = _run(server.web_extract(URL, SCHEMA))

    assert fetcher.calls == 1
    assert second["cached"] is True
    assert second["data"] == first["data"]


def test_web_extract_different_schema_misses_cache(tmp_path, monkeypatch):
    _enable_cache(tmp_path, monkeypatch)
    fetcher = _patch(monkeypatch, _outcome())
    _run(server.web_extract(URL, SCHEMA))
    other = {"fields": {"body": {"selector": "p"}}}
    _run(server.web_extract(URL, other))
    assert fetcher.calls == 2


def test_web_extract_does_not_cache_required_missing(tmp_path, monkeypatch):
    _enable_cache(tmp_path, monkeypatch)
    fetcher = _patch(monkeypatch, _outcome())
    schema = {"fields": {"absent": {"selector": ".nope", "required": True}}}
    first = _run(server.web_extract(URL, schema))
    second = _run(server.web_extract(URL, schema))
    assert fetcher.calls == 2
    assert first["error_code"] == "missing_required"
    assert second["error_code"] == "missing_required"
