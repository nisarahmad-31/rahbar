"""Tests validating Part F2/F3/F4/F5 Kubernetes manifests."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
K8S_ROOT = REPO_ROOT / "k8s"
K8S_BASE = K8S_ROOT / "base"


def _load_docs(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def test_f2_eval_job_and_gate_configmap_are_decoupled() -> None:
    gate_docs = _load_docs(K8S_BASE / "gate-configmap.yaml")
    by_kind = {d["kind"]: d for d in gate_docs}
    assert by_kind["PersistentVolumeClaim"]["metadata"]["name"] == "rahbar-eval-pvc"
    cm = by_kind["ConfigMap"]
    assert cm["metadata"]["name"] == "rahbar-gate-config"
    assert "gate_config.json" in cm["data"]

    eval_job = _load_docs(K8S_ROOT / "eval-job.yaml")[0]
    assert eval_job["kind"] == "Job"
    assert eval_job["spec"]["backoffLimit"] == 0
    pod_spec = eval_job["spec"]["template"]["spec"]
    assert pod_spec["restartPolicy"] == "Never"
    vols = {v["name"]: v for v in pod_spec["volumes"]}
    assert vols["eval-results"]["persistentVolumeClaim"]["claimName"] == "rahbar-eval-pvc"
    assert vols["gate-config"]["configMap"]["name"] == "rahbar-gate-config"


def test_f3_api_rolling_update_and_candidate_isolation() -> None:
    api_docs = _load_docs(K8S_BASE / "api.yaml")
    api_deploy = next(d for d in api_docs if d["kind"] == "Deployment")
    strategy = api_deploy["spec"]["strategy"]
    assert strategy["type"] == "RollingUpdate"
    assert strategy["rollingUpdate"]["maxUnavailable"] == 0
    api_vols = {v["name"]: v for v in api_deploy["spec"]["template"]["spec"]["volumes"]}
    assert api_vols["traces"]["persistentVolumeClaim"]["claimName"] == "rahbar-traces-pvc"

    cand_docs = _load_docs(K8S_ROOT / "candidate.yaml")
    kinds = [d["kind"] for d in cand_docs]
    assert "ConfigMap" in kinds
    assert "Deployment" in kinds
    assert "Service" in kinds
    assert "Job" in kinds
    cand_cm = next(d for d in cand_docs if d["kind"] == "ConfigMap")
    assert cand_cm["data"]["CHROMA_COLLECTION"] == "rahbar_candidate"
    live_cm = _load_docs(K8S_BASE / "configmap.yaml")[0]
    assert cand_cm["data"]["CHROMA_COLLECTION"] != live_cm["data"]["CHROMA_COLLECTION"]
