# ADR 0003: Redis Persistent Semantic Cache vs Secondary Chroma Collection

## Status
Accepted · 2026-11-05

## Context
In high-throughput question-answering systems, students frequently ask identical or semantically equivalent questions (e.g., "What is the fee for BSCS?" and "How much does a BS in Computer Science cost?"). Hitting the downstream LLM generation stage on every query incurs unacceptable latency (2–5 seconds per query) and exhausts free-tier API quotas.

We needed to introduce a semantic caching layer that evaluates question similarity against previously answered questions and returns cached answers when cosine similarity exceeds a configured threshold (`0.88`).

## Decision
We selected **Redis with persistent volume storage (PVC)** as the caching backend (`k8s/base/redis.yaml`), backed by the `api/app/services/cache.py` service.

The cache implements strict never-cache invariants:
- Follow-up questions are never looked up or cached.
- Honest abstentions (`answered_from_context: false`) are never cached.
- Failed schema generations and low-confidence fallbacks are never cached.
- Whenever the indexer Job executes a re-index, a cache invalidation signal (`flushdb()`) purges stale answers.

## Options Considered
1. **Secondary ChromaDB Collection**: Evaluated using a separate Chroma collection to store question vectors. Rejected due to unnecessary write locks and slow collection-level invalidation.
2. **In-Memory Python Dictionary (`lru_cache`)**: Rejected because memory is not shared across the 3 API replicas, and cache state is completely lost upon pod restarts.
3. **Redis with PVC Persistence (Chosen)**: Sub-millisecond lookup latency, atomic operations, built-in key expiration (TTL), seamless cache invalidation via `flushdb()`, and state persistence across pod rollouts.

## Consequences
- **Positive**: Shared cache across all 3 API replicas; sub-10ms response times for cached queries; eliminates redundant LLM calls; automatically flushed on corpus updates.
- **Negative**: Adds an operational dependency (Redis Deployment + PVC) inside the cluster namespace.
