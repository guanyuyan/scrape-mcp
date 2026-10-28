"""站点变更检测：哈希归一化、diff 摘要、快照存储与"失败不覆盖基线"。"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp import server
from scrape_mcp.core import errors
from scrape_mcp.core.fetcher import FetchOutcome
from scrape_mcp.core.watch import WatchStore, content_hash, diff_summary, watch_key

URL = "https://a.com/list"


def _call(*args, **kwargs) -> dict:
    return json.loads(asyncio.run(server.web_watch(*args, **kwargs)))


def _outcome(html: str, **overrides) -> FetchOutcome:
    base = {
        "url": URL,
        "final_url": URL,
        "status": 200,
        "tier": "l1",
        "blocked": "none",
        "block_reason": "",
        "content_type": "text/html",
        "html": html,
        "elapsed_ms": 9,
    }
    base.update(overrides)
    return FetchOutcome(**base)


def _page(items: list[str]) -> str:
    lis = "".join(f"<li>{item}</li>" for item in items)
    return f"<html><body><article><h1>标题</h1><ul>{lis}</ul></article></body></html>"


def test_content_hash_ignores_whitespace_noise():
    # 站点每次渲染的行尾空白与空行差异不该算"变过"
    assert content_hash("a\nb") == content_hash("a  \n\nb\r\n")
    assert content_hash("a\nb") != content_hash("a\nc")


def test_diff_summary_reports_added_and_removed():
    summary = diff_summary("a\nb\nc", "a\nc\nd")
    assert summary["added_lines"] == 1
    assert summary["removed_lines"] == 1
    assert "-b" in summary["diff"] and "+d" in summary["diff"]
    assert summary["diff_truncated"] is False


def test_diff_summary_truncates_preview():
    summary = diff_summary("", "\n".join(f"行{i}" for i in range(100)))
    assert summary["added_lines"] == 100
    assert len(summary["diff"]) == 40
    assert summary["diff_truncated"] is True


def test_watch_store_roundtrip_and_bad_file(tmp_path):
    store = WatchStore(tmp_path)
    key = watch_key(URL, "ul")
    assert store.load(key) is None
    store.save(key, {"url": URL, "selector": "ul", "hash": "h", "chars": 1, "text": "x"})
    record = store.load(key)
    assert record is not None and record["hash"] == "h" and "saved_at" in record
    # 不同 selector 各记一份，互不覆盖
    assert store.load(watch_key(URL, None)) is None
    # 文件损坏时按"没有快照"处理，而不是抛异常
    (tmp_path / "watch" / f"{key}.json").write_text("{", encoding="utf-8")
    assert store.load(key) is None


def test_watch_first_seen_then_unchanged_then_changed(monkeypatch):
    pages = [_page(["一"]), _page(["一"]), _page(["一", "二"])]
    calls = {"n": 0}

    async def fake_fetch_html(url, **_kwargs):
        html = pages[min(calls["n"], len(pages) - 1)]
        calls["n"] += 1
        return _outcome(html), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)

    first = _call(URL, selector="li")
    assert first["ok"] is True
    assert first["first_seen"] is True and first["changed"] is True
    assert first["added_lines"] == 0  # 首次没有旧快照可比，不给 diff

    second = _call(URL, selector="li")
    assert second["first_seen"] is False and second["changed"] is False
    assert second["diff"] == [] and second["added_lines"] == 0
    assert second["previous_hash"] == second["hash"]

    third = _call(URL, selector="li")
    assert third["changed"] is True
    assert third["added_lines"] == 1 and third["removed_lines"] == 0
    assert any("二" in line for line in third["diff"])


def test_watch_failure_keeps_baseline(monkeypatch):
    state = {"html": _page(["一"]), "fail": False}

    async def fake_fetch_html(url, **_kwargs):
        if state["fail"]:
            return None, {
                "ok": False,
                "url": URL,
                "tier": "l1",
                "error_code": errors.BLOCKED,
                "error": "命中反爬拦截",
                "retryable": False,
                "diagnostics": {},
            }
        return _outcome(state["html"]), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)
    assert _call(URL, selector="li")["first_seen"] is True

    state["fail"] = True
    failed = _call(URL, selector="li")
    assert failed["ok"] is False and failed["error_code"] == errors.BLOCKED
    assert "changed" not in failed  # 失败结果不冒充"没变化"

    state["fail"] = False
    state["html"] = _page(["一", "二"])
    after = _call(URL, selector="li")
    # 基线仍是失败前那份，所以这里对比出的是新增一行，而不是"首次"
    assert after["first_seen"] is False
    assert after["changed"] is True and after["added_lines"] == 1


def test_watch_reset_rebuilds_baseline(monkeypatch):
    async def fake_fetch_html(url, **_kwargs):
        return _outcome(_page(["一"])), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)
    assert _call(URL, selector="li")["first_seen"] is True
    assert _call(URL, selector="li")["changed"] is False
    reset = _call(URL, selector="li", reset=True)
    assert reset["first_seen"] is True and reset["changed"] is True
    assert reset["added_lines"] == 0
