# Package F2 — Kubernetes Deployment, Eval Job & Controlled Release (Part F2–F5)

Owner: **Nisar Ahmad (`nisarahmad-31`)**  
Branch: `feature/f2-kubernetes-release-nisar`

## 1. Deliverables Added

- [k8s/eval-job.yaml](../../k8s/eval-job.yaml) & [k8s/base/gate-configmap.yaml](../../k8s/base/gate-configmap.yaml): Dedicated `eval-gate` Job (`backoffLimit: 0`, `rahbar-eval:dev` built via [eval/Dockerfile](../../eval/Dockerfile)) writing timestamped results to `rahbar-eval-pvc` and reading release-gate thresholds from ConfigMap `rahbar-gate-config` without baking thresholds into the image.
- [k8s/candidate.yaml](../../k8s/candidate.yaml): Isolated candidate stack (`ConfigMap rahbar-candidate-config`, `Job indexer-candidate` writing `rahbar_candidate`, `Deployment api-candidate`, `Service api-candidate`).
- [k8s/base/observability-pvc.yaml](../../k8s/base/observability-pvc.yaml) & [k8s/base/api.yaml](../../k8s/base/api.yaml): `rahbar-traces-pvc` (`1Gi`) mounted at `/traces` alongside raw JSONL `stdout` logs (`TRACE_STDOUT=true`, `TRACE_LOG_PATH=/traces/traces.jsonl`), `serviceAccountName: rahbar-api`, `maxUnavailable: 0` rolling updates, and measured resource requests/limits across all workloads.
- [k8s/base/networkpolicy.yaml](../../k8s/base/networkpolicy.yaml): Added `allow-eval-gate` and `api-candidate` rules so `eval-gate` talks only to `api` / `api-candidate` (never Chroma directly).

## 2. Live Minikube Verification Results

### A. Evaluation Job (`eval-gate`) & ConfigMap Threshold Flip (`rahbar-eval-pvc`)

| Run | Target / Config | Recall@5 | Faithfulness (lex) | Abstention | Citation Valid | Latency p50 / p95 (ms) | Gate / Job Outcome |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- |
| **Baseline** | `svc/api` (`TOP_K=5`) | `0.7778` | `0.7024` | `1.0000` | `0.8750` | `51235.71 / 86205.29` | `Complete` (`baseline`) |
| **Candidate (`TOP_K=3`)** | `svc/api-candidate` (`max_lat <= 110000`) | `0.7778` | `0.7083` | `1.0000` | `1.0000` | `25711.79 / 45713.30` | **`PASS`** (`Complete`, exit `0`) |
| **ConfigMap Flip** | Same candidate, `ConfigMap` patched (`faith >= 0.99`, `lat <= 10000`) | `0.7778` | `0.8472` | `1.0000` | `1.0000` | `30251.28 / 44193.06` | **`FAIL`** (`Failed 0/1`, no image rebuild) |
| **Bad Candidate (`TOP_K=1`)** | `svc/api-candidate` (`TOP_K=1`) under normal gate | `0.6667` (`-0.1111`) | `0.6945` | `1.0000` | `1.0000` | `15624.68 / 29710.88` | **`FAIL`** (`Failed 0/1`, blocked!) |

### B. Controlled Release, `maxUnavailable: 0`, Rollback & Post-Rollback Trace

1. **Blocked Candidate (`TOP_K=1`):** Stopped by `job/eval-gate` (`recall@5_drop = 0.1111 > 0.02`); never rolled out to live `deployment/api`.
2. **Passing Candidate Rollout (`maxUnavailable: 0`):** Promoted `TOP_K=3` (`rahbar_candidate`, `v2-cand`) onto `deployment/api` (`Revision 3`), watched with `kubectl rollout status` (`1 out of 3` -> `2 out of 3` -> `successfully rolled out`, 0 unavailable).
3. **Bad Release Protection & Undo:** Setting `CHROMA_COLLECTION=rahbar_broken_release` (`Revision 4`) failed `/readyz` on the 1 surge pod while `maxUnavailable: 0` kept all 3 healthy pods serving. Executed `kubectl rollout undo deployment/api -n rahbar --to-revision=2` (`deployment "api" successfully rolled out`).
4. **Post-Rollback Trace Proof ([traces/k8s_post_rollback_trace.jsonl](../../traces/k8s_post_rollback_trace.jsonl)):** Query `48f6de4fc51446099959797a1814f9ae` read directly from `kubectl logs` and reconstructed with `scripts/inspect_trace.py` confirmed `"collection_name": "rahbar"`, `"collection_version": "v1"`, `"embedding_model": "nomic-embed-text"`, `"embedding_version": "latest"`.

### C. Observability & Cluster Resources

- **Metrics (`/metrics` via `kubectl port-forward svc/api 18000:8000 -n rahbar`):** Verified `rahbar_requests_total`, `rahbar_request_seconds_bucket`, `rahbar_retrieval_seconds`, `rahbar_generation_seconds`, `rahbar_injection_attempts_total` live.
- **Persisted Cluster Trace Summary ([eval/results/k8s/d3_k8s_trace_summary.json](../../eval/results/k8s/d3_k8s_trace_summary.json)):** `24` traces extracted from `rahbar-traces-pvc` (`99.0 KB` `traces.jsonl`, `163.2 KB` `candidate_traces.jsonl`), `3` live injection attempts neutralized (`p95 guardrail = 2.92 ms`).
- **Measured `kubectl top` vs. Manifests:** See table in [k8s/README.md](../../k8s/README.md).
