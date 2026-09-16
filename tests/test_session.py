"""M3 登录态持久化与 login 上报逻辑的离线测试（不联网、不起浏览器）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scrape_mcp.config import Settings  # noqa: E402
from scrape_mcp.core.browser import BrowserOutcome  # noqa: E402
from scrape_mcp.core.fetcher import Fetcher  # noqa: E402
from scrape_mcp.core.http_client import HttpResponse  # noqa: E402
from scrape_mcp.core.session import SessionStore  # noqa: E402


def test_session_store_roundtrip(tmp_path):
    store = SessionStore(str(tmp_path))
    host = "www.example.com"
    assert not store.has(host)
    store.save(host, {"cookies": [{"name": "sid", "value": "abc"}], "origins": []})
    assert store.has(host)
    assert store.load(host)["cookies"][0]["value"] == "abc"
    assert store.delete(host)
    assert not store.has(host)


def test_session_store_ignores_bad_json(tmp_path):
    host = "bad.example.com"
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sessions" / "bad.example.com.json").write_text("not json", encoding="utf-8")
    assert SessionStore(str(tmp_path)).load(host) is None


# ---- fetcher 层面：登录墙上报 ----

class FakeHttp:
    def __init__(self, status: int = 200, text: str = "正常正文" * 500) -> None:
        self.status = status
        self.text = text

    async def get(self, url, **_kwargs) -> HttpResponse:
        return HttpResponse(ok=True, status=self.status, url=url, content_type="text/html", text=self.text, elapsed_ms=10)


CLOUDFLARE_HTML = "<html><body>Just a moment... cf-chl</body></html>"


class FakeBrowser:
    def __init__(self, outcome: BrowserOutcome, has_session: bool = False) -> None:
        self.outcome = outcome
        self._has = has_session

    def has_session(self, host: str) -> bool:
        return self._has

    async def render(self, url: str, host: str | None = None) -> BrowserOutcome:
        return self.outcome

    async def aclose(self) -> None:
        pass


async def test_no_login_needed_for_clean_page(tmp_path):
    fetcher = Fetcher(Settings(data_dir=str(tmp_path)))
    fetcher._http = FakeHttp(text="<html><body><p>正常文章正文" * 300 + "</p></body></html>")
    outcome = await fetcher.fetch("https://example.com/a")
    assert outcome.login_required is False


async def test_login_required_reported_when_still_blocked_after_l2(tmp_path):
    still_blocked = BrowserOutcome(ok=True, url="https://www.zhihu.com/q", final_url="https://www.zhihu.com/q", status=200, html=CLOUDFLARE_HTML, elapsed_ms=100)
    fetcher = Fetcher(Settings(data_dir=str(tmp_path)))
    fetcher._http = FakeHttp(status=200, text=CLOUDFLARE_HTML)
    fetcher._browser = FakeBrowser(still_blocked)
    outcome = await fetcher.fetch("https://www.zhihu.com/q/1")
    assert outcome.tier == "l2"
    assert outcome.login_required is True
    assert outcome.session_loaded is False


async def test_session_loaded_when_state_exists(tmp_path):
    # 预置一个登录态文件，模拟之前 login 过
    host = "www.zhihu.com"
    SessionStore(str(tmp_path)).save(host, {"cookies": [], "origins": []})
    still_blocked = BrowserOutcome(ok=True, url="https://www.zhihu.com/q", final_url="https://www.zhihu.com/q", status=200, html=CLOUDFLARE_HTML, elapsed_ms=100)
    fetcher = Fetcher(Settings(data_dir=str(tmp_path)))
    fetcher._http = FakeHttp(status=200, text=CLOUDFLARE_HTML)
    fetcher._browser = FakeBrowser(still_blocked, has_session=True)
    outcome = await fetcher.fetch("https://www.zhihu.com/q/1")
    assert outcome.login_required is True
    assert outcome.session_loaded is True