# Package D1-D2 — Per-Query JSONL Traceability & Prompt Lookup Tool

Owner: **Mussa Raza (`stackmussa`)**  
Branch: `feature/d1-trace-tool-mussa`

## 1. Canonical JSONL Trace Record (`api/app/services/tracing.py`)

Every call to `/api/ask` (`answer_question` in [api/app/services/answer.py](../../api/app/services/answer.py)) emits a Pydantic-validated `TraceRecord` JSON line both to **stdout** (via raw `rahbar.trace` logger for `kubectl logs`) and to `TRACE_LOG_PATH` (`traces/traces.jsonl` locally, `/data/traces/traces.jsonl` in Kubernetes).

### Fields Recorded per Query

| Group | Fields | Source / Behavior |
| :--- | :--- | :--- |
| **Identity & Timing** | `record_type`, `schema_version`, `trace_id`, `timestamp` | Correlation ID from `request_id_var` + UTC ISO-8601 timestamp |
| **Queries** | `user_query`, `rewritten_query` | Whitespace normalization (`rewritten_query` set when modified); secrets/emails redacted via `redact_secrets()` |
| **Model & Index Pins** | `embedding_model`, `embedding_version`, `collection_name`, `collection_version` | From `Settings` (`EMBED_MODEL`, `EMBED_MODEL_VERSION`, `CHROMA_COLLECTION`, `COLLECTION_VERSION`) |
| **Retrieval Context** | `retrieved_chunks` (`chunk_id`, `doc_id`, `doc_version`, `source`, `section`, `page`, `score`, `text`), `filters_applied`, `reranked_order` | Populated directly from `ChromaRetriever` metadata (`superseded=False` filter when `include_superseded=False`) |
| **Prompt Provenance** | `system_prompt_sha256`, `prompt_sha256`, `prompt` | Full rendered `build_evidence_prompt(...)` plus SHA-256 hashes of system and user prompts |
| **Generation & Result** | `model`, `model_parameters`, `response`, `answered_from_context`, `confidence`, `citations` | From `Settings` (`llm_model`, `temperature`, `top_k`, `relevance_floor`) + verified `AskResponse` |
| **Ops & Guardrails** | `guardrail_actions`, `latency_ms_per_stage` (`total`, `embed`, `cache`, `retrieval`, `guardrail`, `generation`), `cache_hit`, `status` | Records cache hits, length rejections, relevance-floor abstentions, retries, fallbacks, dropped citations |

## 2. Live Trace Sample Verification ([traces/traces.jsonl](../../traces/traces.jsonl))

Four real queries executed against ChromaDB (`rahbar_b2`, `batch_a`) and Ollama (`llama3.2:3b`, `nomic-embed-text`):

1. `87a7cca0db8b44a6acc3661e56cb5a8f`: `status="answered"`, `rewritten_query` normalized double spaces, retrieved 3 chunks (`academic-rules@2024-08`, `attendance-policy@2025-01`), `total=69881.04 ms` (`generation=54124.75 ms`).
2. `81af52b98dc94855afd84862a8cda7ab`: `status="cache_hit"`, `cache_hit=True`, `guardrail_actions=["semantic_cache_hit"]`, `total=188.57 ms`.
3. `97ed8d4d7b224fffaff0bef8ad87361e`: `status="abstained"`, top score `0.5353 < 0.65`, `guardrail_actions=["relevance_floor_abstain"]`, `generation=0.0 ms`.
4. `13ac2540a6e549efb101245139b3f2a9`: `filters_applied.where=null` (`include_superseded=True` triggered by `"2023 older version"`).

## 3. Trace Lookup CLI ([scripts/inspect_trace.py](../../scripts/inspect_trace.py))

```bash
# Reconstruct exact retrieved context chunks and rendered prompt for a trace_id
.venv/bin/python scripts/inspect_trace.py 87a7cca0db8b44a6acc3661e56cb5a8f \
  --trace-file traces/traces.jsonl

# Or pipe mixed pod logs from stdin
kubectl logs deployment/api | .venv/bin/python scripts/inspect_trace.py <TRACE_ID> --trace-file -
```

Verified:
- Valid trace ID -> prints full `TraceRecord` metadata, numbered `[1..k]` retrieved chunks (`chunk_id`, `doc_id`, `doc_version`, `score`, chunk text), `SYSTEM_PROMPT`, and exact `prompt` (`sha256`), exiting `0`.
- Missing trace ID (`missing-trace-000`) -> prints clean error to stderr and exits `1`.
- Unit tests (`api/tests/test_tracing_d1_d2.py`): **23 passed**, `mypy` **23 source files clean**, `ruff` **All checks passed**.
