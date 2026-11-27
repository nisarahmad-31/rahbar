#!/usr/bin/env python3
"""Part C2 configurable release gate.

Evaluates a candidate Part B result against the baseline Part B result using
thresholds loaded from an external JSON ConfigMap/file (never baked into image).
Exits 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPO_ROOT / "eval" / "results" / "part_b_baseline_latest.json"
DEFAULT_CONFIG = REPO_ROOT / "eval" / "gate_config.json"


def _extract_summary(report: dict[str, Any]) -> dict[str, Any]:
    if "run1" in report and isinstance(report["run1"], dict):
        return dict(report["run1"]["summary"])
    if "summary" in report and isinstance(report["summary"], dict):
        return dict(report["summary"])
    return dict(report)


def evaluate_gate(
    baseline_report: dict[str, Any],
    candidate_report: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    base = _extract_summary(baseline_report)
    cand = _extract_summary(candidate_report)

    checks: list[dict[str, Any]] = []

    # 1. Recall@5 drop vs baseline
    base_r5 = float(base.get("recall@5", 0.0))
    cand_r5 = float(cand.get("recall@5", 0.0))
    max_drop = float(config.get("max_recall5_drop", 0.02))
    r5_drop = round(base_r5 - cand_r5, 4)
    checks.append({
        "metric": "recall@5_drop",
        "baseline": base_r5,
        "candidate": cand_r5,
        "measured": r5_drop,
        "threshold": f"<= {max_drop}",
        "passed": r5_drop <= max_drop,
    })

    # 2. Faithfulness minimum
    min_faith = float(config.get("min_faithfulness_lexical", 0.70))
    cand_faith = float(cand.get("faithfulness_lexical", 0.0))
    checks.append({
        "metric": "faithfulness_lexical",
        "baseline": float(base.get("faithfulness_lexical", 0.0)),
        "candidate": cand_faith,
        "measured": cand_faith,
        "threshold": f">= {min_faith}",
        "passed": cand_faith >= min_faith,
    })

    # 3. Latency p95 maximum (ms)
    max_p95 = float(config.get("max_latency_p95_ms", 65000.0))
    cand_p95 = float(cand.get("latency_p95_ms", 0.0))
    checks.append({
        "metric": "latency_p95_ms",
        "baseline": float(base.get("latency_p95_ms", 0.0)),
        "candidate": cand_p95,
        "measured": cand_p95,
        "threshold": f"<= {max_p95}",
        "passed": cand_p95 <= max_p95,
    })

    # 4. Abstention correctness (must meet min AND not drop below baseline if configured)
    base_abs = float(base.get("abstention_correctness", 1.0))
    cand_abs = float(cand.get("abstention_correctness", 0.0))
    min_abs = float(config.get("min_abstention_correctness", 1.0))
    if config.get("require_abstention_not_lower_than_baseline", True):
        min_abs = max(min_abs, base_abs)
    checks.append({
        "metric": "abstention_correctness",
        "baseline": base_abs,
        "candidate": cand_abs,
        "measured": cand_abs,
        "threshold": f">= {min_abs}",
        "passed": cand_abs >= min_abs,
    })

    # 5. Citation validity minimum
    min_cit = float(config.get("min_citation_validity", 0.90))
    cand_cit = float(cand.get("citation_validity", 0.0))
    checks.append({
        "metric": "citation_validity",
        "baseline": float(base.get("citation_validity", 0.0)),
        "candidate": cand_cit,
        "measured": cand_cit,
        "threshold": f">= {min_cit}",
        "passed": cand_cit >= min_cit,
    })

    overall_passed = all(c["passed"] for c in checks)
    return {
        "decision": "PASS" if overall_passed else "FAIL",
        "passed": overall_passed,
        "checks": checks,
        "config": config,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply Part C2 release gate against baseline and candidate results.")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("GATE_CONFIG_PATH", str(DEFAULT_CONFIG))))
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    baseline_report = json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate_report = json.loads(args.candidate.read_text(encoding="utf-8"))
    config = json.loads(args.config.read_text(encoding="utf-8"))

    # Allow optional env overrides from Kubernetes ConfigMap without rebuilding images
    env_map = {
        "GATE_MAX_RECALL5_DROP": ("max_recall5_drop", float),
        "GATE_MIN_FAITHFULNESS": ("min_faithfulness_lexical", float),
        "GATE_MAX_LATENCY_P95_MS": ("max_latency_p95_ms", float),
        "GATE_MIN_ABSTENTION": ("min_abstention_correctness", float),
        "GATE_MIN_CITATION_VALIDITY": ("min_citation_validity", float),
    }
    for env_key, (cfg_key, caster) in env_map.items():
        if env_key in os.environ and os.environ[env_key].strip():
            config[cfg_key] = caster(os.environ[env_key])

    result = evaluate_gate(baseline_report, candidate_report, config)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"RELEASE GATE: {result['decision']}")
    for chk in result["checks"]:
        mark = "PASS" if chk["passed"] else "FAIL"
        print(
            f"  [{mark}] {chk['metric']:24s} | base={chk['baseline']} | cand={chk['candidate']} | measured={chk['measured']} (req {chk['threshold']})"
        )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
