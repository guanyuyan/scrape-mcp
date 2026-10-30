"""搜索：把关键词变成结构化结果列表（标题 / URL / 摘要）。

只对接搜索服务本身，不抓结果页正文。默认后端是 DuckDuckGo 的 HTML 端点，
无需 API key；也可指向自托管 SearXNG 的 JSON API。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

PROVIDERS = ("duckduckgo", "searxng")
DUCKDUCKGO_ENDPOINT = "https://html.duckduckgo.com/html/"


class SearchError(ValueError):
    """结果页无法解析（结构变化，或返回了不符合预期的内容形态）。"""


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


def search_url(query: str, provider: str, *, endpoint: str = "") -> str:
    """按后端拼出请求 URL；SearXNG 的 endpoint 来自配置。"""
    q = quote_plus(query)
    if provider == "searxng":
        return f"{(endpoint or '').rstrip('/')}/search?q={q}&format=json&language=all"
    return f"{DUCKDUCKGO_ENDPOINT}?q={q}"


def parse_results(provider: str, body: str, max_results: int) -> list[SearchHit]:
    """把服务端返回解析成结果列表；解析不了就抛 SearchError，不静默返回空。"""
    if provider == "searxng":
        return _parse_searxng(body, max_results)
    return _parse_duckduckgo(body, max_results)


def _parse_searxng(body: str, max_results: int) -> list[SearchHit]:
    text = (body or "").strip()
    if not text.startswith("{"):
        # 有的出口会按 Accept 把 JSON 包进 <pre> 里返回（text/html），先把 JSON 抠出来
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SearchError(f"SearXNG 返回的不是 JSON: {exc}") from exc
    raw = data.get("results") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise SearchError("SearXNG 响应缺少 results 数组")
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        title = _clean(item.get("title")) or url
        snippet = _clean(item.get("content") or item.get("snippet"))
        hits.append(SearchHit(title=title, url=url, snippet=snippet))
        if len(hits) >= max_results:
            break
    return hits


def _parse_duckduckgo(body: str, max_results: int) -> list[SearchHit]:
    # 反爬挑战页（HTTP 202 + anomaly.js / anomaly-modal）没有任何结果卡片，
    # 不能当成"搜到了 0 条"，否则上层会把拦截误报成空结果
    if "anomaly.js" in body or "anomaly-modal" in body:
        raise SearchError("DuckDuckGo 返回人机验证页，请更换出口或改用 SearXNG")
    tree = HTMLParser(body)
    hits: list[SearchHit] = []
    seen: set[str] = set()

    for card in tree.css("div.result"):
        classes = card.attributes.get("class") or ""
        if "result--ad" in classes:
            continue  # 广告位不是自然结果
        link = card.css_first("a.result__a")
        if link is None:
            continue
        url = _ddg_target(link.attributes.get("href") or "")
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        snippet_node = card.css_first(".result__snippet")
        hits.append(
            SearchHit(
                title=_clean(link.text()) or url,
                url=url,
                snippet=_clean(snippet_node.text()) if snippet_node is not None else "",
            )
        )
        if len(hits) >= max_results:
            return hits

    # 兼容没有结果卡片容器的版本：退化为只取标题与链接，摘要留空
    if hits:
        return hits
    for link in tree.css("a.result__a"):
        url = _ddg_target(link.attributes.get("href") or "")
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        hits.append(SearchHit(title=_clean(link.text()) or url, url=url, snippet=""))
        if len(hits) >= max_results:
            break
    return hits


def _ddg_target(href: str) -> str:
    """还原 DuckDuckGo 的跳转链接（/l/?uddg=<encoded>）为真实目标地址。"""
    href = (href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("/"):
        href = "https://duckduckgo.com" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in (parsed.netloc or "").lower() and parsed.path.rstrip("/") == "/l":
        target = (parse_qs(parsed.query).get("uddg") or [""])[0]
        if target:
            return target
    return href


def _clean(text: object) -> str:
    return " ".join(str(text or "").split())
