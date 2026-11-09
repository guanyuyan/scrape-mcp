"""挑战页诊断：从拦截页里分辨"哪家厂商、什么类型、需要什么参数"。

本模块只做只读识别，不改既有 ``blocked`` 取值：
- ``detector.detect_block`` 决定"拦没拦"，本模块补充"为什么拦、拦在哪一步"；
- 识别出的 ``sitekey`` / ``action`` 只用于下发给你自己配置的求解服务，
  不内置任何打码平台，也不做验证码破解。

特征相对厂商改版较脆弱，故全部集中在本文件，便于用夹具（tests/test_challenge.py）
定期校准；无法归类时返回 ``None``，调用方按无挑战处理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .detector import looks_like_login_wall

# 只读前 200 KB：挑战特征都在文档头部，读全文只会被长文里的"验证码"教程误伤
_MAX_HTML = 200_000

# 厂商识别顺序：先匹配更具体的（Turnstile 属于 Cloudflare 家族，必须先判）
# 每个厂商给出若干特征词，命中第一个即采用。
_PROVIDER_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("turnstile", ("cf-turnstile", "challenges.cloudflare.com/turnstile", "turnstile.render")),
    ("datadome", ("datadome", "dd_cookie", "captcha-delivery.com", "geo.captcha-delivery")),
    ("perimeterx", ("perimeterx", "px-captcha", "_pxhd", "px-cloud.net", "px-cdn.net")),
    ("akamai", ("_abck", "ak_bmsc", "bm-verify", "sec-cpt", "akamai bot manager")),
    (
        "cloudflare",
        (
            "just a moment",
            "cf-chl",
            "cf_chl_opt",
            "__cf_chl",
            "checking your browser",
            "attention required! | cloudflare",
            "needs to review the security of your connection",
        ),
    ),
    ("hcaptcha", ("hcaptcha", "h-captcha")),
    ("recaptcha", ("recaptcha", "g-recaptcha", "grecaptcha")),
)

# 验证码类厂商：即使没有 sitekey 也按 captcha 归类
_CAPTCHA_PROVIDERS = frozenset({"recaptcha", "hcaptcha", "turnstile"})

# 页面上出现这些词时按验证码处理（与 detector 的口径一致，只是用于细分 kind）
_CAPTCHA_HINTS = (
    "验证码",
    "人机验证",
    "安全验证",
    "滑动验证",
    "拖动滑块",
    "verify you are human",
    "captcha",
)

# 插页式（interstitial）特征：整页挡住、渲染后由 JS 自动放行
_INTERSTITIAL_HINTS = (
    "just a moment",
    "checking your browser",
    "attention required",
    "please wait",
)

_SITEKEY_PATTERNS = (
    re.compile(r"""data-sitekey\s*=\s*["']([A-Za-z0-9_-]{6,200})["']""", re.I),
    re.compile(r"""sitekey["']?\s*[:=]\s*["']([A-Za-z0-9_-]{6,200})["']""", re.I),
    # reCAPTCHA v3 的 render= 查询参数就是 sitekey
    re.compile(r"""\brender\s*=\s*["']?([A-Za-z0-9_-]{20,})["']?""", re.I),
)

_ACTION_PATTERN = re.compile(r"""["']?action["']?\s*[:=]\s*["']([A-Za-z0-9_\-/]{1,64})["']""", re.I)


@dataclass(frozen=True)
class ChallengeInfo:
    """一次挑战页识别的结果；字段按"诊断 + 求解服务入参"设计。"""

    provider: str
    kind: str
    sitekey: str | None = None
    action: str | None = None
    status: int = 0
    retry_after: float | None = None
    evidence: str = ""

    def as_dict(self, *, mask_sitekey: bool = False) -> dict[str, Any]:
        """转成可序列化字典；``mask_sitekey=True`` 时只保留前 6 位。"""
        sitekey = self.sitekey
        if mask_sitekey and sitekey:
            sitekey = sitekey[:6]
        return {
            "provider": self.provider,
            "kind": self.kind,
            "sitekey": sitekey,
            "action": self.action,
            "status": self.status,
            "retry_after": self.retry_after,
            "evidence": self.evidence,
        }


def _find_provider(low: str) -> tuple[str | None, str]:
    """返回 (厂商, 命中的特征词)；都没有命中时厂商为 None。"""
    for provider, markers in _PROVIDER_MARKERS:
        for marker in markers:
            if marker in low:
                return provider, marker
    return None, ""


def _find_sitekey(raw: str) -> str | None:
    for pattern in _SITEKEY_PATTERNS:
        match = pattern.search(raw)
        if match:
            return match.group(1)
    return None


def _find_action(raw: str) -> str | None:
    match = _ACTION_PATTERN.search(raw)
    return match.group(1) if match else None


def _classify_kind(provider: str, low: str, sitekey: str | None) -> str:
    """按厂商/页面特征细分挑战类型。"""
    if provider in _CAPTCHA_PROVIDERS or sitekey:
        return "captcha"
    if any(hint in low for hint in _CAPTCHA_HINTS):
        return "captcha"
    if any(hint in low for hint in _INTERSTITIAL_HINTS):
        return "interstitial"
    return "js_challenge"


def analyze(
    html: str,
    status: int = 0,
    headers: dict[str, str] | None = None,
    *,
    retry_after: float | None = None,
) -> ChallengeInfo | None:
    """识别挑战页；无法归类返回 ``None``。

    识别范围：
    - 厂商特征（Cloudflare / Turnstile / DataDome / Akamai / PerimeterX / reCAPTCHA / hCaptcha）
    - 429 限流（provider 尽量识别，识别不到为 ``unknown``）
    - 401/403 且页面给出登录入口 → ``login_wall``

    普通 403/404/5xx 错误页不带任何挑战特征，返回 ``None``：
    这类拒绝换出口或等冷却才有用，不需要求解服务。
    """
    if not html:
        return None
    # 特征词用小写匹配；sitekey/action 取值必须保留原始大小写（它们是区分大小写的凭证）
    raw = html[:_MAX_HTML]
    low = raw.lower()
    provider, evidence = _find_provider(low)

    if status == 429:
        return ChallengeInfo(
            provider=provider or "unknown",
            kind="rate_limit",
            status=status,
            retry_after=retry_after,
            evidence=_header_evidence(headers) or evidence or "HTTP 429",
        )
    if provider is None:
        if status in (401, 403) and looks_like_login_wall(html):
            return ChallengeInfo(
                provider="unknown",
                kind="login_wall",
                status=status,
                evidence=_header_evidence(headers) or f"HTTP {status} 且含登录入口",
            )
        return None

    sitekey = _find_sitekey(raw)
    return ChallengeInfo(
        provider=provider,
        kind=_classify_kind(provider, low, sitekey),
        sitekey=sitekey,
        action=_find_action(raw),
        status=status,
        retry_after=retry_after,
        evidence=_header_evidence(headers) or f"命中 {provider} 特征: {evidence}",
    )


def _header_evidence(headers: dict[str, str] | None) -> str:
    """从响应头里找可佐证挑战的线索（如 server / cf-ray / x-datadome）。"""
    if not headers:
        return ""
    wanted = ("server", "cf-ray", "x-datadome", "x-px", "x-akamai")
    parts = []
    for key, value in headers.items():
        if key.lower() in wanted:
            parts.append(f"{key}={value}")
    return "；".join(parts)[:160]


__all__ = ["ChallengeInfo", "analyze"]
