"""web_batch 离线单测：monkeypatch _fetch_dict，不触碰真实网络。"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp import server


def _run(urls, **kw) -> dict:
    return json.loads(asyncio.run(server.web_batch(urls, **kw)))


def test_web_batch_empty_urls():
    out = _run(["ftp://x", "", None])
    assert out["ok"] is False and "urls" in out["error"]


def test_web_batch_invalid_link_policy():
    out = _run(["https://a.com/"], link_policy="bogus")
    assert out["ok"] is False


def test_web_batch_too_many_urls():
    urls = [f"https://a.com/{i}" for i in range(21)]
    out = _run(urls)
    assert out["ok"] is False and "上限" in out["error"]


def test_web_batch_gathers_and_counts(monkeypatch):
    async def fake_fetch(url, max_tokens, link_policy):
        return {"ok": True, "url": url, "content": "x"}

    monkeypatch.setattr(server, "_fetch_dict", fake_fetch)
    out = _run(["https://a.com/1", "https://a.com/2", "https://a.com/3"])
    assert out["ok"] is True
    assert out["total"] == 3 and out["ok_count"] == 3
    assert [r["url"] for r in out["results"]] == [
        "https://a.com/1",
        "https://a.com/2",
        "https://a.com/3",
    ]


def test_web_batch_isolates_single_failure(monkeypatch):
    async def fake_fetch(url, max_tokens, link_policy):
        if url == "https://bad/b":
            raise RuntimeError("boom")
        return {"ok": True, "url": url}

    monkeypatch.setattr(server, "_fetch_dict", fake_fetch)
    out = _run(["https://a.com/a", "https://bad/b", "https://a.com/c"])
    assert out["ok"] is True  # 单条异常不拖垮整批
    assert out["ok_count"] == 2
    bad = next(r for r in out["results"] if r["url"] == "https://bad/b")
    assert bad["ok"] is False and "boom" in bad["error"]
