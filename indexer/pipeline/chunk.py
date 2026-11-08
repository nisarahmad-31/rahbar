"""Stage 3: Chunk. Structure-aware, with overlap."""

from __future__ import annotations

import hashlib
import re
from .models import Chunk, Document

HEADING_PATTERN = re.compile(
    r"^(?:#{1,4}\s+|(?:\d+\.)+\d*\s+|[A-Z][A-Za-z0-9\s,-]{2,40}:)(.+)$",
    re.M,
)


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
        paragraphs = [p.strip() for p in page.text.split("\n\n") if p.strip()]
        
        for para in paragraphs:
            # Check if paragraph begins with a heading or clause
            lines = para.split("\n")
            first_line = lines[0].strip()
            heading_match = HEADING_PATTERN.match(first_line)
            if heading_match or first_line.startswith("#"):
                current_section = first_line.lstrip("#").strip()

            para_tokens = _approx_token_count(para)
            if para_tokens <= size:
                # Whole paragraph fits in one chunk
                cid = hashlib.sha256(f"{doc.source}:{page.page}:{chunk_index}:{para[:40]}".encode()).hexdigest()[:16]
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
