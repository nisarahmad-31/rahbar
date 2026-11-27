# Package A1 — Verification Stage & Batch B Summary

**Owner:** Mussa Raza (`stackmussa` / `i243022@isb.nu.edu.pk`)  
**Branch:** `feature/a1-verification-mussa`  
**Status:** Complete & Verified  

---

## 1. Implementation Summary

- **Verification module:** `indexer/pipeline/verify.py` (`verify_documents`) runs strictly **after** `extract()` + `clean()` and **before** `chunk()` + `embed()` + `store()`.
- **Models & settings:** `indexer/pipeline/models.py` (`Document.doc_id`, `title`, `date`, `content_hash`, `extraction_error`, `metadata_present`, `CheckResult`, `DocumentVerification`, `VerificationReport`) and `indexer/pipeline/config.py` (`DATA_LAKE_DIR`, `QUARANTINE_DIR`, `ARCHIVE_DIR`, `VERIFICATION_REPORT_DIR`, `NEAR_DUP_THRESHOLD`, `QUARANTINE_MODE`).
- **Four mandatory checks per document (pass/fail + reason):**
  1. `quality`: rejects extraction errors (corrupt PDFs), empty/short text (`< 40` chars), and garbled/non-printable text.
  2. `completeness`: verifies required metadata (`source`, `title`, `date` or `doc_version`, `page_count > 0`) and rejects truncated text (`[TRUNCATED]`, trailing `...`, or abrupt mid-sentence cutoffs).
  3. `recency`: uses logical `doc_id` (`derive_doc_id`) across the incoming batch and `data_lake/manifest.json`; older/superseded versions fail `recency`, while newer versions pass and mark older entries `superseded: true` in the manifest for A2 archival/exclusion.
  4. `uniqueness`: checks exact file/text SHA-256 hashes and document-level embedding cosine similarity (`>= 0.85` across distinct `doc_id`s).
- **Quarantine & non-indexing:** Failed documents are copied/moved to `quarantine/<run_id>/` and excluded from `chunk()`, `embed()`, and `store()`.
- **Contract for Package A2 (Nisar):**
  - Stable logical `doc_id` propagated to `Document.doc_id`, `Chunk.doc_id` (`Chunk.metadata()["doc_id"]`), and `manifest.json`.
  - Per-run machine-readable reports written to JSON (`eval/results/verification_batch_a.json`, `eval/results/verification_batch_b.json`).

---

## 2. Exact Commands & Actual Results

```bash
make test
# api/: 20 passed; indexer/: 9 passed (including 3 new verification/quarantine tests)

make lint
# ruff check api indexer eval corpus -> All checks passed!
# mypy app (22 files) & mypy pipeline (11 files) -> Success: no issues found

make check
# python3 scripts/check_submission.py -> 165 passed · 2 warnings · 0 failures

cd indexer
../.venv/bin/python -m pipeline.run --corpus-dir ../corpus/batches/batch_a --batch-name batch-a --verify-only --report-out ../eval/results/verification_batch_a.json
../.venv/bin/python -m pipeline.run --corpus-dir ../corpus/batches/batch_b --batch-name batch-b --verify-only --report-out ../eval/results/verification_batch_b.json
```

### Actual Batch Counts
- **Batch A (`corpus/batches/batch_a`, 5 docs):** `total=5, passed=5, quarantined=0` (`quality=0, completeness=0, recency=0, uniqueness=0` failures).
- **Batch B (`corpus/batches/batch_b`, 11 docs):** `total=11, passed=4, quarantined=7` (`quality=1, completeness=3, recency=1, uniqueness=4` check failures; 7 unique documents quarantined: `b04`, `b05`, `b06`, `b07`, `b08`, `b09`, `b10`; `b01`, `b02`, `b03`, `b11` passed).
