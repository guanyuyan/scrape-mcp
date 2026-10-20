"""链接发现：过滤外链/静态资源/伪协议并去重。"""

from scrape_mcp.extract.links import extract_links, next_link

_HTML = """
<html><body>
  <a href="/a">A</a>
  <a href="/a">A 重复</a>
  <a href="/b?x=1#frag">B</a>
  <a href="https://other.com/x">外链</a>
  <a href="/img/logo.png">图片</a>
  <a href="javascript:void(0)">JS</a>
  <a href="mailto:a@b.com">邮件</a>
  <a href="#top">锚点</a>
  <a href="file.zip">压缩包</a>
</body></html>
"""


def test_extract_internal_links_filters_and_dedups():
    links = extract_links(_HTML, "https://example.com/list/1")
    assert links == ["https://example.com/a", "https://example.com/b?x=1"]


def test_same_host_false_keeps_external():
    links = extract_links(_HTML, "https://example.com/list/1", same_host=False)
    assert "https://other.com/x" in links


def test_limit_caps_result():
    links = extract_links(_HTML, "https://example.com/", limit=1)
    assert len(links) == 1


def test_empty_html_returns_empty():
    assert extract_links("", "https://example.com/") == []


_PAGER = """
<html><body>
  <a class="next" href="/list?page=2">下一页</a>
</body></html>
"""


def test_next_link_resolves_relative_and_strips_fragment():
    assert next_link(_PAGER, "https://example.com/list", "a.next") == (
        "https://example.com/list?page=2"
    )


def test_next_link_missing_selector_returns_none():
    assert next_link(_PAGER, "https://example.com/list", "a.more") is None
    assert next_link("", "https://example.com/list", "a.next") is None


def test_next_link_rejects_pseudo_and_external():
    html = '<a class="next" href="javascript:void(0)">x</a>'
    assert next_link(html, "https://example.com/list", "a.next") is None

    html = '<a class="next" href="https://other.com/p2">x</a>'
    assert next_link(html, "https://example.com/list", "a.next") is None
    assert next_link(html, "https://example.com/list", "a.next", same_host=False) == (
        "https://other.com/p2"
    )


def test_next_link_reads_data_href():
    html = '<button class="next" data-href="/p2">下一页</button>'
    assert next_link(html, "https://example.com/list", "button.next") == "https://example.com/p2"
