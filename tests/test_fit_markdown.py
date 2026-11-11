"""fit_markdown 与 token 报告：裁剪特征、代码保护、保护性回退。"""

from __future__ import annotations

import asyncio
import json

from scrape_mcp import server
from scrape_mcp.core.fetcher import FetchOutcome
from scrape_mcp.extract.fit import build_token_report, fit_markdown

NAV = "- [首页](https://a.test/) · [文档](https://a.test/docs) · [博客](https://a.test/blog)"


def test_fit_drops_navigation_link_rows():
    content = f"{NAV}\n\n# 标题\n\n正文段落，保留。"
    result = fit_markdown(content)
    assert "[首页]" not in result.content
    assert "正文段落，保留。" in result.content
    assert result.dropped["nav"] == 1


def test_fit_drops_footer_line():
    content = "正文内容。\n\n© 2026 A 公司 版权所有 · 京ICP备 12345 号"
    result = fit_markdown(content)
    assert "版权所有" not in result.content
    assert result.dropped["footer"] == 1


def test_fit_drops_related_section_until_next_heading():
    content = (
        "# 正文\n\n第一段。\n\n## 相关阅读\n\n- [另一篇文章](https://a.test/x)\n\n"
        "## 继续\n\n第二段。"
    )
    result = fit_markdown(content)
    assert "相关阅读" not in result.content
    assert "另一篇文章" not in result.content
    assert "第二段。" in result.content
    assert result.dropped["related"] >= 1


def test_fit_keeps_code_fences_even_with_keywords():
    content = "```\n/* 广告位 */\n© 2026 demo\n- [a](b) - [c](d) - [e](f)\n```\n\n正文。"
    result = fit_markdown(content)
    assert "/* 广告位 */" in result.content
    assert "© 2026 demo" in result.content
    assert result.dropped == {"nav": 0, "footer": 0, "comments": 0, "related": 0, "ads": 0}


def test_fit_returns_original_when_cut_too_much():
    body = "正文句子。" * 20
    content = "\n".join([NAV] * 40) + "\n\n" + body
    result = fit_markdown(content)
    assert result.applied is False
    assert result.content == content
    assert result.dropped["nav"] == 0


def test_fit_keeps_plain_content_untouched():
    content = "# 标题\n\n第一段。\n\n第二段。"
    result = fit_markdown(content)
    assert result.content == content
    assert result.applied is True


def test_token_report_shape_and_ratio():
    report = build_token_report(
        html_chars=1000,
        content="x" * 200,
        tokens=50,
        dropped={"nav": 2, "footer": 0, "comments": 1, "related": 0, "ads": 0},
        fit=True,
    )
    assert report == {
        "raw_chars": 1000,
        "output_chars": 200,
        "output_tokens_est": 50,
        "compression_ratio": 0.2,
        "dropped": {"nav": 2, "footer": 0, "comments": 1, "related": 0, "ads": 0},
        "fit": True,
    }


def test_token_report_handles_empty_html():
    report = build_token_report(html_chars=0, content="", tokens=0, dropped={}, fit=False)
    assert report["compression_ratio"] == 0.0
    assert report["dropped"]["nav"] == 0


# ---- 工具层 ----


def _page() -> str:
    nav = " ".join(f'<a href="/{name}">{name}</a>' for name in ("首页", "文档", "博客", "关于"))
    return (
        "<html><head><title>示例</title></head><body><article>"
        "<h1>正文标题</h1>"
        f"<p>{nav}</p>"
        "<p>这是需要保留的正文内容，不能被裁剪。</p>"
        "<p>© 2026 示例公司 版权所有</p>"
        "</article></body></html>"
    )


def _fake(monkeypatch, html: str | None = None) -> None:
    class _Fetcher:
        async def fetch(self, url: str, **_kwargs) -> FetchOutcome:
            return FetchOutcome(
                url=url,
                final_url=url,
                status=200,
                tier="l1",
                blocked="none",
                block_reason="",
                content_type="text/html",
                html=html or _page(),
                elapsed_ms=3,
            )

    async def allowed(_url: str, *_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(server, "_compliance_check", allowed)
    monkeypatch.setattr(server, "_get_fetcher", lambda: _Fetcher())


def _run(coro) -> dict:
    return json.loads(asyncio.run(coro))


def test_web_fetch_fit_markdown_reports_tokens(monkeypatch):
    _fake(monkeypatch)
    out = _run(server.web_fetch("https://a.test/post", format="fit_markdown", bypass_cache=True))
    assert out["ok"] is True
    assert out["format"] == "fit_markdown"
    report = out["token_report"]
    assert report["raw_chars"] > 0
    assert report["output_chars"] == len(out["content"])
    assert report["output_tokens_est"] == out["token_estimate"]
    assert report["dropped"]["nav"] + report["dropped"]["footer"] >= 1
    assert "这是需要保留的正文内容" in out["content"]


def test_web_fetch_default_markdown_has_no_token_report(monkeypatch):
    _fake(monkeypatch)
    out = _run(server.web_fetch("https://a.test/plain", bypass_cache=True))
    assert out["ok"] is True
    assert "token_report" not in out


def test_web_fetch_fit_markdown_default_switch(monkeypatch):
    _fake(monkeypatch)
    settings = server.get_settings()
    monkeypatch.setattr(settings, "fit_markdown_default", True)
    out = _run(server.web_fetch("https://a.test/switch", bypass_cache=True))
    assert out["ok"] is True
    assert "token_report" in out
    assert out["token_report"]["fit"] is True
