"""Non-LLM local work: page rendering, text-layer extraction, HEIC conversion."""
from __future__ import annotations

from pathlib import Path
from typing import Optional


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


def pdf_text(pdf: Path, min_chars_per_page: int = 40) -> Optional[str]:
    """Text-layer content with `Page N:` markers, or None when the PDF is a scan."""
    import pdfplumber
    pages = []
    with pdfplumber.open(pdf) as doc:
        for i, page in enumerate(doc.pages, start=1):
            pages.append(page.extract_text() or "")
    if not pages or sum(len(t.strip()) for t in pages) < min_chars_per_page * len(pages):
        return None
    return "\n\n".join(f"Page {i}:\n{t}" for i, t in enumerate(pages, start=1))


def pdf_pages_text(pdf: Path, min_chars_per_page: int = 40) -> Optional[list[str]]:
    """One string per page from the text layer, or None when the PDF is a scan."""
    import pdfplumber
    pages = []
    with pdfplumber.open(pdf) as doc:
        for page in doc.pages:
            pages.append(page.extract_text() or "")
    if not pages or sum(len(t.strip()) for t in pages) < min_chars_per_page * len(pages):
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
    if ext in (".heic", ".heif"):
        return heic_to_jpeg(path, cache)
    return path
