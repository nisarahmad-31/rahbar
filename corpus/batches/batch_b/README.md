# Batch B — Planted-Fault Verification Dataset (11 Documents)

Deterministic second ingestion batch for Assignment 03 Part A1, A2, and D4.

| File | Logical `doc_id` | Planted Fault / Role | Expected A1 Check Outcome |
|---|---|---|---|
| `b01_scholarship_policy_2026.md` | `scholarship-policy` | Clean valid new policy | **PASS** (indexed) |
| `b02_exam_conduct_2026.md` | `exam-conduct` | Clean valid new policy | **PASS** (indexed) |
| `b03_academic_rules_2026.md` | `academic-rules` | **Newer version** (`2026-11`) of Batch A `academic-rules-2024.md` | **PASS** (indexed; supersedes Batch A `2024-08`) |
| `b04_fee_schedule_2023.md` | `fee-schedule` | **Older / superseded version** (`2023-05` vs Batch A `2026-01`) | **FAIL (`recency`)** → quarantined |
| `b05_exact_dup_scholarship.md` | `scholarship-dup-copy` | **Exact duplicate #1** of `b01_scholarship_policy_2026.md` | **FAIL (`uniqueness`)** → quarantined |
| `b06_exact_dup_grading.md` | `grading-dup-copy` | **Exact duplicate #2** of Batch A `grading-scale-2026.md` | **FAIL (`uniqueness`)** → quarantined |
| `b07_near_dup_exam_conduct.md` | `exam-conduct-near-dup` | **Near-duplicate #1** of `b02_exam_conduct_2026.md` | **FAIL (`uniqueness`)** → quarantined |
| `b08_corrupt_lab_policy.pdf` | `lab-policy` | **Corrupt / unreadable PDF** | **FAIL (`quality`)** → quarantined |
| `b09_truncated_internship_policy.md` | `internship-policy` | **Truncated text** (abrupt cut + `[TRUNCATED]`) | **FAIL (`completeness`)** → quarantined |
| `b10_missing_meta_hostel_draft.md` | `hostel-draft` | **Missing required metadata** (`title`, `doc_version`/`date`) | **FAIL (`completeness`)** → quarantined |
| `b11_injected_advisor_notice.md` | `advisor-notice` | **Injected instruction** (valid structure/metadata; untrusted text for D4 guardrail test) | **PASS** A1 (indexed as untrusted text; defended by retrieval guardrail in D4) |
