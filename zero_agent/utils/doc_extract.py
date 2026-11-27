"""零依赖文档解析（DOCX / XLSX / PPTX / macOS PDF）。

设计目标：在未安装 zero-agent[docs]（pypdf / python-docx / openpyxl / python-pptx）
的环境下，仍能提取正文文本。

- DOCX/XLSX/PPTX 走标准库 zipfile + xml.etree（OOXML 本质是 zip+xml）
- PDF 优先 pypdf，其次 macOS 原生 PDFKit（PyObjC），都不行则给出可执行报错
"""

from __future__ import annotations

import re
import zipfile
from typing import Any, Dict, List, Tuple

_DOCX_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_PPTX_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


# ---------------------------------------------------------------- page spec
def parse_page_spec(spec: Any, total: int) -> List[int]:
    """把 "1-3,5" 这类页码描述解析为 0 基索引列表。"""
    if total <= 0:
        return []
    if spec in (None, ""):
        return list(range(min(total, 20)))
    out: List[int] = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            low, _, high = part.partition("-")
            try:
                start = int(low) if low else 1
                end = int(high) if high else total
            except ValueError:
                continue
            for index in range(start, end + 1):
                if 1 <= index <= total:
                    out.append(index - 1)
        else:
            try:
                index = int(part)
            except ValueError:
                continue
            if 1 <= index <= total:
                out.append(index - 1)
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------- docx
def extract_docx(path: str) -> Tuple[str, Dict[str, Any]]:
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "word/document.xml" not in names:
            raise RuntimeError(f"not a DOCX file (missing word/document.xml): {path}")
        root = ET.fromstring(archive.read("word/document.xml"))

    lines: List[str] = []
    paragraphs = 0
    for paragraph in root.iter(_DOCX_W + "p"):
        texts = [node.text or "" for node in paragraph.iter(_DOCX_W + "t")]
        text = "".join(texts).strip()
        if text:
            lines.append(text)
            paragraphs += 1
    return "\n".join(lines), {"paragraphs": paragraphs, "backend": "zipfile"}


# ------------------------------------------------------------------ xlsx
def _col_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref or "")
    if not letters:
        return 0
    index = 0
    for char in letters.group(1):
        index = index * 26 + (ord(char) - 64)
    return max(index - 1, 0)


def extract_xlsx(path: str, max_rows: int = 2000) -> Tuple[str, Dict[str, Any]]:
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.iter(_XLSX_NS + "si"):
                shared.append("".join(node.text or "" for node in item.iter(_XLSX_NS + "t")))

        sheets = sorted(
            (n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
            key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[-1]).group(1)),
        )
        if not sheets:
            raise RuntimeError(f"not an XLSX file (no worksheets): {path}")

        lines: List[str] = []
        rows_seen = 0
        for sheet_name in sheets:
            root = ET.fromstring(archive.read(sheet_name))
            for row in root.iter(_XLSX_NS + "row"):
                if rows_seen >= max_rows:
                    break
                cells: Dict[int, str] = {}
                for cell in row.iter(_XLSX_NS + "c"):
                    kind = cell.get("t")
                    if kind == "inlineStr":
                        value = "".join(n.text or "" for n in cell.iter(_XLSX_NS + "t"))
                    else:
                        node = cell.find(_XLSX_NS + "v")
                        value = node.text if node is not None and node.text else ""
                        if kind == "s" and value.isdigit():
                            idx = int(value)
                            value = shared[idx] if idx < len(shared) else ""
                    cells[_col_index(cell.get("r") or "")] = value
                if not cells:
                    continue
                row_values = [cells.get(index, "") for index in range(max(cells) + 1)]
                while row_values and row_values[-1] == "":
                    row_values.pop()
                if not row_values:
                    continue
                lines.append("\t".join(row_values))
                rows_seen += 1
            if rows_seen >= max_rows:
                break
    return "\n".join(lines), {"rows": rows_seen, "backend": "zipfile"}


# ------------------------------------------------------------------ pptx
def extract_pptx(path: str) -> Tuple[str, Dict[str, Any]]:
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        slides = sorted(
            (n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
            key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[-1]).group(1)),
        )
        if not slides:
            raise RuntimeError(f"not a PPTX file (no slides): {path}")
        parts: List[str] = []
        for index, slide in enumerate(slides, 1):
            root = ET.fromstring(archive.read(slide))
            texts = [node.text for node in root.iter(_PPTX_A + "t") if node.text]
            if texts:
                parts.append(f"--- slide {index} ---\n" + "\n".join(texts))
    return "\n".join(parts), {"slides": len(slides), "backend": "zipfile"}


# ------------------------------------------------------------ pdf (macOS)
def extract_pdf_quartz(path: str, pages: Any) -> Tuple[str, Dict[str, Any]]:
    """macOS 原生 PDFKit 提取。仅 macOS 可用（PyObjC）。"""
    try:
        import Quartz  # type: ignore
        from Foundation import NSURL  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "PDF support requires pypdf (pip install 'zero-agent[docs]')"
            " or macOS PyObjC-framework-Quartz",
        ) from exc

    url = NSURL.fileURLWithPath_(path)
    document = Quartz.PDFDocument.alloc().initWithURL_(url)
    if document is None:
        raise RuntimeError(f"failed to open PDF: {path}")
    total = int(document.pageCount())
    selected = parse_page_spec(pages, total)
    parts: List[str] = []
    for index in selected:
        text = document.pageAtIndex_(index).string() or ""
        parts.append(f"--- page {index + 1} ---\n{text}")
    return "\n\n".join(parts), {
        "pages": [i + 1 for i in selected],
        "total_pages": total,
        "backend": "quartz",
    }
