# ADR 0001: Separation of Indexing as a Batch Job from Serving API

## Status
Accepted · 2026-11-03

## Context
In a Kubernetes deployment with multiple API replicas (`api` Deployment running 3 pods for high availability) and a shared vector store backed by ChromaDB, we needed to decide where document ingestion, text extraction, chunking, and embedding generation should execute.

The naive implementation performs indexing during API startup (e.g. in the FastAPI startup hook / lifespan). However, in a cluster environment, this creates severe architectural failures:
1. Three API replicas booting simultaneously race to write to the single Chroma persistent volume claim, corrupting index files.
2. Every pod restart (from node drain, rolling updates, or memory spikes) re-indexes the entire PDF corpus, wasting CPU and saturating network bandwidth.
3. API readiness probes report ready either before the index exists (serving empty retrievals) or after several minutes, causing Kubernetes to terminate the pod for failing startup probes.
4. Corpus re-indexing cannot be triggered on demand without a full application redeployment.

## Decision
We decouple indexing from the API process entirely by implementing indexing as a Kubernetes one-shot `Job` (`indexer-job.yaml`) and a scheduled weekly `CronJob` (`reindex-cronjob.yaml`).

The API service strictly reads the index and never writes to Chroma. The API readiness probe (`/readyz`) verifies that Chroma is reachable and that the target collection exists and is non-empty before marking the pod ready to receive user traffic.

## Options Considered
1. **Indexing in API startup (Lifespan)**: Rejected due to race conditions across replicas, startup probe timeouts, and redundant re-indexing on restarts.
2. **Kubernetes InitContainers on the API pod**: Rejected because InitContainers run on *every* pod replica startup, recreating the same multi-writer race condition on the shared PVC.
3. **Dedicated Kubernetes Job & CronJob (Chosen)**: Guarantees single-writer consistency, allows independent scheduling and resource limits, and enables zero-downtime re-indexing via `kubectl create job --from=cronjob/reindex`.

## Consequences
- **Positive**: Strict single-writer semantics; zero race conditions on the Chroma PVC; rapid API pod boot times (< 2 seconds); seamless rolling updates with zero unavailability.
- **Negative**: Requires managing a separate container image and Kubernetes Job manifest; API pods remain `0/1 READY` until the initial indexing Job completes.
