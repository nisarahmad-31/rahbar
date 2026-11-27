# Package F1 — Persistent Data Storage & Ingestion Jobs (Part F1–F2)

Owner: **Mussa Raza (`stackmussa`)**  
Branch: `feature/f1-ingestion-persistence-mussa`

## 1. Implemented Kubernetes Resources

- [k8s/base/data-pvc.yaml](../../k8s/base/data-pvc.yaml): `PersistentVolumeClaim` `rahbar-data-pvc` (`1Gi`, `ReadWriteOnce`, `standard` provisioner) storing `/data/lake`, `/data/quarantine`, `/data/archive`, and `/data/reports`.
- [k8s/base/serviceaccounts.yaml](../../k8s/base/serviceaccounts.yaml): Dedicated non-privileged `ServiceAccount` objects (`rahbar-indexer`, `rahbar-api`) with `automountServiceAccountToken: false`.
- [k8s/base/indexer-job.yaml](../../k8s/base/indexer-job.yaml) & [k8s/base/reindex-cronjob.yaml](../../k8s/base/reindex-cronjob.yaml): One-shot `Job` (`indexer`) and weekly `CronJob` (`reindex`, schedule `"0 0 * * 0"`, `concurrencyPolicy: Forbid`) mounting `rahbar-data-pvc` at `/data` and `rahbar-corpus` ConfigMap at `/corpus`, running as non-root UID `10002` with `readOnlyRootFilesystem: true`.
- [k8s/base/configmap.yaml](../../k8s/base/configmap.yaml): Shared `DATA_LAKE_DIR`, `QUARANTINE_DIR`, `ARCHIVE_DIR`, `VERIFICATION_REPORT_DIR`, `RELEVANCE_FLOOR=0.65`, `EMBED_MODEL_VERSION`, and `COLLECTION_VERSION`.
- [k8s/base/networkpolicy.yaml](../../k8s/base/networkpolicy.yaml): Grants `rahbar-indexer` egress to `chroma:8000`, `redis:6379`, DNS, and host Ollama (`11434`), keeping Chroma isolated from unauthorized workloads (`ui`, etc.).

## 2. Live Minikube Verification Summary

1. **Batch A Initial Job (`job/indexer`):** Verified `5/5` passed, `0` quarantined; embedded `17` chunks in `3` API calls (`7.5s`) into Chroma (`chroma:8000`).
2. **Unchanged Rerun (`job/reindex-unchanged` via `cronjob/reindex`):** Executed in a brand-new pod against persisted `rahbar-data-pvc`: `documents_unchanged: 5`, **`chunks_embedded: 0`**, **`embed_api_calls: 0`** (`2.3s`).
3. **Batch B Merge (`job/reindex-batch-b`):** Verified `total: 11, passed: 4, quarantined: 7`; embedded `12` new chunks (`total_chunks_in_store: 29`), superseded `academic-rules@2024-08` (`4` superseded + `3` active chunks), and preserved Batch A active docs.
4. **Pod Restart Persistence:** Deleted `pod/chroma-0` (`StatefulSet` PVC `chroma-data-chroma-0`), waited for Ready, and inspected `rahbar-data-pvc` + Chroma counts in a fresh pod (`reindex-verify-persist`): confirmed all `29` chunks (`8` doc_ids), `7` quarantined Batch B files in `/data/quarantine/20261127_150830_cec4406f`, archived `academic-rules/2024-08` (`academic-rules-2024.md` + `metadata.json`) in `/data/archive`, and `3` verification JSON reports in `/data/reports`.

Detailed cluster instructions are documented in [k8s/README.md](../../k8s/README.md).
