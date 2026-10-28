"""M3 登录态持久化与 login 上报逻辑的离线测试（不联网、不起浏览器）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scrape_mcp.config import Settings
from scrape_mcp.core.browser import BrowserOutcome
from scrape_mcp.core.fetcher import Fetcher
from scrape_mcp.core.http_client import HttpResponse
from scrape_mcp.core.session import (
    CookieFileError,
    SessionStore,
    load_cookie_file,
    normalize_session,
    parse_cookie_text,
)


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


# ---- 命名会话 ----


def test_named_sessions_are_isolated(tmp_path):
    store = SessionStore(str(tmp_path))
    store.save("a.test", {"cookies": [{"name": "sid", "value": "work"}], "origins": []}, "work")
    store.save("a.test", {"cookies": [{"name": "sid", "value": "me"}], "origins": []}, "personal")

    assert store.load("a.test", "work")["cookies"][0]["value"] == "work"
    assert store.load("a.test", "personal")["cookies"][0]["value"] == "me"
    # 默认会话不因命名会话的存在而被写入
    assert store.load("a.test") is None
    assert (tmp_path / "sessions" / "work" / "a.test.json").is_file()


def test_default_session_keeps_legacy_path(tmp_path):
    store = SessionStore(str(tmp_path))
    store.save("a.test", {"cookies": []})
    assert (tmp_path / "sessions" / "a.test.json").is_file()


def test_invalid_session_name_rejected(tmp_path):
    store = SessionStore(str(tmp_path))
    with pytest.raises(ValueError):
        store.save("a.test", {"cookies": []}, "../evil")
    with pytest.raises(ValueError):
        store.has("a.test", "名字")


def test_normalize_session_defaults_and_rejects():
    assert normalize_session(None) == "default"
    assert normalize_session("  ") == "default"
    assert normalize_session("Work_1") == "Work_1"


def test_list_sessions_reports_name_host_and_time(tmp_path):
    store = SessionStore(str(tmp_path))
    store.save("b.test", {"cookies": []})
    store.save("a.test", {"cookies": []}, "work")

    items = store.list_sessions()
    assert [(item["session"], item["host"]) for item in items] == [
        ("default", "b.test"),
        ("work", "a.test"),
    ]
    assert all(item["updated_at"].endswith("+00:00") for item in items)


# ---- cookie 导入 / 导出 ----


def test_export_returns_cookies_without_origins(tmp_path):
    store = SessionStore(str(tmp_path))
    store.save(
        "a.test",
        {"cookies": [{"name": "sid", "value": "1"}], "origins": [{"origin": "x"}]},
    )
    assert store.export_cookies("a.test") == {
        "host": "a.test",
        "session": "default",
        "cookies": [{"name": "sid", "value": "1"}],
    }
    assert store.export_cookies("missing.test") is None


def test_import_merges_by_name_domain_path(tmp_path):
    store = SessionStore(str(tmp_path))
    store.save(
        "a.test",
        {
            "cookies": [{"name": "sid", "value": "old", "domain": "a.test", "path": "/"}],
            "origins": [{"origin": "keep"}],
        },
    )

    count = store.import_cookies(
        "a.test",
        [{"name": "sid", "value": "new"}, {"name": "token", "value": "t"}, {"value": "no-name"}],
    )

    assert count == 2
    state = store.load("a.test")
    cookies = {c["name"]: c for c in state["cookies"]}
    assert cookies["sid"]["value"] == "new"
    assert cookies["token"]["domain"] == "a.test" and cookies["token"]["path"] == "/"
    # 导入 cookie 不应冲掉 localStorage 等已有状态
    assert state["origins"] == [{"origin": "keep"}]


def test_import_replace_drops_existing_cookies(tmp_path):
    store = SessionStore(str(tmp_path))
    store.save("a.test", {"cookies": [{"name": "old", "value": "1"}], "origins": []})
    store.import_cookies("a.test", [{"name": "new", "value": "2"}], replace=True)
    assert [c["name"] for c in store.load("a.test")["cookies"]] == ["new"]


def test_import_without_valid_cookie_does_not_write(tmp_path):
    store = SessionStore(str(tmp_path))
    assert store.import_cookies("a.test", [{"value": "x"}, "bad"]) == 0
    assert not store.has("a.test")


# ---- cookie 文件解析（JSON / Netscape）----

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    "# https://curl.se/docs/http-cookies.html\n"
    ".a.test\tTRUE\t/\tFALSE\t0\tsid\tabc\n"
    "#HttpOnly_www.a.test\tFALSE\t/login\tTRUE\t1893456000\ttoken\txyz\n"
)


def test_parse_netscape_cookies():
    cookies = parse_cookie_text(NETSCAPE)
    assert cookies[0] == {
        "name": "sid",
        "value": "abc",
        "domain": ".a.test",
        "path": "/",
    }
    # HttpOnly 前缀、secure 与过期时间都要保留，否则登录态会提前失效
    assert cookies[1]["name"] == "token"
    assert cookies[1]["domain"] == "www.a.test"
    assert cookies[1]["httpOnly"] is True and cookies[1]["secure"] is True
    assert cookies[1]["expires"] == 1893456000


def test_parse_json_accepts_all_three_shapes():
    assert parse_cookie_text('{"cookies": [{"name": "a"}]}')[0]["name"] == "a"
    assert parse_cookie_text('[{"name": "b"}]')[0]["name"] == "b"
    assert parse_cookie_text('{"cookies": [{"name": "c"}], "origins": []}')[0]["name"] == "c"


def test_load_cookie_file_ignores_bom(tmp_path):
    source = tmp_path / "cookies.txt"
    source.write_text("\ufeff" + NETSCAPE, encoding="utf-8")
    assert len(load_cookie_file(source)) == 2


def test_parse_cookie_text_rejects_unsupported_content():
    with pytest.raises(CookieFileError):
        parse_cookie_text("   ")
    with pytest.raises(CookieFileError):
        parse_cookie_text("not a cookie file")
    with pytest.raises(CookieFileError):
        parse_cookie_text('{"foo": 1}')
    with pytest.raises(CookieFileError):
        parse_cookie_text('{"cookies": "oops"}')


# ---- fetcher 层面：登录墙上报 ----


class FakeHttp:
    def __init__(self, status: int = 200, text: str = "正常正文" * 500) -> None:
        self.status = status
        self.text = text

    async def get(self, url, **_kwargs) -> HttpResponse:
        return HttpResponse(
            ok=True,
            status=self.status,
            url=url,
            content_type="text/html",
            text=self.text,
            elapsed_ms=10,
        )


CLOUDFLARE_HTML = "<html><body>Just a moment... cf-chl</body></html>"


class FakeBrowser:
    def __init__(self, outcome: BrowserOutcome, has_session: bool = False) -> None:
        self.outcome = outcome
        self._has = has_session

    def has_session(self, host: str, session: str = "default") -> bool:
        return self._has

    async def render(self, url: str, host: str | None = None, **kwargs) -> BrowserOutcome:
        return self.outcome

    async def aclose(self) -> None:
        pass


async def test_no_login_needed_for_clean_page(tmp_path):
    fetcher = Fetcher(Settings(data_dir=str(tmp_path)))
    fetcher._http = FakeHttp(text="<html><body><p>正常文章正文" * 300 + "</p></body></html>")
    outcome = await fetcher.fetch("https://example.com/a")
    assert outcome.login_required is False


async def test_login_required_reported_when_still_blocked_after_l2(tmp_path):
    still_blocked = BrowserOutcome(
        ok=True,
        url="https://www.zhihu.com/q",
        final_url="https://www.zhihu.com/q",
        status=200,
        html=CLOUDFLARE_HTML,
        elapsed_ms=100,
    )
    fetcher = Fetcher(Settings(data_dir=str(tmp_path)))
    fetcher._http = FakeHttp(status=200, text=CLOUDFLARE_HTML)
    fetcher._browser = FakeBrowser(still_blocked)
    outcome = await fetcher.fetch("https://www.zhihu.com/q/1")
    assert outcome.tier == "l2"
    assert outcome.login_required is True
    assert outcome.session_loaded is False


BARE_403 = "<html><body><h1>403 Forbidden</h1><p>Your access is blocked.</p></body></html>"


async def test_bare_403_is_reported_as_blocked_not_login_required(tmp_path):
    """裸 403（WAF/出口风控）页面上没有登录入口：登录也救不了，不该提示 login。"""
    blocked_page = BrowserOutcome(
        ok=True,
        url="https://www.thepaper.cn/",
        final_url="https://www.thepaper.cn/",
        status=403,
        html=BARE_403,
        elapsed_ms=100,
    )
    fetcher = Fetcher(Settings(data_dir=str(tmp_path)))
    fetcher._http = FakeHttp(status=403, text=BARE_403)
    fetcher._browser = FakeBrowser(blocked_page)
    outcome = await fetcher.fetch("https://www.thepaper.cn/")
    assert outcome.blocked == "waf"
    assert outcome.login_required is False


async def test_403_with_login_form_still_reports_login_required(tmp_path):
    login_page = '<html><body><form><input type="password"></form></body></html>'
    blocked_page = BrowserOutcome(
        ok=True,
        url="https://example.com/private",
        final_url="https://example.com/private",
        status=403,
        html=login_page,
        elapsed_ms=100,
    )
    fetcher = Fetcher(Settings(data_dir=str(tmp_path)))
    fetcher._http = FakeHttp(status=403, text=login_page)
    fetcher._browser = FakeBrowser(blocked_page)
    outcome = await fetcher.fetch("https://example.com/private")
    assert outcome.blocked == "waf"
    assert outcome.login_required is True


async def test_session_loaded_when_state_exists(tmp_path):
    # 预置一个登录态文件，模拟之前 login 过
    host = "www.zhihu.com"
    SessionStore(str(tmp_path)).save(host, {"cookies": [], "origins": []})
    still_blocked = BrowserOutcome(
        ok=True,
        url="https://www.zhihu.com/q",
        final_url="https://www.zhihu.com/q",
        status=200,
        html=CLOUDFLARE_HTML,
        elapsed_ms=100,
    )
    fetcher = Fetcher(Settings(data_dir=str(tmp_path)))
    fetcher._http = FakeHttp(status=200, text=CLOUDFLARE_HTML)
    fetcher._browser = FakeBrowser(still_blocked, has_session=True)
    outcome = await fetcher.fetch("https://www.zhihu.com/q/1")
    assert outcome.login_required is True
    assert outcome.session_loaded is True
