"""Stage 1: Extract. PDF (or HTML, or Markdown) to text, page by page."""

from __future__ import annotations

import logging
import os
from html.parser import HTMLParser
from pathlib import Path

import pdfplumber
from pypdf import PdfReader

from .models import Document, Page

log = logging.getLogger("rahbar.extract")


_SKIP_HTML_TAGS = frozenset({"style", "script", "noscript", "svg", "nav", "header", "footer"})
_HEADING_HTML_TAGS = frozenset({"h1", "h2", "h3", "h4"})


class _HTMLToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        self._skip_depth = 0
        self._heading_level: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _SKIP_HTML_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if t in _HEADING_HTML_TAGS:
            self._heading_level = int(t[1])
            self.text_parts.append("\n\n" + ("#" * self._heading_level) + " ")
        elif t in ("p", "tr", "li", "div", "section", "br"):
            self.text_parts.append("\n")
        elif t in ("td", "th"):
            self.text_parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _SKIP_HTML_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if t in _HEADING_HTML_TAGS:
            self._heading_level = None
            self.text_parts.append("\n\n")
        elif t in ("p", "tr", "li"):
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        clean = " ".join(data.split())
        if clean:
            self.text_parts.append(clean + " ")

    def get_text(self) -> str:
        return "".join(self.text_parts)


def _ocr_page(pdf_path: str, page_number: int) -> str:
    """Attempt OCR using pdf2image and pytesseract on a specific page."""
    return _ocr_page_range(pdf_path, page_number, page_number).get(page_number, "")


def _ocr_page_range(pdf_path: str, first_page: int, last_page: int) -> dict[int, str]:
    """Batch-rasterize a contiguous page range once and OCR each page with low memory."""
    results: dict[int, str] = {}
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    try:
        from pdf2image import convert_from_path
        import pytesseract

        # 130 DPI grayscale reduces bitmap memory ~6x vs default 200 DPI RGB while keeping text crisp.
        images = convert_from_path(
            pdf_path,
            dpi=130,
            grayscale=True,
            first_page=first_page,
            last_page=last_page,
        )
        try:
            for offset, img in enumerate(images):
                pno = first_page + offset
                text = pytesseract.image_to_string(img, config="--psm 6").strip()
                if text:
                    results[pno] = text
        finally:
            for img in images:
                img.close()
    except Exception as exc:
        log.warning(f"OCR failed for {pdf_path} pages {first_page}-{last_page}: {exc}")
    return results


def _run_batched_ocr(pdf_path: str, page_numbers: list[int], batch_size: int = 6) -> dict[int, str]:
    """OCR only contiguous groups in small batches to avoid re-parsing large PDFs per page."""
    if not page_numbers:
        return {}
    texts: dict[int, str] = {}
    i = 0
    while i < len(page_numbers):
        start = page_numbers[i]
        end = start
        while i + 1 < len(page_numbers) and page_numbers[i + 1] == end + 1 and (end - start + 1) < batch_size:
            i += 1
            end = page_numbers[i]
        texts.update(_ocr_page_range(pdf_path, start, end))
        i += 1
    return texts


def extract(path: str, doc_version: str, superseded: bool) -> Document:
    """Return the document's text page by page.

    Detect pages with no text layer and OCR them; mark those pages ocr=True.
    Preserves table structure and document headings.
    """
    file_path = Path(path)
    source = file_path.name
    suffix = file_path.suffix.lower()
    pages: list[Page] = []

    if suffix == ".pdf":
        try:
            ocr_needed: list[int] = []
            with pdfplumber.open(file_path) as pdf:
                for idx, pl_page in enumerate(pdf.pages, start=1):
                    try:
                        text = pl_page.extract_text() or ""

                        # Extract tables only on pages with grid lines/rectangles to avoid costly scans on prose pages.
                        if len(pl_page.lines) >= 4 or len(pl_page.rects) >= 4:
                            tables = pl_page.extract_tables()
                            table_texts = []
                            for table in tables:
                                for row in table:
                                    clean_row = [str(cell).strip() if cell else "" for cell in row]
                                    non_empty = [c for c in clean_row if c]
                                    if len(non_empty) >= 2 and any(any(ch.isalnum() for ch in c) for c in non_empty):
                                        table_texts.append(" | ".join(clean_row))
                            if table_texts:
                                text += "\n\n" + "\n".join(table_texts)

                        if len(text.strip()) < 20:
                            ocr_needed.append(idx)

                        pages.append(Page(source=source, page=idx, text=text, ocr=False))
                    finally:
                        pl_page.flush_cache()

            if ocr_needed:
                ocr_texts = _run_batched_ocr(str(file_path), ocr_needed)
                for p in pages:
                    ocr_result = ocr_texts.get(p.page)
                    if ocr_result:
                        p.text = ocr_result
                        p.ocr = True
        except Exception as exc:
            log.warning(f"pdfplumber failed for {source}, falling back to pypdf: {exc}")
            reader = PdfReader(str(file_path))
            for idx, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                pages.append(Page(source=source, page=idx, text=text, ocr=False))

    elif suffix in (".html", ".htm"):
        raw_html = file_path.read_text(encoding="utf-8", errors="replace")
        parser = _HTMLToText()
        parser.feed(raw_html)
        text = parser.get_text()
        pages.append(Page(source=source, page=1, text=text, ocr=False))

    else:
        text = file_path.read_text(encoding="utf-8", errors="replace")
        # Strip YAML front matter (used by CI fixture Markdown files) so metadata is not indexed as prose
        lines = text.splitlines()
        if lines and lines[0].strip() == "---":
            for end_idx in range(1, len(lines)):
                if lines[end_idx].strip() == "---":
                    text = "\n".join(lines[end_idx + 1 :]).lstrip()
                    break
        pages.append(Page(source=source, page=1, text=text, ocr=False))

    return Document(source=source, doc_version=doc_version, superseded=superseded, pages=pages)
