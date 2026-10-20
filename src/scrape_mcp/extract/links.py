"""链接发现：从页面里抽出可继续抓取的候选 URL（供 web_crawl 使用）。

只做"发现"，不做抓取与去重调度——调度在 server.web_crawl 里按层推进，
这样链接规则可以独立测试，也避免把爬取策略耦进 DOM 解析。
"""

from __future__ import annotations

from urllib.parse import urldefrag, urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

# 静态资源/二进制附件：抓回来也榨不出正文，直接跳过
SKIP_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".ico",
        ".bmp",
        ".avif",
        ".css",
        ".js",
        ".mjs",
        ".json",
        ".xml",
        ".rss",
        ".zip",
        ".rar",
        ".7z",
        ".gz",
        ".tar",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".wmv",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".exe",
        ".dmg",
        ".apk",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
    }
)

_SKIP_PREFIXES = ("#", "javascript:", "mailto:", "tel:", "data:")


def extract_links(
    html: str,
    base_url: str,
    *,
    same_host: bool = True,
    limit: int = 200,
) -> list[str]:
    """按页面出现顺序返回去重后的候选链接。

    Args:
        html: 页面 HTML。
        base_url: 用于解析相对链接的基准页（通常是 final_url）。
        same_host: 只保留与基准页同主机的链接。
        limit: 最多返回多少条，防止大列表页把队列撑爆。
    """
    if limit <= 0 or not html:
        return []
    host = urlparse(base_url).netloc.lower()
    tree = HTMLParser(html)
    seen: set[str] = set()
    out: list[str] = []

    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.lower().startswith(_SKIP_PREFIXES):
            continue
        target, _fragment = urldefrag(urljoin(base_url, href))
        parsed = urlparse(target)
        if parsed.scheme not in ("http", "https"):
            continue
        if same_host and parsed.netloc.lower() != host:
            continue
        if any(parsed.path.lower().endswith(ext) for ext in SKIP_EXTENSIONS):
            continue
        if target in seen:
            continue
        seen.add(target)
        out.append(target)
        if len(out) >= limit:
            break
    return out


def next_link(
    html: str,
    base_url: str,
    selector: str,
    *,
    same_host: bool = True,
) -> str | None:
    """按选择器取"下一页"链接（分页爬取用）；无命中/非法返回 None。"""
    if not html or not selector:
        return None
    try:
        node = HTMLParser(html).css_first(selector)
    except Exception:
        return None
    if node is None:
        return None

    href = (node.attributes.get("href") or node.attributes.get("data-href") or "").strip()
    if not href or href.lower().startswith(_SKIP_PREFIXES):
        return None
    target, _fragment = urldefrag(urljoin(base_url, href))
    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https"):
        return None
    if same_host and parsed.netloc.lower() != urlparse(base_url).netloc.lower():
        return None
    return target
