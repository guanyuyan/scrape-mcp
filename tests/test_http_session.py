"""L1 连接复用：同一事件循环内复用会话，换循环重建，关闭时释放。"""

from __future__ import annotations

import asyncio
from typing import ClassVar

import scrape_mcp.core.http_client as hc
from scrape_mcp.config import Settings
from scrape_mcp.core.fetcher import Fetcher

URL = "https://a.test/x"


class _FakeResponse:
    status_code = 200
    content = b"<html><body>hi</body></html>"
    headers: ClassVar[dict[str, str]] = {"content-type": "text/html; charset=utf-8"}
    url = URL


class _FakeSession:
    created = 0
    closed = 0
    gets = 0

    def __init__(self, **_kwargs) -> None:
        type(self).created += 1

    async def get(self, *_args, **_kwargs) -> _FakeResponse:
        type(self).gets += 1
        return _FakeResponse()

    async def close(self) -> None:
        type(self).closed += 1


def _reset() -> None:
    _FakeSession.created = 0
    _FakeSession.closed = 0
    _FakeSession.gets = 0


def _client(tmp_path, monkeypatch) -> hc.HttpClient:
    monkeypatch.setattr(hc, "AsyncSession", _FakeSession)
    _reset()
    return hc.HttpClient(Settings(data_dir=str(tmp_path), cache_ttl=0))


def test_session_reused_within_one_loop(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    async def run() -> None:
        await client.get(URL, bypass_cache=True)
        await client.get(URL, bypass_cache=True)

    asyncio.run(run())
    assert _FakeSession.created == 1
    assert _FakeSession.gets == 2


def test_session_rebuilt_when_event_loop_changes(tmp_path, monkeypatch):
    """curl_cffi 会话绑定事件循环；多次 asyncio.run 时必须重建，否则报会话已关闭。"""
    client = _client(tmp_path, monkeypatch)
    asyncio.run(client.get(URL, bypass_cache=True))
    asyncio.run(client.get(URL, bypass_cache=True))
    assert _FakeSession.created == 2
    assert _FakeSession.gets == 2


def test_aclose_closes_sessions_and_is_idempotent(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    async def run() -> None:
        await client.get(URL, bypass_cache=True)
        await client.aclose()
        await client.aclose()  # 重复调用不应抛错

    asyncio.run(run())
    assert _FakeSession.closed == 1


def test_fetcher_aclose_releases_http_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "AsyncSession", _FakeSession)
    _reset()
    fetcher = Fetcher(Settings(data_dir=str(tmp_path), cache_ttl=0))

    async def run() -> None:
        fetcher._http._session("chrome")  # 只建会话，不真的发请求
        await fetcher.aclose()

    asyncio.run(run())
    assert _FakeSession.created == 1
    assert _FakeSession.closed == 1
