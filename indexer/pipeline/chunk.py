"""Stage 3: Chunk. Structure-aware, with overlap."""

from __future__ import annotations

import re
from .models import Chunk, Document

HEADING_PATTERN = re.compile(
    r"^(?:#{1,6}\s+\S+|(?:\d+\.)+\s+[A-Z][^.!?\n]{1,75}|[A-Z][A-Za-z0-9\s,'’/-]{2,60}:)$"
)
_CONNECTORS = frozenset({"a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "under", "with"})
_IGNORE_HEADINGS = frozenset(
    {
        "vision",
        "mission",
        "disclaimer",
        "foreword",
        "table of contents",
        "national university of computer and emerging sciences",
    }
)


def _is_heading(line: str) -> bool:
    """Return True for Markdown headers or concise section titles, not wrapped clauses or TOC rows."""
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
    # Numbered subsection headings (e.g. "1. Medium of Instruction") vs numbered rule prose ("1.21 Students are required...")
    if re.match(r"^\d+(?:\.\d+)*\.?\s+", s):
        # Numbered clause body sentences are usually >5 words with lowercase non-connector verbs/words
        title_body = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", s)
        bwords = title_body.split()
        if not bwords:
            return False
        content_words = [w for w in bwords if w.lower() not in _CONNECTORS]
        return bool(HEADING_PATTERN.match(s)) and (
            len(bwords) <= 5 or all(w[0].isupper() for w in content_words)
        )
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


def _split_oversized_unit(unit: str, size: int, overlap: int) -> list[str]:
    """Split a long prose block by sentences (or words if sentence-less) under `size` tokens."""
    parts = _split_into_sentences(unit)
    if len(parts) <= 1 and _approx_token_count(unit) > size:
        words = unit.split()
        max_words = max(20, int(size / 1.3))
        step_overlap = max(5, int(overlap / 1.3))
        windows: list[str] = []
        i = 0
        while i < len(words):
            windows.append(" ".join(words[i : i + max_words]))
            if i + max_words >= len(words):
                break
            i += max(1, max_words - step_overlap)
        return windows

    out: list[str] = []
    buffer: list[str] = []
    buffer_tokens = 0
    for sent in parts:
        stoks = _approx_token_count(sent)
        if buffer_tokens + stoks > size and buffer:
            out.append(" ".join(buffer))
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
        out.append(" ".join(buffer))
    return out


def chunk(doc: Document, size: int, overlap: int) -> list[Chunk]:
    """Split on sections, then paragraphs, then sentences, keeping each chunk under
    `size` tokens, with `overlap` tokens shared between neighbours. Every chunk keeps
    the section heading it sits under, its page, doc_version and superseded."""
    chunks: list[Chunk] = []
    current_section = "General"
    chunk_index = 0

    def _emit(section: str, page_num: int, text: str) -> None:
        nonlocal chunk_index
        chunks.append(
            Chunk(
                id=f"{doc.source}-{page_num}-{chunk_index}",
                text=text,
                source=doc.source,
                section=section,
                page=page_num,
                doc_version=doc.doc_version,
                superseded=doc.superseded,
            )
        )
        chunk_index += 1

    for page in doc.pages:
        raw_paragraphs = _split_paragraphs_on_headings(page.text)
        paragraphs = _merge_standalone_headings(raw_paragraphs)

        sec_buffer: list[str] = []
        sec_tokens = 0

        def _flush_buffer(sec: str, page_num: int) -> None:
            nonlocal sec_buffer, sec_tokens
            if not sec_buffer:
                return
            _emit(sec, page_num, "\n\n".join(sec_buffer))
            sec_buffer = []
            sec_tokens = 0

        for para in paragraphs:
            lines = para.split("\n")
            first_line = lines[0].strip()
            if _is_heading(first_line):
                new_section = first_line.lstrip("#").rstrip(":").strip()
                if new_section != current_section:
                    _flush_buffer(current_section, page.page)
                    current_section = new_section

            ptoks = _approx_token_count(para)
            if ptoks > size:
                _flush_buffer(current_section, page.page)
                for piece in _split_oversized_unit(para, size, overlap):
                    _emit(current_section, page.page, piece)
                continue

            if sec_tokens + ptoks > size and sec_buffer:
                last_overlap = sec_buffer[-1] if overlap > 0 and _approx_token_count(sec_buffer[-1]) <= overlap else None
                _flush_buffer(current_section, page.page)
                if last_overlap is not None and last_overlap != para and _approx_token_count(last_overlap) + ptoks <= size:
                    sec_buffer.append(last_overlap)
                    sec_tokens = _approx_token_count(last_overlap)

            sec_buffer.append(para)
            sec_tokens += ptoks

        _flush_buffer(current_section, page.page)

    return chunks
