"""字段级溯源：节点记录、选择器定位、片段偏移与必填缺失候选。"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp import server
from scrape_mcp.core.fetcher import FetchOutcome
from scrape_mcp.extract.grounding import build, candidates, locate
from scrape_mcp.extract.schema import extract_fields, parse_schema

URL = "https://shop.example.com/item/1"

HTML = """<html><body>
<h1 class="title">示例商品</h1>
<div class="price">299 元</div>
<div class="stock-left">现货 5 件</div>
<ul class="items">
  <li class="item"><span class="name">甲</span><span class="cost">10</span></li>
  <li class="item"><span class="name">乙</span><span class="cost">20</span></li>
</ul>
</body></html>"""

SCHEMA = {
    "title": {"selector": "h1", "type": "text", "required": True},
    "price": {"selector": ".price", "type": "text"},
    "items": {
        "selector": ".item",
        "type": "list",
        "fields": {
            "name": {"selector": ".name", "type": "text", "required": True},
            "cost": {"selector": ".cost", "type": "text", "convert": "float"},
        },
    },
    "stock": {"selector": ".stock-count", "type": "text", "required": True},
}


def _outcome(html: str = HTML) -> FetchOutcome:
    return FetchOutcome(
        url=URL,
        final_url=URL,
        status=200,
        tier="l1",
        blocked="none",
        block_reason="",
        content_type="text/html",
        html=html,
        elapsed_ms=5,
    )


def test_locate_prefers_direct_hit_then_prefix():
    text = "示例商品\n\n299 元"
    assert locate("299 元", text) == text.index("299 元")
    assert locate("示例商品（含税）", text) == 0
    assert locate("不存在", text) is None
    assert locate("", text) is None


def test_candidates_find_near_miss_by_class_subword():
    found = candidates(HTML, ".product-price")
    assert found
    assert found[0]["snippet"] == "299 元"
    assert found[0]["selector"]

    assert candidates(HTML, ".nothing-here") == []
    assert candidates(HTML, "") == []


def test_schema_hits_align_with_data_paths():
    hits: dict[str, dict] = {}
    result = extract_fields(HTML, parse_schema(SCHEMA), base_url=URL, hits=hits)

    hit_paths = set(hits)
    assert {"title", "price", "items[0].name", "items[1].cost"} <= hit_paths
    assert "stock" not in hit_paths  # 缺失字段不记命中

    grounding = build(hits, markdown="示例商品\n\n299 元\n10 20", html=HTML, snippet_chars=20)
    for _path, entry in grounding.items():
        assert set(entry) == {"selector", "snippet", "offset", "basis"}
        assert entry["selector"]
        assert entry["snippet"]
        assert entry["basis"] in ("markdown", "html")
        assert entry["offset"] is None or entry["offset"] >= 0

    assert grounding["price"]["snippet"] == "299 元"
    assert grounding["price"]["basis"] == "markdown"
    assert grounding["price"]["offset"] == "示例商品\n\n299 元\n10 20".index("299 元")
    # 嵌套列表字段按下标给出，路径与 data 一致
    assert grounding["items[0].name"]["snippet"] == "甲"
    assert result.fields["items"][0]["name"] == "甲"


def test_build_falls_back_to_html_basis_and_keeps_dynamic_pages_usable():
    hits = {"price": {"node": None, "type": "text", "value": "299 元", "selector": ".price"}}
    grounding = build(hits, markdown="", html=HTML, snippet_chars=20)
    entry = grounding["price"]
    assert entry["basis"] == "html"
    assert entry["selector"] == ".price"  # 没有节点时退回 schema 选择器
    assert entry["offset"] == HTML.index("299 元")


def _call_extract(**kwargs) -> dict:
    return json.loads(asyncio.run(server.web_extract(URL, **kwargs)))


def test_web_extract_grounding_payload(monkeypatch):
    async def fake_fetch_html(url, **_kwargs):
        return _outcome(), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)

    payload = _call_extract(schema=SCHEMA, grounding=True)

    # 必填字段缺失按既有语义 ok=false（data 与 grounding 仍照常返回）
    assert payload["ok"] is False
    assert payload["error_code"] == "missing_required"
    assert payload["required_missing"] == ["stock"]
    grounding = payload["grounding"]
    assert {"title", "price", "items[0].cost"} <= set(grounding)
    assert set(grounding["title"]) == {"selector", "snippet", "offset", "basis"}
    assert grounding["items[0].cost"]["snippet"] == "10"
    assert payload["missing_candidates"]["stock"]
    candidate = payload["missing_candidates"]["stock"][0]
    assert set(candidate) == {"selector", "snippet"}
    assert "5" in candidate["snippet"]


def test_web_extract_without_grounding_keeps_old_payload(monkeypatch):
    async def fake_fetch_html(url, **_kwargs):
        return _outcome(), None

    monkeypatch.setattr(server, "_fetch_html", fake_fetch_html)

    payload = _call_extract(schema=SCHEMA)
    assert "grounding" not in payload
    assert "missing_candidates" not in payload
