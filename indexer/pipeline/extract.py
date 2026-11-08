"""Stage 1: Extract. PDF (or HTML, or Markdown) to text, page by page."""

from __future__ import annotations

import logging
from html.parser import HTMLParser
from pathlib import Path

import pdfplumber
from pypdf import PdfReader

from .models import Document, Page

log = logging.getLogger("rahbar.extract")


class _HTMLToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        self._in_table = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("h1", "h2", "h3", "h4", "p", "tr"):
            self.text_parts.append("\n")
        elif tag == "td" or tag == "th":
            self.text_parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("h1", "h2", "h3", "h4", "p", "tr"):
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        clean = data.strip()
        if clean:
            self.text_parts.append(clean + " ")

    def get_text(self) -> str:
        return "".join(self.text_parts)


def _ocr_page(pdf_path: str, page_number: int) -> str:
    """Attempt OCR using pdf2image and pytesseract on a specific page."""
    try:
        from pdf2image import convert_from_path
        import pytesseract

        images = convert_from_path(pdf_path, first_page=page_number, last_page=page_number)
        if images:
            return pytesseract.image_to_string(images[0]).strip()
    except Exception as exc:
        log.warning(f"OCR failed for {pdf_path} page {page_number}: {exc}")
    return ""


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
            with pdfplumber.open(file_path) as pdf:
                for idx, pl_page in enumerate(pdf.pages, start=1):
                    is_ocr = False
                    text = pl_page.extract_text() or ""
                    
                    # Extract tables cleanly and append as markdown tables
                    tables = pl_page.extract_tables()
                    table_texts = []
                    for table in tables:
                        for row in table:
                            clean_row = [str(cell).strip() if cell else "" for cell in row]
                            table_texts.append(" | ".join(clean_row))
                    if table_texts:
                        text += "\n\n" + "\n".join(table_texts)

                    # If page has virtually no text layer, attempt OCR
                    if len(text.strip()) < 20:
                        ocr_result = _ocr_page(str(file_path), idx)
                        if ocr_result:
                            text = ocr_result
                            is_ocr = True

                    pages.append(Page(source=source, page=idx, text=text, ocr=is_ocr))
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
        pages.append(Page(source=source, page=1, text=text, ocr=False))

    return Document(source=source, doc_version=doc_version, superseded=superseded, pages=pages)
