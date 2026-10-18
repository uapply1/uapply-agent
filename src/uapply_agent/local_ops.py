"""Non-LLM local work: page rendering, text-layer extraction, HEIC conversion."""
from __future__ import annotations

import re
from pathlib import Path

from .constants import HEIC_EXTENSIONS


def render_pdf_page(pdf: Path, page: int, out_dir: Path, dpi: int = 150) -> Path:
    import pymupdf  # PyMuPDF
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{pdf.stem}_p{page:03d}.png"
    if out.exists():
        return out
    with pymupdf.open(pdf) as doc:
        pix = doc[page - 1].get_pixmap(dpi=dpi)
        pix.save(out)
    return out


def pdf_page_count(pdf: Path) -> int:
    import pymupdf
    with pymupdf.open(pdf) as doc:
        return doc.page_count


def looks_like_real_text(text: str, min_ratio: float = 0.85) -> bool:
    """Guard against a text layer that is mojibake or symbol soup: most non-space
    characters must be letters, digits, CJK or common punctuation."""
    import unicodedata
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return False
    ok = 0
    for c in chars:
        cat = unicodedata.category(c)
        if c.isalnum() or cat.startswith("P") or cat in ("Sc", "Sm") or c in "()[]{}<>/\\|-_+=*&%$#@!?.,;:'\"":
            ok += 1
        elif cat.startswith("C") or cat in ("So",):  # control chars, private use, symbols other
            pass
        else:
            ok += 1
    return ok / len(chars) >= min_ratio


# Control characters (except newline, tab, return) and lone surrogates: PDF text layers can carry
# them, and the backend's database cannot store them.
_UNSTORABLE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]")


def clean_extracted_text(text: str) -> str:
    return _UNSTORABLE.sub("", text)


def pdf_pages_text(pdf: Path, min_chars_per_page: int = 40, force_ocr: bool = False) -> list[str] | None:
    """One string per page from the text layer, or None when the PDF is a scan
    (or its text layer does not look like real text, or OCR is forced)."""
    if force_ocr:
        return None
    import pdfplumber
    pages = []
    with pdfplumber.open(pdf) as doc:
        for page in doc.pages:
            pages.append(clean_extracted_text(page.extract_text() or ""))
    joined = "".join(pages)
    if not pages or sum(len(t.strip()) for t in pages) < min_chars_per_page * len(pages):
        return None
    if not looks_like_real_text(joined):
        return None
    return pages


def render_pdf_pages(pdf: Path, out_dir: Path, first: int, last: int, dpi: int = 150) -> list[Path]:
    return [render_pdf_page(pdf, n, out_dir, dpi) for n in range(first, last + 1)]


def heic_to_jpeg(src: Path, out_dir: Path, quality: int = 92) -> Path:
    import pillow_heif
    from PIL import Image
    pillow_heif.register_heif_opener()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem}.jpg"
    if out.exists():
        return out
    with Image.open(src) as im:
        im.convert("RGB").save(out, "JPEG", quality=quality)
    return out


def to_image_for_model(path: Path, cache: Path) -> Path:
    """Whatever the task input is, hand the model a PNG/JPEG path."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return render_pdf_page(path, 1, cache)
    if ext in HEIC_EXTENSIONS:
        return heic_to_jpeg(path, cache)
    return path


def docx_text(path: Path) -> str:
    """A Word file's paragraphs and tables (tables as markdown), in document order, like uApply's
    own python-docx reader."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(str(path))
    parts = []
    for child in document.element.body.iterchildren():
        if child.tag.endswith('}p'):
            text = Paragraph(child, document).text.strip()
            if text:
                parts.append(text)
        elif child.tag.endswith('}tbl'):
            rows = [[c.text.strip().replace('\n', '<br>').replace('|', '\\|') for c in row.cells]
                    for row in Table(child, document).rows]
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [''] * (width - len(r)) for r in rows]
                lines = ['| ' + ' | '.join(r) + ' |' for r in rows]
                lines.insert(1, '|' + ' --- |' * width)
                parts.append('\n'.join(lines))
    return '\n\n'.join(parts)


def xlsx_text(path: Path) -> str:
    """Every sheet as '# 表格: <name>' plus a markdown table, the shape uApply reads Excel in."""
    import warnings

    import openpyxl

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
        workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        sheets = []
        for name in workbook.sheetnames:
            rows = [[cell if cell is not None else "" for cell in row]
                    for row in workbook[name].iter_rows(values_only=True)]
            rows = [r for r in rows if any(str(v).strip() for v in r)]
            if not rows or not rows[0]:
                table = "*(Empty sheet)*"
            else:
                headers = [str(c) for c in rows[0]]
                lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
                for r in rows[1:]:
                    padded = (list(r) + [""] * len(headers))[:len(headers)]
                    lines.append("| " + " | ".join(str(c) for c in padded) + " |")
                table = "\n".join(lines)
            sheets.append(f"# 表格: {name}\n\n{table}")
        return "\n\n".join(sheets)
    finally:
        workbook.close()
