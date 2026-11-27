# Assignment 03 — Package 0 Bootstrap & Baseline Report

**Owner:** Mussa Raza (`stackmussa` / `i243022@isb.nu.edu.pk`)  
**Integration branch:** `assignment-03` (branched from `main`)  
**Bootstrap branch:** `chore/assignment-03-baseline-mussa`  
**Date:** 2026-11-27  

---

## 1. Repository & Workspace Verification

- **Parent workspace:** `E:\S5\SCD\#3`
- **Assignment 03 specification:** `E:\S5\SCD\#3\Assignment_03.md` (untouched; SHA-256 `D81DB089E934226FAD9A504FA3DAD2309122CBC19B3762DB2646B863B534239B`)
- **Repository clone:** `E:\S5\SCD\#3\rahbar` (`/mnt/e/S5/SCD/#3/rahbar`)
- **Remote (`origin`):** `https://github.com/nisarahmad-31/rahbar.git`
- **Default branch:** `main` (also has `remotes/origin/dev`)
- **Baseline commit hash:** `afff6f9795abc6e75d37b520c78fba7c6cef08bd` (`Merge pull request #2 from nisarahmad-31/dev`)
- **Git identity (repo-local):** `user.name = Mussa Raza`, `user.email = i243022@isb.nu.edu.pk` (verified primary email on `stackmussa`)

---

## 2. Existing RAHBAR (Assignment 02) Architecture & Flows

### Entry Points & Components
- **API (`api/`):** FastAPI service (`api/app/main.py`, `uvicorn app.main:app --port 8000`) exposing:
  - `POST /api/ask` (`api/app/routes/ask.py` -> `api/app/services/answer.py:answer_question`)
  - `GET /healthz` & `GET /readyz` (`api/app/routes/health.py`) — `/readyz` checks Chroma reachability, non-empty collection, Redis ping, and `EMBED_MODEL` pin match
  - `GET /metrics` (`api/app/routes/metrics.py`) — Prometheus metrics
- **Indexer (`indexer/`):** Batch Kubernetes Job / CronJob entry point (`indexer/pipeline/run.py`, `python -m pipeline.run [--dummy]`).
  - Stages: `discover()` -> `extract()` (`indexer/pipeline/extract.py`: `pypdf`, `pdfplumber`, `pytesseract` + `pdf2image` OCR) -> `clean()` (`indexer/pipeline/clean.py`) -> `chunk()` (`indexer/pipeline/chunk.py`: structure-aware heading + sentence/word overlap chunker) -> `embed()` (`indexer/pipeline/embed.py`: `OLLAMA_URL/api/embed` with `EMBED_MODEL=nomic-embed-text` and `_deterministic_vector` offline fallback) -> `store()` (`indexer/pipeline/store.py`: **deletes and recreates** `settings.collection` in Chroma, adds chunks in batches of 100, and calls Redis `FLUSHDB`).
- **Retrieval & Generation (`api/app/`):**
  - `ChromaRetriever` (`api/app/retrieval/chroma.py`): embeds query via Ollama (`/api/embed`) or `_deterministic_query_vector` fallback, queries Chroma with `where={"superseded": False}` unless `_wants_superseded(question)` matches historical keywords.
  - `SemanticCache` (`api/app/services/cache.py`): Redis-backed (with in-memory fallback), cosine threshold `0.88`, skips follow-ups/abstentions/fallbacks/uncited low-confidence replies.
  - Relevance floor (`RELEVANCE_FLOOR=0.55`): abstains cleanly (`answered_from_context=False`) if `passages[0].score < 0.55`.
  - Providers: `StubProvider`, `OllamaProvider`, `HostedProvider` (OpenAI-compatible / Groq). Validates output against `Answer` Pydantic schema (`api/app/schemas.py`), retries once, falls back safely, and verifies citations against retrieved chunks.
- **UI (`ui/`):** Static `ui/index.html` served by `nginxinc/nginx-unprivileged:1.27-alpine` on port `8080` (`ui/Dockerfile`).
- **Evaluation & Corpus (`eval/`, `corpus/`):** `corpus/fetch.py` (`sources.yaml` -> `corpus/raw/_index.json` + `MANIFEST.md`), `eval/run_eval.py` (`eval/golden.yaml`, `eval/fixtures/golden.fixture.yaml`, `eval/baseline.json`).
- **Kubernetes (`k8s/`):** Base manifests (`namespace`, `configmap`, `chroma` StatefulSet, `redis` Deployment/PVC, `api` Deployment/Service, `ui` Deployment/Service, `ingress`, `hpa`, `networkpolicy`, `indexer-job`, `reindex-cronjob`) + `overlays/{dev,prod}`.

### Existing Assignment 02 Injection Defence
- **Mechanism:** Prompt-level isolation and schema enforcement in `api/app/providers/llm/base.py` (`SYSTEM_PROMPT` instructs grounding strictly in numbered passages; `build_evidence_prompt` wraps passages inside `=== EVIDENCE === ... === END EVIDENCE ===` as untrusted data).
- **Invocation:** Called by `OllamaProvider.generate()` (`api/app/providers/llm/ollama.py`) and `HostedProvider.generate()` (`api/app/providers/llm/hosted.py`), followed by `Answer.model_validate_json()` and `_verify_citations()` in `api/app/services/answer.py`.
- **Unit test:** `api/tests/test_safety_and_pipeline.py::test_prompt_injection_is_treated_as_data`.
- **Gap for Assignment 03 (D4):** There is currently no retrieval-stage injection classifier/rail or structured guardrail action field logged into per-query JSONL traces.

---

## 3. Dependency & Environment Audit

| Required tool/dependency | Already installed? | Version | Location/environment | Compatible with this project? | Action |
|---|---|---|---|---|---|
| **OS / WSL 2** | Yes | WSL `2.6.3.0`, Ubuntu `24.04.5 LTS` (`noble`) | `E:\WSL\Ubuntu-24.04\ext4.vhdx` | Yes | Reuse |
| **Python (Ubuntu)** | Yes | `3.12.3` | `/usr/bin/python3` | Yes (`py312` required) | Reuse (Windows `3.13.5` untouched) |
| **Build toolchain (`make`, `gcc`, `venv`, `pip`)** | Yes | `make 4.3`, `gcc 13.3.0`, `pip 24.0` | Ubuntu 24.04 | Yes | Reused / installed in Phase 2 |
| **RAHBAR `.venv` Python packages** | Yes | `fastapi 0.162.4`, `chromadb-client 1.0.12`, `redis 5.2.1`, `pytest 8.3.5`, `ruff 0.11.8`, `mypy 1.15.0`, etc. | `E:\S5\SCD\#3\rahbar\.venv` | Yes (exact pinned versions) | Created via `make install` |
| **Git** | Yes | Win `2.54.0` / Ubuntu `2.43.0` | Windows & Ubuntu | Yes | Reuse |
| **Docker Desktop** | Yes | `4.100.1` / Engine `29.9.3` / Compose `v5.7.1` | `E:\Docker\Docker` (data: `E:\Docker\data`) | Yes (WSL 2 integration enabled for `Ubuntu-24.04`) | Reuse |
| **`kubectl` / Kustomize** | Yes | `kubectl v1.36.1`, `Kustomize v5.8.1` | `/usr/local/bin/kubectl` (Docker Desktop WSL) | Yes (client ready; no cluster context active yet) | Reuse |
| **OCR system libs (`tesseract-ocr`, `poppler-utils`)** | In Docker image | Installed in `indexer/Dockerfile` (`python:3.12-slim`) | Container image `rahbar-indexer:dev` | Yes | Keep in Docker (avoid host duplication) |
| **GitHub CLI (`gh`)** | No | — | — | Optional (GCM handles `git` auth) | Report / install only if desired for PR CLI |
| **Minikube** | No | — | — | Required for full K8s cluster (`make cluster`) unless Docker Desktop K8s is used | **Blocked on user review / disk check** |
| **Ollama** | No | — | — | Required for local `nomic-embed-text` / `llama3.2:3b` (unless using `hosted` Groq) | **Blocked on user decision (Ollama vs Groq)** |

---

## 4. Baseline Verification Results (Pre-Assignment 03)

All baseline checks were executed inside `Ubuntu-24.04` at `/mnt/e/S5/SCD/#3/rahbar`:

| Check | Exact command | Result | Notes |
|---|---|---|---|
| **Unit tests** | `make test` | **PASS** (exit `0`) | `api/`: `20 passed`; `indexer/`: `6 passed` (stubs/offline) |
| **Lint & types** | `make lint` | **PASS** (exit `0`) | `ruff check api indexer eval corpus` passed; `mypy app` (22 files) & `mypy pipeline` (10 files) passed |
| **Submission lint** | `make check` | **PASS** (exit `0`) | `scripts/check_submission.py`: `165 passed · 2 warnings · 0 failures` |
| **Local stub API/UI** | `make dev` + `curl` checks | **PASS** | `/healthz` `200`, `/readyz` `200`, `/` UI `200`, `POST /api/ask` `200` valid `AskResponse` |
| **Fixture eval (stub mode)** | `make eval-fixtures` | **EXPECTED FAIL** (exit `1`) | Ran against stub API: `recall@5=0.0` (baseline `0.915`), `abstention_correctness=0.0%`, `citation_validity=1.0`. Expected in stub mode because `StubRetriever` always returns `stub-handbook.pdf`. |
| **Docker image builds** | `docker build -t rahbar-{ui,api,indexer}:dev` | **PASS** (exit `0`) | Built `rahbar-ui:dev` (`97MB`), `rahbar-api:dev` (`364MB`), `rahbar-indexer:dev` (`621MB`) |

---

## 5. Blockers & Decisions Needed Before Starting Package A1

1. **GitHub write permission for `stackmussa`:**
   - Git Credential Manager is authenticated as `stackmussa` (primary verified email `i243022@isb.nu.edu.pk`), **however** GitHub API reports `permissions: {"push": false, "pull": true}` on `nisarahmad-31/rahbar` with no pending repository invitation, and `git push --dry-run origin main` returns `Permission to nisarahmad-31/rahbar.git denied to stackmussa` (`403`).
   - **Action needed:** Nisar must add `stackmussa` as a collaborator with Write access on `nisarahmad-31/rahbar` (and Mussa accepts the invite), **or** confirm using a fork (`stackmussa/rahbar`) with cross-repo PRs.
2. **Local Kubernetes cluster choice (for Part F):**
   - `kubectl v1.36.1` is installed, `E:` has `20.31 GB` free (`C:` has `2.94 GB` free). Neither `minikube` nor Docker Desktop's built-in Kubernetes is currently enabled.
   - **Decision needed:** Approve installing `minikube` (to match Assignment 02 `Makefile`: `minikube start --driver=docker --cni=calico`) on `E:`, or enabling Docker Desktop Kubernetes (`kind`).
3. **Embedding / LLM runtime choice:**
   - **Decision needed:** Confirm whether to install `ollama` locally (`nomic-embed-text` + `llama3.2:3b`) or use a hosted OpenAI-compatible provider (e.g., Groq via `LLM_API_KEY`).
