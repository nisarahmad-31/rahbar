"""Tests validating Part F1 Kubernetes manifests for persistence, Jobs, and sole-writer policy."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
K8S_BASE = REPO_ROOT / "k8s" / "base"


def _load_docs(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def test_f1_data_pvc_and_mounts_match_configmap() -> None:
    pvcs = _load_docs(K8S_BASE / "data-pvc.yaml")
    assert len(pvcs) == 1
    pvc = pvcs[0]
    assert pvc["kind"] == "PersistentVolumeClaim"
    assert pvc["metadata"]["name"] == "rahbar-data-pvc"

    cm = _load_docs(K8S_BASE / "configmap.yaml")[0]["data"]
    assert cm["DATA_LAKE_DIR"] == "/data/lake"
    assert cm["QUARANTINE_DIR"] == "/data/quarantine"
    assert cm["ARCHIVE_DIR"] == "/data/archive"
    assert cm["VERIFICATION_REPORT_DIR"] == "/data/reports"

    for manifest in ("indexer-job.yaml", "reindex-cronjob.yaml"):
        obj = _load_docs(K8S_BASE / manifest)[0]
        pod_spec = (
            obj["spec"]["template"]["spec"]
            if obj["kind"] == "Job"
            else obj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        )
        assert pod_spec["serviceAccountName"] == "rahbar-indexer"
        vols = {v["name"]: v for v in pod_spec["volumes"]}
        assert vols["data-lake"]["persistentVolumeClaim"]["claimName"] == "rahbar-data-pvc"
        container = pod_spec["containers"][0]
        mounts = {m["name"]: m for m in container["volumeMounts"]}
        assert mounts["data-lake"]["mountPath"] == "/data"
        assert container["securityContext"]["readOnlyRootFilesystem"] is True


def test_f1_cronjob_and_sole_writer_isolation() -> None:
    cron = _load_docs(K8S_BASE / "reindex-cronjob.yaml")[0]
    assert cron["kind"] == "CronJob"
    assert cron["spec"]["schedule"] == "0 0 * * 0"
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"

    sas = {d["metadata"]["name"]: d for d in _load_docs(K8S_BASE / "serviceaccounts.yaml")}
    assert "rahbar-indexer" in sas
    assert "rahbar-api" in sas

    netpols = {d["metadata"]["name"]: d for d in _load_docs(K8S_BASE / "networkpolicy.yaml")}
    chroma_from = netpols["allow-chroma"]["spec"]["ingress"][0]["from"]
    allowed_names = {entry["podSelector"]["matchLabels"]["app.kubernetes.io/name"] for entry in chroma_from}
    assert allowed_names == {"api", "indexer"}

    api_docs = _load_docs(K8S_BASE / "api.yaml")
    api_deploy = next(d for d in api_docs if d["kind"] == "Deployment")
    api_volumes = api_deploy["spec"]["template"]["spec"]["volumes"]
    pvc_claims = [v.get("persistentVolumeClaim", {}).get("claimName") for v in api_volumes]
    assert "rahbar-data-pvc" not in pvc_claims
