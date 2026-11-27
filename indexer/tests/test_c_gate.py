"""Unit tests for Package C2 configurable release gate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "eval"))

from release_gate import evaluate_gate  # noqa: E402


def test_release_gate_pass_and_fail_decisions() -> None:
    cfg = json.loads((REPO_ROOT / "eval" / "gate_config.json").read_text(encoding="utf-8"))
    baseline = {
        "run1": {
            "summary": {
                "recall@5": 0.80,
                "faithfulness_lexical": 0.75,
                "latency_p95_ms": 50000.0,
                "abstention_correctness": 1.0,
                "citation_validity": 0.94,
            }
        }
    }
    good_cand = {
        "run1": {
            "summary": {
                "recall@5": 0.79,
                "faithfulness_lexical": 0.76,
                "latency_p95_ms": 42000.0,
                "abstention_correctness": 1.0,
                "citation_validity": 0.95,
            }
        }
    }
    bad_cand = {
        "run1": {
            "summary": {
                "recall@5": 0.55,
                "faithfulness_lexical": 0.60,
                "latency_p95_ms": 40000.0,
                "abstention_correctness": 1.0,
                "citation_validity": 0.92,
            }
        }
    }

    res_pass = evaluate_gate(baseline, good_cand, cfg)
    assert res_pass["passed"] is True
    assert res_pass["decision"] == "PASS"

    res_fail = evaluate_gate(baseline, bad_cand, cfg)
    assert res_fail["passed"] is False
    assert res_fail["decision"] == "FAIL"
    failed_metrics = {c["metric"] for c in res_fail["checks"] if not c["passed"]}
    assert "recall@5_drop" in failed_metrics
    assert "faithfulness_lexical" in failed_metrics


def test_release_gate_cli_exit_codes_and_config_override(tmp_path: Path) -> None:
    base_f = tmp_path / "base.json"
    cand_f = tmp_path / "cand.json"
    cfg_f = tmp_path / "gate.json"

    base_f.write_text(json.dumps({"recall@5": 0.80, "faithfulness_lexical": 0.75, "latency_p95_ms": 45000, "abstention_correctness": 1.0, "citation_validity": 0.94}), encoding="utf-8")
    cand_f.write_text(json.dumps({"recall@5": 0.76, "faithfulness_lexical": 0.75, "latency_p95_ms": 45000, "abstention_correctness": 1.0, "citation_validity": 0.94}), encoding="utf-8")
    cfg_f.write_text(json.dumps({"max_recall5_drop": 0.02, "min_faithfulness_lexical": 0.70, "max_latency_p95_ms": 65000, "min_abstention_correctness": 1.0, "min_citation_validity": 0.90}), encoding="utf-8")

    # Fails with max_recall5_drop=0.02 (drop=0.04)
    r_fail = subprocess.run(
        [sys.executable, str(REPO_ROOT / "eval" / "release_gate.py"), "--baseline", str(base_f), "--candidate", str(cand_f), "--config", str(cfg_f)],
        capture_output=True,
        text=True,
    )
    assert r_fail.returncode == 1

    # Passes when ConfigMap file changes threshold to 0.05 without changing code
    cfg_f.write_text(json.dumps({"max_recall5_drop": 0.05, "min_faithfulness_lexical": 0.70, "max_latency_p95_ms": 65000, "min_abstention_correctness": 1.0, "min_citation_validity": 0.90}), encoding="utf-8")
    r_pass = subprocess.run(
        [sys.executable, str(REPO_ROOT / "eval" / "release_gate.py"), "--baseline", str(base_f), "--candidate", str(cand_f), "--config", str(cfg_f)],
        capture_output=True,
        text=True,
    )
    assert r_pass.returncode == 0
