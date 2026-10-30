"""异步爬取任务：状态落盘、可恢复、可取消。

状态按任务写一份 JSON（``DATA_DIR/jobs/<id>.json``），原子替换避免读到半个文件。
真正的抓取逻辑由调用方注入（``fetch_page``），这里只负责调度、进度与状态机，
这样任务管理可以脱离网络单独测试，也不会把爬取策略耦进 core/jobs。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

# 状态机：queued/running 为未完成，其余为终态
_TERMINAL = ("done", "failed", "cancelled")

PageFetcher = Callable[[str, dict[str, Any]], Awaitable[tuple[dict, list[str], str | None]]]
StoreFactory = Callable[[], "JobStore"]


def job_id_for(request: dict[str, Any]) -> str:
    """同一组请求参数得到同一个任务 ID，重复 start 天然幂等。"""
    raw = json.dumps(request, sort_keys=True, ensure_ascii=False, default=str)
    return "crawl-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class JobStore:
    """每个任务一份 JSON，写入用同目录临时文件 + os.replace（Windows 下带重试）。"""

    def __init__(self, root: str | Path) -> None:
        self._path = Path(os.path.expanduser(str(root))).resolve()

    def _file(self, job_id: str) -> Path:
        if not _JOB_ID_RE.match(job_id or ""):
            raise ValueError(f"非法任务 ID: {job_id}")
        return self._path / f"{job_id}.json"

    def load(self, job_id: str) -> dict[str, Any] | None:
        try:
            with self._file(job_id).open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("job_id") or "")
        path = self._file(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        # Windows 上目标文件可能正被 status 读取，短暂占用时重试几次
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 2:
                    with contextlib.suppress(OSError):
                        tmp.unlink()
                    raise
                time.sleep(0.05)

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            files = sorted(self._path.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        jobs: list[dict[str, Any]] = []
        for fp in files[: max(1, int(limit))]:
            try:
                with fp.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                jobs.append(data)
        return jobs


class CrawlJobManager:
    """在后台协程里推进任务；进程重启后由 status 重新挂起未完成的任务。"""

    def __init__(self, store_factory: StoreFactory, fetch_page: PageFetcher) -> None:
        self._store_factory = store_factory
        self._fetch_page = fetch_page
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancelled: set[str] = set()

    def _store(self) -> JobStore:
        return self._store_factory()

    # ---------- 对外操作 ----------

    def start(self, request: dict[str, Any], *, cap: int) -> tuple[dict[str, Any], bool]:
        """创建任务并立即排队；同参数的任务已存在时原样返回（existing=True）。"""
        job_id = job_id_for(request)
        store = self._store()
        existing = store.load(job_id)
        if existing is not None:
            return existing, True

        now = time.time()
        job: dict[str, Any] = {
            "job_id": job_id,
            "kind": "crawl",
            "state": "queued",
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "request": request,
            "progress": {"pages_fetched": 0, "ok_count": 0, "limit": cap},
            "queue": [[request["url"], 0]],
            "visited": [request["url"]],
            "results": [],
            "error": None,
        }
        deadline_ms = request.get("deadline_ms")
        if isinstance(deadline_ms, int) and deadline_ms > 0:
            job["deadline_epoch"] = now + deadline_ms / 1000
        store.save(job)
        self._ensure_task(job_id, job)
        return job, False

    def status(self, job_id: str) -> dict[str, Any] | None:
        """读取状态；未完成但本进程没有协程时自动续跑（进程重启恢复）。"""
        job = self._store().load(job_id)
        if job is None:
            return None
        if job.get("state") in ("queued", "running"):
            self._ensure_task(job_id, job)
        return job

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        """取消未完成任务；终态任务原样返回，不做二次修改。"""
        store = self._store()
        job = store.load(job_id)
        if job is None:
            return None
        if job.get("state") in _TERMINAL:
            return job
        job["state"] = "cancelled"
        job["finished_at"] = time.time()
        job["updated_at"] = job["finished_at"]
        store.save(job)
        self._cancelled.add(job_id)
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        return job

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._store().list(limit)

    async def wait(self, job_id: str, timeout: float = 10.0) -> dict[str, Any] | None:
        """等任务跑完（测试与脚本用）；超时或任务被取消时返回当前状态。"""
        task = self._tasks.get(job_id)
        if task is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout)
        return self._store().load(job_id)

    async def aclose(self) -> None:
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # ---------- 后台推进 ----------

    def _ensure_task(self, job_id: str, job: dict[str, Any]) -> None:
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            return
        self._tasks[job_id] = asyncio.create_task(self._run(job_id))

    async def _run(self, job_id: str) -> None:
        store = self._store()
        job = store.load(job_id)
        if job is None:
            return
        request = job.get("request") or {}
        paginate = bool(request.get("next_selector"))
        depth = int(request.get("depth") or 0)
        cap = int((job.get("progress") or {}).get("limit") or 0)
        if cap <= 0:
            return

        job["state"] = "running"
        job["started_at"] = job.get("started_at") or time.time()
        job["updated_at"] = time.time()
        store.save(job)

        visited: set[str] = set(job.get("visited") or [])
        try:
            while job.get("queue") and len(job.get("results") or []) < cap:
                if job_id in self._cancelled or self._deadline_hit(job):
                    break
                item = job["queue"].pop(0)
                url = str(item[0])
                level = int(item[1]) if len(item) > 1 else 0
                payload, links, nxt = await self._fetch_page(url, request)

                results = job.setdefault("results", [])
                results.append(payload)
                job["progress"] = {
                    "pages_fetched": len(results),
                    "ok_count": sum(1 for r in results if r.get("ok")),
                    "limit": cap,
                }
                job["updated_at"] = time.time()

                if payload.get("ok"):
                    if paginate:
                        if nxt and nxt not in visited and len(results) < cap:
                            visited.add(nxt)
                            job["queue"] = [[nxt, level]]
                    elif level < depth:
                        for link in links:
                            if len(visited) >= cap:
                                break
                            if link in visited:
                                continue
                            visited.add(link)
                            job["queue"].append([link, level + 1])
                job["visited"] = list(visited)
                store.save(job)
        except asyncio.CancelledError:
            # cancel() 已经写好状态，这里只结束协程
            return
        except Exception as exc:  # 任务级意外异常：落盘失败原因，别让状态永远停在 running
            job["state"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["finished_at"] = time.time()
            job["updated_at"] = job["finished_at"]
            store.save(job)
            return

        # 最后再读一次，避免把外部（另一个请求）刚写入的 cancelled 覆盖成 done
        fresh = store.load(job_id) or job
        if fresh.get("state") in _TERMINAL:
            return
        fresh["state"] = "done"
        fresh["finished_at"] = time.time()
        fresh["updated_at"] = fresh["finished_at"]
        if self._deadline_hit(fresh):
            fresh["deadline_exceeded"] = True
        store.save(fresh)

    @staticmethod
    def _deadline_hit(job: dict[str, Any]) -> bool:
        epoch = job.get("deadline_epoch")
        return isinstance(epoch, (int, float)) and time.time() >= epoch
