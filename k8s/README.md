# RAHBAR on Kubernetes (Minikube)

Manifests under `k8s/base/` and `k8s/overlays/dev/` run the full RAHBAR RAGOps pipeline on local Minikube (`--driver=docker --cni=calico`) connected to host Ollama (`http://host.minikube.internal:11434`).

## 1. Fresh Cluster Setup

```bash
minikube start --driver=docker --cni=calico --memory=4096 --cpus=3
minikube addons enable metrics-server
minikube addons enable ingress

# Build and load local images into Minikube
docker build -t rahbar-api:dev api
docker build -t rahbar-indexer:dev indexer
docker build -t rahbar-ui:dev ui
minikube image load rahbar-api:dev
minikube image load rahbar-indexer:dev
minikube image load rahbar-ui:dev
```

## 2. Persistence & Ingestion Jobs (Part F1)

### Persistent Volumes & Mount Paths

| Claim | Size | Mount / Purpose | Sole / Primary Consumers |
| :--- | :--- | :--- | :--- |
| `rahbar-data-pvc` | `1Gi` | `/data` (`lake/`, `quarantine/`, `archive/`, `reports/`) | `indexer` Job & `reindex` CronJob (`sa/rahbar-indexer`, UID `10002`) |
| `chroma-data` (`StatefulSet` template) | `2Gi` | `/data` inside `chroma-0` (ChromaDB SQLite/HNSW store) | `chroma` StatefulSet |
| `redis-data` | `1Gi` | `/data` inside `redis` (AOF semantic cache) | `redis` Deployment |

### Apply Infrastructure & Load Corpus Batch

```bash
# Apply namespace, service accounts, PVCs, ConfigMap, Chroma, Redis, NetworkPolicies, and CronJob
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

### Status & Persistence Verification

```bash
kubectl get pvc,sa,jobs,cronjobs,pods -n rahbar

# Delete chroma-0 to verify vector-store PVC persistence across pod restarts
kubectl delete pod chroma-0 -n rahbar
kubectl wait --for=condition=ready pod/chroma-0 -n rahbar --timeout=180s
```

### Sole Vector-Store Writer Enforcement

- Only `job/indexer` and `cronjob/reindex` (`ServiceAccount: rahbar-indexer`) run `pipeline.store` against `chroma:8000` (writes, deletions, metadata updates, Redis cache flush).
- `deployment/api` (`ServiceAccount: rahbar-api`) does not mount `rahbar-data-pvc`, has no `pipeline.store` dependency, and only issues read-only `collection.query(...)` calls.
- `NetworkPolicy` (`default-deny-all`, `allow-chroma`, `allow-indexer`, `allow-redis`) restricts pod-to-pod traffic to explicit service ports (`chroma:8000`, `redis:6379`).
