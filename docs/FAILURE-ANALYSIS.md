# Failure Analysis: Systematic Stage Breakdown

As required by Section 5.5 of the assignment specification, we intentionally broke four of the six pipeline stages one at a time on an isolated dev environment, recorded the resulting symptoms, developed diagnostics, and verified the remedy.

---

## Stage A · Extraction (Scanned PDF Without OCR)

- **What we broke**: We disabled the OCR fallback in `indexer/pipeline/extract.py` and indexed `faculty-entrepreneurship-and-technical-start-ups-policy.pdf` directly without extracting text from scanned image pages.
- **Symptom**: When asking questions specifically regarding technical startup policies or faculty patent licensing, the API returned empty retrievals or completely failed to find relevant passages, resulting in an immediate abstention (`answered_from_context: false`).
- **Diagnosis (as if we had not caused it)**:
  1. Running `kubectl logs -l app.kubernetes.io/name=indexer -n rahbar` revealed `source=faculty-entrepreneurship-and-technical-start-ups-policy.pdf chunks=0`.
  2. Inspecting the indexed document using `pdffonts corpus/raw/faculty-entrepreneurship-and-technical-start-ups-policy.pdf` returned an empty font table, confirming the file is image-only.
  3. Querying the API logged:
     ```json
     {"logger": "rahbar.answer", "msg": "abstaining: relevance floor not reached", "top_score": 0.12, "floor": 0.55}
     ```
- **Remedy**: Re-enabled `pdf2image` and `pytesseract` OCR in `indexer/pipeline/extract.py`. Re-running the indexing Job extracted 24 structured chunks from the scanned pages, restoring recall@5 for startup policy queries to 100%.

---

## Stage B · Chunking (Fixed-Size 100-Token Chunks With Zero Overlap)

- **What we broke**: Replaced structure-aware chunking in `indexer/pipeline/chunk.py` with fixed 100-token windows without overlap (`chunk_size=100, chunk_overlap=0`).
- **Symptom**: Complex policy clauses (e.g. academic warning regulations, multi-paragraph Generative AI guidelines, and fee schedules) were arbitrarily severed across chunk boundaries. Model answers were incoherent or hallucinated missing conditional clauses.
- **Diagnosis (as if we had not caused it)**:
  1. CI evaluation gate failed with `recall@5 dropped by 0.142 (from 0.915 to 0.773), exceeds maximum allowed drop 0.05`.
  2. Looking at retrieved chunks in `api` logs:
     ```json
     {"chunk_id": "academic-rules-2026-12", "text": "shall be placed on academic warning if", "section": "General"}
     ```
     The conditional threshold (`CGPA < 2.00`) was severed into the subsequent chunk, leaving the retrieved context incomplete.
- **Remedy**: Restored structure-aware hierarchical chunking (splitting on section headers, then paragraphs, then sentences) with target size 400 tokens and 50 tokens overlap. Re-running `eval/run_eval.py` restored recall@5 to 0.915.

---

## Stage C · Retrieval (Altered EMBED_MODEL on API Only)

- **What we broke**: Changed `EMBED_MODEL` in `k8s/base/configmap.yaml` to `all-MiniLM-L6-v2` while leaving the Chroma index populated with vectors from `nomic-embed-text`.
- **Symptom**: The API refused to become ready. API pods remained at `0/1 READY`, and traffic was safely withheld from the deployment by Kubernetes.
- **Diagnosis (as if we had not caused it)**:
  1. `kubectl get pods -n rahbar` displayed:
     ```text
     NAME                   READY   STATUS    RESTARTS   AGE
     api-7f99b8d85f-k2l8x   0/1     Running   0          45s
     ```
  2. `kubectl describe pod api-7f99b8d85f-k2l8x -n rahbar` reported:
     ```text
     Warning  Unhealthy  Readiness probe failed: HTTP probe failed with statuscode: 503
     ```
  3. `kubectl logs api-7f99b8d85f-k2l8x -n rahbar` contained the exact guardrail conflict log:
     ```json
     {"logger": "rahbar.health", "level": "ERROR", "msg": "embedding_model mismatch: collection was built with 'nomic-embed-text' but API is configured with 'all-MiniLM-L6-v2'"}
     ```
- **Remedy**: Aligned `EMBED_MODEL` in the ConfigMap to match the indexer's model (`nomic-embed-text`). The readiness probe immediately returned HTTP 200, and the pods entered `1/1 READY`.

---

## Stage E · Source / Versioning (Removed Superseded Filter)

- **What we broke**: Removed the `where={"superseded": False}` filter in `api/app/retrieval/chroma.py`, allowing the retriever to return repealed rules from the 2023 undergraduate regulations.
- **Symptom**: When asked about graduation criteria and attendance requirements, RAHBAR confidently quoted repealed 2023 clauses instead of the prevailing 2026 handbook regulations.
- **Diagnosis (as if we had not caused it)**:
  1. Evaluation runner flagged failures on version-sensitive golden questions:
     ```text
     q09  version  FAIL: expected citation to Academic Rules Handbook 2026, received Undergraduate Academic Rules Aug 2023
     ```
  2. Inspecting the API log's `chunk_ids` revealed that chunks from `undergraduate-academic-rules-aug-2023.pdf` were being retrieved with higher cosine similarity than the 2026 document due to keyword density.
- **Remedy**: Reinstated the metadata filter `where={"superseded": False}` on standard queries, ensuring older policy versions are only surfaced if the user explicitly requests historical rules.
