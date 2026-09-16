"""compact 输出的离线回归测试（不依赖网络）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scrape_mcp.extract.compact import compact_html  # noqa: E402

SAMPLE = """<!doctype html>
<html><head><title>测试标题</title>
<style>body{color:red}</style>
<script>var track=1;</script>
</head><body>
<nav class="main-nav"><a href="/">首页</a><a href="/news">新闻</a></nav>
<header class="site-header"><h2>站点名</h2></header>
<article>
  <!-- 注释应被丢弃 -->
  <h1>正文大标题</h1>
  <p>第一段，包含<a href="/inner/page">站内链接</a>与<a href="https://other.com/x">站外链接</a>。</p>
  <p>第二段，<strong>加粗</strong>和<em>斜体</em>只保留文字。</p>
  <ul><li>要点一</li><li>要点二</li></ul>
  <table>
    <tr><th>列A</th><th>列B</th></tr>
    <tr><td>1</td><td>含,逗号</td></tr>
  </table>
  <pre><code>print("hi")</code></pre>
  <div class="highlight"><pre><span>def</span> <span>f</span>():<span>
    return 1</span></pre></div>
  <blockquote>引用内容</blockquote>
  <img src="/a.png" alt="示意图">
  <div class="related-posts"><a href="/hot">热门推荐</a></div>
</article>
<footer><p>版权所有</p></footer>
</body></html>"""

URL = "https://example.com/article/1"


def test_compact_basic():
    r = compact_html(SAMPLE, URL)
    c = r.content

    assert r.title == "测试标题"
    assert "# 正文大标题" in c
    # 站内链接保留为相对路径
    assert "[站内链接](/inner/page)" in c
    # 站外链接降级为纯文本
    assert "站外链接" in c
    assert "other.com" not in c
    # 非内容节点被清除
    assert "var track" not in c
    assert "color:red" not in c
    assert "<!--" not in c
    # 模板块被丢弃
    assert "首页" not in c
    assert "版权所有" not in c
    assert "热门推荐" not in c
    # 行内标记被剥离
    assert "加粗" in c and "**加粗**" not in c
    # 列表 / 表格 / 代码块 / 引用
    assert "- 要点一" in c
    assert "列A,列B" in c
    assert '"含,逗号"' in c
    assert "```" in c and 'print("hi")' in c
    # pre 内的 span 不能被拆行，缩进要保留
    assert "def f():\n    return 1" in c
    assert "> 引用内容" in c
    assert "[示意图]" in c
    assert r.links_kept == 1 and r.links_dropped == 1


def test_link_policy_all_keeps_external():
    r = compact_html(SAMPLE, URL, link_policy="all")
    assert "[站外链接](https://other.com/x)" in r.content
    assert r.links_kept == 2


def test_link_policy_none_drops_all():
    r = compact_html(SAMPLE, URL, link_policy="none")
    assert "站内链接" in r.content
    assert "/inner/page" not in r.content
    assert r.links_kept == 0


def test_external_icon_link_does_not_leak_url():
    html = '<html><body><article><p>正文内容足够长以避免空页判定。</p><a href="https://other.com/hidden"><img src="/i.png" alt=""></a></article></body></html>'
    r = compact_html(html, URL)
    assert "other.com" not in r.content


def test_empty_link_text_is_dropped():
    html = '<html><body><article><p>正文内容足够长以避免空页判定。</p><a href="/list_1"><img src="/i.png" alt=""></a></article></body></html>'
    r = compact_html(html, URL)
    assert "/list_1" not in r.content
    assert r.links_kept == 0 and r.links_dropped == 1


def test_brackets_in_link_text_are_escaped():
    html = '<html><body><article><p>正文内容足够长以避免空页判定。</p><a href="/p/1">[标题带括号]</a></article></body></html>'
    r = compact_html(html, URL)
    assert r"[\[标题带括号\]](/p/1)" in r.content


def test_downgraded_external_links_do_not_glue():
    # 相邻的站外链接降级成纯文字后必须能分清，不能粘成"新华网人民网"
    html = (
        '<html><body><article><h1>合作媒体</h1>'
        '<p><a href="https://news.xinhuanet.com">新华网</a>'
        '<a href="https://www.people.com.cn">人民网</a>'
        '<a href="https://www.cctv.com">央视网</a></p>'
        "</article></body></html>"
    )
    r = compact_html(html, URL)
    assert "新华网 人民网 央视网" in r.content
    assert "新华网人民网" not in r.content


def test_hint_match_is_token_based_not_substring():
    # downloads 不能被 "ads" 命中，commentary 不能被 "comment" 命中
    html = (
        '<html><body><article><p>正文开头，用于通过空页判定。</p>'
        '<div class="downloads"><p>下载区的可用内容。</p></div>'
        '<div class="commentary"><p>这是一篇评论文章的内容。</p></div>'
        "</article></body></html>"
    )
    r = compact_html(html, URL)
    assert "下载区的可用内容" in r.content
    assert "这是一篇评论文章的内容" in r.content


def test_comment_blocks_are_skipped():
    html = (
        '<html><body><article><p>正文开头，用于通过空页判定。</p>'
        '<div class="comments"><p>评论区内容。</p></div>'
        '<div class="comment-list"><p>评论列表内容。</p></div>'
        "</article></body></html>"
    )
    r = compact_html(html, URL)
    assert "评论区内容" not in r.content
    assert "评论列表内容" not in r.content


def test_density_root_picks_content_container_over_chrome():
    # class 名刻意不含模板块特征词，只有内容密度选根能排除它
    nav = "".join(f'<a href="/c{i}">频道{i}</a>' for i in range(30))
    paragraphs = "".join(f"<p>这是正文第{i}段，内容足够长以便通过内容密度判定。</p>" for i in range(20))
    html = (
        f'<html><body><div class="channel-bar">{nav}</div>'
        f'<div class="post-body"><h1>真正的标题</h1>{paragraphs}</div>'
        "</body></html>"
    )
    r = compact_html(html, URL)
    assert "真正的标题" in r.content
    assert "频道0" not in r.content


def test_footer_variants_are_skipped():
    # 页脚常写作 foot 而非 footer，版权/备案行也属模板噪声
    # beian__AMcCz 是 CSS Module 的哈希类名，词元化后应命中 beian
    html = (
        '<html><body><article><p>正文开头，用于通过空页判定。</p>'
        '<div class="foot"><p>沪ICP备12345678号</p></div>'
        '<div class="copyright-bar"><p>版权所有 © 2026</p></div>'
        '<div class="beian__AMcCz"><p>增值电信业务经营许可证</p></div>'
        "</article></body></html>"
    )
    r = compact_html(html, URL)
    assert "沪ICP备12345678号" not in r.content
    assert "版权所有" not in r.content
    assert "增值电信业务经营许可证" not in r.content


def test_truncate_respects_budget():
    long_html = "<html><body><article><h1>T</h1>" + "".join(
        f"<p>第{i}段内容，用于验证预算截断行为是否生效。</p>" for i in range(400)
    ) + "</article></body></html>"
    r = compact_html(long_html, URL, max_tokens=300)
    assert r.truncated is True
    assert "省略" in r.content

    from scrape_mcp.tokenizer import count_tokens

    assert count_tokens(r.content) <= 400


def test_empty_html():
    r = compact_html("", URL)
    assert r.content == ""
    assert r.blocks == 0
