"""Stage 2: Clean. Remove artefacts without destroying structure."""

from __future__ import annotations

import re
from .models import Document, Page

# Patterns for repeated running headers, footers, and page numbers
RUNNING_HEADER_PATTERNS = [
    re.compile(r"^Page \d+(?: of \d+)?$", re.M | re.I),
    re.compile(r"^FAST-NUCES (?:Student|Academic) Handbook.*$", re.M | re.I),
    re.compile(r"^Undergraduate Academic Rules and Regulations \d{4}$", re.M | re.I),
    re.compile(r"^Student Handbook \d{4}$", re.M | re.I),
    re.compile(r"^NATIONAL UNIVERSITY OF COMPUTER AND EMERGING SCIENCES$", re.M),
    re.compile(r"^Annual Report \d{4}-\d{2,4}$", re.M | re.I),
    re.compile(r"^\d+\s*\|\s*P a g e$", re.M | re.I),
    re.compile(r"^P a g e\s*\|\s*\d+$", re.M | re.I),
]


def clean_text(raw: str) -> str:
    """Clean repeating headers and footers while preserving headings and section boundaries."""
    text = raw
    for pat in RUNNING_HEADER_PATTERNS:
        text = pat.sub("", text)

    # Normalize excessive carriage returns and non-breaking spaces
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    # Collapse 3+ newlines to 2 newlines (preserve paragraphs)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Collapse multiple inline spaces to single space
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def clean(doc: Document) -> Document:
    """Strip headers and footers that repeat on every page; keep headings and
    section boundaries, because chunking and citations depend on them."""
    cleaned_pages: list[Page] = []
    for page in doc.pages:
        cleaned_text = clean_text(page.text)
        cleaned_pages.append(
            Page(source=page.source, page=page.page, text=cleaned_text, ocr=page.ocr)
        )
    return Document(
        source=doc.source,
        doc_version=doc.doc_version,
        superseded=doc.superseded,
        pages=cleaned_pages,
    )
