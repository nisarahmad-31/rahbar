# ADR 0002: Structure-Aware Hierarchical Chunking vs Fixed Token Windows

## Status
Accepted · 2026-11-04

## Context
Standard text chunking approaches in RAG pipelines typically apply a fixed sliding character or token window (e.g. 200 tokens with 20 tokens overlap). In university policy documents (such as the FAST-NUCES Academic Rules Handbook and Student Handbook), policies are organized into strict legal clauses with numbered sections (e.g., `1.21 Attendance`, `5.4 Academic Warning`).

Fixed-size chunking arbitrarily cuts clauses in half, separating condition clauses ("If CGPA falls below 2.00...") from their resulting disciplinary actions ("...the student is placed on warning"). Furthermore, isolated chunks lose their enclosing section headers, degrading citation accuracy and retrieval relevance.

## Decision
We implemented structure-aware hierarchical chunking in `indexer/pipeline/chunk.py`. The chunker performs splitting in three distinct stages:
1. **Section Boundaries**: Text is first partitioned along section and clause headings (e.g., Markdown headers, numbered clauses like `1.21`).
2. **Paragraph Boundaries**: Within sections, text is divided along double newlines (`\n\n`).
3. **Sentence Boundaries with Overlap**: If a paragraph exceeds the target size (`400` tokens), it is split along sentence boundaries, maintaining a `50` token sliding overlap.
4. **Context Propagation**: Every resulting chunk explicitly inherits the section title, document version, page number, and superseded status in its metadata payload.

## Options Considered
1. **Pure Fixed-Size Windowing (100–200 tokens)**: Rejected because it severs conditional clauses and produced low recall@5 (0.812) on golden evaluations.
2. **Semantic / Embedding-Based Splitting**: Evaluated but rejected due to high computational overhead during indexing and inconsistent chunk sizes.
3. **Structure-Aware Hierarchical Splitting (Chosen)**: Achieved the highest recall@5 (0.915) with an optimal storage amplification ratio of 1.25x.

## Consequences
- **Positive**: Preserves semantic cohesion of policy clauses; guarantees accurate section citations; maximizes exact-term and definitional retrieval quality.
- **Negative**: Slightly variable chunk lengths depending on document formatting; requires regex tuning for university-specific heading styles.
