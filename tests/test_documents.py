"""Office 文档抽取、内容形态识别与扫描 PDF 的 OCR 回退。"""

from __future__ import annotations

import asyncio
import json
import zipfile
from io import BytesIO

import pytest

from scrape_mcp import server
from scrape_mcp.core import errors
from scrape_mcp.core.fetcher import FetchOutcome, _content_kind
from scrape_mcp.extract.documents import DocumentError, document_text
from scrape_mcp.extract.ocr import OcrError

URL = "https://a.test/report.docx"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

_DOC_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_SHEET_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_SLIDE_NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def _zip(entries: dict[str, str]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        for name, body in entries.items():
            package.writestr(name, body)
    return buffer.getvalue()


def _docx(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = f"<w:document {_DOC_NS}><w:body>{body}</w:body></w:document>"
    return _zip({"word/document.xml": xml})


def _xlsx() -> bytes:
    shared = f"<sst {_SHEET_NS}><si><t>姓名</t></si><si><t>城市</t></si></sst>"
    sheet = (
        f"<worksheet {_SHEET_NS}><sheetData>"
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>张三</t></is></c>'
        '<c r="B2"><v>42</v></c></row>'
        "</sheetData></worksheet>"
    )
    return _zip({"xl/sharedStrings.xml": shared, "xl/worksheets/sheet1.xml": sheet})


def _pptx() -> bytes:
    def slide(text: str) -> str:
        return (
            f"<sld {_SLIDE_NS}><a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
            "<a:p><a:r><a:t>副标题</a:t></a:r></a:p></sld>"
        )

    return _zip(
        {
            "ppt/slides/slide1.xml": slide("第一页"),
            "ppt/slides/slide2.xml": slide("第二页"),
        }
    )


def test_document_text_docx_paragraphs():
    assert document_text("docx", _docx("第一章", "正文一", "正文二")) == "第一章\n正文一\n正文二"


def test_document_text_xlsx_shared_and_inline_cells():
    text = document_text("xlsx", _xlsx())
    assert "[工作表 1]" in text
    assert "姓名\t城市" in text
    assert "张三\t42" in text


def test_document_text_pptx_keeps_slide_order():
    text = document_text("pptx", _pptx())
    assert "第一页" in text and "第二页" in text
    assert text.index("第一页") < text.index("第二页")


def test_document_text_rejects_bad_package():
    with pytest.raises(DocumentError):
        document_text("docx", b"not a zip")
    with pytest.raises(DocumentError):
        document_text("docx", _zip({"word/other.xml": "<a/>"}))


def test_content_kind_detects_office_formats():
    assert _content_kind(DOCX_MIME, URL) == "docx"
    assert _content_kind(XLSX_MIME, URL) == "xlsx"
    assert _content_kind(PPTX_MIME, URL) == "pptx"
    assert _content_kind("application/octet-stream", "https://a.test/a.pptx") == "pptx"
    assert _content_kind("text/html", "https://a.test/x") == "html"


class _FakeFetcher:
    def __init__(self, outcome: FetchOutcome) -> None:
        self.outcome = outcome

    async def fetch(self, _url: str, **_kwargs) -> FetchOutcome:
        return self.outcome

    async def aclose(self) -> None:
        pass


def _patch(monkeypatch, outcome: FetchOutcome) -> None:
    async def allowed(_url: str, *_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(server, "_compliance_check", allowed)
    monkeypatch.setattr(server, "_get_fetcher", lambda: _FakeFetcher(outcome))


def _doc_outcome(kind: str, data: bytes) -> FetchOutcome:
    return FetchOutcome(
        url=URL,
        final_url=URL,
        status=200,
        tier="l1",
        blocked="none",
        block_reason="",
        content_type="application/octet-stream",
        html="",
        elapsed_ms=5,
        kind=kind,
        content=data,
    )


def test_web_fetch_extracts_docx(monkeypatch):
    _patch(monkeypatch, _doc_outcome("docx", _docx("季度总结", "收入增长 12%")))
    out = json.loads(asyncio.run(server.web_fetch(URL, max_tokens=2000)))
    assert out["ok"] is True
    assert out["kind"] == "docx"
    assert "季度总结" in out["content"]


def test_web_fetch_reports_broken_office_package(monkeypatch):
    _patch(monkeypatch, _doc_outcome("xlsx", b"broken"))
    out = json.loads(asyncio.run(server.web_fetch(URL, max_tokens=2000)))
    assert out["error_code"] == errors.UNSUPPORTED_CONTENT


def test_web_fetch_ocrs_scanned_pdf_when_enabled(monkeypatch, _isolated_data_dir):
    _isolated_data_dir.ocr_enabled = True
    monkeypatch.setattr(server, "pdf_pages", lambda *_a, **_k: ([], 2, 2))
    monkeypatch.setattr(server, "ocr_pages", lambda *_a, **_k: (["识别文本"], 2, 2))
    outcome = _doc_outcome("pdf", b"%PDF-1.4")
    _patch(monkeypatch, outcome)

    out = json.loads(asyncio.run(server.web_fetch(URL, max_tokens=2000)))
    assert out["ok"] is True
    assert out["ocr"] is True
    assert "识别文本" in out["content"]


def test_web_fetch_scanned_pdf_without_ocr_is_empty(monkeypatch, _isolated_data_dir):
    monkeypatch.setattr(server, "pdf_pages", lambda *_a, **_k: ([], 2, 2))
    outcome = _doc_outcome("pdf", b"%PDF-1.4")
    _patch(monkeypatch, outcome)

    out = json.loads(asyncio.run(server.web_fetch(URL, max_tokens=2000)))
    assert out["error_code"] == errors.EMPTY
    assert "扫描件" in out["error"]


def test_web_fetch_ocr_unavailable_is_unsupported(monkeypatch, _isolated_data_dir):
    _isolated_data_dir.ocr_enabled = True

    def boom(*_args, **_kwargs):
        raise OcrError("未安装 OCR 支持")

    monkeypatch.setattr(server, "pdf_pages", lambda *_a, **_k: ([], 2, 2))
    monkeypatch.setattr(server, "ocr_pages", boom)
    outcome = _doc_outcome("pdf", b"%PDF-1.4")
    _patch(monkeypatch, outcome)

    out = json.loads(asyncio.run(server.web_fetch(URL, max_tokens=2000)))
    assert out["error_code"] == errors.UNSUPPORTED_CONTENT
    assert "OCR" in out["error"]
