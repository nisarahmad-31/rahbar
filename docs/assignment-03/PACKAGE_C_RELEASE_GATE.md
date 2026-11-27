# Package C — Experimentation & Release Gate (Part C)

Owner: **Nisar Ahmad (`nisarahmad-31`)**  
Branch: `feature/c-release-gate-nisar`

## 1. Change Tested (`TOP_K` Sweep)

To evaluate latency–recall trade-offs cleanly while preserving identical embedding, corpus (`Batch A`, 17 chunks in `rahbar_b2`), prompt, `RELEVANCE_FLOOR=0.65`, `TEMPERATURE=0.2`, and `LLM_MODEL=llama3.2:3b` settings, we evaluated two retrieval `TOP_K` candidates against the `TOP_K=5` baseline (`part_b_baseline_20261127_115127.json`):

1. **Candidate 1 (`candidate_topk3`, `TOP_K=3`):** reduces retrieved context chunks from 5 to 3 to cut CPU LLM prompt tokens and p50/p95 latency without dropping multi-chunk recall.
2. **Candidate 2 (`candidate_topk1`, `TOP_K=1`):** aggressive single-chunk context reduction to test whether the release gate blocks a fast candidate that degrades multi-passage recall.

## 2. Release Gate Thresholds ([eval/gate_config.json](../../eval/gate_config.json))

| Check | Rule / Threshold | Rationale |
| :--- | :--- | :--- |
| `recall@5_drop` | `baseline - candidate <= 0.02` | Protects multi-document/section retrieval coverage from dropping more than 2 percentage points. |
| `faithfulness_lexical` | `>= 0.70` | Ensures generated claims stay grounded in retrieved context (lexical grounding metric; baseline `0.75`). |
| `latency_p95_ms` | `<= 65000.0` (`65 s`) | Bounds tail latency on local CPU `llama3.2:3b` inference (`56.75 s` baseline p95 + ~15% headroom). |
| `abstention_correctness` | `>= 1.0` & not below baseline | Enforces zero regressions on unanswerable/out-of-scope policy questions. |
| `citation_validity` | `>= 0.90` | Requires at least 90% of citations to resolve cleanly against retrieved chunks/pages. |

Thresholds live in external configuration ([eval/gate_config.json](../../eval/gate_config.json) and ConfigMap / `GATE_*` environment overrides in [eval/release_gate.py](../../eval/release_gate.py)), so tightening or relaxing the gate requires zero code changes or image rebuilds.

## 3. Baseline vs. Candidate Comparison

| Metric | Baseline (`TOP_K=5`) | Candidate 1 (`TOP_K=3`) | Candidate 2 (`TOP_K=1`) |
| :--- | ---: | ---: | ---: |
| **Recall@3** | `0.7955` | `0.7955` | `0.7500` |
| **Recall@5** | `0.7955` | `0.7955` | `0.7500` |
| **Recall@5 Drop vs. Baseline** | `0.0000` | `0.0000` | `0.0455` |
| **MRR** | `0.8182` | `0.8182` | `0.8182` |
| **nDCG@5** | `0.8006` | `0.8006` | `0.7654` |
| **Noise Typos (`Recall@5` / `MRR`)** | `0.7955` / `0.7955` | `0.7955` / `0.7955` | `0.7045` / `0.7727` |
| **Faithfulness (lexical)** | `0.7500` | `0.7244` | `0.8500` |
| **Abstention Correctness** | `1.0000` | `1.0000` | `1.0000` |
| **Citation Validity** | `0.9412` | `0.9474` | `0.9412` |
| **Latency p50 (ms)** | `33195.89` | `23038.67` | `15117.84` |
| **Latency p95 (ms)** | `56754.17` | `56538.82` | `26778.72` |
| **Release Gate Decision** | `BASELINE` | **`PASS` (exit `0`)** | **`FAIL` (exit `1`)** |

### Why `TOP_K=3` Passes and `TOP_K=1` Fails

- **`candidate_topk3` (`TOP_K=3`) -> `PASS` ([gate_pass_topk3.json](../../eval/results/gate_pass_topk3.json)):** Cuts median latency by **30.6%** (`33.20 s -> 23.04 s`) and trims `p95` (`56.75 s -> 56.54 s`), improves `Citation Validity` (`0.9412 -> 0.9474`), preserves exact `Recall@5` (`0.7955`), `MRR` (`0.8182`), `nDCG@5` (`0.8006`), and `1.0` abstention. Ship candidate approved.
- **`candidate_topk1` (`TOP_K=1`) -> `FAIL` ([gate_fail_topk1.json](../../eval/results/gate_fail_topk1.json)):** Halves `p50`/`p95` latency (`15.12 s` / `26.78 s`) and pushes lexical grounding on single chunks up to `0.8500`, **but** causes `Recall@5` to drop by **`0.0455`** (`0.7955 -> 0.7500`, failing `<= 0.02`) and typo-noise `Recall@5` to fall to `0.7045`. Blocked with exit code `1`.

## 4. Exact Reproduction Commands

```bash
# 1) Run Part B evaluation against candidate (example TOP_K=3)
.venv/bin/python eval/run_part_b.py \
  --api http://127.0.0.1:8012 \
  --noise-via-chroma --chroma-host 127.0.0.1 --chroma-port 18001 \
  --collection rahbar_b2 --relevance-floor 0.65 --top-k 3 \
  --label candidate_topk3

# 2) Run release gate (PASS -> exit 0)
.venv/bin/python eval/release_gate.py \
  --baseline eval/results/part_b_baseline_latest.json \
  --candidate eval/results/part_b_candidate_topk3_latest.json \
  --config eval/gate_config.json \
  --out eval/results/gate_pass_topk3.json

# 3) Run release gate (FAIL -> exit 1)
.venv/bin/python eval/release_gate.py \
  --baseline eval/results/part_b_baseline_latest.json \
  --candidate eval/results/part_b_candidate_topk1_latest.json \
  --config eval/gate_config.json \
  --out eval/results/gate_fail_topk1.json
```
