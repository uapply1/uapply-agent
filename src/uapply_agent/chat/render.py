"""Transcript markdown → a text-layer PDF the pipeline can read with pdfplumber."""
from __future__ import annotations

from pathlib import Path

import pymupdf

FONT = "china-s"      # PyMuPDF built-in CJK-capable font (covers Latin too)
SIZE = 10
LEADING = SIZE * 1.45
MARGIN = 48


def _wrap(line: str, max_width: float) -> list[str]:
    """Break a line by character (transcripts are mostly CJK, which has no spaces to break at)."""
    if not line:
        return [""]
    out, cur, width = [], "", 0.0
    for ch in line:
        w = pymupdf.get_text_length(ch, fontname=FONT, fontsize=SIZE)
        if cur and width + w > max_width:
            out.append(cur)
            cur, width = "", 0.0
        cur += ch
        width += w
    out.append(cur)
    return out


def transcript_to_pdf(md_path: Path, pdf_path: Path, title: str, header_lines: list[str]) -> Path:
    text = md_path.read_text(encoding="utf-8", errors="replace")
    page_rect = pymupdf.paper_rect("a4")
    width = page_rect.width - 2 * MARGIN
    lines = [title, ""] + header_lines + ["", "-" * 40, ""]
    for raw in text.splitlines():
        lines.extend(_wrap(raw.rstrip(), width))

    doc = pymupdf.open()
    per_page = int((page_rect.height - 2 * MARGIN) // LEADING)
    for start in range(0, max(len(lines), 1), per_page):
        page = doc.new_page(width=page_rect.width, height=page_rect.height)
        y = MARGIN + SIZE
        for line in lines[start:start + per_page]:
            page.insert_text((MARGIN, y), line, fontname=FONT, fontsize=SIZE)
            y += LEADING
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(pdf_path)
    doc.close()
    return pdf_path
