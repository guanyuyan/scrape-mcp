"""字段级 diff：路径摊平、数值 delta/pct、web_changes 与 webhook 摘要。"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp import server
from scrape_mcp.core import errors
from scrape_mcp.core.fetcher import FetchOutcome
from scrape_mcp.core.watch import change_summary, field_changes

URL = "https://shop.example.com/item/1"

SCHEMA = {
    "title": {"selector": "h1", "type": "text"},
    "price": {"selector": ".price", "type": "text", "convert": "int"},
    "tags": {"selector": ".tag", "type": "list"},
}

HTML_V1 = """<html><body><article><h1>示例商品</h1>
<div class="price">299</div>
<ul><li class="tag">甲</li><li class="tag">乙</li></ul>
</article></body></html>"""

HTML_V2 = """<html><body><article><h1>示例商品</h1>
<div class="price">249</div>
<ul><li class="tag">甲</li><li class="tag">乙</li><li class="tag">丙</li></ul>
</article></body></html>"""


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
        elapsed_ms=4,
    )


def _call(tool, *args, **kwargs) -> dict:
    return json.loads(asyncio.run(tool(*args, **kwargs)))


def test_field_changes_flattens_nested_paths():
    previous = {"title": "旧", "price": 299, "items": [{"name": "甲", "cost": 10}]}
    current = {
        "title": "新",
        "price": 249,
        "items": [{"name": "甲", "cost": 12}, {"name": "乙", "cost": 20}],
    }

    changes = field_changes(previous, current)

    assert changes["added"] == ["items[1].name", "items[1].cost"]
    assert changes["removed"] == []
    paths = {item["path"]: item for item in changes["modified"]}
    assert paths["title"]["from"] == "旧" and paths["title"]["to"] == "新"
    assert "delta" not in paths["title"]  # 非数值不给 delta
    assert paths["price"]["delta"] == -50
    assert paths["price"]["pct"] == -16.7
    assert paths["items[0].cost"]["delta"] == 2


def test_field_changes_handles_missing_sides_and_bools():
    assert field_changes(None, {"a": 1})["added"] == ["a"]
    assert field_changes({"a": 1}, None)["removed"] == ["a"]
    changes = field_changes({"flag": True}, {"flag": False})
    assert changes["modified"][0]["path"] == "flag"
    assert "delta" not in changes["modified"][0]


def test_field_changes_respects_limit():
    previous = {f"k{index}": 1 for index in range(5)}
    current = {f"k{index}": 2 for index in range(5)}
    changes = field_changes(previous, current, limit=2)
    assert len(changes["modified"]) == 2


def test_change_summary_is_human_readable():
    changes = field_changes({"price": 299}, {"price": 249})
    assert change_summary(changes) == ["price 299→249（-16.7%）"]


def test_watch_with_schema_stores_fields_and_reports_changes(monkeypatch):
    pages = [HTML_V1, HTML_V2]

    async def fake_fetch_html(url, **_kwargs):
        return _outcome(pages.pop(0)), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)

    first = _call(server.web_watch, URL, schema=SCHEMA)
    assert first["ok"] is True and "changes" not in first

    second = _call(server.web_watch, URL, schema=SCHEMA)
    assert second["changes"]["modified"][0]["path"] == "price"
    assert second["changes"]["added"] == ["tags[2]"]
    assert second["summary_text"] == ["price 299→249（-16.7%）", "新增 tags[2]"]


def test_web_changes_field_mode(monkeypatch):
    pages = [HTML_V1, HTML_V2]

    async def fake_fetch_html(url, **_kwargs):
        return _outcome(pages.pop(0)), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)
    _call(server.web_watch, URL, schema=SCHEMA)
    _call(server.web_watch, URL, schema=SCHEMA)

    payload = _call(server.web_changes, URL, schema=SCHEMA)

    assert payload["ok"] is True
    assert payload["mode"] == "fields"
    assert payload["from_version"] == 2 and payload["to_version"] == 1
    assert payload["changes"]["added"] == ["tags[2]"]
    assert payload["changes"]["modified"][0]["pct"] == -16.7
    assert payload["summary"][0] == "price 299→249（-16.7%）"
    assert payload["captured_from"] < payload["captured_to"]


def test_web_changes_line_mode_and_fallback(monkeypatch):
    pages = [HTML_V1, HTML_V2]

    async def fake_fetch_html(url, **_kwargs):
        return _outcome(pages.pop(0)), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)
    _call(server.web_watch, URL)
    _call(server.web_watch, URL)

    lines = _call(server.web_changes, URL)
    assert lines["mode"] == "lines"
    assert lines["added_lines"] >= 1
    assert "changes" not in lines
    assert lines["summary"][0].startswith("新增 ")

    # 快照没存字段时，即便传了 schema 也退回行级 diff 并说明原因
    fallback = _call(server.web_changes, URL, schema=SCHEMA)
    assert fallback["mode"] == "lines"
    assert fallback["notes"]


def test_web_changes_requires_two_versions(monkeypatch):
    async def fake_fetch_html(url, **_kwargs):
        return _outcome(HTML_V1), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)

    assert _call(server.web_changes, URL)["error_code"] == errors.NO_SNAPSHOT

    _call(server.web_watch, URL)
    assert _call(server.web_changes, URL)["error_code"] == errors.NO_SNAPSHOT
    assert _call(server.web_changes, URL, compare="latest")["error_code"] == errors.NO_SNAPSHOT


def test_schedule_webhook_carries_field_changes(monkeypatch):
    pages = [HTML_V1, HTML_V2]
    captured: list[dict] = []

    async def fake_fetch_html(url, **_kwargs):
        return _outcome(pages.pop(0)), None

    async def fake_post(url, event):
        captured.append(event)
        return {"ok": True, "status": 200}

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(server, "_post_webhook", fake_post)

    task = {
        "id": "task-1",
        "kind": "watch",
        "url": URL,
        "selector": None,
        "session": "default",
        "webhook_url": "https://hook.example/cb",
        "schema": SCHEMA,
    }
    asyncio.run(server.run_scheduled_task(task))
    assert captured == []  # 首次只建基线，不发通知

    result = asyncio.run(server.run_scheduled_task(task))
    assert result["changed"] is True
    assert len(captured) == 1
    event = captured[0]
    assert event["changes"]["modified"][0]["path"] == "price"
    assert event["summary_text"] == ["price 299→249（-16.7%）", "新增 tags[2]"]
    assert event["summary"]  # 旧的 summary 键保持不变
