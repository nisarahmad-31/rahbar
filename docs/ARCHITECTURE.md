# RAHBAR System Architecture

RAHBAR runs on Kubernetes inside the dedicated `rahbar` namespace under two strict invariants: **one writer, many readers** and **one shared embedding model pin**.

---

## 1. Component Topology

* **`ui` (`Deployment`, 1+ replicas):** Non-root unprivileged Nginx container serving static UI assets on port `8080`. It calls `/api/ask` on the same origin through Ingress and has zero network path to Chroma or Redis.
* **`api` (`Deployment`, 3 replicas + `HorizontalPodAutoscaler` 3–8):** FastAPI service on port `8000` handling validation, Redis semantic cache checks, Chroma similarity retrieval, honest abstention, JSON schema enforcement, and Prometheus metrics.
* **`indexer` (`Job` + weekly `CronJob`):** Batch pipeline container that extracts (with Tesseract OCR fallback), cleans, structure-aware chunks, embeds with `EMBED_MODEL`, stamps collection metadata in Chroma, invalidates Redis cache, and exits.
* **`chroma` (`StatefulSet`, 1 replica):** Vector store backed by a PersistentVolumeClaim via `volumeClaimTemplates`.
* **`redis` (`Deployment` + `PersistentVolumeClaim`):** Persistent semantic cache shared across all API replicas.
* **`Ingress` (`networking.k8s.io/v1`):** Routes `/` to `ui:8080` and `/api` to `api:8000` under host `rahbar.local`.
* **`NetworkPolicy`:** Default-deny ingress and egress across `rahbar`, selectively permitting Ingress -> UI/API, UI -> API, API -> Chroma/Redis, Indexer -> Chroma, DNS egress (`kube-dns` TCP/UDP 53), and controlled egress to the LLM/corpus endpoints.

---

## 2. Why Chroma Is a `StatefulSet` (Not a `Deployment`)

As required by Section 4.1 of the specification, ChromaDB (`k8s/base/chroma.yaml`) is deployed as a **`StatefulSet`** with `volumeClaimTemplates` rather than a **`Deployment`**:

1. **Single-Writer SQLite/HNSW Volume Lock Safety:** ChromaDB stores embedded vector indices (HNSW files) and metadata (SQLite) on local disk (`/data`). With a `Deployment` during a rolling update, Kubernetes can briefly start a new pod before terminating the old pod (`maxSurge`), resulting in two pods attaching or competing for the same `ReadWriteOnce` PVC and corrupting the index. A `StatefulSet` provides ordered at-most-one pod identity (`chroma-0`), terminating the old pod completely before creating the replacement.
2. **Deterministic Network Identity & Stable Storage Binding:** `volumeClaimTemplates` binds a dedicated PersistentVolumeClaim (`chroma-data-chroma-0`) permanently to ordinal `chroma-0` across pod restarts, evictions, and node rescheduling, ensuring the vector index survives cluster restarts without data loss.
3. **Headless/ClusterIP DNS Consistency:** Combined with `serviceName: chroma`, `chroma-0` retains predictable identity while callers connect cleanly through the `chroma` `ClusterIP` Service.

---

## 3. One-Writer / Many-Readers & Embedding-Pin Invariant

* **API (`api/app/main.py` & `api/app/routes/health.py`):** Opens Chroma read-only (`query()`). On startup and every `/readyz` probe, it verifies Chroma has a non-empty collection and compares `collection.metadata["embed_model"]` against `EMBED_MODEL` from `rahbar-config`. On any mismatch, `/readyz` returns HTTP `503`, withdrawing the pod from the `api` Service so incompatible vectors are never queried silently.
* **Indexer (`k8s/base/indexer-job.yaml` & `indexer/pipeline/store.py`):** Runs as a one-shot Kubernetes `Job` (`restartPolicy: OnFailure`, `backoffLimit: 3`) or manual run from `cronjob/reindex`, writes vectors with `hnsw:space: cosine` and `embed_model` metadata, and flushes Redis via `flushdb()` so stale answers are never served after corpus updates.
