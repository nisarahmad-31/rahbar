# Package E — Drift and Coverage Checking (Part E)

Owner: **Mussa Raza (`stackmussa`)**  
Branch: `feature/e-drift-coverage-mussa`

## 1. Live Query Log & Coverage Threshold (E1 & E2)

- **Live query log ([eval/live_queries.jsonl](../../eval/live_queries.jsonl)):** **42** simulated user queries (`28` in-domain policy questions + `14` out-of-domain queries spanning campus facilities, sports, housing, counseling, international, dining, admin-services, career, transport, health, internships, student-life, appeals, IT-services).
- **Similarity threshold (`0.72`):** Grounded directly in the B1 `nomic-embed-text` embedding module evaluation ([module_embedding_eval.json](../../eval/results/module_embedding_eval.json)), where true paraphrase pairs averaged cosine similarity **`0.7194`** (`std`/spread down to `0.57`) while unrelated pairs averaged **`0.4358`** (`max = 0.549`). Choosing `0.72` avoids treating an arbitrary `0.85` cutoff as automatic while filtering genuine off-topic misses (`0/14` OOD false positives at `0.75`, `2/14` at `0.72`).
- **Baseline coverage before expansion (`28` golden questions, [coverage_e2_before.json](../../eval/results/coverage_e2_before.json)):**
  - Covered: **`21 / 42 = 0.5000` (`50.0%`)** -> **`FAIL`** (exit code `1` against minimum coverage `0.75`).
  - Uncovered queries (`21` total) grouped into `18` topic buckets (`IT-services`, `academic-rules`, `admin-services`, `appeals`, `attendance`, `campus-facilities`, `career`, `counseling`, `degree-completion`, `dining`, `fees`, `grading`, `health`, `international`, `internships`, `library`, `sports`, `student-life`).

## 2. Batch B Data Drift Evaluation (E3)

Ingested `corpus/batches/batch_b` onto `rahbar_b2` using `--incremental-mode merge` (`11` input docs -> `4` passed, `7` quarantined; `3` new active docs added: `scholarship-policy`, `exam-conduct`, `advisor-notice`; `1` superseded version archived: `academic-rules@2024-08`, replaced by `academic-rules@2026-11`; total store `29` chunks = `25` active + `4` superseded). Then re-ran `eval/run_part_b.py` identically (`TOP_K=5`, `RELEVANCE_FLOOR=0.65`, `28` baseline golden questions):

| Level | Metric | Batch A Baseline (`part_b_baseline`) | Batch A + B Drift (`part_b_batch_b_drift`) | Change & Explanation |
| :--- | :--- | ---: | ---: | :--- |
| **Retrieval** | Recall@3 | `0.7955` | **`0.8182`** | **`+0.0227`**: Newly indexed Batch B docs unlocked `g15`, `g16`, `g20`, `g21` (`0.0 -> 1.0`) and `g22` (`0.5 -> 1.0`), outweighing `g01..g04` source-version drift. |
| **Retrieval** | Recall@5 | `0.7955` | **`0.8182`** | **`+0.0227`**: Matches Recall@3 gain across Batch B ground truths. |
| **Retrieval** | MRR | `0.8182` | **`0.7955`** | **`-0.0227`**: Larger active pool (`25` vs `17` chunks) plus `g01..g04` source rename (`academic-rules-2024.md -> b03_academic_rules_2026.md`) lowered rank-1 matches. |
| **Retrieval** | nDCG@5 | `0.8006` | **`0.8006`** | **`0.0000`**: Ranking gains on Batch B balanced source-filename drift on superseded Batch A labels. |
| **End-to-End** | Faithfulness (lexical) | `0.7500` | **`0.6911`** | **`-0.0589`**: More answerable questions executed LLM generation; verbose multi-clause answers (`g20`, `g22`) dilute word overlap. |
| **End-to-End** | Abstention Correctness | `1.0000` | **`0.8333`** | **`-0.1667` (`5/6`)**: On `g28` (advising override query), Chroma retrieved `b11_injected_advisor_notice.md` above `0.65`; D4 guardrail neutralized the hostile line, and LLM fallback set `answered_from_context=True` on safe holding text without waiving fees. |
| **End-to-End** | Citation Validity | `0.9412` | **`0.9091`** | **`-0.0321`**: 4 validation fallbacks out of 57 traces cleanly fell back to top retrieved chunk metadata. |
| **End-to-End** | Latency p50 / p95 (ms) | `33195.89 / 56754.17` | `32170.36 / 56845.98` | Comparable CPU inference (`-1025 ms` p50 / `+92 ms` p95). |

- **D3 Trace Summary during E3 ([d3_summary_e3.json](../../eval/results/d3_summary_e3.json)):** `57` traced queries (`38` answered, `15` abstained, `4` fallback), **`19` prompt-injection events flagged & neutralized** in `b11_injected_advisor_notice.md` (`advisor-notice@2026-10-1-2`) in `1.1 ms` p95 guardrail overhead, top-1 similarity median **`0.8027`**.

## 3. Closing the Gap (E4)

Appended **`14` new traceable golden questions (`g29..g42`, tagged `"added_in": "part_e4"`)** covering previously uncovered in-domain (`fees`, `library`, `grading`, `academic-rules`, `attendance`, `scholarships`, `degree-completion`) and out-of-domain abstention topics (`campus-facilities`, `counseling`, `health`, `IT-services`, `career`, `admin-services`), without deleting `g01..g28`:

| Stage | Golden Size | Cosine Threshold | Covered / Total | Coverage Rate | Gate Exit (`--min-coverage 0.75`) |
| :--- | ---: | ---: | ---: | ---: | :--- |
| **Before Expansion (`E2`)** | `28` | `0.72` | `21 / 42` | **`0.5000` (`50.00%`)** | `FAIL` (`exit 1`) |
| **After Expansion (`E4`)** | `42` (`+14`) | `0.72` | `36 / 42` | **`0.8571` (`85.71%`)** | **`PASS` (`exit 0`)** |

Checking a stricter target (`--min-coverage 0.90`) still triggers a non-zero exit code (`exit 1` at `0.8571 < 0.90`), leaving the remaining 6 uncovered queries (`attendance` athlete waiver, `grading` F->GPA edge phrase, `dining`, `international`, `sports`, `student-life`) clearly surfaced.

## 4. Verification Commands

```bash
.venv/bin/python eval/query_coverage.py --threshold 0.72 --min-coverage 0.75 --label e4_after_072
(cd indexer && ../.venv/bin/pytest -q && ../.venv/bin/mypy pipeline)
.venv/bin/ruff check api indexer eval scripts
```
