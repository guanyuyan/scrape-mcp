"""单次调用的时间预算：把 ``deadline_ms`` 换算成绝对截止时间，供各层逐级降级。

预算不是"到点掐断"，而是"不够就少做一步"：剩余时间被压进单步超时，
不足时跳过重试、跳过 L2 升级，尽量把已经拿到的结果交回调用方。
"""

from __future__ import annotations

import time


def start(ms: float | None) -> float | None:
    """毫秒预算 → 绝对截止时间（monotonic）；None 或 <=0 表示不限。"""
    if ms is None or ms <= 0:
        return None
    return time.monotonic() + ms / 1000


def remaining(deadline: float | None) -> float | None:
    """剩余秒数；没有预算时返回 None（与"剩 0 秒"区分开）。"""
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def expired(deadline: float | None) -> bool:
    left = remaining(deadline)
    return left is not None and left <= 0


def cap(deadline: float | None, timeout: float) -> float:
    """把单步超时压到剩余时间内，避免一步就把整个预算耗光。"""
    left = remaining(deadline)
    if left is None:
        return timeout
    return max(0.05, min(timeout, left))
