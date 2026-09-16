"""拦截判定：识别响应是"正常页面"还是"反爬挑战/空壳页"。

这是分级升级链路的开关。判定错了，要么白跑浏览器（慢），要么把挑战页当成正文喂给模型（脏）。
"""

from __future__ import annotations

import re
from typing import Literal

BlockType = Literal[
    "none",
    "cloudflare",
    "captcha",
    "waf",
    "rate_limit",
    "empty_spa",
    "empty",
]

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
# script/style 的源码在剥掉标签后会留下来被当成"可见文本"计数，
# 于是 JS 重的空壳页反而显得内容很多，被漏判成正常页面
_OPAQUE_BLOCK = re.compile(r"<(script|style|noscript|template|svg)\b[^>]*>.*?</\1\s*>", re.S | re.I)

_CLOUDFLARE_MARKERS = (
    "just a moment",
    "cf-chl",
    "cf_chl_opt",
    "checking your browser",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
)
_CAPTCHA_MARKERS = (
    "验证码",
    "人机验证",
    "安全验证",
    "滑动验证",
    "拖动滑块",
    "verify you are human",
    "geetest",
    "recaptcha",
    "hcaptcha",
)
_SPA_SHELL_MARKERS = (
    '<div id="app"></div>',
    "<div id=app></div>",
    '<div id="root"></div>',
    "<div id=root></div>",
)

# 可见文本低于该值视为无有效内容
_MIN_VISIBLE_CHARS = 200
# 验证码特征词出现在长文里多半是文章本身在讲验证码，不做为挑战判定
# 挑战页的可见文本通常极短，1500 字以上基本可排除
_CAPTCHA_MAX_VISIBLE_CHARS = 1500


def visible_text_length(html: str) -> int:
    return len(_WS.sub("", _TAG.sub("", _OPAQUE_BLOCK.sub("", html))))


# HTML 体积不小却几乎没有可见文本 → 骨架页/登录墙（正文靠 JS 拉），值得交给浏览器渲染。
# 阈值取自实测：正常页面可见文本 5696~21742 字，SPA/登录墙 17~1019 字，2000 与 30KB
# 落在两者的空档里。代价只是给少数短小页面白跑一次浏览器（正文不会错，只多几秒）。
_SHELL_MIN_HTML = 30_000
_SHELL_MAX_VISIBLE = 2000


def looks_like_shell(html: str) -> bool:
    return len(html) >= _SHELL_MIN_HTML and visible_text_length(html) < _SHELL_MAX_VISIBLE


def detect_block(status: int, html: str, content_type: str = "") -> tuple[BlockType, str]:
    low = html[:200_000].lower()
    visible = visible_text_length(html)

    for marker in _CLOUDFLARE_MARKERS:
        if marker in low:
            return "cloudflare", f"命中 Cloudflare 挑战特征: {marker}"
    if status == 429:
        return "rate_limit", "HTTP 429 请求过于频繁"
    if status in (401, 403, 451):
        return "waf", f"HTTP {status} 被拦截"
    if status in (503, 521, 522, 1020):
        return "cloudflare", f"HTTP {status}（Cloudflare 系）"
    if status >= 500:
        return "waf", f"HTTP {status} 服务端异常"
    for marker in _CAPTCHA_MARKERS:
        if marker in low and visible < _CAPTCHA_MAX_VISIBLE_CHARS:
            return "captcha", f"命中验证码特征: {marker}"
    if visible < _MIN_VISIBLE_CHARS:
        if any(s in low for s in _SPA_SHELL_MARKERS):
            return "empty_spa", f"可见文本仅 {visible} 字，疑似 JS 渲染空壳页"
        return "empty", f"可见文本仅 {visible} 字，疑似空页"
    return "none", ""
