"""deadline_ms 总预算：单步压缩、逐级降级与预算耗尽的统一上报。"""

from __future__ import annotations

import asyncio
import json
import time

from scrape_mcp import server
from scrape_mcp.config import Settings
from scrape_mcp.core import deadline as dl
from scrape_mcp.core import errors
from scrape_mcp.core.browser import BrowserOutcome
from scrape_mcp.core.fetcher import Fetcher, FetchOutcome
from scrape_mcp.core.http_client import HttpResponse

URL = "https://example.com/page"
LONG_HTML = "<html><body><p>" + "正文" * 800 + "</p></body></html>"
CLOUDFLARE_HTML = "<html><body>Just a moment... cf-chl</body></html>"


class _RecordingHttp:
    def __init__(self, ok: bool = True, status: int = 200, text: str = LONG_HTML, error=None):
        self.ok = ok
        self.status = status
        self.text = text
        self.error = error
        self.calls = 0
        self.timeouts: list[float | None] = []

    async def get(self, url: str, **kwargs) -> HttpResponse:
        self.calls += 1
        self.timeouts.append(kwargs.get("timeout"))
        return HttpResponse(
            ok=self.ok,
            status=self.status,
            url=url,
            content_type="text/html",
            text=self.text,
            elapsed_ms=5,
            error=self.error,
        )


class _RecordingBrowser:
    def __init__(self, html: str = LONG_HTML) -> None:
        self.html = html
        self.calls = 0
        self.timeouts: list[float | None] = []

    async def render(self, url: str, host: str | None = None, **kwargs) -> BrowserOutcome:
        self.calls += 1
        self.timeouts.append(kwargs.get("timeout"))
        return BrowserOutcome(
            ok=True, url=url, final_url=url, status=200, html=self.html, elapsed_ms=100
        )

    def has_session(self, host: str, session: str = "default") -> bool:
        return False

    async def aclose(self) -> None:
        pass


def _fetcher(http: _RecordingHttp, browser=None, **overrides) -> Fetcher:
    fetcher = Fetcher(Settings(**overrides))
    fetcher._http = http
    if browser is not None:
        fetcher._browser = browser
    return fetcher


def _outcome(**overrides) -> FetchOutcome:
    base = {
        "url": URL,
        "final_url": URL,
        "status": 200,
        "tier": "l1",
        "blocked": "none",
        "block_reason": "",
        "content_type": "text/html",
        "html": LONG_HTML,
        "elapsed_ms": 5,
    }
    base.update(overrides)
    return FetchOutcome(**base)


class _CountingFetcher:
    def __init__(self, outcome: FetchOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    async def fetch(self, url: str, **_kwargs) -> FetchOutcome:
        self.calls += 1
        return self.outcome

    async def aclose(self) -> None:
        pass


def _patch_server(monkeypatch, outcome: FetchOutcome) -> _CountingFetcher:
    fetcher = _CountingFetcher(outcome)

    async def allowed(_url: str, *_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(server, "_compliance_check", allowed)
    monkeypatch.setattr(server, "_get_fetcher", lambda: fetcher)
    return fetcher


def _enable_cache(tmp_path, monkeypatch, ttl: float = 300) -> None:
    settings = Settings(data_dir=str(tmp_path / "data"), cache_ttl=ttl)
    monkeypatch.setattr(server, "get_settings", lambda: settings)


def _run(coro) -> dict:
    return json.loads(asyncio.run(coro))


# ---- 预算本体 ----


def test_start_none_or_zero_means_unlimited():
    assert dl.start(None) is None
    assert dl.start(0) is None
    assert dl.remaining(None) is None
    assert dl.expired(None) is False
    assert dl.cap(None, 30) == 30


def test_cap_shrinks_to_remaining_time():
    deadline = dl.start(5000)
    assert dl.cap(deadline, 30) <= 5.0
    assert dl.cap(deadline, 1) == 1

    past = time.monotonic() - 1
    assert dl.expired(past) is True
    # 已耗尽时仍给一个下限，避免把 0 传给底层库
    assert dl.cap(past, 30) == 0.05


# ---- Fetcher：逐级降级 ----


async def test_l1_timeout_is_capped_by_budget():
    http = _RecordingHttp()
    fetcher = _fetcher(http)
    await fetcher.fetch(URL, deadline=dl.start(500))

    assert http.calls == 1
    assert 0 < http.timeouts[0] <= 0.5


async def test_short_budget_skips_l2_escalation():
    browser = _RecordingBrowser()
    http = _RecordingHttp(status=403, text=CLOUDFLARE_HTML)
    fetcher = _fetcher(http, browser)

    # 预算足够跑完 L1，但不够再启动浏览器（阈值 1.5 秒）
    outcome = await fetcher.fetch(URL, deadline=dl.start(1200))

    assert browser.calls == 0
    assert outcome.tier == "l1"
    assert "deadline:skip_l2" in outcome.decisions


async def test_enough_budget_escalates_with_capped_navigate_timeout():
    browser = _RecordingBrowser()
    http = _RecordingHttp(status=403, text=CLOUDFLARE_HTML)
    fetcher = _fetcher(http, browser)

    outcome = await fetcher.fetch(URL, deadline=dl.start(5000))

    assert browser.calls == 1
    assert outcome.tier == "l2"
    assert 0 < browser.timeouts[0] <= 5.0


async def test_short_budget_skips_retry():
    http = _RecordingHttp(ok=False, error="Read timeout")
    fetcher = _fetcher(http, retry_times=3, retry_backoff=1.0)

    outcome = await fetcher.fetch(URL, deadline=dl.start(400))

    assert http.calls == 1
    assert outcome.attempts == 1
    assert "deadline:skip_retry" in outcome.decisions


async def test_exhausted_budget_does_not_send_request():
    http = _RecordingHttp()
    fetcher = _fetcher(http)

    outcome = await fetcher.fetch(URL, deadline=time.monotonic() - 1)

    assert http.calls == 0
    assert outcome.error and "总预算" in outcome.error
    assert "deadline:exceeded" in outcome.decisions


def test_deadline_exceeded_is_classified_as_timeout():
    """预算耗尽的失败必须归到 timeout，而不是靠文本关键词猜成 unknown。"""
    outcome = _outcome(
        status=0,
        html="",
        error="超出总预算 deadline_ms，未发起请求",
        decisions=["deadline:exceeded"],
    )

    assert errors.classify_outcome(outcome) == errors.TIMEOUT
    assert errors.is_retryable(errors.TIMEOUT) is True


# ---- 合规检查：同样受总预算约束 ----


class _RecordingRobots:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[float | None] = []

    async def is_allowed(self, _url: str, timeout: float | None = None) -> bool:
        self.calls.append(timeout)
        return self.allowed


def test_compliance_returns_timeout_when_budget_too_small(monkeypatch):
    robots = _RecordingRobots()
    monkeypatch.setattr(server, "_get_robots", lambda: robots)

    payload = asyncio.run(server._compliance_check(URL, dl.start(100)))

    assert payload is not None
    assert payload["error_code"] == errors.TIMEOUT
    assert payload["retryable"] is True
    # 预算不够就别去抓 robots.txt，把时间留给调用方重试
    assert robots.calls == []


def test_compliance_caps_robots_timeout_to_remaining_budget(monkeypatch):
    robots = _RecordingRobots()
    monkeypatch.setattr(server, "_get_robots", lambda: robots)

    assert asyncio.run(server._compliance_check(URL, dl.start(2000))) is None
    assert len(robots.calls) == 1
    assert 0 < robots.calls[0] <= 2.0


def test_compliance_without_budget_keeps_default_robots_timeout(monkeypatch):
    robots = _RecordingRobots()
    monkeypatch.setattr(server, "_get_robots", lambda: robots)

    assert asyncio.run(server._compliance_check(URL)) is None
    assert robots.calls == [None]


# ---- 工具层：校验与降级上报 ----


def test_deadline_ms_must_be_positive_int(monkeypatch):
    fetch = _patch_server(monkeypatch, _outcome())
    for bad in (0, -100, True, "500"):
        out = _run(server.web_fetch(URL, deadline_ms=bad))
        assert out["error_code"] == errors.INVALID_ARGUMENT
    assert fetch.calls == 0


def test_web_fetch_reports_timeout_when_budget_already_spent(monkeypatch):
    monkeypatch.setattr(server.dl, "start", lambda _ms: time.monotonic() - 1)
    fetcher = _patch_server(monkeypatch, _outcome())

    out = _run(server.web_fetch(URL, deadline_ms=100))

    assert out["ok"] is False
    assert out["error_code"] == errors.TIMEOUT
    assert out["retryable"] is True
    assert out["diagnostics"]["decisions"] == ["deadline:exceeded"]
    assert fetcher.calls == 0


def test_web_batch_degrades_remaining_tasks(monkeypatch):
    monkeypatch.setattr(server.dl, "start", lambda _ms: time.monotonic() - 1)

    out = _run(server.web_batch([URL, "https://b.test/2"], deadline_ms=100))

    assert out["ok"] is True
    assert out["ok_count"] == 0
    assert {r["error_code"] for r in out["results"]} == {errors.TIMEOUT}


def test_web_crawl_stops_when_budget_already_spent(monkeypatch):
    monkeypatch.setattr(server.dl, "start", lambda _ms: time.monotonic() - 1)

    out = _run(server.web_crawl(URL, deadline_ms=100))

    assert out["ok"] is False
    assert out["error_code"] == errors.TIMEOUT


def test_degraded_result_is_not_cached(tmp_path, monkeypatch):
    """预算不足跳过了 L2 的结果不能进缓存，否则后续预算充足的调用会拿到残缺内容。"""
    _enable_cache(tmp_path, monkeypatch)
    fetcher = _patch_server(monkeypatch, _outcome(decisions=["deadline:skip_l2"]))

    first = _run(server.web_fetch(URL))
    second = _run(server.web_fetch(URL))

    assert first["ok"] is True and second["ok"] is True
    assert fetcher.calls == 2
    assert second.get("cached") is not True


def test_full_result_with_budget_still_cached(tmp_path, monkeypatch):
    _enable_cache(tmp_path, monkeypatch)
    fetcher = _patch_server(monkeypatch, _outcome())

    _run(server.web_fetch(URL, deadline_ms=5000))
    second = _run(server.web_fetch(URL, deadline_ms=5000))

    assert fetcher.calls == 1
    assert second["cached"] is True
