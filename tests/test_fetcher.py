"""升级链路的离线测试：不联网、不起浏览器，只验证调度决策。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scrape_mcp.config import Settings  # noqa: E402
from scrape_mcp.core.browser import BrowserOutcome  # noqa: E402
from scrape_mcp.core.fetcher import Fetcher  # noqa: E402
from scrape_mcp.core.http_client import HttpResponse  # noqa: E402

URL = "https://example.com/page"

LONG_TEXT = "正文" * 800  # 远超 _SHELL_MAX_VISIBLE，避免被当成骨架页
CLOUDFLARE_HTML = "<html><body>Just a moment... cf-chl</body></html>"
# 可见文本 300 字（超过空页门限 200，不会被判成 empty_spa），但 HTML 超 100KB → 骨架页
SHELL_HTML = (
    "<html><body><div id='root'>" + "载入中" * 100 + "<span></span>" * 8000 + "</body></html>"
)
RENDERED_HTML = "<html><body><article><h1>浏览器渲染出的标题</h1><p>" + LONG_TEXT + "</p></article></body></html>"


class FakeHttp:
    def __init__(self, status: int = 200, text: str = LONG_TEXT) -> None:
        self.status = status
        self.text = text
        self.calls = 0

    async def get(self, url, **_kwargs) -> HttpResponse:
        self.calls += 1
        return HttpResponse(
            ok=True, status=self.status, url=url, content_type="text/html", text=self.text, elapsed_ms=10
        )


class FakeBrowser:
    def __init__(self, outcome: BrowserOutcome | None = None) -> None:
        self.outcome = outcome or BrowserOutcome(
            ok=True, url=URL, final_url=URL, status=200, html=RENDERED_HTML, elapsed_ms=500
        )
        self.calls = 0

    async def render(self, url: str, host: str | None = None) -> BrowserOutcome:
        self.calls += 1
        return self.outcome

    def has_session(self, host: str) -> bool:
        return False

    async def aclose(self) -> None:
        pass


def make_fetcher(status: int = 200, text: str = LONG_TEXT, browser=None, **overrides):
    settings = Settings(**overrides)
    fetcher = Fetcher(settings)
    fetcher._http = FakeHttp(status, text)
    if browser is not None:
        fetcher._browser = browser
    return fetcher, fetcher._http


async def test_clean_l1_does_not_start_browser():
    browser = FakeBrowser()
    fetcher, http = make_fetcher(text=f"<html><body><p>{LONG_TEXT}</p></body></html>", browser=browser)
    outcome = await fetcher.fetch(URL)
    assert outcome.tier == "l1"
    assert outcome.blocked == "none"
    assert browser.calls == 0 and http.calls == 1


async def test_challenge_escalates_and_uses_rendered_html():
    browser = FakeBrowser()
    fetcher, _ = make_fetcher(status=403, text=CLOUDFLARE_HTML, browser=browser)
    outcome = await fetcher.fetch(URL)
    assert browser.calls == 1
    assert outcome.tier == "l2"
    assert outcome.blocked == "none"
    assert "浏览器渲染出的标题" in outcome.html
    # 耗时按两段之和上报，如实反映代价
    assert outcome.elapsed_ms == 510


async def test_shell_page_escalates_even_without_block_signal():
    # 未命中任何拦截特征，但大 HTML 榨不出可见文本 → 仍应升级
    browser = FakeBrowser()
    fetcher, _ = make_fetcher(text=SHELL_HTML, browser=browser)
    outcome = await fetcher.fetch(URL)
    assert browser.calls == 1
    assert outcome.tier == "l2"


async def test_normal_short_page_is_not_treated_as_shell():
    browser = FakeBrowser()
    fetcher, _ = make_fetcher(text=f"<html><body><p>{LONG_TEXT}</p></body></html>", browser=browser)
    await fetcher.fetch(URL)
    assert browser.calls == 0


async def test_still_blocked_after_l2_reports_l2_tier():
    blocked_page = BrowserOutcome(
        ok=True, url=URL, final_url=URL, status=403, html=CLOUDFLARE_HTML, elapsed_ms=300
    )
    browser = FakeBrowser(blocked_page)
    fetcher, _ = make_fetcher(status=403, text=CLOUDFLARE_HTML, browser=browser)
    outcome = await fetcher.fetch(URL)
    assert outcome.tier == "l2" and outcome.blocked == "cloudflare"


async def test_l2_failure_keeps_l1_body_and_reason():
    failed = BrowserOutcome(
        ok=False, url=URL, final_url=URL, status=0, html="", elapsed_ms=120, error="浏览器启动失败: 未安装"
    )
    browser = FakeBrowser(failed)
    fetcher, _ = make_fetcher(status=403, text=CLOUDFLARE_HTML, browser=browser)
    outcome = await fetcher.fetch(URL)
    assert outcome.tier == "l1"
    assert outcome.blocked == "cloudflare"
    assert "L2 渲染失败" in outcome.block_reason
    assert outcome.html == CLOUDFLARE_HTML


async def test_rate_limit_is_not_escalated():
    browser = FakeBrowser()
    fetcher, _ = make_fetcher(status=429, text="<html><body>too many requests</body></html>", browser=browser)
    outcome = await fetcher.fetch(URL)
    assert browser.calls == 0 and outcome.blocked == "rate_limit"


async def test_l2_can_be_disabled():
    browser = FakeBrowser()
    fetcher, _ = make_fetcher(status=403, text=CLOUDFLARE_HTML, browser=browser, l2_enabled=False)
    outcome = await fetcher.fetch(URL)
    assert browser.calls == 0 and outcome.tier == "l1"


async def test_transport_error_is_not_escalated():
    class FailingHttp:
        async def get(self, url, **_kwargs):
            return HttpResponse(ok=False, status=0, url=url, content_type="", text="", elapsed_ms=5, error="超时")

    browser = FakeBrowser()
    fetcher = Fetcher(Settings())
    fetcher._http = FailingHttp()
    fetcher._browser = browser
    outcome = await fetcher.fetch(URL)
    assert browser.calls == 0 and outcome.error == "超时"
