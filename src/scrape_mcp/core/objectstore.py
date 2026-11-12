"""快照对象存储：默认本地磁盘，可选 S3 兼容后端（MinIO / R2 / OSS / AWS S3）。

快照是旁路能力：读写失败都不该影响抓取本身，所以两个后端都把异常消化在内部，
对外只表达"有/没有"。只有显式设置 ``SNAPSHOT_BACKEND=s3`` 时才需要
``mcp-scrape[s3]`` 可选依赖。
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..config import get_settings

# 文件名用固定 13 位毫秒时间戳：按字符串排序即按时间排序，跨平台也稳定
_TS_WIDTH = 13


def _ts_name(ts: float) -> str:
    return f"{int(ts * 1000):0{_TS_WIDTH}d}"


def _ts_from_name(name: str) -> float | None:
    stem = name.rsplit(".", 1)[0]
    try:
        return int(stem) / 1000
    except ValueError:
        return None


def _limits() -> tuple[int, float]:
    """版本数与保留天数双限；配置异常时退回文档承诺的默认值。"""
    settings = get_settings()
    versions = max(1, int(getattr(settings, "watch_max_versions", 20) or 20))
    days = float(getattr(settings, "watch_retention_days", 90) or 0)
    return versions, days


def _keep_flags(ordered: list[float]) -> list[bool]:
    """ordered 为"从新到旧"的时间戳；返回每一条是否保留（写入前先淘汰）。"""
    versions, days = _limits()
    now = time.time()
    flags: list[bool] = []
    for index, ts in enumerate(ordered):
        fresh = days <= 0 or ts >= now - days * 86400
        flags.append(index < versions and fresh)
    return flags


def _records(items: list[tuple[float, dict[str, Any]]], limit: int | None) -> list[dict[str, Any]]:
    """按时间倒序编号：version=1 表示最新，序号供 web_fetch / web_changes 引用。"""
    items.sort(key=lambda pair: pair[0], reverse=True)
    if limit is not None:
        items = items[: max(1, limit)]
    records: list[dict[str, Any]] = []
    for index, (ts, data) in enumerate(items, start=1):
        record = dict(data)
        record["version"] = index
        record["ts"] = ts
        record["captured_at"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        record.setdefault("saved_at", ts)
        records.append(record)
    return records


@runtime_checkable
class SnapshotBackend(Protocol):
    """快照后端的最小接口：按稳定 key 读写 JSON，并可按版本回读。"""

    def load(self, key: str) -> dict[str, Any] | None: ...

    def save(self, key: str, data: dict[str, Any]) -> None: ...

    def versions(self, key: str, limit: int | None = None) -> list[dict[str, Any]]: ...


class LocalSnapshotBackend:
    """落在 ``<root>/snapshots/<key>/<ts>.json``；旧版单文件 ``<root>/watch/<key>.json`` 仍可读。"""

    def __init__(self, root: str | Path) -> None:
        base = Path(os.path.expanduser(str(root))).resolve()
        self._path = base / "watch"
        self._snapshots = base / "snapshots"

    def _dir(self, key: str) -> Path:
        return self._snapshots / key

    def _files(self, key: str) -> list[Path]:
        try:
            return sorted(self._dir(key).glob("*.json"), reverse=True)
        except OSError:
            return []

    def _legacy(self, key: str) -> dict[str, Any] | None:
        try:
            with (self._path / f"{key}.json").open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def versions(self, key: str, limit: int | None = None) -> list[dict[str, Any]]:
        items: list[tuple[float, dict[str, Any]]] = []
        for path in self._files(key)[: max(1, limit) if limit is not None else None]:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            ts = data.get("ts")
            if not isinstance(ts, (int, float)):
                ts = _ts_from_name(path.name) or 0.0
            items.append((float(ts), data))
        if not items:
            legacy = self._legacy(key)
            if legacy is None:
                return []
            ts = float(legacy.get("ts") or legacy.get("saved_at") or 0) or (
                _ts_from_name(f"{key}.json") or 0.0
            )
            return _records([(ts, legacy)], limit)
        return _records(items, limit)

    def load(self, key: str) -> dict[str, Any] | None:
        records = self.versions(key, limit=1)
        return records[0] if records else None

    def save(self, key: str, data: dict[str, Any]) -> None:
        directory = self._dir(key)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            ts = time.time()
            while (directory / f"{_ts_name(ts)}.json").exists():
                # 同一毫秒内重复写入：顺延 1ms，保证不覆盖上一版
                ts += 0.001
            name = f"{_ts_name(ts)}.json"
            payload = {"ts": ts, **data}
            tmp = directory / f".{name}.tmp"
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp, directory / name)
        except OSError:
            return
        self._prune(key)

    def _prune(self, key: str) -> None:
        files = self._files(key)
        if not files:
            return
        stamps = [(_ts_from_name(path.name) or 0.0) for path in files]
        for path, keep in zip(files, _keep_flags(stamps), strict=False):
            if keep:
                continue
            with contextlib.suppress(OSError):
                path.unlink()


class S3SnapshotBackend:
    """S3 兼容对象存储：版本对象键为 ``<prefix>/snapshots/<key>/<ts>.json``。"""

    def __init__(self, client: Any, bucket: str, prefix: str = "") -> None:
        if not bucket:
            raise ValueError("SNAPSHOT_BACKEND=s3 时必须设置 SCRAPE_MCP_S3_BUCKET")
        self._client = client
        self._bucket = bucket
        self._prefix = (prefix or "").strip().strip("/")

    def _key(self, key: str) -> str:
        parts = [part for part in (self._prefix, "snapshots", key) if part]
        return "/".join(parts)

    def _legacy_key(self, key: str) -> str:
        parts = [part for part in (self._prefix, "watch", f"{key}.json") if part]
        return "/".join(parts)

    def _list(self, key: str) -> list[tuple[float, str]]:
        prefix = f"{self._key(key)}/"
        found: list[tuple[float, str]] = []
        token: str | None = None
        try:
            while True:
                kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
                if token:
                    kwargs["ContinuationToken"] = token
                response = self._client.list_objects_v2(**kwargs)
                for item in response.get("Contents") or []:
                    name = str(item.get("Key") or "").rsplit("/", 1)[-1]
                    ts = _ts_from_name(name)
                    if ts is not None:
                        found.append((ts, str(item["Key"])))
                token = response.get("NextContinuationToken")
                if not token:
                    break
        except Exception:
            return []
        return found

    def versions(self, key: str, limit: int | None = None) -> list[dict[str, Any]]:
        entries = sorted(self._list(key), key=lambda pair: pair[0], reverse=True)
        if limit is not None:
            entries = entries[: max(1, limit)]
        items: list[tuple[float, dict[str, Any]]] = []
        for ts, object_key in entries:
            try:
                response = self._client.get_object(Bucket=self._bucket, Key=object_key)
                data = json.loads(response["Body"].read().decode("utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                items.append((ts, data))
        if not items:
            legacy = self._load_key(self._legacy_key(key))
            return _records([(float(legacy.get("saved_at") or 0), legacy)], limit) if legacy else []
        return _records(items, limit)

    def _load_key(self, object_key: str) -> dict[str, Any] | None:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=object_key)
            data = json.loads(response["Body"].read().decode("utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def load(self, key: str) -> dict[str, Any] | None:
        records = self.versions(key, limit=1)
        return records[0] if records else None

    def save(self, key: str, data: dict[str, Any]) -> None:
        ts = time.time()
        payload = {"ts": ts, **data}
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._client.put_object(
                Bucket=self._bucket,
                Key=f"{self._key(key)}/{_ts_name(ts)}.json",
                Body=body,
                ContentType="application/json; charset=utf-8",
            )
        except Exception:
            # 快照是旁路，上传失败不阻断本次抓取
            return
        self._prune(key)

    def _prune(self, key: str) -> None:
        entries = sorted(self._list(key), key=lambda pair: pair[0], reverse=True)
        if not entries:
            return
        for (_ts, object_key), keep in zip(
            entries, _keep_flags([ts for ts, _ in entries]), strict=False
        ):
            if keep:
                continue
            with contextlib.suppress(Exception):
                self._client.delete_object(Bucket=self._bucket, Key=object_key)


class S3ObjectStore:
    """通用 S3 对象读写（数据集记录 / 任务产物），键前缀 ``<S3_PREFIX>/<namespace>``。

    与快照后端一样把异常消化在内部：导出与归档都是旁路，失败不应影响抓取。
    """

    def __init__(self, client: Any, bucket: str, prefix: str = "") -> None:
        if not bucket:
            raise ValueError("使用 S3 对象存储时必须设置 SCRAPE_MCP_S3_BUCKET")
        self._client = client
        self._bucket = bucket
        self._prefix = (prefix or "").strip().strip("/")

    def _key(self, key: str) -> str:
        return "/".join(part for part in (self._prefix, key.lstrip("/")) if part)

    def put_json(self, key: str, data: dict[str, Any]) -> None:
        try:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._client.put_object(
                Bucket=self._bucket,
                Key=self._key(key),
                Body=body,
                ContentType="application/json; charset=utf-8",
            )
        except Exception:
            pass

    def put_bytes(self, key: str, body: bytes, content_type: str = "application/json") -> None:
        with contextlib.suppress(Exception):
            self._client.put_object(
                Bucket=self._bucket,
                Key=self._key(key),
                Body=body,
                ContentType=content_type,
            )

    def get_json(self, key: str) -> dict[str, Any] | None:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._key(key))
            data = json.loads(response["Body"].read().decode("utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def list_keys(self, prefix: str = "") -> list[str]:
        """列出命名空间下（可再加子前缀）的所有对象键，自动翻页。"""
        wanted = "/".join(part for part in (self._prefix, prefix.strip("/")) if part)
        keys: list[str] = []
        token: str | None = None
        try:
            while True:
                kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": wanted}
                if token:
                    kwargs["ContinuationToken"] = token
                response = self._client.list_objects_v2(**kwargs)
                keys.extend(item["Key"] for item in response.get("Contents") or [])
                token = response.get("NextContinuationToken")
                if not token:
                    break
        except Exception:
            return []
        return keys


def create_s3_client() -> Any:
    """按配置建 boto3 客户端；凭据留空时走 boto3 默认链（环境变量 / IAM 角色）。"""
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - 取决于是否安装可选依赖
        raise RuntimeError(
            "使用 SCRAPE_MCP_SNAPSHOT_BACKEND=s3 需要可选依赖，请先 pip install 'mcp-scrape[s3]'"
        ) from exc
    settings = get_settings()
    kwargs: dict[str, Any] = {}
    endpoint = getattr(settings, "s3_endpoint_url", "")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    region = getattr(settings, "s3_region", "")
    if region:
        kwargs["region_name"] = region
    access = getattr(settings, "s3_access_key_id", "")
    secret = getattr(settings, "s3_secret_access_key", "")
    if access and secret:
        kwargs["aws_access_key_id"] = access
        kwargs["aws_secret_access_key"] = secret
    config = None
    if getattr(settings, "s3_path_style", False):
        config = Config(s3={"addressing_style": "path"})
    return boto3.client("s3", config=config, **kwargs)


def build_s3_object_store(namespace: str) -> S3ObjectStore:
    """按 ``S3_*`` 配置建通用对象存储；namespace 决定键前缀（dataset / jobs）。"""
    settings = get_settings()
    bucket = getattr(settings, "s3_bucket", "")
    if not bucket:
        raise RuntimeError("使用 S3 对象存储时必须设置 SCRAPE_MCP_S3_BUCKET")
    base = getattr(settings, "s3_prefix", "").strip().strip("/")
    prefix = "/".join(part for part in (base, namespace.strip().strip("/")) if part)
    return S3ObjectStore(create_s3_client(), bucket, prefix)


def build_snapshot_backend(root: str | Path) -> SnapshotBackend:
    """按 ``SNAPSHOT_BACKEND`` 选择后端；local 是默认，s3 需要 bucket。"""
    settings = get_settings()
    backend = getattr(settings, "snapshot_backend", "local")
    if backend == "local":
        return LocalSnapshotBackend(root)
    if backend == "s3":
        bucket = getattr(settings, "s3_bucket", "")
        if not bucket:
            raise RuntimeError("SNAPSHOT_BACKEND=s3 时必须设置 SCRAPE_MCP_S3_BUCKET")
        return S3SnapshotBackend(create_s3_client(), bucket, getattr(settings, "s3_prefix", ""))
    raise RuntimeError(f"SNAPSHOT_BACKEND 只支持 local / s3，当前为 {backend!r}")
