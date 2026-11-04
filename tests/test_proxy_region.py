"""代理增强：地区标签、按地区分配、会话绑定优先级，以及工具层 region 参数。"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp import server
from scrape_mcp.config import Settings
from scrape_mcp.core import errors
from scrape_mcp.core.browser import BrowserOutcome
from scrape_mcp.core.fetcher import Fetcher
from scrape_mcp.core.http_client import HttpResponse
from scrape_mcp.core.proxy import (
    ProxyPool,
    ProxySpec,
    normalize_region,
    parse_entry,
    parse_pool,
    parse_session_proxies,
    split_pool,
)

URL = "https://a.test/article"
P1 = "http://127.0.0.1:7891"
P2 = "http://127.0.0.1:7892"
P3 = "http://127.0.0.1:7893"
PLAIN_HTML = "<html><body><p>" + "正文" * 800 + "</p></body></html>"


# ---- 解析：地区标签 ----


def test_normalize_region_lowercases_and_rejects_bad_values():
    assert normalize_region(" US ") == "us"
    assert normalize_region("hk-1") == "hk-1"
    assert normalize_region("") is None
    assert normalize_region("u$") is None
    assert normalize_region(None) is None


def test_parse_pool_keeps_region_labels_and_strips_them_from_urls():
    raw = f"{P1}|us, {P2}|hk ;{P3}"
    specs = parse_pool(raw)

    assert [(s.url, s.region) for s in specs] == [(P1, "us"), (P2, "hk"), (P3, None)]
    assert split_pool(raw) == [P1, P2, P3]


def test_parse_entry_keeps_proxy_when_region_tag_is_invalid():
    spec = parse_entry(f"{P1}|u$")

    assert spec is not None
    assert spec.url == P1
    assert spec.region is None


def test_parse_session_proxies_skips_invalid_items():
    raw = f"work={P1}; shop={P2} broken=ftp://127.0.0.1:21 news={P1}"

    assert parse_session_proxies(raw) == {"work": P1, "shop": P2, "news": P1}


# ---- 池分配：按地区过滤 ----


def test_pick_region_filters_and_is_sticky_within_region():
    pool = ProxyPool([ProxySpec(P1, "us"), ProxySpec(P2, "us"), ProxySpec(P3, "hk")])

    us = pool.pick("a.test", region="us")
    hk = pool.pick("a.test", region="hk")

    assert us in {P1, P2} and hk == P3
    assert pool.pick("a.test", region="us") == us


def test_pick_unknown_region_returns_none():
    pool = ProxyPool([ProxySpec(P1, "us")])

    assert pool.pick("a.test", region="jp") is None
    assert pool.pick("a.test") == P1


def test_cooling_exit_falls_back_inside_same_region():
    pool = ProxyPool([ProxySpec(P1, "us"), ProxySpec(P2, "us")], fail_threshold=1)
    first = pool.pick("a.test", region="us")
    pool.report(first, ok=False)

    second = pool.pick("a.test", region="us")

    assert second != first
    assert second in {P1, P2}
    assert pool.regions() == ["us"]


# ---- Fetcher：出口优先级 ----


class _FakeHttp:
    def __init__(self, *, status: int = 200, text: str = PLAIN_HTML) -> None:
        self.status = status
        self.text = text
        self.calls: list[dict] = []

    async def get(self, url: str, **kwargs) -> HttpResponse:
        self.calls.append(kwargs)
        return HttpResponse(
            ok=True,
            status=self.status,
            url=url,
            content_type="text/html",
            text=self.text,
            elapsed_ms=1,
        )


class _FakeBrowser:
    async def render(self, url: str, host: str | None = None, **kwargs) -> BrowserOutcome:
        return BrowserOutcome(
            ok=True, url=url, final_url=url, status=200, html=PLAIN_HTML, elapsed_ms=100
        )

    def has_session(self, host: str, session: str = "default") -> bool:
        return False

    async def aclose(self) -> None:
        pass


def _fetcher(http: _FakeHttp, **overrides) -> Fetcher:
    fetcher = Fetcher(Settings(data_dir=".", l2_enabled=False, **overrides))
    fetcher._http = http
    return fetcher


def test_session_bound_proxy_wins_over_pool_and_skips_shared_cache():
    http = _FakeHttp()
    fetcher = _fetcher(http, proxy_pool=P1, proxy_sessions=f"work={P2}")

    asyncio.run(fetcher.fetch(URL, session="work"))

    assert http.calls[0]["proxy"] == P2
    assert http.calls[0]["allow_cache"] is False
    assert fetcher._pool.snapshot()["cooling"] == 0


def test_other_session_still_uses_pool():
    http = _FakeHttp()
    fetcher = _fetcher(http, proxy_pool=P1, proxy_sessions=f"work={P2}")

    asyncio.run(fetcher.fetch(URL, session="other"))

    assert http.calls[0]["proxy"] == P1
    assert http.calls[0]["allow_cache"] is True


def test_explicit_proxy_wins_over_session_binding():
    http = _FakeHttp()
    fetcher = _fetcher(http, proxy_sessions=f"work={P1}")

    asyncio.run(fetcher.fetch(URL, session="work", proxy=P3))

    assert http.calls[0]["proxy"] == P3
    assert http.calls[0]["allow_cache"] is False


def test_region_selects_tagged_pool_exit_and_notes_egress():
    http = _FakeHttp()
    fetcher = _fetcher(http, proxy_pool=f"{P1}|hk,{P2}|us")

    outcome = asyncio.run(fetcher.fetch(URL, region="us"))

    assert http.calls[0]["proxy"] == P2
    assert http.calls[0]["allow_cache"] is True
    assert any("region=us" in note for note in outcome.notes)
    assert fetcher.proxy_regions() == ["hk", "us"]


def test_region_is_ignored_when_session_proxy_is_bound():
    http = _FakeHttp()
    fetcher = _fetcher(http, proxy_pool=f"{P2}|us", proxy_sessions=f"work={P1}")

    outcome = asyncio.run(fetcher.fetch(URL, session="work", region="us"))

    assert http.calls[0]["proxy"] == P1
    assert any("region 未生效" in note for note in outcome.notes)


# ---- 工具层：region 参数校验与透传 ----


def test_region_arg_rejects_unknown_or_invalid_region(monkeypatch):
    monkeypatch.setattr(server, "_fetcher", _fetcher(_FakeHttp(), proxy_pool=f"{P1}|us"))

    value, err = server._region_arg("us")
    assert value == "us" and err is None

    value, err = server._region_arg("jp")
    assert value is None and err is not None
    assert err["error_code"] == errors.INVALID_ARGUMENT
    assert "region=jp" in err["error"]

    value, err = server._region_arg("u$")
    assert value is None and err is not None
    assert err["error_code"] == errors.INVALID_ARGUMENT


def test_web_fetch_passes_region_to_fetch_dict(monkeypatch):
    monkeypatch.setattr(server, "_fetcher", _fetcher(_FakeHttp(), proxy_pool=f"{P1}|us"))
    seen: dict = {}

    async def fake_fetch_dict(url, max_tokens, link_policy, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "url": url}

    monkeypatch.setattr(server, "_fetch_dict", fake_fetch_dict)
    out = json.loads(asyncio.run(server.web_fetch(URL, region=" US ")))

    assert out["ok"] is True
    assert seen["region"] == "us"


def test_web_fetch_rejects_region_without_matching_exit(monkeypatch):
    monkeypatch.setattr(server, "_fetcher", _fetcher(_FakeHttp(), proxy_pool=P1))

    async def boom(*_args, **_kwargs):
        raise AssertionError("地区不可用时不应发起抓取")

    monkeypatch.setattr(server, "_fetch_dict", boom)
    out = json.loads(asyncio.run(server.web_fetch(URL, region="us")))

    assert out["ok"] is False and out["error_code"] == errors.INVALID_ARGUMENT
