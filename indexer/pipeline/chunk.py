"""Stage 3: Chunk. Structure-aware, with overlap."""

from __future__ import annotations

import re
from .models import Chunk, Document

HEADING_PATTERN = re.compile(
    r"^(?:#{1,6}\s+\S+|(?:\d+\.)+\d*\s+[A-Z][^.!?\n]{1,75}|[A-Z][A-Za-z0-9\s,'’/-]{2,60}:)$"
)
_CONNECTORS = frozenset({"a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "under", "with"})
_IGNORE_HEADINGS = frozenset({"vision", "mission", "disclaimer", "foreword", "table of contents"})


def _is_heading(line: str) -> bool:
    """Return True for Markdown headers or concise section titles, not full prose sentences or TOC entries."""
    s = line.strip()
    if not s:
        return False
    if s.startswith("#"):
        return True
    # Reject Table of Contents lines ending in a page number (e.g. "Attendance Requirements 4")
    if re.search(r"\s+\d{1,3}$", s) or "...." in s:
        return False
    if s.lower() in _IGNORE_HEADINGS:
        return False
    if len(s) > 80 or len(s.split()) > 10 or s.endswith((".", "!", "?", ",", ";")):
        return False
    if HEADING_PATTERN.match(s):
        return True
    # Unnumbered Title-Case section titles common in handbooks (e.g. "Academic Warning/Probation")
    words = s.split()
    if 2 <= len(words) <= 8 and s[0].isupper() and "|" not in s:
        content_words = [w for w in words if w.lower() not in _CONNECTORS]
        if content_words and all(w[0].isupper() for w in content_words) and any(len(w) >= 4 for w in content_words):
            return True
    return False


def _split_paragraphs_on_headings(text: str) -> list[str]:
    """Split page text on blank lines and before any embedded heading lines."""
    blocks: list[str] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        if _is_heading(line) and current:
            blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _merge_standalone_headings(paragraphs: list[str]) -> list[str]:
    """Attach a bare single-line heading paragraph to its following body paragraph so chunks retain context."""
    merged: list[str] = []
    i = 0
    while i < len(paragraphs):
        p = paragraphs[i]
        next_is_heading = (
            i + 1 < len(paragraphs) and _is_heading(paragraphs[i + 1].split("\n")[0])
        )
        if "\n" not in p and _is_heading(p) and i + 1 < len(paragraphs) and not next_is_heading:
            merged.append(f"{p}\n{paragraphs[i + 1]}")
            i += 2
        else:
            merged.append(p)
            i += 1
    return merged


def _approx_token_count(text: str) -> int:
    """Approximate token count based on words (standard ~1.3 tokens per word)."""
    words = text.split()
    return max(1, int(len(words) * 1.3))


def _split_into_sentences(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return [s.strip() for s in sentences if s.strip()]


def chunk(doc: Document, size: int, overlap: int) -> list[Chunk]:
    """Split on sections, then paragraphs, then sentences, keeping each chunk under
    `size` tokens, with `overlap` tokens shared between neighbours. Every chunk keeps
    the section heading it sits under, its page, doc_version and superseded."""
    chunks: list[Chunk] = []
    current_section = "General"
    chunk_index = 0

    for page in doc.pages:
        raw_paragraphs = _split_paragraphs_on_headings(page.text)
        paragraphs = _merge_standalone_headings(raw_paragraphs)

        for para in paragraphs:
            # Check if paragraph begins with a heading or clause title
            lines = para.split("\n")
            first_line = lines[0].strip()
            if _is_heading(first_line):
                current_section = first_line.lstrip("#").rstrip(":").strip()

            para_tokens = _approx_token_count(para)
            if para_tokens <= size:
                # Whole paragraph fits in one chunk
                chunks.append(
                    Chunk(
                        id=f"{doc.source}-{page.page}-{chunk_index}",
                        text=para,
                        source=doc.source,
                        section=current_section,
                        page=page.page,
                        doc_version=doc.doc_version,
                        superseded=doc.superseded,
                    )
                )
                chunk_index += 1
            else:
                # Structure-aware split: break paragraph into sentences and window with overlap
                sentences = _split_into_sentences(para)
                buffer: list[str] = []
                buffer_tokens = 0

                for sent in sentences:
                    stoks = _approx_token_count(sent)
                    if buffer_tokens + stoks > size and buffer:
                        chunk_text = " ".join(buffer)
                        chunks.append(
                            Chunk(
                                id=f"{doc.source}-{page.page}-{chunk_index}",
                                text=chunk_text,
                                source=doc.source,
                                section=current_section,
                                page=page.page,
                                doc_version=doc.doc_version,
                                superseded=doc.superseded,
                            )
                        )
                        chunk_index += 1
                        
                        # Retain overlap sentences from the end of the buffer
                        overlap_buffer: list[str] = []
                        overlap_tokens = 0
                        for s in reversed(buffer):
                            st = _approx_token_count(s)
                            if overlap_tokens + st <= overlap:
                                overlap_buffer.insert(0, s)
                                overlap_tokens += st
                            else:
                                break
                        buffer = overlap_buffer
                        buffer_tokens = overlap_tokens

                    buffer.append(sent)
                    buffer_tokens += stoks

                if buffer:
                    chunk_text = " ".join(buffer)
                    chunks.append(
                        Chunk(
                            id=f"{doc.source}-{page.page}-{chunk_index}",
                            text=chunk_text,
                            source=doc.source,
                            section=current_section,
                            page=page.page,
                            doc_version=doc.doc_version,
                            superseded=doc.superseded,
                        )
                    )
                    chunk_index += 1

    return chunks
