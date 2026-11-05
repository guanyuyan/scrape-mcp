"""快照对象存储：默认本地磁盘，可选 S3 兼容后端（MinIO / R2 / OSS / AWS S3）。

快照是旁路能力：读写失败都不该影响抓取本身，所以两个后端都把异常消化在内部，
对外只表达"有/没有"。只有显式设置 ``SNAPSHOT_BACKEND=s3`` 时才需要
``mcp-scrape[s3]`` 可选依赖。
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..config import get_settings


@runtime_checkable
class SnapshotBackend(Protocol):
    """快照后端的最小接口：按稳定 key 读写一份 JSON 对象。"""

    def load(self, key: str) -> dict[str, Any] | None: ...

    def save(self, key: str, data: dict[str, Any]) -> None: ...


class LocalSnapshotBackend:
    """落在 ``<root>/watch`` 的本地实现，与旧行为逐字节一致。"""

    def __init__(self, root: str | Path) -> None:
        self._path = Path(os.path.expanduser(str(root))).resolve() / "watch"

    def load(self, key: str) -> dict[str, Any] | None:
        try:
            with (self._path / f"{key}.json").open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, key: str, data: dict[str, Any]) -> None:
        try:
            path = self._path / f"{key}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
        except (OSError, TypeError):
            pass


class S3SnapshotBackend:
    """S3 兼容对象存储：对象键为 ``<prefix>/watch/<key>.json``。"""

    def __init__(self, client: Any, bucket: str, prefix: str = "") -> None:
        if not bucket:
            raise ValueError("SNAPSHOT_BACKEND=s3 时必须设置 SCRAPE_MCP_S3_BUCKET")
        self._client = client
        self._bucket = bucket
        self._prefix = (prefix or "").strip().strip("/")

    def _key(self, key: str) -> str:
        parts = [part for part in (self._prefix, "watch", f"{key}.json") if part]
        return "/".join(parts)

    def load(self, key: str) -> dict[str, Any] | None:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._key(key))
            body = response["Body"].read()
            data = json.loads(body.decode("utf-8"))
        except Exception:
            # 对象不存在、权限不足、网络抖动都按"没有快照"处理
            return None
        return data if isinstance(data, dict) else None

    def save(self, key: str, data: dict[str, Any]) -> None:
        try:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._client.put_object(
                Bucket=self._bucket,
                Key=self._key(key),
                Body=body,
                ContentType="application/json; charset=utf-8",
            )
        except Exception:
            # 导出/快照是旁路，上传失败不阻断本次抓取
            pass


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
