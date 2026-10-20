"""v0.4 诊断与契约：diagnostics 载荷、retryable 字段、错误码稳定性。"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp import server
from scrape_mcp.core import errors
from scrape_mcp.core.fetcher import FetchOutcome

URL = "https://a.com/x"
LONG_HTML = "<html><body><article><h1>标题</h1><p>" + "正文" * 500 + "</p></article></body></html>"


def _call(fn, *args, **kwargs) -> dict:
    return json.loads(asyncio.run(fn(*args, **kwargs)))


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
        "elapsed_ms": 12,
    }
    base.update(overrides)
    return FetchOutcome(**base)


def test_all_codes_are_retryable_classified():
    # 协议里承诺的每个错误码都要有明确的 retryable 判定，不允许抛异常
    for code in errors.ALL_CODES:
        assert isinstance(errors.is_retryable(code), bool)
    assert errors.is_retryable(errors.TIMEOUT)
    assert errors.is_retryable(errors.HTTP_5XX)
    assert not errors.is_retryable(errors.BLOCKED)
    assert not errors.is_retryable(errors.LOGIN_REQUIRED)
    assert not errors.is_retryable(errors.HTTP_4XX)


def test_failure_payload_gets_retryable_injected():
    raw = server._payload(ok=False, error_code=errors.INVALID_URL, error="bad")
    payload = json.loads(raw)
    assert payload["retryable"] is False


async def test_diagnostics_shape():
    outcome = _outcome(decisions=["retry:5xx"], attempts=2, l2={"wait_for_hit": True})
    brief = server._diagnostics(outcome)
    assert brief == {"decisions": ["retry:5xx"], "attempts": 2}
    full = server._diagnostics(outcome, detail=True)
    assert full["html_bytes"] == len(LONG_HTML)
    assert full["l2"] == {"wait_for_hit": True}


def test_debug_success_payload_has_diagnostics(monkeypatch):
    async def fake_fetch_html(url, **_kwargs):
        return _outcome(decisions=["escalate_l2:shell", "l2_ok"], attempts=1), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)
    out = _call(server.web_fetch, URL, debug=True)
    assert out["ok"] is True
    diag = out["diagnostics"]
    assert diag["decisions"] == ["escalate_l2:shell", "l2_ok"]
    assert diag["attempts"] == 1
    assert "selector_hits" in diag and diag["selector_hits"]["article"] == 1
    assert diag["root"] in ("article", "body")
    assert diag["content_chars"] > 0
    assert "content" not in diag  # 诊断不回传页面正文


def test_no_debug_means_no_diagnostics(monkeypatch):
    async def fake_fetch_html(url, **_kwargs):
        return _outcome(), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)
    out = _call(server.web_fetch, URL)
    assert "diagnostics" not in out


def test_transport_error_payload_is_retryable(monkeypatch):
    async def no_compliance(_url):
        return None

    class FakeFetcher:
        async def fetch(self, url, **_kwargs):
            return _outcome(
                status=0,
                html="",
                tier="l1",
                error="Connection reset by peer",
                decisions=["retry:transport"],
                attempts=3,
            )

    monkeypatch.setattr(server, "_compliance_check", no_compliance)
    monkeypatch.setattr(server, "_get_fetcher", lambda: FakeFetcher())
    out = _call(server.web_fetch, URL)
    assert out["ok"] is False
    assert out["error_code"] == errors.CONNECTION
    assert out["retryable"] is True
    assert out["diagnostics"]["attempts"] == 3


def test_rate_limit_payload_exposes_retry_after(monkeypatch):
    async def no_compliance(_url):
        return None

    class FakeFetcher:
        async def fetch(self, url, **_kwargs):
            return _outcome(
                status=429,
                html="<html><body>too many requests</body></html>",
                blocked="rate_limit",
                block_reason="HTTP 429 请求过于频繁",
                retry_after=3.0,
                decisions=["no_escalate:rate_limit"],
            )

    monkeypatch.setattr(server, "_compliance_check", no_compliance)
    monkeypatch.setattr(server, "_get_fetcher", lambda: FakeFetcher())
    out = _call(server.web_fetch, URL)
    assert out["ok"] is False and out["error_code"] == errors.BLOCKED
    assert out["retryable"] is False
    assert out["retry_after_s"] == 3.0
    assert out["diagnostics"]["decisions"] == ["no_escalate:rate_limit"]


class _FakeRobots:
    async def is_allowed(self, _url: str) -> bool:
        return False


def test_denied_payload_is_not_retryable(monkeypatch):
    monkeypatch.setattr(server, "_get_robots", lambda: _FakeRobots())
    payload = asyncio.run(server._compliance_check(URL))
    assert payload is not None
    assert payload["error_code"] == errors.ROBOTS_DISALLOWED
    assert payload["retryable"] is False
