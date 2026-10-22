"""HTTP 访问控制：Bearer 令牌、回环兜底与绑定校验。"""

from __future__ import annotations

import asyncio
import json

import pytest

from scrape_mcp.http_auth import BearerAuthMiddleware, ensure_http_access

TOKEN = "s3cret-token"


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


def _call(
    token: str | None,
    *,
    path: str = "/mcp",
    headers: tuple[tuple[bytes, bytes], ...] = (),
    client: tuple[str, int] | None = ("127.0.0.1", 5000),
    kind: str = "http",
):
    scope = {
        "type": kind,
        "path": path,
        "headers": list(headers),
        "client": client,
        "state": {},
    }
    sent: list[dict] = []

    async def app(scope, receive, send):
        scope["reached"] = True
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        sent.append(message)

    asyncio.run(BearerAuthMiddleware(app, token)(scope, _receive, send))
    return scope, sent


def _start(sent: list[dict]) -> dict:
    return next(item for item in sent if item["type"] == "http.response.start")


def _body(sent: list[dict]) -> dict:
    raw = next(item["body"] for item in sent if item["type"] == "http.response.body")
    return json.loads(raw)


def test_missing_token_is_rejected_with_401():
    scope, sent = _call(TOKEN, client=("10.0.0.5", 5000))

    assert _start(sent)["status"] == 401
    assert (b"www-authenticate", b'Bearer realm="scrape-mcp"') in _start(sent)["headers"]
    assert _body(sent) == {"ok": False, "error": "unauthorized"}
    assert scope.get("reached") is None


def test_wrong_token_is_rejected_with_401():
    headers = ((b"authorization", b"Bearer wrong-token"),)
    scope, sent = _call(TOKEN, headers=headers, client=("10.0.0.5", 5000))

    assert _start(sent)["status"] == 401
    assert scope.get("reached") is None


def test_valid_bearer_token_passes():
    headers = ((b"authorization", f"Bearer {TOKEN}".encode()),)
    scope, sent = _call(TOKEN, headers=headers, client=("10.0.0.5", 5000))

    assert _start(sent)["status"] == 200
    assert scope["reached"] is True
    assert scope["state"]["scrape_mcp_authenticated"] is True


def test_loopback_still_requires_token_when_configured():
    """避免本机反向代理把外部请求变成 127.0.0.1 后绕过鉴权。"""
    scope, sent = _call(TOKEN, client=("127.0.0.1", 5000))

    assert _start(sent)["status"] == 401
    assert scope.get("reached") is None


def test_bearer_scheme_is_case_insensitive():
    headers = ((b"authorization", f"bEaReR {TOKEN}".encode()),)
    _, sent = _call(TOKEN, headers=headers, client=("10.0.0.5", 5000))

    assert _start(sent)["status"] == 200


def test_healthz_is_exempt_from_token():
    scope, sent = _call(TOKEN, path="/healthz", client=("10.0.0.5", 5000))

    assert _start(sent)["status"] == 200
    assert scope["reached"] is True


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"])
def test_loopback_passes_without_token(host):
    scope, sent = _call(None, client=(host, 5000))

    assert _start(sent)["status"] == 200
    assert scope["reached"] is True


def test_remote_client_is_rejected_without_token():
    scope, sent = _call(None, client=("10.0.0.5", 5000))

    assert _start(sent)["status"] == 403
    assert _body(sent) == {"ok": False, "error": "forbidden"}
    assert scope.get("reached") is None


def test_missing_client_is_rejected_without_token():
    _, sent = _call(None, client=None)

    assert _start(sent)["status"] == 403


def test_whitespace_token_is_treated_as_unconfigured():
    _, sent = _call("   ", client=("10.0.0.5", 5000))

    assert _start(sent)["status"] == 403


def test_non_http_scope_passes_through():
    scope, sent = _call(TOKEN, kind="lifespan", client=None)

    assert scope["reached"] is True
    assert sent == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_binding_loopback_without_token_is_allowed(host):
    ensure_http_access(host, None)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10"])
def test_binding_public_address_without_token_is_rejected(host):
    with pytest.raises(SystemExit) as exc:
        ensure_http_access(host, None)

    assert "SCRAPE_MCP_HTTP_TOKEN" in str(exc.value)


def test_binding_public_address_with_token_is_allowed():
    ensure_http_access("0.0.0.0", TOKEN)
