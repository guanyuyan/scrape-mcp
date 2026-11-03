"""Office 文档 → 纯文本：DOCX / XLSX / PPTX（无第三方依赖）。

三种格式本质都是 ZIP + OOXML，只用标准库解压并读取对应 XML 即可拿到文字层。
压缩包解压上限用于防止异常文件把内存撑爆；解析失败统一抛 ``DocumentError``。
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from xml.etree import ElementTree

MAX_PART_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 30 * 1024 * 1024

_SHEET = re.compile(r"xl/worksheets/sheet(\d+)\.xml")
_SLIDE = re.compile(r"ppt/slides/slide(\d+)\.xml")
_MULTI_NL = re.compile(r"\n{3,}")


class DocumentError(RuntimeError):
    """文档包损坏、缺少必要 XML，或超出解压上限。"""


def document_text(kind: str, data: bytes) -> str:
    """按 kind（docx/xlsx/pptx）抽取文字；输出按文档自然顺序排列。"""
    try:
        with zipfile.ZipFile(BytesIO(data)) as package:
            _check_sizes(package)
            if kind == "docx":
                text = _docx_text(package)
            elif kind == "xlsx":
                text = _xlsx_text(package)
            elif kind == "pptx":
                text = _pptx_text(package)
            else:
                raise DocumentError(f"不支持的文档类型: {kind}")
    except zipfile.BadZipFile as exc:
        raise DocumentError(f"{kind.upper()} 不是有效的 OOXML 压缩包: {exc}") from exc
    except KeyError as exc:
        raise DocumentError(f"{kind.upper()} 缺少必要部件: {exc}") from exc
    except ElementTree.ParseError as exc:
        raise DocumentError(f"{kind.upper()} XML 解析失败: {exc}") from exc

    text = (text or "").replace("\x00", "").replace("\xa0", " ")
    return _MULTI_NL.sub("\n\n", text).strip()


def _check_sizes(package: zipfile.ZipFile) -> None:
    total = 0
    for info in package.infolist():
        if info.file_size > MAX_PART_BYTES:
            raise DocumentError(f"文档部件过大: {info.filename}")
        total += info.file_size
    if total > MAX_TOTAL_BYTES:
        raise DocumentError("文档解压后体积超出上限")


def _read(package: zipfile.ZipFile, name: str) -> ElementTree.Element:
    return ElementTree.fromstring(package.read(name))


def _tag_name(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _docx_text(package: zipfile.ZipFile) -> str:
    root = _read(package, "word/document.xml")
    lines: list[str] = []
    for paragraph in root.iter():
        if _tag_name(paragraph) != "p":
            continue
        text = "".join(
            node.text or "" for node in paragraph.iter() if _tag_name(node) == "t"
        ).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def _xlsx_text(package: zipfile.ZipFile) -> str:
    shared: list[str] = []
    if "xl/sharedStrings.xml" in package.namelist():
        root = _read(package, "xl/sharedStrings.xml")
        for item in root.iter():
            if _tag_name(item) != "si":
                continue
            shared.append(
                "".join(node.text or "" for node in item.iter() if _tag_name(node) == "t")
            )

    sheets = sorted(
        (name for name in package.namelist() if _SHEET.match(name)),
        key=lambda name: int(_SHEET.match(name).group(1)),  # type: ignore[union-attr]
    )
    sections: list[str] = []
    for order, name in enumerate(sheets, start=1):
        root = _read(package, name)
        rows: list[str] = []
        for row in root.iter():
            if _tag_name(row) != "row":
                continue
            cells: list[str] = []
            for cell in row.iter():
                if _tag_name(cell) != "c":
                    continue
                cells.append(_cell_value(cell, shared))
            if any(cells):
                rows.append("\t".join(cells))
        if rows:
            sections.append(f"[工作表 {order}]\n" + "\n".join(rows))
    return "\n\n".join(sections)


def _cell_value(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.attrib.get("t")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter() if _tag_name(node) == "t").strip()
    raw = ""
    for node in cell.iter():
        if _tag_name(node) == "v":
            raw = (node.text or "").strip()
            break
    if kind == "s" and raw:
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    return raw


def _pptx_text(package: zipfile.ZipFile) -> str:
    slides = sorted(
        (name for name in package.namelist() if _SLIDE.match(name)),
        key=lambda name: int(_SLIDE.match(name).group(1)),  # type: ignore[union-attr]
    )
    parts: list[str] = []
    for name in slides:
        root = _read(package, name)
        lines: list[str] = []
        for paragraph in root.iter():
            if _tag_name(paragraph) != "p":
                continue
            text = "".join(
                node.text or "" for node in paragraph.iter() if _tag_name(node) == "t"
            ).strip()
            if text:
                lines.append(text)
        if lines:
            parts.append("\n".join(lines))
    return "\n\n".join(parts)
