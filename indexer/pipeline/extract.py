"""Stage 1: Extract. PDF (or HTML, or Markdown) to text, page by page."""

from __future__ import annotations

import hashlib
import logging
import os
import re
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


def derive_doc_id(source: str, explicit_doc_id: str = "") -> str:
    """Compute a stable logical document ID across versions of the same document."""
    if explicit_doc_id.strip():
        return re.sub(r"[^a-z0-9]+", "-", explicit_doc_id.lower()).strip("-")
    stem = Path(source).stem.lower()
    stem = re.sub(r"^b\d+_", "", stem)
    stem = re.sub(r"[-_](?:19|20)\d{2}(?:[-_]\d{2,4})?$", "", stem)
    stem = re.sub(r"[-_]v\d+(?:\.\d+)?$", "", stem)
    return re.sub(r"[^a-z0-9]+", "-", stem).strip("-") or "document"


def _extract_markdown_title(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return ""


def _parse_front_matter(raw_text: str) -> tuple[dict[str, str], str]:
    lines = raw_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw_text
    meta: dict[str, str] = {}
    for end_idx in range(1, len(lines)):
        if lines[end_idx].strip() == "---":
            body = "\n".join(lines[end_idx + 1 :]).lstrip()
            return meta, body
        key, _, value = lines[end_idx].partition(":")
        if key.strip():
            meta[key.strip()] = value.strip()
    return {}, raw_text


def extract(
    path: str,
    doc_version: str,
    superseded: bool,
    doc_id: str = "",
    title: str = "",
    date: str = "",
) -> Document:
    """Return the document's text page by page.

    Detect pages with no text layer and OCR them; mark those pages ocr=True.
    Preserves table structure and document headings, and records extraction errors safely.
    """
    file_path = Path(path)
    source = file_path.name
    suffix = file_path.suffix.lower()
    pages: list[Page] = []
    extraction_error: str | None = None
    inferred_title = title.strip()
    resolved_doc_id = doc_id.strip()
    resolved_date = date.strip()
    resolved_version = doc_version.strip()

    try:
        file_bytes = file_path.read_bytes()
        content_hash = hashlib.sha256(file_bytes).hexdigest()
    except Exception as exc:
        return Document(
            source=source,
            doc_version=resolved_version,
            superseded=superseded,
            pages=[],
            doc_id=derive_doc_id(source, resolved_doc_id),
            title=inferred_title,
            date=resolved_date or resolved_version,
            content_hash="",
            extraction_error=f"unreadable file: {exc}",
            metadata_present={
                "source": bool(source),
                "title": bool(inferred_title),
                "date_or_version": bool(resolved_date or (resolved_version and resolved_version.lower() != "unknown")),
                "page_count": False,
            },
        )

    if suffix == ".pdf":
        try:
            ocr_needed: list[int] = []
            reader = PdfReader(str(file_path))
            if not inferred_title and reader.metadata and reader.metadata.title:
                inferred_title = str(reader.metadata.title).strip()
            for idx, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                if len(text.strip()) < 20:
                    ocr_needed.append(idx)
                pages.append(Page(source=source, page=idx, text=text, ocr=False))

            # Run batched OCR only on scanned pages without an embedded text layer.
            if ocr_needed:
                ocr_texts = _run_batched_ocr(str(file_path), ocr_needed)
                for p in pages:
                    ocr_result = ocr_texts.get(p.page)
                    if ocr_result:
                        p.text = ocr_result
                        p.ocr = True
            if not inferred_title and pages:
                inferred_title = _extract_markdown_title(pages[0].text)
        except Exception as exc:
            log.warning(f"pypdf failed for {source}, falling back to pdfplumber: {exc}")
            try:
                with pdfplumber.open(file_path) as pdf:
                    for idx, pl_page in enumerate(pdf.pages, start=1):
                        try:
                            text = pl_page.extract_text() or ""
                            pages.append(Page(source=source, page=idx, text=text, ocr=False))
                        finally:
                            pl_page.flush_cache()
                if not inferred_title and pages:
                    inferred_title = _extract_markdown_title(pages[0].text)
            except Exception as pl_exc:
                extraction_error = f"corrupt or unreadable PDF: {pl_exc}"

    elif suffix in (".html", ".htm"):
        raw_html = file_bytes.decode("utf-8", errors="replace")
        if not inferred_title:
            m = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.I | re.S)
            if m:
                inferred_title = " ".join(m.group(1).split())
        parser = _HTMLToText()
        parser.feed(raw_html)
        text = parser.get_text()
        if not inferred_title:
            inferred_title = _extract_markdown_title(text)
        pages.append(Page(source=source, page=1, text=text, ocr=False))

    else:
        raw_text = file_bytes.decode("utf-8", errors="replace")
        fm, text = _parse_front_matter(raw_text)
        if not resolved_doc_id and fm.get("doc_id"):
            resolved_doc_id = fm["doc_id"]
        if not inferred_title and fm.get("title"):
            inferred_title = fm["title"]
        if not resolved_date and fm.get("date"):
            resolved_date = fm["date"]
        if (not resolved_version or resolved_version.lower() == "unknown") and fm.get("doc_version"):
            resolved_version = fm["doc_version"]
        if not inferred_title and "title" not in fm:
            inferred_title = _extract_markdown_title(text)
        pages.append(Page(source=source, page=1, text=text, ocr=False))

    final_doc_id = derive_doc_id(source, resolved_doc_id)
    final_date = resolved_date or (resolved_version if resolved_version.lower() != "unknown" else "")
    return Document(
        source=source,
        doc_version=resolved_version,
        superseded=superseded,
        pages=pages,
        doc_id=final_doc_id,
        title=inferred_title,
        date=final_date,
        content_hash=content_hash,
        extraction_error=extraction_error,
        metadata_present={
            "source": bool(source.strip()),
            "title": bool(inferred_title.strip()),
            "date_or_version": bool(final_date or (resolved_version and resolved_version.lower() != "unknown")),
            "page_count": len(pages) > 0,
        },
    )
