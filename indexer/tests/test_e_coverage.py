"""Unit tests for Assignment 03 Part E query coverage and drift tooling."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "eval") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "eval"))

import query_coverage  # noqa: E402


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_live_queries_dataset_counts_and_schema() -> None:
    live_path = REPO_ROOT / "eval" / "live_queries.jsonl"
    rows = _load_jsonl(live_path)
    assert len(rows) >= 40
    in_domain = [r for r in rows if r.get("domain") == "in-domain"]
    ood = [r for r in rows if r.get("domain") == "out-of-domain"]
    assert len(in_domain) >= 28
    assert len(ood) >= 12
    for row in rows:
        assert row["id"]
        assert row["query"]
        assert row["topic"]


def test_golden_preserves_original_b1_ids() -> None:
    golden_path = REPO_ROOT / "eval" / "golden.jsonl"
    rows = _load_jsonl(golden_path)
    ids = [r["id"] for r in rows]
    for i in range(1, 29):
        assert f"g{i:02d}" in ids


def test_query_coverage_exit_code_and_topic_grouping(tmp_path: Path) -> None:
    live_file = tmp_path / "live.jsonl"
    golden_file = tmp_path / "golden.jsonl"
    out_pass = tmp_path / "cov_pass.json"
    out_fail = tmp_path / "cov_fail.json"

    live_file.write_text(
        "\n".join([
            json.dumps({"id": "l1", "query": "CGPA standing rule?", "domain": "in-domain", "topic": "standing"}),
            json.dumps({"id": "l2", "query": "Parking lot location?", "domain": "out-of-domain", "topic": "parking"}),
        ]) + "\n",
        encoding="utf-8",
    )
    golden_file.write_text(
        json.dumps({"id": "g1", "question": "CGPA standing?"}) + "\n",
        encoding="utf-8",
    )

    # Orthogonal vectors: l1 matches g1 (sim=1.0), l2 misses (sim=0.0)
    def fake_embed(texts: list[str], ollama_url: str, model: str) -> list[list[float]]:
        out = []
        for t in texts:
            if "CGPA" in t:
                out.append([1.0, 0.0])
            else:
                out.append([0.0, 1.0])
        return out

    with patch.object(query_coverage, "_embed_texts", side_effect=fake_embed):
        rc_fail = query_coverage.main([
            "--live-queries", str(live_file),
            "--golden", str(golden_file),
            "--threshold", "0.72",
            "--min-coverage", "0.85",
            "--label", "test_fail",
            "--out", str(out_fail),
        ])
        assert rc_fail == 1
        rep_fail = json.loads(out_fail.read_text(encoding="utf-8"))
        assert rep_fail["coverage_rate"] == 0.5
        assert "parking" in rep_fail["uncovered_topic_groups"]

        rc_pass = query_coverage.main([
            "--live-queries", str(live_file),
            "--golden", str(golden_file),
            "--threshold", "0.72",
            "--min-coverage", "0.50",
            "--label", "test_pass",
            "--out", str(out_pass),
        ])
        assert rc_pass == 0
