# Package D5 — Trace-Only Root-Cause Analysis

## Question `g15`: "What semester GPA and credit load qualify an undergraduate for the Dean's List Merit Scholarship?"

**Diagnosis from trace fields only (5 lines):**

1. The trace shows `status="abstained"` with `answered_from_context=false` and zero citations, meaning the pipeline refused to answer despite `g15` being a valid, answerable question (`must_abstain=false`).
2. The `retrieved_ids` field lists `['fee-schedule@2026-01-1-1', 'library-rules@2026-02-1-1', 'academic-rules@2024-08-1-1', 'grading-scale@2026-01-1-0', 'grading-scale@2026-01-1-1']` — none of which is the required `scholarship-policy` document (`b01_scholarship_policy_2026.md`), giving `recall@5=0.0`.
3. The `filters_applied.where` field shows `{"superseded": false}` and `collection_name="rahbar_b2"` with `collection_version="batch_a"` — Batch B documents (including the scholarship policy) were **never indexed** into this collection version, so the retriever physically cannot find them.
4. With no relevant chunk retrieved, the top-1 similarity score falls below `relevance_floor=0.65`, triggering `guardrail_actions=["relevance_floor_abstain"]` and an honest abstention instead of a hallucinated answer.
5. **Root cause:** The question references a Batch B document absent from the indexed collection. The retrieval failure is a **data coverage gap**, not an embedding or generation defect. Ingesting Batch B (Part E3) would place `scholarship-policy` in the collection and resolve this abstention.
