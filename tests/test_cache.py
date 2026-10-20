"""M4 缓存：HttpCache 的命中/过期/落盘路径，以及 HTTP 层的缓存绕过。"""

from __future__ import annotations

import time

import scrape_mcp.core.http_client as hc
from scrape_mcp.core.cache import HttpCache


def test_cache_hit_after_put(tmp_path):
    c = HttpCache(str(tmp_path), ttl=300)
    assert c.get("https://a.com/x") is None
    c.put("https://a.com/x", {"status": 200, "text": "hello"})
    got = c.get("https://a.com/x")
    assert got is not None
    assert got["text"] == "hello"


def test_cache_expires_after_ttl(tmp_path):
    c = HttpCache(str(tmp_path), ttl=0.1)
    c.put("https://a.com/x", {"status": 200, "text": "hi"})
    time.sleep(0.15)
    assert c.get("https://a.com/x") is None


def test_cache_off_when_ttl_zero(tmp_path):
    c = HttpCache(str(tmp_path), ttl=0)
    c.put("https://a.com/x", {"status": 200, "text": "hi"})
    assert c.get("https://a.com/x") is None


def test_distinct_urls_do_not_collide(tmp_path):
    c = HttpCache(str(tmp_path), ttl=300)
    c.put("https://a.com/1", {"status": 200, "text": "one"})
    c.put("https://a.com/2", {"status": 200, "text": "two"})
    assert c.get("https://a.com/1")["text"] == "one"
    assert c.get("https://a.com/2")["text"] == "two"


class _FakeResponse:
    def __init__(self) -> None:
        self.status_code = 200
        self.url = "https://a.com/x"
        self.content = b"<html><body>hi</body></html>"
        self.headers = {"content-type": "text/html; charset=utf-8"}


class _FakeSession:
    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, *_args, **_kwargs) -> _FakeResponse:
        return _FakeResponse()


def _client(tmp_path, monkeypatch) -> hc.HttpClient:
    monkeypatch.setattr(hc, "AsyncSession", _FakeSession)
    return hc.HttpClient(hc.Settings(data_dir=str(tmp_path)))


async def test_plain_request_reads_and_writes_cache(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ops: list[str] = []
    monkeypatch.setattr(client._cache, "get", lambda url: ops.append("get"))
    monkeypatch.setattr(client._cache, "put", lambda url, data: ops.append("put"))
    resp = await client.get("https://a.com/x")
    assert resp.ok and ops == ["get", "put"]


async def test_custom_headers_skip_shared_cache(tmp_path, monkeypatch):
    """带自定义头的响应可能因人而异，不能读也不能写共享缓存。"""
    client = _client(tmp_path, monkeypatch)
    ops: list[str] = []
    monkeypatch.setattr(client._cache, "get", lambda url: ops.append("get"))
    monkeypatch.setattr(client._cache, "put", lambda url, data: ops.append("put"))
    resp = await client.get("https://a.com/x", headers={"X-Test": "1"})
    assert resp.ok and ops == []


async def test_cookies_skip_shared_cache(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ops: list[str] = []
    monkeypatch.setattr(client._cache, "get", lambda url: ops.append("get"))
    monkeypatch.setattr(client._cache, "put", lambda url, data: ops.append("put"))
    resp = await client.get("https://a.com/x", cookies={"sid": "1"})
    assert resp.ok and ops == []


async def test_bypass_cache_skips_read_and_write(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ops: list[str] = []
    monkeypatch.setattr(client._cache, "get", lambda url: ops.append("get"))
    monkeypatch.setattr(client._cache, "put", lambda url, data: ops.append("put"))
    resp = await client.get("https://a.com/x", bypass_cache=True)
    assert resp.ok and ops == []
