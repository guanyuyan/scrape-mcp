"""PDF 文本抽取的离线测试：手写最小 PDF，不联网也不需要真实文档。"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scrape_mcp.extract import pdf as pdf_mod
from scrape_mcp.extract.pdf import PdfError, PdfUnavailableError, pdf_pages


def _text_stream(text: str) -> bytes:
    return f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")


def _pdf(pages: list[bytes]) -> bytes:
    """按对象顺序生成带 xref 的最小 PDF；pages 里空 bytes 表示该页没有文字层。"""
    kids = b" ".join(b"%d 0 R" % (4 + i * 2) for i in range(len(pages)))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(pages),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, content in enumerate(pages):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (5 + index * 2)
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Root 1 0 R /Size %d >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_pos,
    )
    return bytes(out)


def test_extracts_text_with_page_counts():
    data = _pdf([_text_stream("Hello PDF")])
    pages, total, parsed = pdf_pages(data)
    assert (total, parsed) == (1, 1)
    assert pages == ["Hello PDF"]


def test_page_without_text_layer_is_skipped():
    # 扫描件的典型形态：页面存在但没有可抽取的文字层
    data = _pdf([b"", _text_stream("Page Two")])
    pages, total, parsed = pdf_pages(data)
    assert (total, parsed) == (2, 2)
    assert pages == ["Page Two"]


def test_max_pages_limits_parsing():
    data = _pdf([_text_stream("P1"), _text_stream("P2")])
    pages, total, parsed = pdf_pages(data, max_pages=1)
    assert (total, parsed) == (2, 1)
    assert pages == ["P1"]


def test_broken_file_raises_pdf_error():
    with pytest.raises(PdfError):
        pdf_pages(b"definitely not a pdf")


def test_missing_dependency_raises_unavailable(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("no pypdf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(PdfUnavailableError, match="mcp-scrape\\[pdf\\]"):
        pdf_pages(b"%PDF-1.4")


def test_clean_strips_control_chars_and_blank_lines():
    assert pdf_mod._clean("a\x00b\xa0c\n\n\n\nd") == "ab c\n\nd"
