"""M4 缓存：HttpCache 的命中/过期/落盘路径。"""

from __future__ import annotations

import time

from scrape_mcp.core.cache import HttpCache


def test_cache_hit_after_put(tmp_path):
    c = HttpCache(str(tmp_path), ttl=300)
    assert c.get("https://a.com/x") is None
    c.put("https://a.com/x", {"status": 200, "text": "hello"})
    got = c.get("https://a.com/x")
    assert got is not None
    assert got["text"] == "hello"


def test_cache_expires_after_ttl(tmp_path):
    c = HttpCache(str(tmp_path), ttl=0.1)
    c.put("https://a.com/x", {"status": 200, "text": "hi"})
    time.sleep(0.15)
    assert c.get("https://a.com/x") is None


def test_cache_off_when_ttl_zero(tmp_path):
    c = HttpCache(str(tmp_path), ttl=0)
    c.put("https://a.com/x", {"status": 200, "text": "hi"})
    assert c.get("https://a.com/x") is None


def test_distinct_urls_do_not_collide(tmp_path):
    c = HttpCache(str(tmp_path), ttl=300)
    c.put("https://a.com/1", {"status": 200, "text": "one"})
    c.put("https://a.com/2", {"status": 200, "text": "two"})
    assert c.get("https://a.com/1")["text"] == "one"
    assert c.get("https://a.com/2")["text"] == "two"