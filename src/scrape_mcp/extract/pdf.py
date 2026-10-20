"""PDF → 文本（可选依赖）。

默认安装不带 PDF 解析能力，装 extra 后才启用：

    pip install "mcp-scrape[pdf]"

解析失败按页兜底：单页抽不出文本（扫描件）只跳过该页，不让整份 PDF 失败。
"""

from __future__ import annotations

import re
from io import BytesIO

_MULTI_NL = re.compile(r"\n{3,}")


class PdfError(RuntimeError):
    """PDF 解析失败（文件损坏、加密等）。"""


class PdfUnavailableError(PdfError):
    """未安装可选依赖 pypdf。"""


def pdf_pages(data: bytes, *, max_pages: int = 200) -> tuple[list[str], int, int]:
    """抽取 PDF 每页文本。

    返回 ``(非空页文本列表, 总页数, 实际解析页数)``；扫描件页会得到空字符串并被跳过。
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # 可选依赖未安装：给调用方可执行的安装提示
        raise PdfUnavailableError(
            '未安装 PDF 支持，请先执行 pip install "mcp-scrape[pdf]"'
        ) from exc

    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        raise PdfError(f"PDF 解析失败: {type(exc).__name__}: {exc}") from exc

    total = len(reader.pages)
    limit = max(1, min(total, max_pages)) if total else 0
    pages: list[str] = []
    for index in range(limit):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception:  # 单页异常（字体/编码问题）不影响其余页
            text = ""
        text = _clean(text)
        if text:
            pages.append(text)
    return pages, total, limit


def _clean(text: str) -> str:
    text = (text or "").replace("\x00", "").replace("\xa0", " ")
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()
