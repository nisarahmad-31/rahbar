# Package B1 — Golden Dataset & Module-Level Embedding Evaluation

**Owner:** Mussa Raza (`stackmussa` / `i243022@isb.nu.edu.pk`)  
**Branch:** `feature/b1-golden-embedding-mussa`  
**Status:** Complete & Verified with live `nomic-embed-text` via Ollama  

---

## 1. Golden Dataset (`eval/golden.jsonl`)

- **Total questions:** `28` (minimum `25` required)
- **Answerable (with ground-truth `doc_id`, `source`, `section`, `page`):** `22` (minimum `15` required)
- **Unanswerable (`must_abstain: true`):** `6` (minimum `5` required)
- **Hard (`difficulty: "hard"` — paraphrased, multi-chunk, or Batch B):** `7` (minimum `5` required)
- **Origin breakdown:**
  - `human`: `12` (minimum `8` required)
  - `synthetic`: `16`, of which `11` have `"reviewed_by_human": true` (minimum `8` required)

---

## 2. Module-Level Embedding Evaluation (`eval/embed_eval.py` & `eval/embed_pairs.json`)

Evaluated the existing RAHBAR embedding model (`EMBED_MODEL=nomic-embed-text`, `dim=768`, `backend=ollama` at `http://172.28.112.1:11434`, `seed=42`, `chunk_size=400`, `chunk_overlap=50`).

```bash
.venv/bin/python eval/embed_eval.py --ollama-url http://172.28.112.1:11434
```

### Observed Results (`eval/results/module_embedding_eval.json`)
- **Paraphrase mean cosine (`16` pairs):** `0.7194`
- **Unrelated mean cosine (`16` pairs):** `0.4358`
- **Cosine similarity gap:** `0.2836`
- **Embedding speed:** `208.605 ms/chunk` (`17` chunks from Batch A)
- **Determinism (`max_abs_diff` & `l2_diff` across 2 calls):** `0.0` (`exact_match: true`)
