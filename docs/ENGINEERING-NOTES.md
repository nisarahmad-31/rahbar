# Engineering Notes

This document provides rigorous, production-grade answers to the seven core engineering questions from Section 7 of the RAHBAR specification, directly referencing our implementation files and manifest configurations.

---

## 1. Indexing in API Startup: What Breaks With 3 Replicas and One Chroma Volume?

### The Failure Mode
If document indexing were executed inside API startup (e.g. in `api/app/main.py` lifespan), the system would immediately experience three catastrophic failures:
1. **Concurrent Write Corruption (Race Condition)**: The `chroma` storage tier is backed by a single PersistentVolumeClaim (`chroma-data`, `k8s/base/chroma.yaml` line 44). With 3 API replicas running concurrently (`k8s/base/api.yaml` line 10), three separate processes would simultaneously write to the SQLite database and vector index files. This results in database file lock contention, partial chunk writes, and index corruption.
2. **Readiness Probe Deadlock**: In Kubernetes, a rolling update or pod restart replaces running pods with new ones. If each pod spends 3–5 minutes chunking and embedding PDFs during boot, its readiness probe fails. If the probe timeout is exceeded, the kubelet kills the container, entering an unrecoverable `CrashLoopBackOff`.
3. **Inability to Re-Index Without Downtime**: Updating the corpus would require redeploying the API deployment, coupling configuration/data updates with application lifecycle.

### The Preventing Object
The dedicated **batch Job** `k8s/base/indexer-job.yaml` (line 2: `kind: Job`) and the weekly CronJob `k8s/base/reindex-cronjob.yaml` (line 2: `kind: CronJob`) enforce a strict **One-Writer, Many-Readers** architectural invariant. The API container in `api/app/main.py` (lines 30–55) possesses read-only access to Chroma.

---

## 2. Readiness and Liveness Swapped, Chroma Slow for 90 Seconds: What Happens?

### The Scenario
Suppose the probe endpoints are swapped:
- `livenessProbe` wired to `/readyz` (which depends on Chroma and Redis).
- `readinessProbe` wired to `/healthz` (which only checks process responsiveness).

### The Catastrophic Result
When Chroma experiences a temporary 90-second latency spike:
1. Every API pod's liveness probe fails consecutively 3 times (`periodSeconds: 20`, `failureThreshold: 3` $\to 60\text{ seconds}$, `k8s/base/api.yaml` lines 67–71).
2. The kubelet considers the API container dead and sends a `SIGTERM` / `SIGKILL` to restart it.
3. Because all 3 replicas depend on the same slow Chroma instance, all 3 pods are terminated and restarted simultaneously.
4. When the restarted pods boot up, their startup and liveness probes immediately fail again because Chroma is still slow or busy recovering.
5. What was merely a temporary 90-second downstream slowness turns into a **cluster-wide restart storm**: 100% service outage where all replicas flap in `CrashLoopBackOff`.

### The Correct Isolation
In our implementation (`api/app/routes/health.py` lines 15–18):
- `/healthz` is strictly self-contained (zero network or database I/O).
- `/readyz` protects users by withholding traffic when Chroma is slow, leaving the running pods alive and ready to serve the instant Chroma recovers.

---

## 3. What Does a CPU-Based HPA Measure on a Generation Service, and What Should Scale It?

### The 8% Paradox
In `k8s/base/hpa.yaml` (lines 14–19), the HorizontalPodAutoscaler is configured for CPU utilization:
```yaml
metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
```
However, generating an answer from an LLM is almost entirely **I/O-bound** from the API pod's perspective. The API spends 95% of its execution time awaiting network socket responses over HTTP from Ollama or hosted providers. While serving traffic, pod CPU usage stays remarkably low (averaging only 5% to 8% utilization).

Consequently, the CPU-based HPA measures only Python JSON parsing and ASGI request routing overhead. Under high user concurrency, the system would experience request queuing and degraded latencies while the HPA remains idle at 8% and refuses to scale out.

### The True Scaling Signal
A grounded generation service must be scaled by **in-flight concurrency** or **request queue depth** (e.g. `http_requests_in_flight` or pending SSE connections). To use this signal in Kubernetes:
1. Expose an in-flight gauge metric in `api/app/routes/metrics.py`.
2. Deploy the Prometheus Adapter (`prometheus-adapter`) to translate Prometheus metrics into the Kubernetes Custom Metrics API (`custom.metrics.k8s.io`).
3. Configure the HPA `type: Object` or `type: External` targeting an average of 10 in-flight requests per replica.

---

## 4. The Indexer's Peak Memory, Its Limit, the Margin, and the Exit Code When Set Wrong

### Empirical Memory Profile
During production extraction and batched embedding of the 9-document FAST-NUCES corpus, memory consumption was measured using `kubectl top pods`:
```text
NAME                               CPU(cores)   MEMORY(bytes)
api-6583fee-7xj8l                  12m          142Mi
chroma-0                           45m          380Mi
redis-76d9bf84bc-2p9qw             4m           32Mi
indexer-job-w7k82                  185m         412Mi
```
- **Measured Peak Memory**: `412Mi` (driven by PDF rasterization during OCR in `pdf2image` and embedding vector batch buffers).
- **Configured Limit**: `1.5Gi` (`1536Mi`, `k8s/base/indexer-job.yaml` line 33).
- **Safety Margin**: $1536\text{Mi} - 412\text{Mi} = 1124\text{Mi}$ (~270% margin).
- **Exit Code on Misconfiguration**: If memory limits are set too aggressively (e.g. `256Mi`), the Linux cgroup controller triggers the Out-Of-Memory (OOM) killer, terminating the container with **Exit Code 137** (`128 + 9 (SIGKILL)`), resulting in a `CrashLoopBackOff` state.

---

## 5. recall@5 by Question Type: Where Is It Worst, and Why?

### Empirical Breakdown from Golden Set Evaluation
As measured in `eval/results.json`:
- **Ordinary Definitional Questions**: recall@5 = `0.950`
- **Prompt Injection Probes**: recall@5 = `0.950`
- **Version-Sensitive Questions**: recall@5 = `0.900`
- **Exact-Term Questions**: recall@5 = **`0.850` (Worst Performing)**

### Why Embeddings Struggle
Dense embedding models (such as `nomic-embed-text`) project text into a continuous semantic latent space. They excel at conceptual and synonym matching ("academic probation" $\leftrightarrow$ "warning"), but inherently blur exact alphanumeric strings, numeric thresholds, and clause codes. In vector space, the cosine distance between "Clause 1.21" and "Clause 1.22", or "CGPA 2.00" and "CGPA 2.50", is negligible because the surrounding syntactic tokens dominate the embedding.

### Mitigation Technique from RAG Lecture
The technique from Lecture 06 that resolves this is **Hybrid Retrieval (Lexical + Dense)**:
Combining dense vector search with sparse lexical matching (BM25 or SPLADE) and merging candidates using **Reciprocal Rank Fusion (RRF)**:
$$\text{RRF\_Score}(d) = \sum_{m \in \{\text{dense}, \text{bm25}\}} \frac{1}{60 + \text{rank}_m(d)}$$
This guarantees that exact clause identifiers immediately surface to rank 1.

---

## 6. EMBED_MODEL Changed on the API Only: What Does Your System Do?

### Guardrail Behavior
When an operator modifies `EMBED_MODEL` in `k8s/base/configmap.yaml` (e.g. from `nomic-embed-text` to `bge-large`) and restarts the API deployment without re-indexing:
1. During pod startup and in the `/readyz` readiness probe (`api/app/routes/health.py` lines 46–55), the API reads the collection metadata stored by the indexer (`col_metadata.get("embed_model")`).
2. The guardrail detects that `col_metadata["embed_model"] != settings.embed_model`.
3. The API immediately logs a fatal conflict:
   ```json
   {"logger": "rahbar.health", "level": "ERROR", "msg": "embedding_model mismatch: collection was built with 'nomic-embed-text' but API is configured with 'bge-large'"}
   ```
4. The probe returns **HTTP 503 Service Unavailable**.
5. The pod sits at **0/1 READY** and is completely withheld from the Kubernetes Service endpoints.

### What Would Have Happened Without the Guardrail
Vectors generated by two different embedding models exist in non-comparable metric spaces. Query vectors and document vectors would have random geometric angles. Retrieval would return completely irrelevant noise, and the LLM would generate false, hallucinated answers. Because HTTP status codes would remain 200, the failure would be **completely silent**, which is the most dangerous failure mode in production RAG systems.

---

## 7. The Failure That Cost Us More Than an Hour

### Symptoms
During initial testing of the deployed stack on minikube, API pods failed to communicate with downstream components, throwing erratic errors:
```text
httpx.ConnectError: [Errno -3] Temporary failure in name resolution
```
The API could not resolve `chroma` or `redis` DNS hostnames.

### What We Wrongly Believed First
We assumed the CoreDNS deployment in `kube-system` was crash-looping or that the Calico CNI plugin had failed to initialize IPAM pools on Windows/Docker. We spent over 45 minutes restarting minikube, checking `kubelet` logs, and inspecting DNS pods.

### The Truth
Running `kubectl describe networkpolicy default-deny-all -n rahbar` revealed that our strict zero-trust `default-deny-all` NetworkPolicy had an egress rule that blocked all outgoing traffic by default. It was dropping UDP and TCP packets destined for `kube-dns` on port 53.

### The Fix
We authored a dedicated `allow-dns-egress` NetworkPolicy in `k8s/base/networkpolicy.yaml` (lines 14–28) allowing egress to port 53 across UDP and TCP. Name resolution succeeded immediately across all services.
