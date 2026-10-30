"""异步爬取任务单测：后台推进、幂等、取消、重启恢复，全部离线。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from scrape_mcp import server
from scrape_mcp.core.fetcher import FetchOutcome
from scrape_mcp.core.jobs import JobStore

BODY = "正文内容" * 200
PAGES = {
    "https://site.test/": (
        f"<html><head><title>首页</title></head><body><article><p>{BODY}</p></article>"
        '<a href="/a">A</a><a href="/b">B</a></body></html>'
    ),
    "https://site.test/a": f"<html><body><article><p>{BODY}</p></article></body></html>",
    "https://site.test/b": f"<html><body><article><p>{BODY}</p></article></body></html>",
}


def _outcome(url: str, html: str) -> FetchOutcome:
    return FetchOutcome(
        url=url,
        final_url=url,
        status=200,
        tier="l1",
        blocked="none",
        block_reason="",
        content_type="text/html",
        html=html,
        elapsed_ms=5,
    )


def _install(monkeypatch):
    async def fake_fetch(url, **_kwargs):
        html = PAGES.get(url)
        if html is None:
            return None, {"ok": False, "url": url, "error_code": "http_4xx", "error": "404"}
        return _outcome(url, html), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch)


def test_start_runs_to_done_with_partial_results(monkeypatch):
    _install(monkeypatch)

    async def scenario():
        started = json.loads(
            await server.web_crawl_start(url="https://site.test/", depth=1, limit=3)
        )
        await server._get_jobs().wait(started["job_id"], timeout=5)
        return started, json.loads(await server.web_crawl_status(started["job_id"]))

    started, status = asyncio.run(scenario())
    assert started["ok"] is True and started["existing"] is False
    assert status["state"] == "done"
    assert status["progress"]["pages_fetched"] == 3
    assert status["progress"]["ok_count"] == 3
    assert status["result_count"] == 3
    assert [item["url"] for item in status["results"]] == [
        "https://site.test/",
        "https://site.test/a",
        "https://site.test/b",
    ]


def test_duplicate_start_is_idempotent(monkeypatch):
    _install(monkeypatch)

    async def scenario():
        first = json.loads(await server.web_crawl_start(url="https://site.test/", depth=0))
        second = json.loads(await server.web_crawl_start(url="https://site.test/", depth=0))
        await server._get_jobs().wait(first["job_id"], timeout=5)
        return first, second

    first, second = asyncio.run(scenario())
    assert first["job_id"] == second["job_id"]
    assert second["existing"] is True


def test_status_include_results_can_be_disabled(monkeypatch):
    _install(monkeypatch)

    async def scenario():
        started = json.loads(await server.web_crawl_start(url="https://site.test/", depth=0))
        await server._get_jobs().wait(started["job_id"], timeout=5)
        return json.loads(await server.web_crawl_status(started["job_id"], include_results=False))

    status = asyncio.run(scenario())
    assert status["results"] == []
    assert status["result_count"] == 1


def test_cancel_stops_job(monkeypatch):
    _install(monkeypatch)

    async def scenario():
        started = json.loads(
            await server.web_crawl_start(url="https://site.test/", depth=1, limit=3)
        )
        cancelled = json.loads(await server.web_crawl_cancel(started["job_id"]))
        await server._get_jobs().wait(started["job_id"], timeout=5)
        return cancelled, json.loads(await server.web_crawl_status(started["job_id"]))

    cancelled, status = asyncio.run(scenario())
    assert cancelled["ok"] is True and cancelled["state"] == "cancelled"
    assert status["state"] == "cancelled"


def test_status_resumes_job_after_restart(monkeypatch):
    _install(monkeypatch)
    store = JobStore(Path(server.get_settings().data_dir) / "jobs")
    now = time.time()
    job_id = "crawl-recover01"
    store.save(
        {
            "job_id": job_id,
            "kind": "crawl",
            "state": "running",
            "created_at": now,
            "updated_at": now,
            "started_at": now,
            "finished_at": None,
            "request": {
                "url": "https://site.test/a",
                "depth": 0,
                "limit": 1,
                "same_host": True,
                "max_tokens": 2000,
                "link_policy": "internal",
                "next_selector": None,
                "max_pages": None,
                "session": "default",
                "proxy": None,
                "deadline_ms": None,
            },
            "progress": {"pages_fetched": 0, "ok_count": 0, "limit": 1},
            "queue": [["https://site.test/a", 0]],
            "visited": ["https://site.test/a"],
            "results": [],
            "error": None,
        }
    )

    async def scenario():
        assert json.loads(await server.web_crawl_status(job_id))["state"] in ("running", "queued")
        await server._get_jobs().wait(job_id, timeout=5)
        return json.loads(await server.web_crawl_status(job_id))

    status = asyncio.run(scenario())
    assert status["state"] == "done"
    assert status["result_count"] == 1


def test_list_reports_latest_jobs(monkeypatch):
    _install(monkeypatch)

    async def scenario():
        started = json.loads(await server.web_crawl_start(url="https://site.test/", depth=0))
        await server._get_jobs().wait(started["job_id"], timeout=5)
        return started, json.loads(await server.web_crawl_list())

    started, out = asyncio.run(scenario())
    assert out["ok"] is True
    assert out["total"] == 1
    assert out["jobs"][0]["job_id"] == started["job_id"]
    assert out["jobs"][0]["state"] == "done"


def test_unknown_job_id_returns_error():
    out = json.loads(asyncio.run(server.web_crawl_status("crawl-nope")))
    assert out["ok"] is False and out["error_code"] == "invalid_argument"
