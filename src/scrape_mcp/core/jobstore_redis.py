"""Redis 任务后端（可选）：任务状态与队列跨进程共享，供多实例横向扩容。

默认仍是本地 JSON（``DATA_DIR/jobs``）。只有设置 ``SCRAPE_MCP_JOB_BACKEND=redis``
时才启用这里的实现，并需要安装可选依赖 ``mcp-scrape[redis]``，另起 ``scrape-mcp worker``
进程消费队列。任务 ID、状态机与推进逻辑复用 ``core/jobs``，这里只换存储与分发。
"""

from __future__ import annotations

import json
import time
from typing import Any

from .jobs import valid_job_id

# 索引最多保留的任务数：只影响 list 的可见范围，不影响队列与任务本身
MAX_INDEX = 5000


def create_client(url: str) -> Any:
    """按 URL 建同步 Redis 客户端；未安装可选依赖时给出可执行的安装提示。"""
    try:
        import redis
    except ImportError as exc:  # pragma: no cover - 取决于运行环境是否装了 extras
        raise RuntimeError(
            "使用 SCRAPE_MCP_JOB_BACKEND=redis 需要可选依赖，请先 pip install 'mcp-scrape[redis]'"
        ) from exc
    return redis.Redis.from_url(url, decode_responses=True)


class RedisJobStore:
    """任务状态存 Redis 字符串，另用有序集合维护 list 顺序、列表维护待执行队列。

    - ``<prefix>:job:<id>``   JSON 快照（带 TTL）
    - ``<prefix>:jobs:index`` 有序集合，score=updated_at，供 list 按更新时间倒序
    - ``<prefix>:jobs:queue`` 待执行任务 ID 列表，worker 用 BLPOP 消费
    """

    def __init__(self, client: Any, prefix: str = "scrape_mcp", ttl: float = 0) -> None:
        self._client = client
        self._prefix = (prefix or "scrape_mcp").rstrip(":")
        # 过短的 TTL 会让正在轮询的任务凭空消失；小于 60 秒按 60 秒处理
        self._ttl = int(ttl) if ttl and ttl >= 60 else 0

    @property
    def queue_key(self) -> str:
        return f"{self._prefix}:jobs:queue"

    @property
    def index_key(self) -> str:
        return f"{self._prefix}:jobs:index"

    def _key(self, job_id: str) -> str:
        return f"{self._prefix}:job:{job_id}"

    # ---------- 与本地 JobStore 同形的接口 ----------

    def load(self, job_id: str) -> dict[str, Any] | None:
        if not valid_job_id(job_id):
            return None
        try:
            raw = self._client.get(self._key(job_id))
        except Exception:  # Redis 抖动时按“读不到”处理，交给上层状态机
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("job_id") or "")
        if not valid_job_id(job_id):
            raise ValueError(f"非法任务 ID: {job_id}")
        text = json.dumps(job, ensure_ascii=False)
        if self._ttl:
            self._client.setex(self._key(job_id), self._ttl, text)
        else:
            self._client.set(self._key(job_id), text)
        self._client.zadd(self.index_key, {job_id: float(job.get("updated_at") or time.time())})
        if self._ttl:
            self._client.expire(self.index_key, self._ttl)
        # 只保留最近 MAX_INDEX 条，防止索引无限增长
        self._client.zremrangebyrank(self.index_key, 0, -MAX_INDEX - 1)

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            ids = self._client.zrevrange(self.index_key, 0, max(1, int(limit)) - 1)
        except Exception:
            return []
        jobs: list[dict[str, Any]] = []
        for job_id in ids:
            job = self.load(str(job_id))
            if job is not None:
                jobs.append(job)
        return jobs

    # ---------- 队列 ----------

    def enqueue(self, job_id: str) -> None:
        self._client.rpush(self.queue_key, job_id)

    def dequeue(self, timeout: float = 1.0) -> str | None:
        """阻塞等待一个任务；超时返回 None，便于上层检查退出信号。"""
        item = self._client.blpop(self.queue_key, timeout=max(1, int(timeout)))
        return str(item[1]) if item else None

    def recover_stale(self, stale_after: float, limit: int = MAX_INDEX) -> int:
        """worker 启动时把失联的 running 任务重新入队，避免进程崩溃后永久卡住。

        只处理超过 ``stale_after`` 秒没有更新的任务：仍在正常推进的任务
        每抓一页都会刷新 updated_at，不会被误抢。
        """
        now = time.time()
        recovered = 0
        for job in self.list(limit):
            if job.get("state") != "running":
                continue
            updated = float(job.get("updated_at") or 0)
            if now - updated < stale_after:
                continue
            job["state"] = "queued"
            job["updated_at"] = now
            job["error"] = None
            self.save(job)
            self.enqueue(str(job["job_id"]))
            recovered += 1
        return recovered
