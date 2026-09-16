"""DOM 剪枝：把 HTML 压成极省 token 的紧凑文本。

压缩手段（按收益排序）：
1. 站外链接降级为纯文本，站内链接只保留相对路径（默认 link_policy="internal"）
2. 属性（class/id/style/data-*）全部丢弃，只保留标签语义
3. 删除 script/style/svg/noscript 等非内容节点
4. 丢弃 nav/footer/aside 等模板块，并对重复块去重
5. 表格转 CSV，图片只留 alt
6. 空白归一化，块之间只用单换行分隔
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser, Node

from ..tokenizer import truncate_to_budget

DROP_SELECTOR = (
    "script, style, noscript, svg, iframe, template, canvas, form, button, input, "
    "select, textarea, object, embed, video, audio, source, track, map, area, link, meta"
)
BOILERPLATE_TAGS = {"nav", "footer", "aside"}
# 模板块特征词。按独立词元匹配，不做子串包含，否则 class="downloads" 会命中 "ads"、
# class="commentary" 会命中 "comment"，整块正文被误删。
BOILERPLATE_HINTS = frozenset(
    {
        "nav",
        "navigation",
        "navbar",
        "menu",
        "sidebar",
        "footer",
        "contentinfo",
        "complementary",
        "comment",
        "share",
        "related",
        "recommend",
        "advert",
        "ad",
        "ads",
        "breadcrumb",
        "cookie",
        "popup",
        "modal",
        "banner",
        "toolbar",
        "pagination",
        # 页脚常写作 foot（而非 footer），版权/备案行也归此类。
        # beian 是中文站点的通用命名（ICP 备案号区块），国内页面几乎必带。
        "foot",
        "copyright",
        "license",
        "beian",
    }
)
_HINT_SPLIT = re.compile(r"[^a-z0-9]+")
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
INLINE_TAGS = {
    "a",
    "span",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "s",
    "del",
    "ins",
    "sup",
    "sub",
    "mark",
    "small",
    "code",
    "br",
    "img",
    "wbr",
    "time",
    "abbr",
    "cite",
    "q",
    "kbd",
    "samp",
    "var",
    "bdi",
    "bdo",
    "ruby",
    "rt",
    "meter",
    "progress",
}

MAX_TABLE_ROWS = 100
# 只对短块去重，避免误删正文里合理重复的长句
DUP_BLOCK_MAX_CHARS = 120

# 正文根候选容器，以及用于计算"内容密度"的块级文字载体
ROOT_SELECTOR = "article, main, [role=main], div, section"
ROOT_TEXT_SELECTOR = "p, h1, h2, h3, h4, h5, h6, pre, blockquote, li, td, dd, dt, figcaption"
ROOT_MIN_CHARS = 400
# 只对文本量最大的前 N 个候选做密度打分，避免大页面上逐个子树遍历导致耗时
ROOT_TOP_N = 25

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_SPACES = re.compile(r"[^\S\n]+")
_SPACE_AROUND_NL = re.compile(r" *\n *")
_MULTI_NL = re.compile(r"\n{2,}")

LINK_POLICIES = ("internal", "all", "none")


@dataclass
class CompactResult:
    content: str
    title: str
    blocks: int
    links_kept: int
    links_dropped: int
    truncated: bool


def _norm(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = _SPACES.sub(" ", text)
    text = _SPACE_AROUND_NL.sub("\n", text)
    text = _MULTI_NL.sub("\n", text)
    return text.strip()


def _children(node: Node) -> list[Node]:
    out: list[Node] = []
    child = node.child
    while child is not None:
        out.append(child)
        child = child.next
    return out


def _csv_cell(cell: str) -> str:
    if any(ch in cell for ch in ',"\n'):
        return '"' + cell.replace('"', '""') + '"'
    return cell


def _escape_link_text(text: str) -> str:
    """链接文字自带方括号时（如 [[标题]](/x)）会破坏紧凑格式的歧义边界，转义掉。"""
    if "[" not in text and "]" not in text:
        return text
    return text.replace("[", "\\[").replace("]", "\\]")


class _Renderer:
    def __init__(self, base_url: str, link_policy: str) -> None:
        self.base_url = base_url
        self.host = urlparse(base_url).netloc.lower()
        self.link_policy = link_policy
        self.links_kept = 0
        self.links_dropped = 0

    def render(self, root: Node) -> list[str]:
        return self._children(root)

    # ---------- 块级 ----------

    def _children(self, node: Node) -> list[str]:
        blocks: list[str] = []
        buf: list[str] = []

        def flush() -> None:
            if not buf:
                return
            text = _norm("".join(buf))
            buf.clear()
            if text:
                blocks.append(text)

        child = node.child
        while child is not None:
            nxt = child.next
            tag = child.tag
            if tag == "-text":
                if child.text() and child.text().strip():
                    buf.append(child.text())
            elif tag in INLINE_TAGS:
                buf.append(self._inline(child))
            else:
                flush()
                if not self._skip(child):
                    blocks.extend(self._block(child))
            child = nxt
        flush()
        return blocks

    def _skip(self, node: Node) -> bool:
        if node.tag in BOILERPLATE_TAGS:
            return True
        attrs = node.attributes
        raw = (
            f"{attrs.get('id') or ''} {attrs.get('class') or ''} {attrs.get('role') or ''}".lower()
        )
        tokens = {t for t in _HINT_SPLIT.split(raw) if t}
        if not tokens:
            return False
        return any(t in BOILERPLATE_HINTS or t.rstrip("s") in BOILERPLATE_HINTS for t in tokens)

    def _block(self, node: Node) -> list[str]:
        tag = node.tag

        if tag in HEADINGS:
            text = " ".join(self._children(node))
            return [f"{'#' * HEADINGS[tag]} {text}"] if text else []

        if tag == "li":
            inner = self._children(node)
            if not inner:
                return []
            return [f"- {inner[0]}"] + [f"  {b}" for b in inner[1:]]

        if tag in ("dt", "dd", "p", "figcaption", "summary", "address", "legend"):
            return self._children(node)

        if tag == "pre":
            # pre 内部已含字面换行与缩进，拼接子节点时不能插入分隔符，否则代码会被逐 token 拆行
            code = node.css_first("code") or node
            raw = (code.text(deep=True, separator="") or "").strip("\n")
            return [f"```\n{raw}\n```"] if raw.strip() else []

        if tag == "blockquote":
            return [f"> {b}" for b in self._children(node)]

        if tag == "table":
            return self._table(node)

        if tag in ("br", "hr"):
            return []

        return self._children(node)

    def _table(self, node: Node) -> list[str]:
        lines: list[str] = []
        for tr in node.css("tr"):
            cells = [
                _norm(c.text(deep=True, separator=" ") or "")
                for c in _children(tr)
                if c.tag in ("td", "th")
            ]
            if any(cells):
                lines.append(",".join(_csv_cell(c) for c in cells))
            if len(lines) >= MAX_TABLE_ROWS:
                lines.append("…(表格已截断)")
                break
        return ["\n".join(lines)] if lines else []

    # ---------- 行内 ----------

    def _inline(self, node: Node) -> str:
        tag = node.tag
        if tag == "br":
            return "\n"
        if tag in ("wbr",):
            return ""
        if tag == "img":
            alt = (node.attributes.get("alt") or "").strip()
            return f"[{alt}]" if alt else ""
        if tag == "a":
            return self._link(self._inline_text(node), node.attributes.get("href"))
        if tag == "code":
            text = self._inline_text(node).strip()
            return f"`{text}`" if text else ""
        return self._inline_text(node)

    def _inline_text(self, node: Node) -> str:
        parts: list[str] = []
        for child in _children(node):
            tag = child.tag
            if tag == "-text":
                parts.append(child.text() or "")
            elif tag in INLINE_TAGS:
                parts.append(self._inline(child))
        return "".join(parts)

    def _link(self, text: str, href: str | None) -> str:
        text = _norm(text)
        href = (href or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return text
        if self.link_policy == "none":
            self.links_dropped += 1
            return f"{text} " if text else ""

        target = urljoin(self.base_url, href)
        parsed = urlparse(target)
        external = parsed.netloc.lower() != self.host

        if external and self.link_policy != "all":
            self.links_dropped += 1
            # 降级成纯文字后若不加分隔符，行内相邻链接会粘成"新华网人民网"，模型读不出是几个名称
            return f"{text} " if text else ""

        # 无文字链接（纯图标链、空锚点）对模型没有信息量，只输出 URL 纯属浪费 token
        if not text:
            self.links_dropped += 1
            return ""

        text = _escape_link_text(text)
        self.links_kept += 1
        if external:
            return f"[{text}]({target})"

        rel = parsed.path or "/"
        if parsed.query:
            rel = f"{rel}?{parsed.query}"
        return f"[{text}]({rel})"


def _norm_len(node: Node) -> int:
    return len(_norm(node.text(deep=True, separator=" ") or ""))


def _text_len(node: Node) -> int:
    # 粗筛用：不做 _norm。对页面上每个候选子树跑正则归一化会让大页面耗时爆炸，
    # 这里只用于排序取前 N 个，精度要求低。
    return len(node.text(deep=True, separator=" ") or "")


def _node_depth(node: Node) -> int:
    depth = 0
    cur = node.parent
    while cur is not None:
        depth += 1
        cur = cur.parent
    return depth


def _density_score(node: Node) -> int:
    """非链接正文文字量 = 块级文字 − 其中的链接文字。

    导航空壳由 li>a 组成，两者相抵接近 0；正文容器则剩下一大截 prose。
    用差值而不是比值，可以避免容器里的空白与装饰文字把链接密度算歪。
    """
    block_len = sum(len(b.text() or "") for b in node.css(ROOT_TEXT_SELECTOR))
    link_len = sum(len(a.text() or "") for a in node.css("a"))
    return max(0, block_len - link_len)


def _density_pick(tree: HTMLParser, body_len: int) -> Node | None:
    candidates = tree.css(ROOT_SELECTOR)
    if not candidates:
        return None
    # 先用原始长度粗筛前 N 个，再对它们做归一化：原始长度虚高倍数不均匀，
    # 但"谁更大"的序关系大体可信，用它剪枝能把归一化次数从几百降到 25。
    candidates.sort(key=_text_len, reverse=True)

    pool: list[tuple[int, Node]] = []
    for node in candidates[:ROOT_TOP_N]:
        n = _norm_len(node)
        # 候选至少要占 body 的 15%，否则多半是页内的推荐位/摘要块
        if n >= ROOT_MIN_CHARS and (not body_len or n >= body_len * 0.15):
            pool.append((n, node))
    if not pool:
        return None

    scored = [(node, _density_score(node)) for _, node in pool]
    top = max(score for _, score in scored)
    if top <= 0:
        return None
    # 得分接近最高的候选里取最深的一个，即"恰好装住正文的最紧容器"
    tightest = [node for node, score in scored if score >= top * 0.9]
    return max(tightest, key=_node_depth)


def _pick_root(tree: HTMLParser) -> Node | None:
    body = tree.body
    # 归一化长度：原始 text 长度被节点间的分隔空格与源码缩进严重放大，而放大倍数
    # 在不同子树间差异极大（节点多、缩进深的导航壳虚高最猛）。
    # 语义标签与 body 的体量对比必须用归一化长度，否则正文容器会被导航壳按比例挤掉。
    body_len = _norm_len(body) if body is not None else 0

    # 路 1：语义标签最可信，命中且体量达标就直接用
    semantic: Node | None = None
    semantic_len = 0
    for node in tree.css("article, main, [role=main]"):
        node_len = _norm_len(node)
        if node_len > semantic_len:
            semantic, semantic_len = node, node_len
    if (
        semantic is not None
        and semantic_len >= ROOT_MIN_CHARS
        and (not body_len or semantic_len >= body_len * 0.15)
    ):
        return semantic

    # 路 2：没有可用语义标签（国内站点很常见），按内容密度找正文容器
    picked = _density_pick(tree, body_len)
    if picked is not None:
        return picked

    # 路 3：兜底
    return body or tree.root


def _dedup(blocks: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for block in blocks:
        stripped = block.strip()
        if not stripped:
            continue
        if len(stripped) <= DUP_BLOCK_MAX_CHARS:
            if stripped in seen:
                continue
            seen.add(stripped)
        out.append(stripped)
    return out


def compact_html(
    html: str,
    url: str,
    *,
    link_policy: str = "internal",
    max_tokens: int | None = None,
) -> CompactResult:
    html = _COMMENT.sub("", html or "")
    if not html.strip():
        return CompactResult("", "", 0, 0, 0, False)
    tree = HTMLParser(html)

    title = ""
    title_node = tree.css_first("title")
    if title_node is not None:
        title = _norm(title_node.text(deep=True) or "")
    if not title:
        h1 = tree.css_first("h1")
        if h1 is not None:
            title = _norm(h1.text(deep=True) or "")

    for node in tree.css(DROP_SELECTOR):
        node.decompose()

    root = _pick_root(tree)
    renderer = _Renderer(url, link_policy)
    blocks = _dedup(renderer.render(root)) if root is not None else []
    content = "\n".join(blocks)

    truncated = False
    if max_tokens and max_tokens > 0:
        content, truncated, _ = truncate_to_budget(content, max_tokens)

    return CompactResult(
        content=content,
        title=title,
        blocks=len(blocks),
        links_kept=renderer.links_kept,
        links_dropped=renderer.links_dropped,
        truncated=truncated,
    )
