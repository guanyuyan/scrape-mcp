"""请求关联 ID：每次工具调用生成一个短 ID，贯穿日志与返回载荷。

用 contextvar 而不是全局变量：并发工具调用各自独立；web_batch / web_crawl 的子任务
在同一调用任务内 await，天然继承外层 ID，不会出现父子串号。日志与载荷都从
``current()`` 取同一个值，调用方报障时只提供 ``request_id`` 即可定位整条链路。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# 12 位十六进制足够区分单机进程内的并发请求，又比 UUID 短，便于贴进日志与工单
_ID_LENGTH = 12

_current: ContextVar[str | None] = ContextVar("scrape_mcp_request_id", default=None)


def new_id() -> str:
    """生成一个新的 request_id。"""
    return uuid.uuid4().hex[:_ID_LENGTH]


def current() -> str | None:
    """当前请求的 ID；不在工具调用作用域内时返回 None。"""
    return _current.get()


@contextmanager
def scope(request_id: str | None = None) -> Iterator[str]:
    """建立一次请求作用域；已有外层作用域时沿用外层 ID（嵌套调用共享同一次请求）。"""
    existing = _current.get()
    rid = existing or (request_id or "").strip() or new_id()
    token = _current.set(rid)
    try:
        yield rid
    finally:
        _current.reset(token)
