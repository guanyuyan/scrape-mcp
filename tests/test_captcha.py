"""可选 CAPTCHA 钩子：默认关闭、显式开启后的转发与按 cookie 重试。"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp.config import Settings
from scrape_mcp.core import captcha
from scrape_mcp.core.captcha import HookResult, request_solve
from scrape_mcp.core.fetcher import Fetcher
from scrape_mcp.core.http_client import HttpResponse

URL = "https://a.test/article"
PROXY = "http://127.0.0.1:7890"
CAPTCHA_HTML = "<html><body>请完成安全验证</body></html>"
PLAIN_HTML = "<html><body><p>" + "正文" * 800 + "</p></body></html>"
ENDPOINT = "http://127.0.0.1:9999/solve"


class _FakeHttp:
    def __init__(self, *, get_status: int = 200, get_text: str = PLAIN_HTML) -> None:
        self.get_status = get_status
        self.get_text = get_text
        self.get_calls: list[dict] = []
        self.post_calls: list[dict] = []

    async def get(self, url: str, **kwargs) -> HttpResponse:
        self.get_calls.append({"url": url, **kwargs})
        return HttpResponse(
            ok=True,
            status=self.get_status,
            url=url,
            content_type="text/html",
            text=self.get_text,
            elapsed_ms=1,
        )

    async def post_json(self, url: str, *, payload, headers=None, timeout=None, proxy=None):
        self.post_calls.append({"url": url, "payload": payload, "headers": headers})
        return HttpResponse(
            ok=True,
            status=200,
            url=url,
            content_type="application/json",
            text=json.dumps(
                {"ok": True, "cookies": {"cf_clearance": "token"}, "note": "已人工放行"}
            ),
            elapsed_ms=5,
        )


def test_hook_is_disabled_without_endpoint():
    settings = Settings(data_dir=".")

    assert captcha.enabled(settings) is False
    assert (
        asyncio.run(request_solve(_FakeHttp(), settings, url=URL, status=403, blocked="captcha"))
        is None
    )


def test_request_solve_parses_cookies_and_sends_bearer_key():
    settings = Settings(
        data_dir=".",
        captcha_solver_url=ENDPOINT,
        captcha_solver_key="secret",
    )
    http = _FakeHttp()

    result = asyncio.run(request_solve(http, settings, url=URL, status=403, blocked="captcha"))

    assert isinstance(result, HookResult)
    assert result.ok is True
    assert result.cookies == {"cf_clearance": "token"}
    assert result.note == "已人工放行"
    assert http.post_calls[0]["url"] == ENDPOINT
    assert http.post_calls[0]["payload"] == {"url": URL, "status": 403, "blocked": "captcha"}
    assert http.post_calls[0]["headers"]["Authorization"] == "Bearer secret"


class _BadHookHttp(_FakeHttp):
    async def post_json(self, url: str, *, payload, headers=None, timeout=None, proxy=None):
        return HttpResponse(
            ok=True,
            status=200,
            url=url,
            content_type="application/json",
            text="oops",
            elapsed_ms=5,
        )


def test_request_solve_rejects_non_json_response():
    settings = Settings(data_dir=".", captcha_solver_url=ENDPOINT)

    result = asyncio.run(
        request_solve(_BadHookHttp(), settings, url=URL, status=403, blocked="captcha")
    )

    assert result is not None and result.ok is False
    assert "非 JSON" in result.note


def test_captcha_without_hook_does_not_call_external_service():
    http = _FakeHttp(get_text=CAPTCHA_HTML)
    fetcher = Fetcher(Settings(data_dir=".", l2_enabled=False, retry_times=0))
    fetcher._http = http

    outcome = asyncio.run(fetcher.fetch(URL))

    assert outcome.blocked == "captcha"
    assert http.post_calls == []
    assert "captcha_hook:called" not in outcome.decisions


def test_hook_retries_once_with_returned_cookies():
    class _SequenceHttp(_FakeHttp):
        async def get(self, url: str, **kwargs) -> HttpResponse:
            self.get_calls.append({"url": url, **kwargs})
            text = CAPTCHA_HTML if len(self.get_calls) == 1 else PLAIN_HTML
            return HttpResponse(
                ok=True,
                status=200,
                url=url,
                content_type="text/html",
                text=text,
                elapsed_ms=1,
            )

    http = _SequenceHttp()
    fetcher = Fetcher(
        Settings(data_dir=".", l2_enabled=False, retry_times=0, captcha_solver_url=ENDPOINT)
    )
    fetcher._http = http

    outcome = asyncio.run(fetcher.fetch(URL))

    assert outcome.blocked == "none"
    assert "captcha_hook:solved" in outcome.decisions
    assert any("已人工放行" in note for note in outcome.notes)
    assert http.get_calls[1]["cookies"] == {"cf_clearance": "token"}
    assert http.get_calls[1]["allow_cache"] is False


def test_hook_without_cookies_keeps_original_block():
    class _NoCookieHttp(_FakeHttp):
        async def post_json(self, url: str, *, payload, headers=None, timeout=None, proxy=None):
            self.post_calls.append({"url": url, "payload": payload})
            return HttpResponse(
                ok=True,
                status=200,
                url=url,
                content_type="application/json",
                text=json.dumps({"ok": True}),
                elapsed_ms=5,
            )

    http = _NoCookieHttp()
    http.get_text = CAPTCHA_HTML
    fetcher = Fetcher(
        Settings(data_dir=".", l2_enabled=False, retry_times=0, captcha_solver_url=ENDPOINT)
    )
    fetcher._http = http

    outcome = asyncio.run(fetcher.fetch(URL))

    assert outcome.blocked == "captcha"
    assert "captcha_hook:called" in outcome.decisions
    assert len(http.get_calls) == 1


RECAPTCHA_HTML = (
    '<html><body><div class="g-recaptcha" data-sitekey="6LcabcDEF_123456"></div></body></html>'
)


def test_request_solve_forwards_challenge_details():
    settings = Settings(data_dir=".", captcha_solver_url=ENDPOINT)
    http = _FakeHttp()

    asyncio.run(
        request_solve(
            http,
            settings,
            url=URL,
            status=403,
            blocked="captcha",
            challenge={
                "provider": "recaptcha",
                "kind": "captcha",
                "sitekey": "6LcabcDEF_123456",
                "action": "login",
                "status": 403,
            },
        )
    )

    assert http.post_calls[0]["payload"] == {
        "url": URL,
        "status": 403,
        "blocked": "captcha",
        "provider": "recaptcha",
        "kind": "captcha",
        "sitekey": "6LcabcDEF_123456",
        "action": "login",
    }


def test_hook_returns_user_agent_when_provided():
    class _UaHttp(_FakeHttp):
        async def post_json(self, url: str, *, payload, headers=None, timeout=None, proxy=None):
            return HttpResponse(
                ok=True,
                status=200,
                url=url,
                content_type="application/json",
                text=json.dumps(
                    {"ok": True, "cookies": {"sid": "1"}, "user_agent": "Mozilla/5.0 solver"}
                ),
                elapsed_ms=5,
            )

    settings = Settings(data_dir=".", captcha_solver_url=ENDPOINT)

    result = asyncio.run(request_solve(_UaHttp(), settings, url=URL, status=403, blocked="captcha"))

    assert result is not None and result.ok is True
    assert result.user_agent == "Mozilla/5.0 solver"


def test_fetcher_passes_challenge_to_hook():
    class _CaptchaHttp(_FakeHttp):
        async def get(self, url: str, **kwargs) -> HttpResponse:
            self.get_calls.append({"url": url, **kwargs})
            return HttpResponse(
                ok=True,
                status=403,
                url=url,
                content_type="text/html",
                text=RECAPTCHA_HTML,
                elapsed_ms=1,
            )

    http = _CaptchaHttp()
    fetcher = Fetcher(
        Settings(data_dir=".", l2_enabled=False, retry_times=0, captcha_solver_url=ENDPOINT)
    )
    fetcher._http = http

    outcome = asyncio.run(fetcher.fetch(URL))

    # 403 的 reCAPTCHA 页被 detector 归为 waf，但挑战诊断识别出 captcha，仍应交给钩子
    assert outcome.blocked == "waf"
    assert "captcha_hook:called" in outcome.decisions
    assert outcome.challenge is not None
    assert outcome.challenge["provider"] == "recaptcha"
    assert outcome.challenge["sitekey"] == "6LcabcDEF_123456"
    assert http.post_calls[0]["payload"]["provider"] == "recaptcha"
    assert http.post_calls[0]["payload"]["sitekey"] == "6LcabcDEF_123456"


def test_cookie_cache_skips_second_solve(tmp_path):
    class _CacheHttp(_FakeHttp):
        async def get(self, url: str, **kwargs) -> HttpResponse:
            self.get_calls.append({"url": url, **kwargs})
            # 第 1、3 次是被拦的挑战页，第 2、4 次带 cookie 后放行
            text = CAPTCHA_HTML if len(self.get_calls) % 2 == 1 else PLAIN_HTML
            return HttpResponse(
                ok=True,
                status=200,
                url=url,
                content_type="text/html",
                text=text,
                elapsed_ms=1,
            )

    http = _CacheHttp()
    fetcher = Fetcher(
        Settings(
            data_dir=str(tmp_path),
            l2_enabled=False,
            retry_times=0,
            captcha_solver_url=ENDPOINT,
            captcha_cookie_cache=True,
        )
    )
    fetcher._http = http

    first = asyncio.run(fetcher.fetch(URL))
    second = asyncio.run(fetcher.fetch(URL))

    assert first.blocked == "none" and second.blocked == "none"
    assert "captcha_cache:hit" in second.decisions
    assert len(http.post_calls) == 1  # 第二次不再调用求解服务
    assert (tmp_path / "captcha_cookies.json").exists()


def test_cookie_cache_disabled_by_default_keeps_solving(tmp_path):
    class _AlwaysCaptchaHttp(_FakeHttp):
        async def get(self, url: str, **kwargs) -> HttpResponse:
            self.get_calls.append({"url": url, **kwargs})
            return HttpResponse(
                ok=True,
                status=200,
                url=url,
                content_type="text/html",
                text=CAPTCHA_HTML,
                elapsed_ms=1,
            )

    http = _AlwaysCaptchaHttp()
    fetcher = Fetcher(
        Settings(
            data_dir=str(tmp_path), l2_enabled=False, retry_times=0, captcha_solver_url=ENDPOINT
        )
    )
    fetcher._http = http

    first = asyncio.run(fetcher.fetch(URL))
    second = asyncio.run(fetcher.fetch(URL))

    assert "captcha_cache:hit" not in first.decisions
    assert "captcha_cache:hit" not in second.decisions
    assert len(http.post_calls) == 2
    assert not (tmp_path / "captcha_cookies.json").exists()
