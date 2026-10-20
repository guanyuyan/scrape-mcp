"""错误分类：把网络异常/拦截判定收敛成稳定错误码。"""

from scrape_mcp.core import errors
from scrape_mcp.core.fetcher import FetchOutcome


def _outcome(**kw) -> FetchOutcome:
    base = {
        "url": "https://a.test/",
        "final_url": "https://a.test/",
        "status": 200,
        "tier": "l1",
        "blocked": "none",
        "block_reason": "",
        "content_type": "text/html",
        "html": "<html><body>ok</body></html>",
        "elapsed_ms": 1,
    }
    base.update(kw)
    return FetchOutcome(**base)


def test_ok_on_clean_response():
    assert errors.classify_outcome(_outcome()) == errors.OK


def test_timeout_message():
    assert errors.classify_message("TimeoutError: Request timed out") == errors.TIMEOUT


def test_dns_message():
    assert errors.classify_message("Could not resolve host: nope.test") == errors.DNS


def test_tls_message():
    assert errors.classify_message("SSLError: certificate verify failed") == errors.TLS


def test_connection_message():
    assert errors.classify_message("ConnectionError: connection refused") == errors.CONNECTION


def test_outcome_error_wins_over_status():
    out = _outcome(status=0, html="", error="TimeoutError: timed out")
    assert errors.classify_outcome(out) == errors.TIMEOUT


def test_blocked_maps_to_blocked():
    out = _outcome(blocked="cloudflare", block_reason="cf")
    assert errors.classify_outcome(out) == errors.BLOCKED


def test_login_required_takes_priority():
    out = _outcome(blocked="captcha", block_reason="captcha", login_required=True)
    assert errors.classify_outcome(out) == errors.LOGIN_REQUIRED


def test_http_status_codes():
    assert errors.classify_outcome(_outcome(status=404)) == errors.HTTP_4XX
    assert errors.classify_outcome(_outcome(status=503)) == errors.HTTP_5XX


def test_empty_html():
    assert errors.classify_outcome(_outcome(html="   ")) == errors.EMPTY


def test_classify_exception():
    assert errors.classify_exception(RuntimeError("boom")) == errors.UNKNOWN
