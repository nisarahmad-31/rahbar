# RAHBAR on Kubernetes (Minikube)

Manifests under `k8s/base/`, `k8s/overlays/dev/`, `k8s/candidate.yaml`, and `k8s/eval-job.yaml` run the full RAHBAR RAGOps pipeline on local Minikube (`--driver=docker --cni=calico`) connected to host Ollama (`http://host.docker.internal:11434`).

## 1. Fresh Cluster Setup

```bash
minikube start --driver=docker --cni=calico --memory=4096 --cpus=3
minikube addons enable metrics-server
minikube addons enable ingress

# Build and load local images into Minikube
docker build -t rahbar-api:dev api
docker build -t rahbar-indexer:dev indexer
docker build -t rahbar-ui:dev ui
docker build -t rahbar-eval:dev eval
minikube image load rahbar-api:dev
minikube image load rahbar-indexer:dev
minikube image load rahbar-ui:dev
minikube image load rahbar-eval:dev
```

## 2. Persistence & Ingestion Jobs (Part F1)

### Persistent Volumes & Mount Paths

| Claim | Size | Mount / Purpose | Sole / Primary Consumers |
| :--- | :--- | :--- | :--- |
| `rahbar-data-pvc` | `1Gi` | `/data` (`lake/`, `quarantine/`, `archive/`, `reports/`) | `indexer` Job & `reindex` CronJob (`sa/rahbar-indexer`, UID `10002`) |
| `chroma-data` (`StatefulSet` template) | `2Gi` | `/data` inside `chroma-0` (ChromaDB SQLite/HNSW store) | `chroma` StatefulSet |
| `redis-data` | `1Gi` | `/data` inside `redis` (AOF semantic cache) | `redis` Deployment |
| `rahbar-traces-pvc` | `1Gi` | `/traces` (`traces.jsonl`, `candidate_traces.jsonl`) | `api` & `api-candidate` Deployments (UID `10001`) |
| `rahbar-eval-pvc` | `1Gi` | `/results` (Part B & Release Gate JSON reports) | `eval-gate` Job (`k8s/eval-job.yaml`, UID `10003`) |

### Apply Infrastructure & Load Corpus Batch

```bash
# Apply namespace, service accounts, PVCs, ConfigMaps, Chroma, Redis, API, UI, NetworkPolicies, and CronJob
kubectl apply -k k8s/overlays/dev

# Load Batch A into rahbar-corpus ConfigMap and run the one-shot ingestion Job
kubectl create configmap rahbar-corpus -n rahbar \
  --from-file=corpus/batches/batch_a --dry-run=client -o yaml | kubectl apply -f -
kubectl delete job indexer -n rahbar --ignore-not-found
kubectl apply -f k8s/base/indexer-job.yaml
kubectl wait --for=condition=complete job/indexer -n rahbar --timeout=240s
kubectl logs job/indexer -n rahbar
```

### Trigger Scheduled Re-Index & Batch B Ingestion

```bash
# Unchanged rerun from CronJob template (verifies 0 re-embeddings on persisted PVC)
kubectl create job --from=cronjob/reindex reindex-unchanged -n rahbar
kubectl wait --for=condition=complete job/reindex-unchanged -n rahbar --timeout=180s
kubectl logs job/reindex-unchanged -n rahbar

# Update corpus ConfigMap to Batch B and re-run ingestion (quarantines 7 docs, archives academic-rules@2024-08)
kubectl create configmap rahbar-corpus -n rahbar \
  --from-file=corpus/batches/batch_b --dry-run=client -o yaml | kubectl apply -f -
kubectl create job --from=cronjob/reindex reindex-batch-b -n rahbar
kubectl wait --for=condition=complete job/reindex-batch-b -n rahbar --timeout=240s
kubectl logs job/reindex-batch-b -n rahbar
```

### Sole Vector-Store Writer Enforcement

- Only `job/indexer`, `job/indexer-candidate`, and `cronjob/reindex` (`ServiceAccount: rahbar-indexer`) run `pipeline.store` against `chroma:8000` (writes, deletions, metadata updates, Redis cache flush).
- `deployment/api` and `deployment/api-candidate` (`ServiceAccount: rahbar-api`) do not mount `rahbar-data-pvc` and only issue read-only `collection.query(...)` calls.
- `NetworkPolicy` (`default-deny-all`, `allow-chroma`, `allow-indexer`, `allow-redis`, `allow-eval-gate`) isolates `chroma:8000` from `ui` and `eval-gate`.

## 3. Evaluation Job & ConfigMap Release Gate (Part F2)

```bash
# 1) Deploy candidate collection indexer + api-candidate Deployment & Service
kubectl apply -f k8s/candidate.yaml
kubectl wait --for=condition=complete job/indexer-candidate -n rahbar --timeout=240s
kubectl wait --for=condition=available deployment/api-candidate -n rahbar --timeout=180s

# 2) Run baseline Part B evaluation Job (writes /results/part_b_baseline_latest.json to rahbar-eval-pvc)
kubectl patch configmap rahbar-gate-config -n rahbar --type merge \
  -p '{"data":{"TARGET_API_URL":"http://api:8000","EVAL_MODE":"baseline","EVAL_LABEL":"baseline"}}'
kubectl delete job eval-gate -n rahbar --ignore-not-found
kubectl apply -f k8s/eval-job.yaml
kubectl wait --for=condition=complete job/eval-gate -n rahbar --timeout=720s
kubectl logs job/eval-gate -n rahbar

# 3) Evaluate passing candidate (TOP_K=3) against ConfigMap gate -> PASS (exit 0)
kubectl apply -f k8s/base/gate-configmap.yaml
kubectl delete job eval-gate -n rahbar --ignore-not-found
kubectl apply -f k8s/eval-job.yaml
kubectl wait --for=condition=complete job/eval-gate -n rahbar --timeout=720s
kubectl logs job/eval-gate -n rahbar

# 4) Flip ConfigMap thresholds WITHOUT rebuilding images -> FAIL (Job Failed, exit 1)
kubectl patch configmap rahbar-gate-config -n rahbar --type merge \
  -p '{"data":{"EVAL_LABEL":"k8s_configmap_flip","gate_config.json":"{\"max_recall5_drop\":0.02,\"min_faithfulness_lexical\":0.99,\"max_latency_p95_ms\":10000.0,\"min_abstention_correctness\":0.80,\"min_citation_validity\":0.85,\"require_abstention_not_lower_than_baseline\":false}"}}'
kubectl delete job eval-gate -n rahbar --ignore-not-found
kubectl apply -f k8s/eval-job.yaml
kubectl wait --for=condition=failed job/eval-gate -n rahbar --timeout=720s
kubectl logs job/eval-gate -n rahbar
```

## 4. Controlled Release & Rollback (Part F3)

```bash
# Block bad candidate (TOP_K=1) when gate fails (never rolled out to deployment/api)
kubectl patch configmap rahbar-candidate-config -n rahbar --type merge -p '{"data":{"TOP_K":"1","COLLECTION_VERSION":"candidate_topk1"}}'
kubectl rollout restart deployment/api-candidate -n rahbar
kubectl apply -f k8s/eval-job.yaml   # exits 1 -> blocked!

# Roll passing candidate out to live deployment/api (maxUnavailable: 0)
kubectl set env deployment/api -n rahbar CHROMA_COLLECTION=rahbar_candidate COLLECTION_VERSION=candidate_topk3 TOP_K=3 EMBED_MODEL_VERSION=v2-cand
kubectl rollout status deployment/api -n rahbar

# Simulate bad release (e.g., missing collection -> /readyz prevents killing healthy pods under maxUnavailable: 0) and roll back
kubectl set env deployment/api -n rahbar CHROMA_COLLECTION=rahbar_broken_release COLLECTION_VERSION=bad_v3 EMBED_MODEL_VERSION=v3-broken TOP_K=1
kubectl rollout undo deployment/api -n rahbar --to-revision=2
kubectl rollout status deployment/api -n rahbar
```

## 5. Observability & Measured Resource Limits (Part F4–F5)

```bash
# Stream JSONL traces from stdout and inspect /metrics via port-forward
kubectl logs -l app.kubernetes.io/name=api -n rahbar --prefix=false --tail=200 | grep '"record_type":"rag_trace"'
kubectl port-forward svc/api 18000:8000 -n rahbar
curl -s http://127.0.0.1:18000/metrics | grep "^rahbar_"

# Inspect persisted trace summary & lookup via PVC copy or pod stdout
python scripts/inspect_trace.py <TRACE_ID> --trace-file traces/k8s_post_rollback_trace.jsonl
python scripts/summarize_traces.py --trace-file traces/k8s_traces.jsonl
```

### Measured Cluster Utilization vs. Manifest Requests / Limits (`kubectl top pods -n rahbar`)

| Component | Measured Idle / Active CPU | Measured Memory | Configured Requests | Configured Limits |
| :--- | :--- | :--- | :--- | :--- |
| `api` (`Deployment`) | `4–10m` / `352m` burst | `71–80Mi` | `50m` CPU / `128Mi` | `500m` CPU / `256Mi` |
| `api-candidate` (`Deployment`) | `4–10m` | `72–77Mi` | `50m` CPU / `128Mi` | `500m` CPU / `256Mi` |
| `chroma-0` (`StatefulSet`) | `4–9m` | `61–69Mi` | `50m` CPU / `128Mi` | `500m` CPU / `512Mi` |
| `redis` (`Deployment`) | `8–12m` | `3Mi` | `20m` CPU / `32Mi` | `200m` CPU / `128Mi` |
| `ui` (`Deployment`) | `1m` | `2Mi` | `10m` CPU / `16Mi` | `100m` CPU / `64Mi` |
| `indexer` / `reindex` (`Job`/`CronJob`) | batch embed burst | ~`120Mi` | `200m` CPU / `512Mi` | `1000m` CPU / `1.5Gi` |
| `eval-gate` (`Job`) | client HTTP poll | ~`40Mi` | `50m` CPU / `96Mi` | `250m` CPU / `256Mi` |
