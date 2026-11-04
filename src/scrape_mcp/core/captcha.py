"""可选的 CAPTCHA 服务钩子：默认关闭，只在显式配置端点后启用。

本模块不做验证码破解，也不内置任何求解服务。它只把"这一页检测到人机验证"
这一事实（URL、状态码、拦截类型，不含页面正文与凭证）转发给用户自己指定的
合规服务——例如人工值守台、已获授权的第三方 solver——并接受对方返回的 cookie
做一次重试。未配置 ``SCRAPE_MCP_CAPTCHA_SOLVER_URL`` 时整条链路完全不生效，
抓取行为与未实现该能力时一致。

合规提示：使用前请确认目标站条款与当地法律；本能力不允许用于大规模绕过
人机验证或批量注册。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from . import deadline as budget

# 返回的 cookie 数量与取值长度上限：防止错误配置的端点把异常数据灌进请求
_MAX_COOKIES = 20
_MAX_COOKIE_LEN = 1000


@dataclass
class HookResult:
    """一次钩子调用的结果；cookies 非空表示可以拿它做一次重试。"""

    ok: bool
    cookies: dict[str, str] = field(default_factory=dict)
    note: str = ""


def enabled(settings: Settings) -> bool:
    """是否配置了 CAPTCHA 服务端点（默认空 = 关闭）。"""
    return bool((settings.captcha_solver_url or "").strip())


def _parse_cookies(raw: Any) -> dict[str, str]:
    """只接受扁平字符串字典，并做数量/长度截断。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if len(out) >= _MAX_COOKIES:
            break
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        name = key.strip()
        if not name or len(name) > 128 or len(value) > _MAX_COOKIE_LEN:
            continue
        out[name] = value
    return out


async def request_solve(
    http: Any,
    settings: Settings,
    *,
    url: str,
    status: int,
    blocked: str,
    deadline: float | None = None,
) -> HookResult | None:
    """把挑战页信息转发给外部服务；未配置端点返回 None（调用方视为未启用）。

    失败（网络异常/非 JSON/ok=false）都收敛成 ``HookResult(ok=False, note=...)``，
    由调用方写进 notes，不影响已经拿到的 L1 结果。
    """
    endpoint = (settings.captcha_solver_url or "").strip()
    if not endpoint:
        return None
    headers = {"Accept": "application/json"}
    key = (settings.captcha_solver_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    timeout = budget.cap(deadline, max(1.0, float(settings.captcha_solver_timeout)))
    response = await http.post_json(
        endpoint,
        payload={"url": url, "status": int(status), "blocked": blocked},
        headers=headers,
        timeout=timeout,
    )
    if not response.ok:
        detail = (response.error or f"HTTP {response.status}").strip()[:160]
        return HookResult(ok=False, note=f"CAPTCHA 服务调用失败：{detail}")
    try:
        body = json.loads(response.text or "")
    except (TypeError, ValueError):
        return HookResult(ok=False, note="CAPTCHA 服务返回非 JSON 内容")
    if not isinstance(body, dict):
        return HookResult(ok=False, note="CAPTCHA 服务返回结构不是对象")
    cookies = _parse_cookies(body.get("cookies"))
    note = body.get("note")
    note = note.strip()[:200] if isinstance(note, str) else ""
    if not cookies:
        return HookResult(ok=False, note=note or "CAPTCHA 服务未返回可用 cookie")
    return HookResult(ok=True, cookies=cookies, note=note)
