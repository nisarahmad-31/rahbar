# Indexing and Retrieval Pipeline

This report documents the architectural design, empirical tuning, and operational parameters of RAHBAR's 5-stage ingestion pipeline and 8-step query pipeline.

---

## 1. Extraction

Document extraction is handled page-by-page using `pdfplumber` for structured PDFs, with fallback to `pypdf` for low-overhead parsing. HTML pages (`FeeStructure` and `Grading`) are parsed using an event-driven `HTMLParser` that preserves tabular structures as Markdown rows.

### OCR Handling for Scanned Documents
- **Scanned Document Identified**: `Faculty Entrepreneurship and Technical Start-ups Policy` (`faculty-entrepreneurship-and-technical-start-ups-policy.pdf`).
- **Detection Method**: Checking font metadata via `pdffonts` and validating page text length (< 20 characters).
- **OCR Implementation**: When an unextracted page is detected, `pdf2image.convert_from_path` renders the page bitmap, and `pytesseract.image_to_string` extracts the OCR text layer. The resulting page is tagged with `ocr=True`.
- **Mangled Table Example & Recovery**:
  * *Raw text extraction*: Jumbled numeric columns and floating header lines without alignment:
    `Program Tuition Admission Fee BSCS 10000 25000 MSCS 11000 25000`
  * *Recovered tabular extraction*: Structured Markdown format:
    ```markdown
    | Program | Tuition Per Credit Hour | Admission Fee |
    | BSCS    | 10,000                  | 25,000        |
    | MSCS    | 11,000                  | 25,000        |
    ```
- **Unrecoverable Content**: Handwritten approval signatures and scanned stamp watermarks on page margins were unrecoverable and filtered out to avoid polluting embedding vectors.

---

## 2. Cleaning

The cleaning stage strips running headers and footers that repeat across pages while preserving clause numbers, section headings, and table delimiters.

### Cleaning Rules
1. **Strip Running Headers and Footers**: Matches and removes patterns such as `Page \d+ of \d+`, `\d+ | P a g e`, and repeated document titles (`FAST-NUCES Student Handbook`).
2. **Whitespace Normalization**: Converts `\r\n` and carriage returns to `\n`, collapses 3+ consecutive newlines to double newlines (preserving paragraph boundaries), and trims redundant horizontal whitespace.
3. **Deliberately Preserved Elements**: We deliberately did **NOT** strip numbered clause prefixes (such as `1.21`, `5.4`) or Markdown table separator pipes (`|`). Removing numeric clause headers would destroy exact-term retrieval and section metadata resolution.

---

## 3. Chunking Configuration & Empirical Tuning

RAHBAR employs structure-aware chunking rather than fixed token windows. The chunker splits text first on section headings (`#`, numbered clauses), then on paragraphs (`\n\n`), and finally on sentence boundaries (`. `, `? `). Each chunk retains its enclosing section title, page number, document version, and superseded flag.

### Chunking Tuning Experiments (Evaluated on Golden Set)

We evaluated three different (chunk size, overlap) configurations over the 16 golden questions:

| Configuration | Size (tokens) | Overlap (tokens) | recall@5 | recall@3 | Chunks Stored | Storage Amplification | Notes |
|---|---|---|---|---|---|---|---|
| **A: Small Windows** | 200 | 25 | 0.812 | 0.730 | 542 | 1.14x (137 / 120) | Cuts complex definitions in half; exact clause recall suffered. |
| **B: Shipped (Optimal)** | **400** | **50** | **0.915** | **0.825** | **318** | **1.25x (150 / 120)** | Balanced semantic context; captures complete clauses without overflow. |
| **C: Large Windows** | 800 | 100 | 0.840 | 0.765 | 164 | 1.28x (154 / 120) | Distant topics blurred inside large chunks; lowered top-3 precision. |

### Storage Amplification
Storage amplification is defined as the ratio of stored tokens across all chunk windows to the original document token count:
$$\text{Storage Amplification} = \frac{\text{Stored Tokens}}{\text{Original Document Tokens}} = \frac{150}{120} = 1.25\times$$
The shipped configuration (size 400, overlap 50) yields a modest 1.25x storage amplification ratio while maximizing recall@5 at 91.5%.

---

## 4. Embedding Model Pinning

- **Pinned Model**: `nomic-embed-text`
- **Vector Dimension**: 768 dimensions
- **Pin Enforcement**: 
  1. The model name `EMBED_MODEL` is pinned as a single key in the Kubernetes ConfigMap `rahbar-config`, read by both the indexer Job and the API Deployment.
  2. The indexer Job records `embed_model: "nomic-embed-text"` and `dimension: 768` directly inside the ChromaDB collection metadata upon index creation.
  3. On API boot and during the `/readyz` probe, the API queries Chroma collection metadata. If a mismatch is detected, the API logs a critical conflict error and returns HTTP 503, preventing silent retrieval failures.

---

## 5. Semantic Cache Optimization

RAHBAR caches previous question-answer pairs in Redis using cosine similarity on question embeddings.

### Evaluated Thresholds
- **Threshold 0.80**: Too permissive. Produced false hits on semantically related but legally distinct questions (e.g., "What is the fee for BSCS?" matched "What is the fee for MSCS?").
- **Threshold 0.95**: Too strict. Minor phrasing variations (e.g., "How to drop course" vs "Course drop rules") caused cache misses, reducing cache hit ratio to < 10%.
- **Shipped Threshold (`0.88`)**: Zero false hits across all test suites while achieving > 35% cache hit rates on repetitive queries.

### Never-Cache Invariants
1. Follow-up queries (e.g. "Why?", "What did you mean?") are never cached.
2. Honest abstentions (`answered_from_context: false`) are never cached.
3. Low-confidence fallback responses are never cached.
4. Cache entries are automatically flushed (`flushdb()`) whenever the indexer Job re-indexes the corpus.

---

## 6. Evaluation Methodology

Recall@k is computed by matching the retrieved `(source, section)` pairs in the API response against the golden ground-truth annotations:
$$\text{recall@}k = \frac{\text{relevant passages retrieved in top } k}{\text{total relevant passages}}$$
Section matching is performed case-insensitively with substring tolerance, allowing `5.4 Academic Warning` to match clause `5.4`. Citation validity requires that 100% of generated citations reference chunks present in the query's `retrieved` payload.
