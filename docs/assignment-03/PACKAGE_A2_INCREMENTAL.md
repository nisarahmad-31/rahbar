# Package A2 — Incremental Data-Lake & Retrieval-Source Update Summary

**Owner:** Nisar Ahmad (`nisarahmad-31` / `i243131@isb.nu.edu.pk`)  
**Branch:** `feature/a2-incremental-nisar`  
**Status:** Complete & Verified against live `chromadb/chroma:1.0.12`  

---

## 1. Implementation Summary

- **Incremental store engine (`indexer/pipeline/store.py`):**
  - `get_or_create_collection()`: preserves existing Chroma collection unless missing or `EMBED_MODEL` changed.
  - `incremental_sync()`: tracks active documents (`data_lake/index_state.json`) and active files (`data_lake/active/<doc_id>/`), computing exact deltas:
    - **Unchanged corpus rerun:** skips chunking & embedding (`chunks_embedded=0`, `embed_api_calls=0`).
    - **Changed document:** deletes only that `doc_id`'s chunks and re-embeds/adds only its new chunks.
    - **Deleted document (`sync` mode):** deletes all chunks for removed `doc_id`s and cleans `data_lake/active/` + `manifest.json`, leaving 0 orphan chunks (`count_chunks_by_doc_id`).
    - **Superseded version (`merge` / version update):** archives older file + `metadata.json` under `archive/<doc_id>/<doc_version>/` and sets `superseded=True` on its Chroma chunks via `collection.update()`, excluding it from `where={"superseded": False}` queries.
  - `restore_document_version()`: restores an archived version back to active (`superseded=False`), archives and demotes the newer version (`superseded=True`), and invalidates Redis cache.
- **Versioned chunk IDs (`indexer/pipeline/chunk.py`):** uses `{doc_id}@{doc_version}-{page}-{index}` so active and archived versions of the same logical `doc_id` coexist cleanly in Chroma without ID collisions.
- **Embedding instrumentation (`indexer/pipeline/embed.py`):** `embed_with_stats()` tracks real `chunks_embedded` and `embed_api_calls`.
- **CLI (`indexer/pipeline/run.py`):** supports `--incremental-mode {sync,merge}`, `--full-rebuild`, `--restore-doc <id> --restore-version <ver>`, and `--counts`.

---

## 2. Exact Commands & Actual Results (`chromadb/chroma:1.0.12`)

```bash
make test
# api/: 20 passed; indexer/: 11 passed (including test_incremental.py)

make lint
# ruff check api indexer eval corpus -> All checks passed!
# mypy app (22 files) & mypy pipeline (11 files) -> Success: no issues found

CHROMA_HOST=127.0.0.1 CHROMA_PORT=18000 CHROMA_COLLECTION=rahbar_a2_test \
  .venv/bin/python scripts/run_a2_evidence.py
```

### Observed Live ChromaDB Counts (`eval/results/a2_incremental_evidence.json`)
1. **Batch A initial:** `documents_new=5`, `chunks_embedded=17`, `embed_api_calls=3`, `total_chunks_in_store=17`.
2. **Unchanged rerun:** `documents_unchanged=5`, `chunks_embedded=0`, `embed_api_calls=0`.
3. **Modify 1 doc (`library-rules-2026.md`):** `documents_changed=1`, `documents_unchanged=4`, `chunks_embedded=3`.
4. **Delete 1 doc (`library-rules-2026.md`):** `documents_deleted=1`, `chunks_embedded=0`, `counts_by_doc_id={'academic-rules': 4, 'attendance-policy': 3, 'fee-schedule': 4, 'grading-scale': 3}` (`library-rules` = 0 orphan chunks).
5. **Batch B merge (`b03` supersedes `academic-rules@2024-08`):** `superseded=['academic-rules@2024-08']`, `active_has_2024=False` under `where={"superseded": False}`.
6. **Restore / rollback (`academic-rules@2024-08`):** `demoted_version='2026-11'`, `active_has_2024=True` under `where={"superseded": False}`.
