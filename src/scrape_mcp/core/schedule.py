"""定时监控任务：把 web_watch / web_index 交给本地 daemon 按间隔执行。

任务只存调度元数据（类型、URL、选择器、间隔、webhook），页面快照仍由
watch / index 各自的存储负责，互不重复。状态落在 ``DATA_DIR/schedule/tasks.json``，
写入走临时文件 + 原子替换：daemon 与 MCP 进程同时写时不会读到半截 JSON。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

# 间隔下限：监控是长期行为，过密轮询既伤目标站也容易触发风控
MIN_INTERVAL_S = 60
# 间隔上限：一周一次，避免误填超大数值后任务看起来"永不到期"
MAX_INTERVAL_S = 7 * 24 * 3600
# 任务类型：watch 对比正文快照，index 对比 sitemap 清单
KINDS = ("watch", "index")
# 对外展示的任务状态
STATUS_OK = "ok"
STATUS_ERROR = "error"


class ScheduleError(ValueError):
    """任务参数不合法（类型、URL、间隔或 webhook）。"""


def task_id(kind: str, url: str, selector: str | None = None) -> str:
    """同类型 + 同 URL + 同选择器固定一个 ID：重复创建即更新，不会留下两条任务。"""
    raw = f"{kind}\n{url}\n{selector or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def valid_webhook(url: str | None) -> bool:
    """webhook 只接受 http/https，避免把通知发到本机文件或其它协议。"""
    if not url:
        return True
    return url.startswith(("http://", "https://"))


def validate_interval(interval_s: Any) -> int:
    """校验执行间隔并返回整数秒；布尔值不算整数。"""
    if isinstance(interval_s, bool) or not isinstance(interval_s, int):
        raise ScheduleError("interval_s 必须是整数秒")
    if interval_s < MIN_INTERVAL_S:
        raise ScheduleError(f"interval_s 不能小于 {MIN_INTERVAL_S} 秒")
    if interval_s > MAX_INTERVAL_S:
        raise ScheduleError(f"interval_s 不能大于 {MAX_INTERVAL_S} 秒（一周）")
    return interval_s


class ScheduleStore:
    """任务集合落在 ``<root>/schedule/tasks.json``；不存在时按空集合处理。"""

    def __init__(self, root: str | Path) -> None:
        base = Path(os.path.expanduser(str(root))).resolve() / "schedule"
        self._path = base / "tasks.json"

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        tasks = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(tasks, dict):
            return {}
        return {key: value for key, value in tasks.items() if isinstance(value, dict)}

    def _save(self, tasks: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump({"tasks": tasks}, f, ensure_ascii=False, indent=2)
        # Windows 上杀毒/索引服务可能短暂持有目标文件，导致 replace 报拒绝访问；短暂重试即可
        for attempt in range(3):
            try:
                os.replace(tmp, self._path)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.05)

    def list(self) -> list[dict[str, Any]]:
        """按创建时间排序，便于 CLI 稳定输出。"""
        tasks = self._load()
        return sorted(tasks.values(), key=lambda task: task.get("created_at") or 0)

    def get(self, task_id_value: str) -> dict[str, Any] | None:
        return self._load().get(task_id_value)

    def create(
        self,
        *,
        kind: str,
        url: str,
        selector: str | None = None,
        interval_s: int = 3600,
        webhook_url: str | None = None,
        session: str = "default",
        enabled: bool | None = None,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建或更新任务；同 kind+url+selector 只有一条，重复创建视为更新。"""
        if kind not in KINDS:
            raise ScheduleError(f"kind 需为 {'/'.join(KINDS)}")
        if not url.startswith(("http://", "https://")):
            raise ScheduleError("url 必须以 http:// 或 https:// 开头")
        interval_s = validate_interval(interval_s)
        if not valid_webhook(webhook_url):
            raise ScheduleError("webhook_url 必须以 http:// 或 https:// 开头")

        now = time.time()
        key = task_id(kind, url, selector)
        tasks = self._load()
        task = tasks.get(key)
        if task is None:
            task = {
                "id": key,
                "kind": kind,
                "url": url,
                "selector": selector,
                "session": session,
                "interval_s": interval_s,
                "webhook_url": webhook_url,
                "schema": schema,
                "enabled": True if enabled is None else bool(enabled),
                "created_at": now,
                "updated_at": now,
                "last_run_at": None,
                "next_run_at": now,
                "last_status": None,
                "last_error": "",
                "last_changed": False,
                "run_count": 0,
                "last_summary": None,
            }
        else:
            task.update(
                {
                    "url": url,
                    "selector": selector,
                    "session": session,
                    "interval_s": interval_s,
                    "webhook_url": webhook_url,
                    "schema": schema,
                    "updated_at": now,
                }
            )
            # 更新时不传 enabled 就保持原状态：改间隔不应该顺手把暂停的任务恢复
            if enabled is not None:
                task["enabled"] = bool(enabled)
            # 间隔变了就按上次执行时间重排；没跑过就尽快建立基线
            last = task.get("last_run_at")
            task["next_run_at"] = (last + interval_s) if isinstance(last, (int, float)) else now
        tasks[key] = task
        self._save(tasks)
        return dict(task)

    def set_enabled(self, task_id_value: str, enabled: bool) -> dict[str, Any]:
        """暂停 / 恢复任务；恢复时排到当前，daemon 下一轮就会执行。"""
        tasks = self._load()
        task = tasks.get(task_id_value)
        if task is None:
            raise ScheduleError(f"任务不存在: {task_id_value}")
        task["enabled"] = bool(enabled)
        task["updated_at"] = time.time()
        if enabled:
            task["next_run_at"] = time.time()
        self._save(tasks)
        return dict(task)

    def delete(self, task_id_value: str) -> bool:
        tasks = self._load()
        if task_id_value not in tasks:
            return False
        del tasks[task_id_value]
        self._save(tasks)
        return True

    def due(self, now: float | None = None) -> list[dict[str, Any]]:
        """到期且启用的任务；暂停的任务保留状态但不会被 daemon 执行。"""
        moment = time.time() if now is None else now
        return [
            task
            for task in self.list()
            if task.get("enabled") and float(task.get("next_run_at") or 0) <= moment
        ]

    def mark_run(
        self,
        task_id_value: str,
        *,
        status: str,
        error: str = "",
        changed: bool = False,
        summary: dict[str, Any] | None = None,
        next_run_at: float | None = None,
    ) -> dict[str, Any] | None:
        """记录一次执行结果；下次执行时间从"现在"起算，避免停机后补跑一整串。"""
        tasks = self._load()
        task = tasks.get(task_id_value)
        if task is None:
            return None
        now = time.time()
        task["last_run_at"] = now
        task["last_status"] = status
        task["last_error"] = error
        task["last_changed"] = bool(changed)
        task["last_summary"] = summary
        task["run_count"] = int(task.get("run_count") or 0) + 1
        interval = int(task.get("interval_s") or MIN_INTERVAL_S)
        task["next_run_at"] = now + interval if next_run_at is None else next_run_at
        self._save(tasks)
        return dict(task)
