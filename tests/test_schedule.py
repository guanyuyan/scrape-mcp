"""定时监控：任务存储、到期计算、web_schedule 管理动作与 webhook 通知。"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from scrape_mcp import cli, server
from scrape_mcp.config import Settings
from scrape_mcp.core import errors
from scrape_mcp.core.fetcher import FetchOutcome
from scrape_mcp.core.schedule import (
    MAX_INTERVAL_S,
    MIN_INTERVAL_S,
    ScheduleError,
    ScheduleStore,
    task_id,
)

URL = "https://a.com/list"


def _call(**kwargs) -> dict:
    return json.loads(asyncio.run(server.web_schedule(**kwargs)))


def _store() -> ScheduleStore:
    return ScheduleStore(server.get_settings().data_dir)


# ---------------------------------------------------------------- 存储层


def test_task_id_is_stable_per_kind_url_and_selector():
    assert task_id("watch", URL) == task_id("watch", URL, None)
    assert task_id("watch", URL, "ul") != task_id("watch", URL)
    assert task_id("index", URL) != task_id("watch", URL)
    assert task_id("watch", URL).isalnum()


def test_store_create_is_idempotent_and_updates(tmp_path):
    store = ScheduleStore(tmp_path)
    first = store.create(kind="watch", url=URL, interval_s=60)
    again = store.create(kind="watch", url=URL, interval_s=600, webhook_url="https://hook")
    assert first["id"] == again["id"]
    assert again["interval_s"] == 600 and again["webhook_url"] == "https://hook"
    assert len(store.list()) == 1
    # 更新间隔不该顺手把已暂停的任务恢复
    store.set_enabled(first["id"], False)
    store.create(kind="watch", url=URL, interval_s=1800)
    assert store.get(first["id"])["enabled"] is False
    # 同 URL 不同 selector 是两条任务，互不覆盖
    store.create(kind="watch", url=URL, selector="ul")
    assert len(store.list()) == 2


def test_store_validation():
    store = ScheduleStore("/tmp/never-used")
    with pytest.raises(ScheduleError):
        store.create(kind="crawl", url=URL)
    with pytest.raises(ScheduleError):
        store.create(kind="watch", url="ftp://a.com/x")
    with pytest.raises(ScheduleError):
        store.create(kind="watch", url=URL, interval_s=MIN_INTERVAL_S - 1)
    with pytest.raises(ScheduleError):
        store.create(kind="watch", url=URL, interval_s=MAX_INTERVAL_S + 1)
    with pytest.raises(ScheduleError):
        store.create(kind="watch", url=URL, interval_s=True)
    with pytest.raises(ScheduleError):
        store.create(kind="watch", url=URL, webhook_url="file:///tmp/x")


def test_store_pause_resume_delete_and_due(tmp_path):
    store = ScheduleStore(tmp_path)
    task = store.create(kind="watch", url=URL, interval_s=60)
    assert task["next_run_at"] <= time.time()
    assert [t["id"] for t in store.due()] == [task["id"]]

    paused = store.set_enabled(task["id"], False)
    assert paused["enabled"] is False
    assert store.due() == []
    # 暂停的任务仍可查询，状态不丢
    assert store.get(task["id"])["enabled"] is False

    resumed = store.set_enabled(task["id"], True)
    assert resumed["enabled"] is True and resumed["next_run_at"] <= time.time()

    assert store.delete(task["id"]) is True
    assert store.get(task["id"]) is None
    assert store.delete(task["id"]) is False
    with pytest.raises(ScheduleError):
        store.set_enabled("missing", True)


def test_store_mark_run_schedules_next_and_records_state(tmp_path):
    store = ScheduleStore(tmp_path)
    task = store.create(kind="watch", url=URL, interval_s=120)
    before = time.time()
    updated = store.mark_run(task["id"], status="ok", changed=True, summary={"added_lines": 2})
    assert updated is not None
    assert updated["last_status"] == "ok" and updated["last_changed"] is True
    assert updated["run_count"] == 1
    assert updated["last_run_at"] >= before
    assert updated["next_run_at"] == updated["last_run_at"] + 120
    assert store.mark_run("missing", status="ok") is None


def test_store_treats_corrupt_file_as_empty(tmp_path):
    store = ScheduleStore(tmp_path)
    store.create(kind="watch", url=URL)
    path = tmp_path / "schedule" / "tasks.json"
    path.write_text("{", encoding="utf-8")
    assert store.list() == []


def test_store_interval_updates_reschedule_from_last_run(tmp_path):
    store = ScheduleStore(tmp_path)
    task = store.create(kind="watch", url=URL, interval_s=60)
    store.mark_run(task["id"], status="ok")
    last = store.get(task["id"])["last_run_at"]
    updated = store.create(kind="watch", url=URL, interval_s=600)
    assert updated["next_run_at"] == last + 600


# ---------------------------------------------------------------- 服务层


def test_schedule_list_and_invalid_action():
    listed = _call(action="list")
    assert listed["ok"] is True and listed["count"] == 0 and listed["tasks"] == []
    bad = _call(action="launch")
    assert bad["ok"] is False and bad["error_code"] == errors.INVALID_ARGUMENT


def test_schedule_create_requires_url_and_valid_args():
    assert _call(action="create", kind="watch")["ok"] is False
    bad_interval = _call(action="create", kind="watch", url=URL, interval_s=10)
    assert bad_interval["ok"] is False and bad_interval["error_code"] == errors.INVALID_ARGUMENT
    bad_hook = _call(action="create", kind="watch", url=URL, webhook_url="file:///tmp/x")
    assert bad_hook["ok"] is False
    task = _call(action="create", kind="watch", url=URL, selector="li", interval_s=600)
    assert task["ok"] is True and task["task"]["selector"] == "li"


def test_schedule_pause_resume_delete_flow():
    created = _call(action="create", kind="watch", url=URL)["task"]
    tid = created["id"]
    assert _call(action="pause", id=tid)["task"]["enabled"] is False
    assert _call(action="list")["due"] == 0
    assert _call(action="resume", id=tid)["task"]["enabled"] is True
    assert _call(action="get", id=tid)["ok"] is True
    assert _call(action="delete", id=tid)["ok"] is True
    assert _call(action="get", id=tid)["ok"] is False
    assert _call(action="pause")["ok"] is False


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
        elapsed_ms=5,
    )


def _page(items: list[str]) -> str:
    lis = "".join(f"<li>{item}</li>" for item in items)
    return f"<html><body><article><h1>标题</h1><ul>{lis}</ul></article></body></html>"


class _FakeFetcher:
    def __init__(self) -> None:
        self.payloads: list[tuple[str, dict]] = []

    async def post_json(self, url, *, payload, **_kwargs):
        self.payloads.append((url, payload))
        return type("Resp", (), {"ok": True, "status": 200, "error": ""})()


def test_run_watch_task_pushes_webhook_only_on_real_change(monkeypatch):
    pages = [_page(["一"]), _page(["一"]), _page(["一", "二"])]
    calls = {"n": 0}

    async def fake_fetch_html(url, **_kwargs):
        html = pages[min(calls["n"], len(pages) - 1)]
        calls["n"] += 1
        return _outcome(html), None

    fake = _FakeFetcher()
    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(server, "_get_fetcher", lambda: fake)

    tid = _call(action="create", kind="watch", url=URL, selector="li", webhook_url="https://h/x")[
        "task"
    ]["id"]

    first = _call(action="run", id=tid)
    # 首次只建立基线，不通知
    assert first["run"]["first_seen"] is True and first["run"]["changed"] is False
    assert fake.payloads == []

    second = _call(action="run", id=tid)
    assert second["run"]["changed"] is False and fake.payloads == []

    third = _call(action="run", id=tid)
    assert third["run"]["changed"] is True
    assert third["run"]["webhook"]["ok"] is True
    url, payload = fake.payloads[-1]
    assert url == "https://h/x"
    assert payload["event"] == "change" and payload["task_id"] == tid
    assert payload["summary"]["added_lines"] == 1
    assert third["task"]["last_changed"] is True and third["task"]["run_count"] == 3


def test_run_task_records_failure_without_webhook(monkeypatch):
    async def fake_fetch_html(url, **_kwargs):
        return None, {
            "ok": False,
            "url": URL,
            "tier": "l1",
            "error_code": errors.BLOCKED,
            "error": "命中反爬拦截",
            "retryable": False,
        }

    fake = _FakeFetcher()
    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(server, "_get_fetcher", lambda: fake)

    tid = _call(action="create", kind="watch", url=URL, webhook_url="https://h/x")["task"]["id"]
    result = _call(action="run", id=tid)
    assert result["ok"] is True and result["run"]["ok"] is False
    assert result["run"]["status"] == "error"
    assert "拦截" in result["run"]["error"]
    assert result["task"]["last_status"] == "error"
    assert fake.payloads == []


def test_run_index_task_notifies_on_new_urls(monkeypatch):
    async def fake_index(url, **_kwargs):
        return json.dumps(
            {
                "ok": True,
                "url": url,
                "first_seen": False,
                "new": 2,
                "changed": 0,
                "total": 10,
                "new_urls": ["https://a.com/1", "https://a.com/2"],
            }
        )

    fake = _FakeFetcher()
    monkeypatch.setattr(server, "web_index", fake_index)
    monkeypatch.setattr(server, "_get_fetcher", lambda: fake)

    task = _call(action="create", kind="index", url="https://a.com", webhook_url="https://h/x")[
        "task"
    ]
    result = _call(action="run", id=task["id"])
    assert result["run"]["changed"] is True
    assert result["run"]["webhook"]["ok"] is True
    assert fake.payloads[-1][1]["summary"]["new"] == 2

    # 首次建立基线不通知
    baseline = _call(action="create", kind="index", url="https://b.com", webhook_url="https://h/x")
    fake.payloads.clear()

    async def fake_first(url, **_kwargs):
        return json.dumps({"ok": True, "url": url, "first_seen": True, "new": 5})

    monkeypatch.setattr(server, "web_index", fake_first)
    assert _call(action="run", id=baseline["task"]["id"])["run"]["changed"] is False
    assert fake.payloads == []


# ---------------------------------------------------------------- CLI


def _patch_cli_settings(monkeypatch, tmp_path):
    settings = Settings(data_dir=str(tmp_path / "cli-data"), cache_ttl=0)
    monkeypatch.setattr("scrape_mcp.config.get_settings", lambda: settings)


def test_cli_schedule_add_list_pause_delete(monkeypatch, tmp_path, capsys):
    _patch_cli_settings(monkeypatch, tmp_path)
    assert cli.run(["schedule", "list"]) == 0
    assert "[空]" in capsys.readouterr().out

    assert (
        cli.run(
            [
                "schedule",
                "add",
                "--url",
                URL,
                "--selector",
                "ul",
                "--interval",
                "60",
                "--webhook",
                "https://h/x",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "[完成]" in out

    assert cli.run(["schedule", "list"]) == 0
    listing = capsys.readouterr().out
    assert URL in listing and "启用" in listing

    tid = _store_from(tmp_path).list()[0]["id"]
    assert cli.run(["schedule", "pause", tid]) == 0
    assert "暂停" in capsys.readouterr().out
    assert cli.run(["schedule", "resume", tid]) == 0
    assert cli.run(["schedule", "delete", tid]) == 0
    assert cli.run(["schedule", "list"]) == 0
    assert "[空]" in capsys.readouterr().out


def _store_from(tmp_path) -> ScheduleStore:
    return ScheduleStore(tmp_path / "cli-data")


def test_cli_schedule_rejects_bad_interval(monkeypatch, tmp_path, capsys):
    _patch_cli_settings(monkeypatch, tmp_path)
    assert cli.run(["schedule", "add", "--url", URL, "--interval", "30"]) == 2
    assert "[错误]" in capsys.readouterr().err


def test_cli_daemon_once_without_due_tasks(monkeypatch, tmp_path, capsys):
    _patch_cli_settings(monkeypatch, tmp_path)
    assert cli.run(["schedule", "add", "--url", URL, "--paused"]) == 0
    capsys.readouterr()
    assert cli.run(["schedule", "daemon", "--once"]) == 0
    assert "没有到期任务" in capsys.readouterr().out
