"""扫描 PDF 的 OCR（可选依赖）：PyMuPDF 渲染 + pytesseract 识别。

默认关闭；只有显式开启且 PDF 没有文字层时才会加载。缺少依赖或系统 tesseract
时抛 ``OcrUnavailableError``，调用方据此返回可执行的处理提示。
"""

from __future__ import annotations

import re
from io import BytesIO

_MULTI_NL = re.compile(r"\n{3,}")


class OcrError(RuntimeError):
    """OCR 渲染或识别失败。"""


class OcrUnavailableError(OcrError):
    """未安装 OCR 可选依赖（pymupdf / pytesseract / pillow）。"""


def ocr_pages(
    data: bytes,
    *,
    dpi: int = 200,
    lang: str = "chi_sim+eng",
    max_pages: int = 10,
) -> tuple[list[str], int, int]:
    """逐页渲染并识别；返回 ``(非空页文本, 总页数, 实际识别页数)``。"""
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # PyMuPDF 旧版只暴露 fitz
        except ImportError as exc:
            raise OcrUnavailableError(
                '未安装 OCR 支持，请执行 pip install "mcp-scrape[ocr]" '
                "并安装系统 tesseract 与对应语言包"
            ) from exc
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise OcrUnavailableError(
            '未安装 OCR 支持，请执行 pip install "mcp-scrape[ocr]" '
            "并安装系统 tesseract 与对应语言包"
        ) from exc

    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise OcrError(f"OCR 打开 PDF 失败: {type(exc).__name__}: {exc}") from exc

    total = document.page_count
    limit = max(1, min(total, max_pages)) if total else 0
    pages: list[str] = []
    for index in range(limit):
        try:
            pixmap = document[index].get_pixmap(dpi=dpi)
            image = Image.open(BytesIO(pixmap.tobytes("png")))
            text = pytesseract.image_to_string(image, lang=lang)
        except Exception as exc:  # 单页失败不影响其余页
            raise OcrError(f"OCR 第 {index + 1} 页失败: {type(exc).__name__}: {exc}") from exc
        text = _clean(text)
        if text:
            pages.append(text)
    return pages, total, limit


def _clean(text: str) -> str:
    text = (text or "").replace("\x00", "").replace("\xa0", " ")
    return _MULTI_NL.sub("\n\n", text).strip()
