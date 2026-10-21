"""载荷契约 golden 测试：键集合与错误码取值一旦发布就只增不改。

这些断言刻意写成"精确集合"而不是"包含"：多出一个键也可能是破坏性变更
（调用方按固定 schema 解析），必须显式更新本文件并记入 CHANGELOG。
"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp import server
from scrape_mcp.core import errors
from scrape_mcp.core.fetcher import FetchOutcome
from scrape_mcp.core.session import SessionStore
from scrape_mcp.extract.pdf import PdfUnavailableError

URL = "https://a.test/x"
HTML = (
    "<html><head><title>示例标题</title></head>"
    "<body><article><h1>示例标题</h1><p>正文内容，用于契约测试。</p></article></body></html>"
)

# ---- 各载荷的 golden 键集合 ----

INVALID_PAYLOAD = {"ok", "error_code", "error", "retryable"}

DENIED_PAYLOAD = {"ok", "url", "error_code", "error", "retryable"}

FETCH_ERROR_PAYLOAD = {
    "ok",
    "url",
    "tier",
    "elapsed_ms",
    "error_code",
    "error",
    "retryable",
    "diagnostics",
}

BLOCKED_PAYLOAD = {
    "ok",
    "url",
    "final_url",
    "status",
    "tier",
    "blocked",
    "block_reason",
    "elapsed_ms",
    "error_code",
    "error",
    "retryable",
    "diagnostics",
}

HTTP_STATUS_PAYLOAD = {
    "ok",
    "url",
    "final_url",
    "status",
    "tier",
    "elapsed_ms",
    "error_code",
    "error",
    "retryable",
    "diagnostics",
}

FETCH_OK_PAYLOAD = {
    "ok",
    "url",
    "final_url",
    "status",
    "tier",
    "title",
    "content",
    "token_estimate",
    "truncated",
    "blocks",
    "links_kept",
    "links_dropped",
    "html_bytes",
    "elapsed_ms",
    "cached",
    "attempts",
}

DIAG_BRIEF = {"decisions", "attempts"}
DIAG_DETAIL = {"decisions", "attempts", "html_bytes", "l2"}

# PDF / 纯文本走同一套主键，额外给出内容形态与原始 Content-Type
PLAIN_OK_PAYLOAD = FETCH_OK_PAYLOAD | {"content_type", "kind"}

EXTRACT_OK_PAYLOAD = {
    "ok",
    "url",
    "final_url",
    "status",
    "tier",
    "cached",
    "elapsed_ms",
    "attempts",
    "data",
    "missing",
    "required_missing",
}

CRAWL_PAGE_PAYLOAD = {
    "ok",
    "url",
    "final_url",
    "status",
    "tier",
    "title",
    "content",
    "token_estimate",
    "truncated",
    "cached",
}

CRAWL_OK_PAYLOAD = {
    "ok",
    "start",
    "depth",
    "limit",
    "mode",
    "pages_fetched",
    "ok_count",
    "links_discovered",
    "results",
}

BATCH_OK_PAYLOAD = {"ok", "total", "ok_count", "results"}


def _outcome(**overrides) -> FetchOutcome:
    base = {
        "url": URL,
        "final_url": URL,
        "status": 200,
        "tier": "l1",
        "blocked": "none",
        "block_reason": "",
        "content_type": "text/html",
        "html": HTML,
        "elapsed_ms": 8,
    }
    base.update(overrides)
    return FetchOutcome(**base)


def _patch_fetch(monkeypatch, outcome: FetchOutcome) -> None:
    """替换抓取器与合规检查，保留 server 层的 payload 组装逻辑。"""

    class _FakeFetcher:
        async def fetch(self, url: str, **_kwargs) -> FetchOutcome:
            return outcome

        async def aclose(self) -> None:
            pass

    async def allowed(_url: str, *_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(server, "_compliance_check", allowed)
    monkeypatch.setattr(server, "_get_fetcher", lambda: _FakeFetcher())


def _run(coro) -> dict:
    return json.loads(asyncio.run(coro))


# ---- 错误码契约 ----


def test_error_code_values_are_locked():
    assert errors.ALL_CODES == (
        "ok",
        "invalid_url",
        "invalid_argument",
        "denied",
        "robots_disallowed",
        "login_required",
        "blocked",
        "timeout",
        "dns",
        "tls",
        "connection",
        "http_4xx",
        "http_5xx",
        "missing_required",
        "unsupported_content",
        "empty",
        "unknown",
    )


def test_retryable_matrix_is_locked():
    retryable = {code for code in errors.ALL_CODES if errors.is_retryable(code)}
    assert retryable == {"timeout", "dns", "tls", "connection", "http_5xx", "unknown"}


# ---- web_fetch ----


def test_web_fetch_success_keys(monkeypatch):
    _patch_fetch(monkeypatch, _outcome())
    out = _run(server.web_fetch(URL))
    assert set(out) == FETCH_OK_PAYLOAD
    assert out["ok"] is True


def test_web_fetch_text_format_adds_format_key(monkeypatch):
    _patch_fetch(monkeypatch, _outcome())
    out = _run(server.web_fetch(URL, format="text"))
    assert set(out) == FETCH_OK_PAYLOAD | {"format"}
    assert isinstance(out["content"], str)


def test_web_fetch_json_format_returns_blocks(monkeypatch):
    _patch_fetch(monkeypatch, _outcome())
    out = _run(server.web_fetch(URL, format="json"))
    assert set(out) == FETCH_OK_PAYLOAD | {"format"}
    assert isinstance(out["content"], list)


def test_web_fetch_debug_diagnostics_keys(monkeypatch):
    _patch_fetch(monkeypatch, _outcome(decisions=["escalate_l2:cloudflare"], attempts=2))
    out = _run(server.web_fetch(URL, debug=True))
    assert set(out) == FETCH_OK_PAYLOAD | {"diagnostics"}
    assert set(out["diagnostics"]) == DIAG_DETAIL | {"root", "selector_hits", "content_chars"}
    assert out["diagnostics"]["attempts"] == 2
    assert out["diagnostics"]["decisions"] == ["escalate_l2:cloudflare"]


def test_web_fetch_invalid_url_keys(monkeypatch):
    out = _run(server.web_fetch("ftp://a.test/x"))
    assert set(out) == INVALID_PAYLOAD
    assert out["error_code"] == errors.INVALID_URL


def test_web_fetch_invalid_argument_keys(monkeypatch):
    out = _run(server.web_fetch(URL, link_policy="bogus"))
    assert set(out) == INVALID_PAYLOAD
    assert out["error_code"] == errors.INVALID_ARGUMENT


def test_web_fetch_transport_error_keys(monkeypatch):
    _patch_fetch(monkeypatch, _outcome(error="ReadTimeout: timed out"))
    out = _run(server.web_fetch(URL))
    assert set(out) == FETCH_ERROR_PAYLOAD
    assert out["error_code"] == errors.TIMEOUT
    assert out["retryable"] is True
    assert set(out["diagnostics"]) == DIAG_BRIEF


def test_web_fetch_blocked_keys(monkeypatch):
    _patch_fetch(
        monkeypatch,
        _outcome(blocked="cloudflare", block_reason="cf challenge", attempts=2),
    )
    out = _run(server.web_fetch(URL))
    assert set(out) == BLOCKED_PAYLOAD
    assert out["error_code"] == errors.BLOCKED
    assert out["retryable"] is False
    assert set(out["diagnostics"]) == DIAG_DETAIL


def test_web_fetch_login_required_adds_hint_keys(monkeypatch):
    _patch_fetch(
        monkeypatch,
        _outcome(blocked="empty_spa", login_required=True, retry_after=3.5),
    )
    out = _run(server.web_fetch(URL))
    assert set(out) == BLOCKED_PAYLOAD | {"login_required", "login_hint", "retry_after_s"}
    assert out["error_code"] == errors.LOGIN_REQUIRED
    assert out["retry_after_s"] == 3.5


def test_web_fetch_http_4xx_keys(monkeypatch):
    _patch_fetch(monkeypatch, _outcome(status=404))
    out = _run(server.web_fetch(URL))
    assert set(out) == HTTP_STATUS_PAYLOAD
    assert out["error_code"] == errors.HTTP_4XX


def test_denied_payload_keys(monkeypatch):
    out = server._denied_payload(URL, errors.ROBOTS_DISALLOWED, "no")
    assert set(out) == DENIED_PAYLOAD
    assert out["retryable"] is False


# ---- PDF / 纯文本 ----


def test_web_fetch_pdf_payload_keys(monkeypatch):
    monkeypatch.setattr(server, "pdf_pages", lambda data, *, max_pages: (["第一页正文"], 3, 2))
    _patch_fetch(
        monkeypatch,
        _outcome(
            content_type="application/pdf",
            html="%PDF-1.4",
            kind="pdf",
            content=b"%PDF-1.4",
        ),
    )

    out = _run(server.web_fetch(URL))
    assert set(out) == PLAIN_OK_PAYLOAD | {"pages", "pages_parsed"}
    assert out["kind"] == "pdf"
    assert out["content"] == "第一页正文"
    assert (out["pages"], out["pages_parsed"]) == (3, 2)


def test_web_fetch_scanned_pdf_reports_empty(monkeypatch):
    monkeypatch.setattr(server, "pdf_pages", lambda data, *, max_pages: ([], 2, 2))
    _patch_fetch(
        monkeypatch,
        _outcome(content_type="application/pdf", html="%PDF-1.4", kind="pdf", content=b"x"),
    )

    out = _run(server.web_fetch(URL))
    assert out["ok"] is False
    assert out["error_code"] == errors.EMPTY
    assert out["pages"] == 2


def test_web_fetch_pdf_without_extra_reports_unsupported(monkeypatch):
    def boom(data, *, max_pages):
        raise PdfUnavailableError("未安装 PDF 支持")

    monkeypatch.setattr(server, "pdf_pages", boom)
    _patch_fetch(
        monkeypatch,
        _outcome(content_type="application/pdf", html="%PDF-1.4", kind="pdf", content=b"x"),
    )

    out = _run(server.web_fetch(URL))
    assert out["error_code"] == errors.UNSUPPORTED_CONTENT
    assert out["retryable"] is False


def test_web_fetch_plain_text_payload_keys(monkeypatch):
    _patch_fetch(
        monkeypatch,
        _outcome(kind="text", content_type="text/plain; charset=utf-8", html="纯文本正文"),
    )

    out = _run(server.web_fetch(URL))
    assert set(out) == PLAIN_OK_PAYLOAD
    assert out["kind"] == "text"
    assert out["content"] == "纯文本正文"


def test_web_extract_rejects_non_html(monkeypatch):
    _patch_fetch(
        monkeypatch,
        _outcome(content_type="application/pdf", html="%PDF-1.4", kind="pdf", content=b"x"),
    )

    out = _run(server.web_extract(URL, {"fields": {"title": {"selector": "h1"}}}))
    assert out["error_code"] == errors.UNSUPPORTED_CONTENT


# ---- 会话工具 ----


def test_session_list_payload_keys(monkeypatch, tmp_path):
    store = SessionStore(str(tmp_path))
    store.save("a.test", {"cookies": [{"name": "sid", "value": "1"}], "origins": []}, "work")
    monkeypatch.setattr(server, "_session_store", lambda: store)

    out = _run(server.session_list())
    assert set(out) == {"ok", "total", "sessions"}
    assert out["total"] == 1
    assert set(out["sessions"][0]) == {"session", "host", "updated_at"}


def test_session_export_payload_keys(monkeypatch, tmp_path):
    store = SessionStore(str(tmp_path))
    store.save("a.test", {"cookies": [{"name": "sid", "value": "1"}], "origins": []})
    monkeypatch.setattr(server, "_session_store", lambda: store)

    out = _run(server.session_export(URL))
    assert set(out) == {"ok", "url", "host", "session", "cookies", "cookie_count"}
    assert out["cookie_count"] == 1


def test_session_export_missing_state_is_invalid_argument(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_session_store", lambda: SessionStore(str(tmp_path)))
    out = _run(server.session_export(URL))
    assert out["error_code"] == errors.INVALID_ARGUMENT


def test_session_import_from_file_keys(monkeypatch, tmp_path):
    store = SessionStore(str(tmp_path))
    monkeypatch.setattr(server, "_session_store", lambda: store)
    source = tmp_path / "cookies.json"
    source.write_text(json.dumps({"cookies": [{"name": "sid", "value": "1"}]}), encoding="utf-8")

    out = _run(server.session_import(URL, str(source), session="work"))
    assert set(out) == {"ok", "url", "host", "session", "imported", "path", "message"}
    assert out["imported"] == 1
    assert store.load("a.test", "work")["cookies"][0]["name"] == "sid"


def test_session_import_rejects_file_without_cookies(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_session_store", lambda: SessionStore(str(tmp_path)))
    source = tmp_path / "bad.json"
    source.write_text('{"foo": 1}', encoding="utf-8")

    out = _run(server.session_import(URL, str(source)))
    assert out["error_code"] == errors.INVALID_ARGUMENT


def test_session_errors_reject_bad_session_name():
    out = _run(server.session_export(URL, session="../evil"))
    assert out["error_code"] == errors.INVALID_ARGUMENT
    assert out["retryable"] is False


# ---- web_extract ----


def test_web_extract_success_keys(monkeypatch):
    _patch_fetch(monkeypatch, _outcome())
    out = _run(server.web_extract(URL, {"fields": {"title": {"selector": "h1"}}}))
    assert set(out) == EXTRACT_OK_PAYLOAD
    assert out["data"]["title"] == "示例标题"
    assert out["missing"] == []


def test_web_extract_debug_adds_diagnostics(monkeypatch):
    _patch_fetch(monkeypatch, _outcome())
    out = _run(server.web_extract(URL, {"fields": {"title": {"selector": "h1"}}}, debug=True))
    assert set(out) == EXTRACT_OK_PAYLOAD | {"diagnostics"}
    assert set(out["diagnostics"]) == DIAG_DETAIL


def test_web_extract_invalid_schema_keys(monkeypatch):
    _patch_fetch(monkeypatch, _outcome())
    out = _run(server.web_extract(URL, {"fields": {"bad": {"type": "nope"}}}))
    assert set(out) == INVALID_PAYLOAD | {"url"}
    assert out["error_code"] == errors.INVALID_ARGUMENT


# ---- web_batch ----


def test_web_batch_top_level_keys(monkeypatch):
    _patch_fetch(monkeypatch, _outcome())
    out = _run(server.web_batch([URL, "https://a.test/y"]))
    assert set(out) == BATCH_OK_PAYLOAD
    assert set(out["results"][0]) == FETCH_OK_PAYLOAD


# ---- web_crawl ----


def test_web_crawl_zero_depth_keys(monkeypatch):
    _patch_fetch(monkeypatch, _outcome())
    out = _run(server.web_crawl(URL, depth=0))
    assert set(out) == CRAWL_OK_PAYLOAD
    assert set(out["results"][0]) == CRAWL_PAGE_PAYLOAD
    assert out["pages_fetched"] == 1
    assert out["mode"] == "links"


def test_web_crawl_paginate_keys(monkeypatch):
    _patch_fetch(monkeypatch, _outcome())
    out = _run(server.web_crawl(URL, next_selector="a.next", max_pages=3))
    assert set(out) == CRAWL_OK_PAYLOAD
    assert set(out["results"][0]) == CRAWL_PAGE_PAYLOAD
    assert out["mode"] == "paginate"


def test_web_extract_required_missing_payload(monkeypatch):
    _patch_fetch(monkeypatch, _outcome())
    out = _run(
        server.web_extract(
            URL,
            {"fields": {"absent": {"selector": ".nope", "required": True}}},
        )
    )
    assert set(out) == EXTRACT_OK_PAYLOAD | {"error_code", "error", "retryable"}
    assert out["ok"] is False
    assert out["error_code"] == errors.MISSING_REQUIRED
    assert out["retryable"] is False
    assert out["required_missing"] == ["absent"]


def test_web_extract_without_schema_or_recipe(monkeypatch):
    _patch_fetch(monkeypatch, _outcome())
    out = _run(server.web_extract(URL))
    assert set(out) == INVALID_PAYLOAD | {"url"}
    assert out["error_code"] == errors.INVALID_ARGUMENT
