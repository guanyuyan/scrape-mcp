"""历史快照：版本序列、保留策略、版本解析与 web_fetch(version=...) 回读。"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from scrape_mcp import server
from scrape_mcp.core import errors, objectstore
from scrape_mcp.core.fetcher import FetchOutcome
from scrape_mcp.core.objectstore import LocalSnapshotBackend
from scrape_mcp.core.watch import SnapshotError, WatchStore

URL = "https://a.com/doc"
HTML_V1 = "<html><body><article><h1>标题</h1><p>第一版正文</p></article></body></html>"
HTML_V2 = "<html><body><article><h1>标题</h1><p>第二版正文，多了内容</p></article></body></html>"


def _outcome(html: str) -> FetchOutcome:
    return FetchOutcome(
        url=URL,
        final_url=URL,
        status=200,
        tier="l1",
        blocked="none",
        block_reason="",
        content_type="text/html",
        html=html,
        elapsed_ms=3,
    )


def _watch(**kwargs) -> dict:
    return json.loads(asyncio.run(server.web_watch(URL, **kwargs)))


def _call(tool, *args, **kwargs) -> dict:
    return json.loads(asyncio.run(tool(*args, **kwargs)))


def test_backend_versions_newest_first_and_limited(tmp_path):
    backend = LocalSnapshotBackend(tmp_path)
    for index in range(3):
        backend.save("k", {"text": f"v{index}"})

    versions = backend.versions("k")
    assert [item["text"] for item in versions] == ["v2", "v1", "v0"]
    assert [item["version"] for item in versions] == [1, 2, 3]
    assert backend.versions("k", limit=2)[0]["text"] == "v2"
    assert backend.load("k")["text"] == "v2"


def test_backend_prunes_by_version_count(tmp_path, monkeypatch):
    monkeypatch.setattr(
        objectstore,
        "get_settings",
        lambda: SimpleNamespace(watch_max_versions=3, watch_retention_days=0),
    )
    backend = LocalSnapshotBackend(tmp_path)
    for index in range(6):
        backend.save("k", {"text": f"v{index}"})

    assert [item["text"] for item in backend.versions("k")] == ["v5", "v4", "v3"]
    assert len(list((tmp_path / "snapshots" / "k").glob("*.json"))) == 3


def test_backend_prunes_by_retention_days(tmp_path, monkeypatch):
    monkeypatch.setattr(
        objectstore,
        "get_settings",
        lambda: SimpleNamespace(watch_max_versions=20, watch_retention_days=90),
    )
    backend = LocalSnapshotBackend(tmp_path)
    directory = tmp_path / "snapshots" / "k"
    directory.mkdir(parents=True)
    old_ts = time.time() - 100 * 86400
    (directory / f"{int(old_ts * 1000):013d}.json").write_text(
        json.dumps({"ts": old_ts, "text": "old"}), encoding="utf-8"
    )

    backend.save("k", {"text": "new"})
    assert [item["text"] for item in backend.versions("k")] == ["new"]


def test_backend_reads_legacy_single_file(tmp_path):
    legacy = tmp_path / "watch"
    legacy.mkdir(parents=True)
    (legacy / "k.json").write_text(
        json.dumps({"saved_at": 1.0, "hash": "h", "text": "旧版快照"}), encoding="utf-8"
    )

    versions = LocalSnapshotBackend(tmp_path).versions("k")
    assert len(versions) == 1
    assert versions[0]["version"] == 1
    assert versions[0]["text"] == "旧版快照"


def test_store_resolve_supports_latest_prev_index_and_time(tmp_path):
    store = WatchStore(tmp_path)
    for text in ("one", "two", "three"):
        store.save("k", {"text": text})
    captured = store.history("k")

    assert store.resolve("k")["text"] == "three"
    assert store.resolve("k", "prev")["text"] == "two"
    assert store.resolve("k", 3)["text"] == "one"
    assert store.resolve("k", str(captured[1]["captured_at"]))["text"] == "two"
    assert store.resolve("k", 9) is None
    with pytest.raises(SnapshotError):
        store.resolve("k", "上个星期")
    with pytest.raises(SnapshotError):
        store.resolve("k", 0)


def test_web_history_lists_versions_and_errors(monkeypatch):
    pages = [HTML_V1, HTML_V2]

    async def fake_fetch_html(url, **_kwargs):
        return _outcome(pages.pop(0)), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)

    missing = _call(server.web_history, URL)
    assert missing["ok"] is False and missing["error_code"] == errors.NO_SNAPSHOT

    _watch()
    _watch()
    history = _call(server.web_history, URL)
    assert history["ok"] is True
    assert history["count"] == 2
    assert [item["version"] for item in history["versions"]] == [1, 2]
    assert all(item["chars"] > 0 and item["hash"] for item in history["versions"])


def test_web_fetch_reads_snapshot_without_network(monkeypatch):
    pages = [HTML_V1, HTML_V2]

    async def fake_fetch_html(url, **_kwargs):
        return _outcome(pages.pop(0)), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)
    _watch()
    _watch()

    async def no_fetch(url, **_kwargs):  # 回读快照不应再发请求
        raise AssertionError("web_fetch(version=...) 不应触发抓取")

    monkeypatch.setattr(server, "_fetch_html", no_fetch)

    latest = _call(server.web_fetch, URL, version="latest")
    assert latest["ok"] is True and latest["cached"] is True
    assert latest["version"] == 1 and latest["source_version"] == 1
    assert "第二版正文" in latest["content"]

    prev = _call(server.web_fetch, URL, version="prev")
    assert prev["version"] == 2
    assert "第一版正文" in prev["content"]

    second = _call(server.web_fetch, URL, version=2)
    assert second["hash"] == prev["hash"]


def test_web_fetch_snapshot_errors_are_specific(monkeypatch):
    async def fake_fetch_html(url, **_kwargs):
        return _outcome(HTML_V1), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)

    none = _call(server.web_fetch, URL, version="latest")
    assert none["ok"] is False and none["error_code"] == errors.NO_SNAPSHOT

    _watch()
    assert _call(server.web_fetch, URL, version=9)["error_code"] == errors.NO_SNAPSHOT
    bad = _call(server.web_fetch, URL, version="昨天")
    assert bad["ok"] is False and bad["error_code"] == errors.INVALID_ARGUMENT
