"""fit_markdown：在 compact 之后继续按正文密度剔除低价值块。

compact 已按标签丢弃 nav/footer/aside，但真实页面仍会把导航链接、评论区、
"相关阅读"、广告位与备案信息渲染成正文行。本模块只做行级裁剪：

- 代码围栏内一律保留，避免把示例代码里的关键字误删
- "评论 / 相关阅读 / 广告"这类小标题会连同其后的段落一起丢弃，直到下一个标题
- 纯链接堆叠行（导航）与版权/备案行（页脚）按特征丢弃
- 裁剪过度时（剩余不足原文 30% 且原文较长）放弃裁剪，保住正文

同时提供 token_report：让"省了多少"变成调用方可验证的数字。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CATEGORIES = ("nav", "footer", "comments", "related", "ads")

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_LINK = re.compile(r"\[[^\]]*\]\([^)]*\)")
_FENCE = "```"

_SECTION_HINTS = {
    "comments": re.compile(r"(评论|留言|Comments?|Discussion|读者反馈)", re.IGNORECASE),
    "related": re.compile(
        r"(相关阅读|相关推荐|相关文章|推荐文章|推荐阅读|猜你喜欢|热门文章|热门推荐|"
        r"延伸阅读|You (?:may|might) also like|Related|Recommended)",
        re.IGNORECASE,
    ),
    "ads": re.compile(r"(广告|赞助|Sponsored|Advertisement)", re.IGNORECASE),
}
_LINE_HINTS = {
    "ads": re.compile(r"(广告|赞助|Sponsored|Advertisement)", re.IGNORECASE),
    "footer": re.compile(
        r"(©|版权|备案|ICP备|All rights reserved|Privacy Policy|Terms of Service|"
        r"隐私政策|服务条款|联系我们)",
        re.IGNORECASE,
    ),
}
# 只对短行做页脚/广告判定：长正文里出现"联系我们"往往只是句子的一部分
_SHORT_LINE = 200
# 裁剪后至少保留原文的这个比例，防止误杀整页正文
_MIN_KEEP_RATIO = 0.3
_MIN_GUARD_CHARS = 400


@dataclass
class FitResult:
    """裁剪结果；applied=False 表示触发了保护逻辑，返回的是原文。"""

    content: str
    dropped: dict[str, int] = field(default_factory=lambda: dict.fromkeys(CATEGORIES, 0))
    applied: bool = True


def _empty_dropped() -> dict[str, int]:
    return dict.fromkeys(CATEGORIES, 0)


def _link_count(line: str) -> int:
    return len(_LINK.findall(line))


def _text_without_links(line: str) -> str:
    return _LINK.sub("", line).strip()


def _classify_section(title: str) -> str | None:
    """小标题是否开启一个应当整体丢弃的区块。"""
    text = title.strip()
    for kind in ("comments", "related", "ads"):
        if _SECTION_HINTS[kind].search(text):
            return kind
    return None


def _classify_line(line: str) -> str | None:
    """单行层面的低价值特征：导航链接堆、页脚、广告。"""
    if len(line) <= _SHORT_LINE:
        for kind in ("ads", "footer"):
            if _LINE_HINTS[kind].search(_text_without_links(line) or line):
                return kind
    links = _link_count(line)
    if links >= 3 and len(_text_without_links(line)) <= 40:
        return "nav"
    if links >= 2 and len(_text_without_links(line)) <= 16:
        return "nav"
    return None


def fit_markdown(content: str) -> FitResult:
    """按行剔除导航/页脚/评论/推荐/广告；拿不准时宁可不裁。"""
    text = content or ""
    if not text.strip():
        return FitResult(content=text, dropped=_empty_dropped(), applied=False)

    out: list[str] = []
    dropped = _empty_dropped()
    in_fence = False
    drop_section: str | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(_FENCE):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        if not stripped:
            out.append("")
            continue

        heading = _HEADING.match(stripped)
        if heading is not None:
            section = _classify_section(heading.group(2))
            drop_section = section
            if section is not None:
                dropped[section] += 1
                continue
            out.append(line)
            continue

        if drop_section is not None:
            dropped[drop_section] += 1
            continue

        kind = _classify_line(stripped)
        if kind is not None:
            dropped[kind] += 1
            continue
        out.append(line)

    fitted = "\n".join(out).strip()
    if not fitted:
        return FitResult(content=text, dropped=_empty_dropped(), applied=False)
    if len(text) >= _MIN_GUARD_CHARS and len(fitted) < len(text) * _MIN_KEEP_RATIO:
        # 裁掉太多说明特征命中了正文，退回原文更安全
        return FitResult(content=text, dropped=_empty_dropped(), applied=False)
    return FitResult(content=fitted, dropped=dropped, applied=True)


def build_token_report(
    *,
    html_chars: int,
    content: str,
    tokens: int,
    dropped: dict[str, int],
    fit: bool,
) -> dict:
    """生成可验证的压缩报告：原始 HTML 体积 → 实际输出体积 / token 估算。"""
    output_chars = len(content or "")
    return {
        "raw_chars": max(0, int(html_chars)),
        "output_chars": output_chars,
        "output_tokens_est": max(0, int(tokens)),
        "compression_ratio": round(output_chars / html_chars, 4) if html_chars > 0 else 0.0,
        "dropped": {key: int(dropped.get(key, 0)) for key in CATEGORIES},
        "fit": bool(fit),
    }
