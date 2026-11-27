"""Automated tests for Package B1: golden.jsonl schema/counts & embedding module eval."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "eval"))

from embed_eval import run_embedding_eval  # noqa: E402


def test_golden_jsonl_meets_all_assignment_counts_and_ground_truth() -> None:
    golden_path = REPO_ROOT / "eval" / "golden.jsonl"
    assert golden_path.exists()
    rows = [json.loads(line) for line in golden_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) >= 25

    answerable = [r for r in rows if not r["must_abstain"]]
    unanswerable = [r for r in rows if r["must_abstain"]]
    hard = [r for r in rows if r["difficulty"] == "hard"]
    human = [r for r in rows if r["origin"] == "human"]
    synthetic_reviewed = [r for r in rows if r["origin"] == "synthetic" and r.get("reviewed_by_human")]

    assert len(answerable) >= 15
    assert len(unanswerable) >= 5
    assert len(hard) >= 5
    assert len(human) >= 8
    assert len(synthetic_reviewed) >= 8

    # Check every question has valid origin label and every answerable question maps to real corpus docs
    batch_a = REPO_ROOT / "corpus" / "batches" / "batch_a"
    batch_b = REPO_ROOT / "corpus" / "batches" / "batch_b"
    for r in rows:
        assert r["origin"] in {"human", "synthetic"}
        if not r["must_abstain"]:
            assert r["relevant"], f"{r['id']} missing relevant passages"
            for ref in r["relevant"]:
                assert ref["source"] and isinstance(ref["page"], int) and ref["page"] >= 1
                assert (batch_a / ref["source"]).exists() or (batch_b / ref["source"]).exists()


def test_embedding_module_eval_runs_deterministically(tmp_path: Path) -> None:
    out_file = tmp_path / "embed_eval.json"
    res = run_embedding_eval(out_path=out_file, ollama_url="http://127.0.0.1:11434")
    assert out_file.exists()
    assert res["paraphrase_pairs_count"] >= 15
    assert res["unrelated_pairs_count"] >= 15
    assert res["dimensions"] == 768
    assert res["ms_per_chunk"] >= 0.0
    assert res["determinism"]["max_abs_diff"] == 0.0
    assert res["cosine_similarity_gap"] > 0.0
