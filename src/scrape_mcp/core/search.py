"""搜索：把关键词变成结构化结果列表（标题 / URL / 摘要 / 来源 / 发布时间）。

只对接搜索服务本身，不抓结果页正文。默认后端是 DuckDuckGo 的 HTML 端点，
无需 API key；也可指向自托管 SearXNG 的 JSON API，或用配置里的 key 走
Brave / Tavily 的商业 API。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

PROVIDERS = ("duckduckgo", "searxng", "brave", "tavily")
DUCKDUCKGO_ENDPOINT = "https://html.duckduckgo.com/html/"
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
TAVILY_ENDPOINT = "https://api.tavily.com/search"

# 对外统一的时间范围取值；各后端再映射成自己的参数
TIME_RANGES = ("day", "week", "month", "year")
_DDG_DF = {"day": "d", "week": "w", "month": "m", "year": "y"}
_BRAVE_FRESHNESS = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
_BRAVE_COUNT_MAX = 20
_TAVILY_MAX_RESULTS = 20


class SearchError(ValueError):
    """结果页无法解析（结构变化，或返回了不符合预期的内容形态）。"""


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    # 结果来自哪个引擎（SearXNG 聚合多个引擎时可逐条不同）；未知留空
    engine: str = ""
    # 发布时间，按后端原样透传（ISO 日期或 "2 days ago" 这类相对描述）；未知留空
    published_at: str = ""


def _query_with_site(query: str, site: str) -> str:
    """把站点过滤拼成搜索引擎都认的 ``site:`` 运算符，包含在查询串里真实生效。"""
    text = query.strip()
    return f"{text} site:{site}" if site else text


def search_url(
    query: str,
    provider: str,
    *,
    endpoint: str = "",
    site: str = "",
    time_range: str = "",
    engines: list[str] | None = None,
    limit: int = _BRAVE_COUNT_MAX,
) -> str:
    """按后端拼出请求 URL；SearXNG 的 endpoint 来自配置。"""
    q = quote_plus(_query_with_site(query, site))
    if provider == "searxng":
        # 不传 language：部分引擎在 language=all 下会直接返回 0 条
        parts = [f"q={q}", "format=json"]
        if time_range:
            parts.append(f"time_range={time_range}")
        if engines:
            parts.append("engines=" + quote_plus(",".join(engines)))
        return f"{(endpoint or '').rstrip('/')}/search?" + "&".join(parts)
    if provider == "brave":
        parts = [f"q={q}", f"count={max(1, min(limit, _BRAVE_COUNT_MAX))}"]
        if time_range:
            parts.append(f"freshness={_BRAVE_FRESHNESS[time_range]}")
        return BRAVE_ENDPOINT + "?" + "&".join(parts)
    parts = [f"q={q}"]
    if time_range:
        parts.append(f"df={_DDG_DF[time_range]}")
    return DUCKDUCKGO_ENDPOINT + "?" + "&".join(parts)


def tavily_payload(
    query: str,
    *,
    api_key: str,
    limit: int,
    site: str = "",
    time_range: str = "",
) -> dict:
    """Tavily 走 POST：过滤条件与 key 都放在 JSON 请求体里。"""
    body: dict = {
        "api_key": api_key,
        "query": _query_with_site(query, site),
        "max_results": max(1, min(limit, _TAVILY_MAX_RESULTS)),
        "search_depth": "basic",
        "include_answer": False,
    }
    if time_range:
        body["time_range"] = time_range
    if site:
        # Tavily 有原生的域名过滤参数；站内路径仍靠查询里的 site: 运算符
        body["include_domains"] = [site.split("/")[0]]
    return body


def parse_results(provider: str, body: str, max_results: int) -> list[SearchHit]:
    """把服务端返回解析成结果列表；解析不了就抛 SearchError，不静默返回空。"""
    if provider == "searxng":
        return _parse_searxng(body, max_results)
    if provider == "brave":
        return _parse_brave(body, max_results)
    if provider == "tavily":
        return _parse_tavily(body, max_results)
    return _parse_duckduckgo(body, max_results)


def _load_json(body: str, provider: str) -> dict:
    text = (body or "").strip()
    if not text.startswith("{"):
        # 有的出口会按 Accept 把 JSON 包进 <pre> 里返回（text/html），先把 JSON 抠出来
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SearchError(f"{provider} 返回的不是 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SearchError(f"{provider} 响应不是 JSON 对象")
    return data


def _hits_from(
    raw: object,
    max_results: int,
    *,
    missing: str,
    text_key: str,
    engine_key: str | None = None,
    published_keys: tuple[str, ...] = (),
    default_engine: str = "",
) -> list[SearchHit]:
    """JSON 搜索结果的通用提取：去重、丢弃非 http 链接、按条数截断。"""
    if not isinstance(raw, list):
        raise SearchError(missing)
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
        snippet = _clean(item.get(text_key))
        engine = _clean(item.get(engine_key)) if engine_key else ""
        published = ""
        for key in published_keys:
            published = _clean(item.get(key))
            if published:
                break
        hits.append(
            SearchHit(
                title=title,
                url=url,
                snippet=snippet,
                engine=engine or default_engine,
                published_at=published,
            )
        )
        if len(hits) >= max_results:
            break
    return hits


def _parse_searxng(body: str, max_results: int) -> list[SearchHit]:
    data = _load_json(body, "SearXNG")
    return _hits_from(
        data.get("results"),
        max_results,
        missing="SearXNG 响应缺少 results 数组",
        text_key="content",
        engine_key="engine",
        published_keys=("publishedDate",),
        default_engine="searxng",
    )


def _parse_brave(body: str, max_results: int) -> list[SearchHit]:
    data = _load_json(body, "Brave")
    web = data.get("web")
    raw = web.get("results") if isinstance(web, dict) else None
    return _hits_from(
        raw,
        max_results,
        missing="Brave 响应缺少 web.results 数组",
        text_key="description",
        published_keys=("page_age", "age"),
        default_engine="brave",
    )


def _parse_tavily(body: str, max_results: int) -> list[SearchHit]:
    data = _load_json(body, "Tavily")
    return _hits_from(
        data.get("results"),
        max_results,
        missing="Tavily 响应缺少 results 数组",
        text_key="content",
        published_keys=("published_date",),
        default_engine="tavily",
    )


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
                engine="duckduckgo",
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
        hits.append(
            SearchHit(
                title=_clean(link.text()) or url,
                url=url,
                snippet="",
                engine="duckduckgo",
            )
        )
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
