"""结构化日志：每行一条 JSON，统一写 stderr。

stdio 传输下 stdout 是 MCP 协议通道，任何日志都不能写进去，否则会污染握手/响应。
字段固定为 ts / level / event，另加调用方传入的结构化字段。
"""

from __future__ import annotations

import json
import logging as _logging
import sys
from datetime import datetime, timezone

_LOGGER_NAME = "scrape_mcp"
_logger = _logging.getLogger(_LOGGER_NAME)


class _JsonFormatter(_logging.Formatter):
    def format(self, record: _logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure(level: str = "WARNING") -> None:
    """安装 stderr JSON handler。重复调用会替换旧 handler，避免日志重复。"""
    _logger.setLevel(getattr(_logging, (level or "").upper(), _logging.WARNING))
    handler = _logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    _logger.handlers = [handler]
    _logger.propagate = False


def log_event(event: str, *, level: int = _logging.INFO, **fields) -> None:
    _logger.log(level, event, extra={"fields": fields})
