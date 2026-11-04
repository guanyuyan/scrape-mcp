"""可观测性：request_id 贯穿日志与载荷、OTEL 关闭/缺依赖时的降级行为。"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from types import SimpleNamespace

import pytest

from scrape_mcp import server
from scrape_mcp.core import requestid, tracing
from scrape_mcp.core.logging import log_event


def _run(coro) -> dict:
    return json.loads(asyncio.run(coro))


def test_new_id_is_short_and_unique():
    ids = {requestid.new_id() for _ in range(500)}
    assert len(ids) == 500
    assert all(len(rid) == 12 and rid.isalnum() for rid in ids)


def test_scope_reuses_outer_id():
    with requestid.scope() as outer:
        with requestid.scope() as inner:
            assert inner == outer
        assert requestid.current() == outer
    assert requestid.current() is None


def test_scope_accepts_explicit_id():
    with requestid.scope("abc123") as rid:
        assert rid == "abc123"


def test_payload_carries_current_request_id():
    with requestid.scope() as rid:
        # 即使载荷里预置了旧值（如结果缓存命中的副本），也以本次调用为准
        out = json.loads(server._payload(ok=True, request_id="stale"))
        assert out["request_id"] == rid
    # 工具作用域之外不伪造 ID，避免把无关调用串到一起
    assert "request_id" not in json.loads(server._payload(ok=True))


def test_each_tool_call_gets_a_new_request_id():
    first = _run(server.web_fetch("ftp://a.test/x"))
    second = _run(server.web_fetch("ftp://a.test/x"))
    assert first["error_code"] == "invalid_url"
    assert first["request_id"] != second["request_id"]
    assert len(first["request_id"]) == 12


def test_log_event_includes_request_id():
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("scrape_mcp")
    handler = _Capture()
    logger.addHandler(handler)
    try:
        with requestid.scope():
            log_event("probe", level=logging.ERROR)
    finally:
        logger.removeHandler(handler)

    assert records and records[-1].fields.get("request_id")
    assert len(records[-1].fields["request_id"]) == 12


@pytest.fixture(autouse=True)
def _reset_tracing():
    tracing.shutdown()
    yield
    tracing.shutdown()


def test_tracing_disabled_by_default():
    settings = SimpleNamespace(otel_enabled=False, otel_endpoint="", otel_service_name="x")
    assert tracing.configure(settings) is False
    with tracing.span("probe") as span:
        assert span is None


def test_tracing_without_sdk_degrades_to_noop(monkeypatch):
    # 把 otel 顶层包置为 None 会让 import 抛 ImportError，模拟未安装可选依赖
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    settings = SimpleNamespace(
        otel_enabled=True, otel_endpoint="http://127.0.0.1:4318", otel_service_name="x"
    )
    assert tracing.configure(settings) is False
    with tracing.span("probe") as span:
        assert span is None
