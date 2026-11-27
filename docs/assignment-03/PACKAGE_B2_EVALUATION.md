# Package B2 — 3-Level Evaluation Suite Summary

**Owner:** Nisar Ahmad (`nisarahmad-31` / `i243131@isb.nu.edu.pk`)  
**Branch:** `feature/b2-retrieval-answer-eval-nisar`  
**Status:** Complete & Verified against live `ChromaDB 1.0.12` + `Ollama` (`nomic-embed-text` + `llama3.2:3b`)  

---

## 1. Implementation Summary

- **Unified 3-level evaluation runner (`eval/run_part_b.py`):**
  - Integrates B1 module-level metrics (`eval/results/module_embedding_eval.json`) without modifying B1 measurements.
  - Computes component-level retrieval metrics (`recall@3`, `recall@5`, `MRR`, `nDCG@5`) on `eval/golden.jsonl` (`28` questions) + deterministic query-typo noise test (`apply_query_typos`).
  - Computes end-to-end metrics (`faithfulness_lexical`, optional `faithfulness_llm_judge` via `llama3.2:3b`, `abstention_correctness`, `citation_validity`, `latency_p50_ms`, `latency_p95_ms`).
  - Runs the full suite twice with fixed `seed=42` (`run1`, `run2`) and computes `stochasticity_diff`.
  - Computes judge-vs-human agreement against `eval/faithfulness_human_labels.json` (`12` manually labeled real answers).
  - Writes timestamped JSON reports + Markdown summary tables under `eval/results/`.

---

## 2. Exact Command & Observed Baseline Results (`Batch A`, `TOP_K=5`, `RELEVANCE_FLOOR=0.65`)

```bash
.venv/bin/python eval/run_part_b.py \
  --api http://127.0.0.1:8011 \
  --judge-ollama-url http://172.28.112.1:11434 \
  --noise-via-chroma \
  --chroma-host 127.0.0.1 \
  --chroma-port 18001 \
  --collection rahbar_b2 \
  --relevance-floor 0.65 \
  --top-k 5 \
  --label baseline
```

| Level | Metric | Run 1 | Noise (Typos) | Run 2 - Run 1 |
| --- | --- | ---: | ---: | ---: |
| Module (Embedding) | Paraphrase / Unrelated / Gap | 0.7194 / 0.4358 / 0.2836 | — | max_diff=0.0 |
| Module (Embedding) | Speed (ms/chunk) | 208.605 | — | — |
| Component (Retrieval) | Recall@3 | 0.7955 | 0.7955 | +0.0000 |
| Component (Retrieval) | Recall@5 | 0.7955 | 0.7955 | +0.0000 |
| Component (Retrieval) | MRR | 0.8182 | 0.7955 | +0.0000 |
| Component (Retrieval) | nDCG@5 | 0.8006 | 0.7838 | +0.0000 |
| End-to-End | Faithfulness (lexical) | 0.7500 | — | +0.0000 |
| End-to-End | Faithfulness (LLM judge `llama3.2:3b`) | 0.1333 | — | — |
| End-to-End | Abstention Correctness | 1.0000 | — | +0.0000 |
| End-to-End | Citation Validity | 0.9412 | — | +0.0032 |
| End-to-End | Latency p50 / p95 (ms) | 33195.89 / 56754.17 | 428.33 / 1291.20 | -13223.64 / +7645.67 |

### Notes on Stochasticity, Noise, and Judge Agreement
- **Why `Run 2 - Run 1` differs on citation validity (`+0.0032`) and latency:** Retrieval is 100% deterministic (`+0.0000` across all retrieval metrics), while `OllamaProvider` uses `TEMPERATURE=0.2`, causing minor citation-list variation on multi-passage queries, and CPU inference latency varies with model KV-cache warmth and OS scheduling.
- **Typo noise impact:** `Recall@5` held at `0.7955`, while rank quality dipped slightly (`MRR` `0.8182 -> 0.7955`, `nDCG@5` `0.8006 -> 0.7838`).
- **Judge-human agreement (`eval/faithfulness_human_labels.json`):** Across `12` manually labeled answered queries (`12/12` genuinely grounded), the 3B `llama3.2:3b` LLM judge agreed only **`2/12` (`16.7%`)**, systematically rejecting terse/numeric answers (`"2.00"`, `"3"`, `"80"`).
