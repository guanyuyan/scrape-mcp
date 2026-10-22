"""代理池：解析、轮询/粘性分配、失败冷却，以及 Fetcher 的出口选择。"""

from __future__ import annotations

import asyncio
import time

from scrape_mcp.config import Settings
from scrape_mcp.core.browser import BrowserOutcome
from scrape_mcp.core.fetcher import Fetcher
from scrape_mcp.core.http_client import HttpResponse
from scrape_mcp.core.proxy import ProxyPool, split_pool, valid_proxy

URL = "https://a.test/article"
P1 = "http://127.0.0.1:7891"
P2 = "http://127.0.0.1:7892"
PLAIN_HTML = "<html><body><p>" + "正文" * 800 + "</p></body></html>"
CLOUDFLARE_HTML = "<html><body>Just a moment... cf-chl</body></html>"


# ---- 解析与校验 ----


def test_valid_proxy_accepts_supported_schemes_and_strips_space():
    assert valid_proxy(" http://127.0.0.1:7890 ") == "http://127.0.0.1:7890"
    assert valid_proxy("socks5://user:pass@127.0.0.1:1080") == "socks5://user:pass@127.0.0.1:1080"


def test_valid_proxy_rejects_invalid_values():
    assert valid_proxy("") is None
    assert valid_proxy(None) is None
    assert valid_proxy("ftp://127.0.0.1:21") is None
    assert valid_proxy("127.0.0.1:7890") is None
    assert valid_proxy("http://host:notaport") is None


def test_split_pool_skips_invalid_and_dedupes():
    raw = f"{P1}, ftp://127.0.0.1:21 ;{P2}\n{P1}  socks5://127.0.0.1:7893"

    assert split_pool(raw) == [P1, P2, "socks5://127.0.0.1:7893"]
    assert split_pool("") == []


# ---- 分配策略 ----


def test_pick_round_robin_across_hosts():
    pool = ProxyPool([P1, P2])

    first = pool.pick("a.test")
    second = pool.pick("b.test")

    assert {first, second} == {P1, P2}
    assert pool.pick("a.test") == first


def test_sticky_binding_expires_after_ttl():
    pool = ProxyPool([P1, P2], sticky_ttl_s=0)

    first = pool.pick("a.test")
    second = pool.pick("a.test")

    assert first != second


def test_failures_cool_down_and_rebind_host():
    pool = ProxyPool([P1, P2], fail_threshold=2, cooldown_s=60)

    first = pool.pick("a.test")
    pool.report(first, ok=False)
    assert pool.pick("a.test") == first  # 未达阈值，粘性绑定保留

    pool.report(first, ok=False)
    assert pool.pick("a.test") != first  # 冷却后换一个出口
    assert pool.snapshot() == {"size": 2, "healthy": 1, "cooling": 1}


def test_success_resets_failure_streak():
    pool = ProxyPool([P1], fail_threshold=2)

    pool.report(P1, ok=False)
    pool.report(P1, ok=True)
    pool.report(P1, ok=False)

    assert pool.snapshot()["cooling"] == 0


def test_all_cooling_falls_back_instead_of_returning_none():
    pool = ProxyPool([P1], fail_threshold=1, cooldown_s=60)
    pool.report(P1, ok=False)

    assert pool.pick("a.test") == P1


def test_report_ignores_proxy_outside_pool():
    pool = ProxyPool([P1])
    pool.report("http://127.0.0.1:9999", ok=False)

    assert pool.snapshot() == {"size": 1, "healthy": 1, "cooling": 0}


# ---- Fetcher 集成 ----


class _FakeHttp:
    def __init__(self, *, status: int = 200, text: str = PLAIN_HTML, error: str | None = None):
        self.status = status
        self.text = text
        self.error = error
        self.calls: list[dict] = []

    async def get(self, url: str, **kwargs) -> HttpResponse:
        self.calls.append(kwargs)
        if self.error:
            return HttpResponse(
                ok=False,
                status=0,
                url=url,
                content_type="",
                text="",
                elapsed_ms=1,
                error=self.error,
            )
        return HttpResponse(
            ok=True,
            status=self.status,
            url=url,
            content_type="text/html",
            text=self.text,
            elapsed_ms=1,
        )


class _FakeBrowser:
    def __init__(self) -> None:
        self.proxies: list[str | None] = []

    async def render(self, url: str, host: str | None = None, **kwargs) -> BrowserOutcome:
        self.proxies.append(kwargs.get("proxy"))
        return BrowserOutcome(
            ok=True, url=url, final_url=url, status=200, html=PLAIN_HTML, elapsed_ms=100
        )

    def has_session(self, host: str, session: str = "default") -> bool:
        return False

    async def aclose(self) -> None:
        pass


def _fetcher(http: _FakeHttp, browser=None, **overrides) -> Fetcher:
    fetcher = Fetcher(Settings(data_dir=".", **overrides))
    fetcher._http = http
    if browser is not None:
        fetcher._browser = browser
    return fetcher


def test_pool_proxy_is_used_for_l1_and_l2():
    http = _FakeHttp(status=403, text=CLOUDFLARE_HTML)
    browser = _FakeBrowser()
    fetcher = _fetcher(http, browser, l2_enabled=True, proxy_pool=P1)

    asyncio.run(fetcher.fetch(URL))

    assert [c["proxy"] for c in http.calls] == [P1]
    assert browser.proxies == [P1]


def test_pool_proxy_keeps_shared_cache():
    http = _FakeHttp()
    fetcher = _fetcher(http, l2_enabled=False, proxy_pool=P1)

    asyncio.run(fetcher.fetch(URL))

    assert http.calls[0]["allow_cache"] is True


def test_explicit_proxy_wins_over_pool_and_skips_cache():
    http = _FakeHttp()
    fetcher = _fetcher(http, l2_enabled=False, proxy_pool=P1)

    asyncio.run(fetcher.fetch(URL, proxy=P2))

    assert http.calls[0]["proxy"] == P2
    assert http.calls[0]["allow_cache"] is False
    assert fetcher._pool.snapshot()["size"] == 1


def test_transport_failure_cools_down_and_switches_exit():
    http = _FakeHttp(error="ProxyError: connection refused")
    fetcher = _fetcher(http, l2_enabled=False, retry_times=0, proxy_pool=f"{P1},{P2}")

    asyncio.run(fetcher.fetch(URL))
    asyncio.run(fetcher.fetch(URL))
    asyncio.run(fetcher.fetch(URL))

    assert [c["proxy"] for c in http.calls] == [P1, P1, P2]


def test_block_signal_counts_as_egress_failure():
    http = _FakeHttp(status=403, text=CLOUDFLARE_HTML)
    fetcher = _fetcher(http, l2_enabled=False, retry_times=0, proxy_pool=P1)

    asyncio.run(fetcher.fetch(URL))
    asyncio.run(fetcher.fetch(URL))

    assert fetcher._pool.snapshot()["cooling"] == 1


def test_deadline_exhausted_does_not_blame_proxy():
    http = _FakeHttp()
    fetcher = _fetcher(http, l2_enabled=False, proxy_pool=P1)

    asyncio.run(fetcher.fetch(URL, deadline=time.monotonic() - 1))

    assert http.calls == []
    assert fetcher._pool.snapshot()["cooling"] == 0
