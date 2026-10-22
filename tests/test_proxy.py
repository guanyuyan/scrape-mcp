"""单请求代理：参数校验、L1/L2 出口透传、缓存与 context 隔离。"""

from __future__ import annotations

import asyncio
import json

import pytest

from scrape_mcp import server
from scrape_mcp.config import Settings
from scrape_mcp.core.browser import BrowserOutcome, BrowserRenderer
from scrape_mcp.core.fetcher import Fetcher
from scrape_mcp.core.http_client import HttpResponse

URL = "https://a.test/article"
LONG_TEXT = "正文" * 800
CLOUDFLARE_HTML = "<html><body>Just a moment... cf-chl</body></html>"
PLAIN_HTML = f"<html><body><p>{LONG_TEXT}</p></body></html>"
PROXY = "http://127.0.0.1:7890"


def test_proxy_arg_accepts_supported_schemes():
    for raw in (
        "http://127.0.0.1:7890",
        "https://proxy.test:8443",
        "socks5://user:pass@127.0.0.1:1080",
        " SOCKS4://127.0.0.1:1080 ",
    ):
        value, err = server._proxy_arg(raw)
        assert err is None
        assert value == raw.strip()


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "ftp://127.0.0.1:21", "127.0.0.1:7890", "http://", "http://host:notaport", 123],
)
def test_proxy_arg_rejects_invalid_values(raw):
    value, err = server._proxy_arg(raw)

    assert value is None
    assert err is not None and err["error_code"] == "invalid_argument"


def test_web_fetch_passes_proxy_to_fetch(monkeypatch):
    seen: dict = {}

    async def fake_fetch_dict(url, max_tokens, link_policy, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "url": url}

    monkeypatch.setattr(server, "_fetch_dict", fake_fetch_dict)
    out = json.loads(asyncio.run(server.web_fetch(URL, proxy=" http://127.0.0.1:7890 ")))

    assert out["ok"] is True
    assert seen["proxy"] == "http://127.0.0.1:7890"


def test_web_fetch_rejects_bad_proxy_before_any_fetch(monkeypatch):
    async def boom(*_args, **_kwargs):  # 校验失败时不应走到抓取
        raise AssertionError("不应发起抓取")

    monkeypatch.setattr(server, "_fetch_dict", boom)
    out = json.loads(asyncio.run(server.web_fetch(URL, proxy="ftp://127.0.0.1:21")))

    assert out["ok"] is False and out["error_code"] == "invalid_argument"


def test_proxy_disables_result_cache(monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: Settings(cache_ttl=300))

    assert server._cacheable(headers=None, cookies=None, bypass_cache=False, debug=False)
    assert not server._cacheable(
        headers=None, cookies=None, bypass_cache=False, debug=False, proxy=PROXY
    )


class _RecordingHttp:
    def __init__(self, *, status: int = 200, text: str = PLAIN_HTML) -> None:
        self.status = status
        self.text = text
        self.proxies: list[str | None] = []

    async def get(self, url: str, **kwargs) -> HttpResponse:
        self.proxies.append(kwargs.get("proxy"))
        return HttpResponse(
            ok=True,
            status=self.status,
            url=url,
            content_type="text/html",
            text=self.text,
            elapsed_ms=5,
        )


class _RecordingBrowser:
    def __init__(self) -> None:
        self.proxies: list[str | None] = []

    async def render(self, url: str, host: str | None = None, **kwargs) -> BrowserOutcome:
        self.proxies.append(kwargs.get("proxy"))
        return BrowserOutcome(
            ok=True,
            url=url,
            final_url=url,
            status=200,
            html=PLAIN_HTML,
            elapsed_ms=100,
        )

    def has_session(self, host: str, session: str = "default") -> bool:
        return False

    async def aclose(self) -> None:
        pass


def _fetcher(http: _RecordingHttp, browser: _RecordingBrowser | None = None) -> Fetcher:
    fetcher = Fetcher(Settings(data_dir=".", l2_enabled=True))
    fetcher._http = http
    if browser is not None:
        fetcher._browser = browser
    return fetcher


def test_l1_uses_request_proxy():
    http = _RecordingHttp()

    asyncio.run(_fetcher(http).fetch(URL, proxy=PROXY))

    assert http.proxies == [PROXY]


def test_l2_escalation_uses_same_proxy():
    http = _RecordingHttp(status=403, text=CLOUDFLARE_HTML)
    browser = _RecordingBrowser()

    asyncio.run(_fetcher(http, browser).fetch(URL, proxy=PROXY))

    assert http.proxies == [PROXY]
    assert browser.proxies == [PROXY]


def test_browser_context_is_isolated_by_proxy(tmp_path):
    renderer = BrowserRenderer(Settings(data_dir=str(tmp_path)))
    created: list[str | None] = []

    async def fake_new_context(host, session="default", *, headless=None, proxy=None):
        created.append(proxy)

        class _Ctx:
            async def close(self) -> None: ...

        return _Ctx()

    renderer._new_context = fake_new_context  # type: ignore[method-assign]

    async def run():
        first = await renderer._context_for("a.test", proxy=PROXY)
        same = await renderer._context_for("a.test", proxy=PROXY)
        other = await renderer._context_for("a.test", proxy="socks5://127.0.0.1:1080")
        plain = await renderer._context_for("a.test")
        return first, same, other, plain

    first, same, other, plain = asyncio.run(run())

    assert first is same
    assert first is not other and first is not plain
    assert created == [PROXY, "socks5://127.0.0.1:1080", None]
