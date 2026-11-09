"""求解 cookie 缓存：TTL、LRU、持久化与损坏文件兜底。"""

from __future__ import annotations

import json
import time

from scrape_mcp.core.captcha_cache import CaptchaCookieCache


def test_put_and_get_roundtrip(tmp_path):
    cache = CaptchaCookieCache(tmp_path, ttl=60)

    cache.put("default", "a.test", {"cf_clearance": "token"}, user_agent="UA/1")

    hit = cache.get("default", "a.test")
    assert hit is not None
    assert hit.cookies == {"cf_clearance": "token"}
    assert hit.user_agent == "UA/1"


def test_session_and_host_are_isolated(tmp_path):
    cache = CaptchaCookieCache(tmp_path, ttl=60)

    cache.put("default", "a.test", {"sid": "1"})

    assert cache.get("work", "a.test") is None
    assert cache.get("default", "b.test") is None


def test_expired_entry_is_not_returned(tmp_path):
    cache = CaptchaCookieCache(tmp_path, ttl=60)
    cache.put("default", "a.test", {"sid": "1"})
    cache._entries["default::a.test"].expires_at = time.time() - 1

    assert cache.get("default", "a.test") is None
    assert len(cache) == 0


def test_lru_evicts_oldest(tmp_path):
    cache = CaptchaCookieCache(tmp_path, ttl=60, max_entries=2)

    cache.put("default", "a.test", {"sid": "1"})
    cache.put("default", "b.test", {"sid": "2"})
    cache.get("default", "a.test")  # a 变成最近使用
    cache.put("default", "c.test", {"sid": "3"})

    assert cache.get("default", "b.test") is None
    assert cache.get("default", "a.test") is not None
    assert cache.get("default", "c.test") is not None


def test_empty_cookies_are_ignored(tmp_path):
    cache = CaptchaCookieCache(tmp_path, ttl=60)

    cache.put("default", "a.test", {})

    assert len(cache) == 0


def test_persists_across_instances(tmp_path):
    first = CaptchaCookieCache(tmp_path, ttl=60)
    first.put("work", "a.test", {"sid": "abc"})

    second = CaptchaCookieCache(tmp_path, ttl=60)

    hit = second.get("work", "a.test")
    assert hit is not None and hit.cookies == {"sid": "abc"}
    assert (tmp_path / "captcha_cookies.json").exists()


def test_corrupt_file_is_ignored(tmp_path):
    (tmp_path / "captcha_cookies.json").write_text("{not json", encoding="utf-8")

    cache = CaptchaCookieCache(tmp_path, ttl=60)

    assert len(cache) == 0
    cache.put("default", "a.test", {"sid": "1"})
    assert cache.get("default", "a.test") is not None


def test_clear_removes_entries(tmp_path):
    cache = CaptchaCookieCache(tmp_path, ttl=60)
    cache.put("default", "a.test", {"sid": "1"})

    cache.clear()

    assert len(cache) == 0
    payload = json.loads((tmp_path / "captcha_cookies.json").read_text(encoding="utf-8"))
    assert payload["entries"] == {}
