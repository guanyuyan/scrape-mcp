"""快照对象存储：本地与 S3 兼容后端的读写、错误消化与配置选择。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scrape_mcp.core import objectstore
from scrape_mcp.core.objectstore import (
    LocalSnapshotBackend,
    S3ObjectStore,
    S3SnapshotBackend,
    build_snapshot_backend,
)
from scrape_mcp.core.watch import WatchStore


class _Body:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.fail_put = False
        self.fail_get = False
        self.fail_list = False

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str = "") -> None:
        if self.fail_put:
            raise RuntimeError("put failed")
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket: str, Key: str) -> dict:
        if self.fail_get or (Bucket, Key) not in self.objects:
            raise RuntimeError("NoSuchKey")
        return {"Body": _Body(self.objects[(Bucket, Key)])}

    def list_objects_v2(self, Bucket: str, Prefix: str = "", ContinuationToken=None) -> dict:
        if self.fail_list:
            raise RuntimeError("list failed")
        keys = sorted(key for bucket, key in self.objects if bucket == Bucket)
        return {"Contents": [{"Key": key} for key in keys if key.startswith(Prefix)]}


def test_local_backend_roundtrip(tmp_path):
    backend = LocalSnapshotBackend(tmp_path)

    assert backend.load("abc") is None
    backend.save("abc", {"text": "第一版"})
    record = backend.load("abc")
    assert record["text"] == "第一版"
    assert record["version"] == 1
    assert list((tmp_path / "snapshots" / "abc").glob("*.json"))

    backend.save("abc", {"text": "第二版"})
    latest = backend.load("abc")
    assert latest["text"] == "第二版"
    assert latest["version"] == 1
    versions = backend.versions("abc")
    assert [item["text"] for item in versions] == ["第二版", "第一版"]
    assert [item["version"] for item in versions] == [1, 2]


def test_local_backend_ignores_broken_files(tmp_path):
    backend = LocalSnapshotBackend(tmp_path)
    path = tmp_path / "watch" / "bad.json"
    path.parent.mkdir(parents=True)
    path.write_text("不是 JSON", encoding="utf-8")

    assert backend.load("bad") is None
    assert backend.load("missing") is None


def test_watch_store_saves_timestamp_with_backend(tmp_path):
    store = WatchStore(tmp_path)

    store.save("k1", {"hash": "h", "text": "hello"})

    record = store.load("k1")
    assert record["hash"] == "h"
    assert record["text"] == "hello"
    assert isinstance(record["saved_at"], float)


def test_s3_backend_roundtrip_and_key_prefix():
    client = FakeS3()
    backend = S3SnapshotBackend(client, "bucket", "prod")

    assert backend.load("abc") is None
    backend.save("abc", {"text": "hello"})

    keys = [key for bucket, key in client.objects if bucket == "bucket"]
    assert len(keys) == 1
    assert keys[0].startswith("prod/snapshots/abc/")
    assert keys[0].endswith(".json")
    assert json.loads(client.objects[("bucket", keys[0])])["text"] == "hello"
    assert backend.load("abc")["text"] == "hello"


def test_s3_backend_without_prefix_uses_short_key():
    client = FakeS3()
    backend = S3SnapshotBackend(client, "bucket", "")
    backend.save("abc", {"n": 1})

    assert any(key.startswith("snapshots/abc/") for _bucket, key in client.objects)


def test_s3_backend_swallows_errors_and_bad_payloads():
    client = FakeS3()
    backend = S3SnapshotBackend(client, "bucket")

    client.fail_put = True
    backend.save("abc", {"n": 1})
    assert client.objects == {}

    client.fail_get = False
    client.objects[("bucket", "watch/abc.json")] = b"[1, 2]"
    assert backend.load("abc") is None

    client.fail_get = True
    assert backend.load("abc") is None


def test_watch_store_persists_via_s3():
    client = FakeS3()
    store = WatchStore("/unused", S3SnapshotBackend(client, "bucket", "env"))

    store.save("k1", {"hash": "h", "text": "hello"})

    record = store.load("k1")
    assert record["hash"] == "h"
    assert isinstance(record["saved_at"], float)


def test_build_backend_defaults_to_local(tmp_path, monkeypatch):
    monkeypatch.setattr(
        objectstore, "get_settings", lambda: SimpleNamespace(snapshot_backend="local")
    )

    backend = build_snapshot_backend(tmp_path)

    assert isinstance(backend, LocalSnapshotBackend)


def test_build_backend_s3_uses_bucket_and_client(monkeypatch):
    fake = FakeS3()
    settings = SimpleNamespace(
        snapshot_backend="s3",
        s3_bucket="bucket",
        s3_prefix="env",
    )
    monkeypatch.setattr(objectstore, "get_settings", lambda: settings)
    monkeypatch.setattr(objectstore, "create_s3_client", lambda: fake)

    backend = build_snapshot_backend("/unused")

    assert isinstance(backend, S3SnapshotBackend)
    backend.save("abc", {"n": 1})
    assert any(key.startswith("env/snapshots/abc/") for _bucket, key in fake.objects)


def test_build_backend_rejects_bad_config(monkeypatch):
    monkeypatch.setattr(
        objectstore,
        "get_settings",
        lambda: SimpleNamespace(snapshot_backend="magic"),
    )
    with pytest.raises(RuntimeError):
        build_snapshot_backend("/unused")

    monkeypatch.setattr(
        objectstore,
        "get_settings",
        lambda: SimpleNamespace(snapshot_backend="s3", s3_bucket="", s3_prefix=""),
    )
    with pytest.raises(RuntimeError):
        build_snapshot_backend("/unused")


def test_s3_backend_requires_bucket():
    with pytest.raises(ValueError):
        S3SnapshotBackend(FakeS3(), "")


def test_object_store_roundtrip_and_list():
    client = FakeS3()
    store = S3ObjectStore(client, "bucket", "env/dataset")

    store.put_json("2026-11-05/a.json", {"n": 1})
    store.put_bytes("2026-11-05/b.bin", b"raw", "application/octet-stream")

    assert ("bucket", "env/dataset/2026-11-05/a.json") in client.objects
    assert ("bucket", "env/dataset/2026-11-05/b.bin") in client.objects
    assert store.get_json("2026-11-05/a.json") == {"n": 1}
    assert store.get_json("2026-11-05/missing.json") is None
    assert store.list_keys() == [
        "env/dataset/2026-11-05/a.json",
        "env/dataset/2026-11-05/b.bin",
    ]


def test_object_store_swallows_failures():
    client = FakeS3()
    store = S3ObjectStore(client, "bucket", "env")

    client.fail_put = True
    store.put_json("a.json", {"n": 1})
    assert client.objects == {}

    client.fail_get = client.fail_list = True
    assert store.get_json("a.json") is None
    assert store.list_keys() == []


def test_build_object_store_uses_namespace(monkeypatch):
    fake = FakeS3()
    settings = SimpleNamespace(s3_bucket="bucket", s3_prefix="env")
    monkeypatch.setattr(objectstore, "get_settings", lambda: settings)
    monkeypatch.setattr(objectstore, "create_s3_client", lambda: fake)

    store = objectstore.build_s3_object_store("dataset")
    store.put_json("a.json", {"n": 1})

    assert ("bucket", "env/dataset/a.json") in fake.objects


def test_build_object_store_requires_bucket(monkeypatch):
    monkeypatch.setattr(
        objectstore, "get_settings", lambda: SimpleNamespace(s3_bucket="", s3_prefix="")
    )
    with pytest.raises(RuntimeError):
        objectstore.build_s3_object_store("dataset")
