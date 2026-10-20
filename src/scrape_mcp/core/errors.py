"""统一错误分类：把各种失败收敛成稳定的错误码，供调用方程序化决策。

调用方不该靠解析中文错误文本来判断"是超时还是被拦"，这里给出稳定的 code，
文本仍保留给人看。code 一旦发布就视为协议的一部分，只增不改。
"""

from __future__ import annotations

from .fetcher import FetchOutcome

# 网络层异常关键词 → 错误码。按从具体到宽泛的顺序匹配
_TIMEOUT_HINTS = ("timeout", "timed out", "time out", "read timeout")
_DNS_HINTS = (
    "could not resolve",
    "name or service not known",
    "getaddrinfo",
    "nodename nor servname",
    "temporary failure in name resolution",
    "no address associated",
)
_TLS_HINTS = ("ssl", "certificate", "tls", "handshake")
_CONNECTION_HINTS = ("connection", "refused", "reset by peer", "proxy", "connect")

OK = "ok"
INVALID_URL = "invalid_url"
INVALID_ARGUMENT = "invalid_argument"
DENIED = "denied"
ROBOTS_DISALLOWED = "robots_disallowed"
LOGIN_REQUIRED = "login_required"
BLOCKED = "blocked"
TIMEOUT = "timeout"
DNS = "dns"
TLS = "tls"
CONNECTION = "connection"
HTTP_4XX = "http_4xx"
HTTP_5XX = "http_5xx"
MISSING_REQUIRED = "missing_required"
UNSUPPORTED_CONTENT = "unsupported_content"
EMPTY = "empty"
UNKNOWN = "unknown"

# 协议中对外承诺的全部错误码（只增不改），用于契约测试与文档同步
ALL_CODES = (
    OK,
    INVALID_URL,
    INVALID_ARGUMENT,
    DENIED,
    ROBOTS_DISALLOWED,
    LOGIN_REQUIRED,
    BLOCKED,
    TIMEOUT,
    DNS,
    TLS,
    CONNECTION,
    HTTP_4XX,
    HTTP_5XX,
    MISSING_REQUIRED,
    UNSUPPORTED_CONTENT,
    EMPTY,
    UNKNOWN,
)

# "过一会儿再试可能成功"的错误码。拦截/登录墙/被拒绝重试无意义，不计入
_RETRYABLE_CODES = frozenset({TIMEOUT, DNS, CONNECTION, TLS, HTTP_5XX, UNKNOWN})


def is_retryable(code: str) -> bool:
    """调用方是否值得稍后重试。与抓取层内部的重试策略分开表达，避免调用方猜。"""
    return code in _RETRYABLE_CODES


def classify_message(message: str | None) -> str:
    """按异常文本推断错误码（网络异常没有结构化类型，只能靠关键词）。"""
    msg = (message or "").lower()
    if not msg:
        return UNKNOWN
    if any(h in msg for h in _TIMEOUT_HINTS):
        return TIMEOUT
    if any(h in msg for h in _DNS_HINTS):
        return DNS
    if any(h in msg for h in _TLS_HINTS):
        return TLS
    if any(h in msg for h in _CONNECTION_HINTS):
        return CONNECTION
    return UNKNOWN


def classify_outcome(outcome: FetchOutcome) -> str:
    """给一次抓取结果定错误码；成功返回 ``ok``。"""
    if outcome.error:
        return classify_message(outcome.error)
    if outcome.blocked != "none":
        return LOGIN_REQUIRED if outcome.login_required else BLOCKED
    if outcome.status >= 500:
        return HTTP_5XX
    if outcome.status >= 400:
        return HTTP_4XX
    if not outcome.html.strip():
        return EMPTY
    return OK


def classify_exception(exc: BaseException) -> str:
    return classify_message(f"{type(exc).__name__}: {exc}")
