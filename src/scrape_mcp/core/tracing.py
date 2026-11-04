"""可选的 OpenTelemetry 链路追踪：未开启或未安装 SDK 时全部退化为空操作。

默认关闭，避免给普通用户引入额外依赖与后台导出线程；开启后每次抓取产生一个
``fetch`` span，工具调用本身是 ``tool.<name>`` span，属性里带 request_id，可与日志
和载荷对齐。缺少依赖时只记一条警告，不影响抓取主流程。
"""

from __future__ import annotations

import logging as _logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .logging import log_event

_lock = threading.Lock()
_configured = False
_provider: Any | None = None
_tracer: Any | None = None


def _configure(settings: Any) -> Any | None:
    """真正初始化 tracer；失败（缺依赖/导出器）时返回 None。调用方须持有 _lock。"""
    global _provider, _tracer
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log_event(
            "otel_unavailable",
            level=_logging.WARNING,
            hint='启用 OTEL 需先安装可选依赖：pip install "mcp-scrape[otel]"',
        )
        return None

    endpoint = (getattr(settings, "otel_endpoint", "") or "").strip()
    if not endpoint:
        endpoint = "http://127.0.0.1:4318"
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": getattr(settings, "otel_service_name", "scrape-mcp") or "scrape-mcp"}
        )
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _provider = provider
    _tracer = trace.get_tracer("scrape_mcp")
    log_event("otel_enabled", endpoint=endpoint)
    return _tracer


def configure(settings: Any | None = None) -> bool:
    """按配置初始化追踪；重复调用只生效一次，返回是否真正启用。"""
    global _configured
    if _configured:
        return _tracer is not None
    with _lock:
        if _configured:
            return _tracer is not None
        _configured = True
        if settings is None:
            from ..config import get_settings

            settings = get_settings()
        if not getattr(settings, "otel_enabled", False):
            return False
        return _configure(settings) is not None


def enabled() -> bool:
    """追踪是否可用；未初始化时按当前配置懒加载一次。"""
    return _tracer is not None or configure()


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any | None]:
    """开启一个 span；未启用时 yield None，调用方无需分支即可 set_attribute。"""
    if not enabled() or _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as current:
        for key, value in attrs.items():
            if value is not None:
                current.set_attribute(key, value)
        yield current


def shutdown() -> None:
    """刷出并关闭导出器；进程退出/测试用例之间调用。"""
    global _configured, _provider, _tracer
    with _lock:
        provider = _provider
        _provider = None
        _tracer = None
        _configured = False
    if provider is not None:
        provider.shutdown()
