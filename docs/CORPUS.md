# Corpus Documentation

Source: FAST-NUCES public website (`https://www.nu.edu.pk`)  
Total Documents: 9 documents  
Total Pages: ~180 pages  

This document accounts for all seven properties required by Section 2 of the RAHBAR specification. The corpus deliberately exposes every RAG failure mode so that architectural mitigations (metadata filtering, hybrid search, OCR, relevance thresholds, and honest refusal) can be systematically verified.

---

## 1. Corpus Inventory and Properties Mapping

| # | Required Property | Document(s) | Description, Evidence, and Notes |
|---|---|---|---|
| **1** | **Scale (>= 6 docs, >= 40 pages)** | All 9 corpus documents | The corpus contains 9 documents spanning over 180 pages, establishing a realistic production scale. |
| **2** | **Two versions of one policy** | `Undergraduate Academic Rules (Aug 2023)` & `Academic Rules Handbook (Jun 2026)` | Demonstrates Failure Mode E (outdated source). The 2023 handbook is marked with `superseded: true` and `doc_version: "2023-08"`, while the 2026 handbook is marked `superseded: false` and `doc_version: "2026-06"`. Attendance is clause 1.21 in 2026 handbook but clause 1 in Prospectus 2026. Without metadata filtering on `superseded: false`, retrievers return repealed clauses. |
| **3** | **Scanned, image-only PDF** | `Faculty Entrepreneurship and Technical Start-ups Policy` (`faculty-entrepreneurship-and-technical-start-ups-policy.pdf`) | Demonstrates Failure Mode A (bad extraction). When checked with `pdffonts`, the document returns an empty font list, confirming that pages have no underlying text layer. Extraction detects empty text layers and triggers OCR (`pytesseract` and `pdf2image`), tagging pages with `ocr=True`. |
| **4** | **Tables with real information** | `Annual Report 2019-20`, `Annual Report 2023-24`, `Fee Structure page` (`.html`), `Grading page` (`.html`) | Multi-column HTML and PDF tables containing tuition fees per credit hour, miscellaneous campus charges, grading scales (grade point equivalencies), and faculty counts. Standard text extraction jumbles columns; `pdfplumber` table extraction formats rows as delimited Markdown tables (`col1 \| col2 \| col3`). |
| **5** | **Long definitions** | `Student Handbook (Jun 2026)` | Multi-page comprehensive policies including the university's Generative AI policy and disciplinary definitions with nested clauses. Naive fixed-size chunking splits definitions mid-sentence; structure-aware chunking keeps headings and full paragraphs intact. |
| **6** | **Deliberately absent topic** | Hostel Room Accommodation & Bus Shuttle Routes | Demonstrates honest abstention (Section 3.4). We intentionally excluded the `Transport and Hostel Facilities.pdf` document. The student handbook only mentions that hostels exist. When users ask specific questions about hostel room allocation or bus fee vouchers, the retriever score drops below the relevance floor (`0.55`) and RAHBAR declines (`answered_from_context: false`). |
| **7** | **Exact-term content** | `Academic Rules Handbook (Jun 2026)` & `Fee Structure page` | Exact clause numbers (`1.21 Attendance`, `5.4 Academic Warning`), numerical thresholds (CGPA `2.00` warning threshold, `3.50` Dean's List), and monetary figures (Rs. 10,000 tuition per credit hour). Standard dense embeddings tend to blur exact digits; metadata schema tracking and hybrid retrieval preserve precise term matching. |

---

## 2. Document Acquisition & Manifest Integrity

The corpus was gathered using `corpus/fetch.py`, which enforces ethical scraping constraints (single-threaded requests, 2-second rate-limiting, and standard `User-Agent` headers).

All downloaded documents are stored in `corpus/raw/` (gitignored to protect repository size and licensing). Verification of document provenance and byte integrity is ensured by cryptographic SHA-256 hashes recorded in `corpus/MANIFEST.md`. Running `python corpus/fetch.py --verify` recalculates the SHA-256 hash of each local file against the committed manifest.
