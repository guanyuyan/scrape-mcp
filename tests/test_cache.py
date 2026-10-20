"""M4 缓存：HttpCache 的命中/过期/落盘路径，条件请求，以及 HTTP 层的缓存绕过。"""

from __future__ import annotations

import time
from typing import ClassVar

import scrape_mcp.core.http_client as hc
from scrape_mcp.core.cache import HttpCache

URL = "https://a.com/x"


def test_cache_hit_after_put(tmp_path):
    c = HttpCache(str(tmp_path), ttl=300)
    assert c.get(URL) is None
    c.put(URL, {"status": 200, "text": "hello"})
    got = c.get(URL)
    assert got is not None
    assert got["text"] == "hello"


def test_cache_expires_after_ttl(tmp_path):
    c = HttpCache(str(tmp_path), ttl=0.1)
    c.put(URL, {"status": 200, "text": "hi"})
    time.sleep(0.15)
    assert c.get(URL) is None


def test_stale_entry_readable_for_conditional_request(tmp_path):
    c = HttpCache(str(tmp_path), ttl=0.1)
    c.put(URL, {"status": 200, "text": "hi", "etag": '"v1"'})
    time.sleep(0.15)
    assert c.get(URL) is None
    stale = c.get(URL, allow_stale=True)
    assert stale is not None and stale["etag"] == '"v1"'


def test_cache_off_when_ttl_zero(tmp_path):
    c = HttpCache(str(tmp_path), ttl=0)
    c.put(URL, {"status": 200, "text": "hi"})
    assert c.get(URL) is None
    assert c.get(URL, allow_stale=True) is None


def test_distinct_urls_do_not_collide(tmp_path):
    c = HttpCache(str(tmp_path), ttl=300)
    c.put("https://a.com/1", {"status": 200, "text": "one"})
    c.put("https://a.com/2", {"status": 200, "text": "two"})
    assert c.get("https://a.com/1")["text"] == "one"
    assert c.get("https://a.com/2")["text"] == "two"


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        content: bytes = b"<html><body>hi</body></html>",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.url = URL
        self.content = content
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}


class _FakeSession:
    response: ClassVar[_FakeResponse] = _FakeResponse()
    sent_headers: ClassVar[dict[str, str]] = {}

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def get(self, *_args, **kwargs) -> _FakeResponse:
        type(self).sent_headers = dict(kwargs.get("headers") or {})
        return type(self).response


def _client(tmp_path, monkeypatch, response=None, **overrides) -> hc.HttpClient:
    monkeypatch.setattr(hc, "AsyncSession", _FakeSession)
    _FakeSession.response = response or _FakeResponse()
    return hc.HttpClient(hc.Settings(data_dir=str(tmp_path), **overrides))


def _spy_cache(client, monkeypatch) -> list[str]:
    ops: list[str] = []
    monkeypatch.setattr(
        client._cache,
        "get",
        lambda url, **kw: ops.append(f"get:{bool(kw.get('allow_stale'))}"),
    )
    monkeypatch.setattr(client._cache, "put", lambda url, data: ops.append("put"))
    return ops


async def test_plain_request_reads_and_writes_cache(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    first = await client.get(URL)
    assert first.ok and not first.cached
    second = await client.get(URL)
    assert second.ok and second.cached


async def test_custom_headers_skip_shared_cache(tmp_path, monkeypatch):
    """带自定义头的响应可能因人而异，不能读也不能写共享缓存。"""
    client = _client(tmp_path, monkeypatch)
    ops = _spy_cache(client, monkeypatch)
    resp = await client.get(URL, headers={"X-Test": "1"})
    assert resp.ok and ops == []


async def test_cookies_skip_shared_cache(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ops = _spy_cache(client, monkeypatch)
    resp = await client.get(URL, cookies={"sid": "1"})
    assert resp.ok and ops == []


async def test_bypass_cache_skips_read_and_write(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ops = _spy_cache(client, monkeypatch)
    resp = await client.get(URL, bypass_cache=True)
    assert resp.ok and ops == []


async def test_response_validators_are_stored(tmp_path, monkeypatch):
    client = _client(
        tmp_path,
        monkeypatch,
        response=_FakeResponse(
            headers={
                "content-type": "text/html",
                "etag": '"v1"',
                "last-modified": "Wed, 21 Oct 2015 07:28:00 GMT",
            }
        ),
    )
    await client.get(URL)
    saved = client._cache.get(URL, allow_stale=True)
    assert saved is not None
    assert saved["etag"] == '"v1"'
    assert saved["last_modified"] == "Wed, 21 Oct 2015 07:28:00 GMT"


async def test_conditional_request_reuses_body_on_304(tmp_path, monkeypatch):
    """过期缓存带 ETag 去问，304 时直接复用旧正文，不发第二次内容。"""
    client = _client(
        tmp_path,
        monkeypatch,
        response=_FakeResponse(status_code=304, content=b""),
        cache_ttl=0.05,
    )
    client._cache.put(
        URL,
        {
            "status": 200,
            "url": URL,
            "content_type": "text/html",
            "text": "<html><body>old body</body></html>",
            "etag": '"v1"',
            "last_modified": "Wed, 21 Oct 2015 07:28:00 GMT",
        },
    )
    time.sleep(0.06)
    resp = await client.get(URL)
    assert resp.ok and resp.cached
    assert "old body" in resp.text
    assert _FakeSession.sent_headers.get("If-None-Match") == '"v1"'
    assert _FakeSession.sent_headers.get("If-Modified-Since") == "Wed, 21 Oct 2015 07:28:00 GMT"
    # 304 后缓存时间戳被刷新，下一轮仍是新鲜命中
    assert client._cache.get(URL) is not None


def test_retry_after_parses_seconds_and_http_date():
    assert hc._parse_retry_after("5") == 5.0
    assert hc._parse_retry_after("") is None
    assert hc._parse_retry_after("not-a-date") is None
    future = hc._parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert future is not None and future > 0
