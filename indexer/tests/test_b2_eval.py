"""Unit tests for Package B2: retrieval metrics, noise perturbation, faithfulness, and suite runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "eval"))
sys.path.insert(0, str(REPO_ROOT / "api"))

import run_part_b  # noqa: E402
from app.main import app  # noqa: E402


def test_retrieval_metrics_recall_mrr_and_ndcg() -> None:
    relevant = [{"source": "fee-schedule-2026.md", "section": "1. Tuition", "page": 1}]
    retrieved = [
        {"source": "other.md", "section": "General", "page": 1},
        {"source": "fee-schedule-2026.md", "section": "1. Tuition Fee per Credit Hour", "page": 1},
        {"source": "library-rules-2026.md", "section": "1. Borrowing", "page": 1},
    ]
    assert run_part_b.recall_at_k(relevant, retrieved, 3) == 1.0
    assert run_part_b.recall_at_k(relevant, retrieved, 1) == 0.0
    assert run_part_b.reciprocal_rank(relevant, retrieved, 5) == 0.5
    ndcg = run_part_b.ndcg_at_k(relevant, retrieved, 5)
    assert ndcg is not None and abs(ndcg - (1.0 / 1.5849625)) < 0.01


def test_invalid_golden_jsonl_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"id": "x1", "question": "Test?", "must_abstain": False, "relevant": []}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty 'relevant'"):
        run_part_b.load_golden_jsonl(bad)


def test_query_typos_and_suite_with_stub_app(tmp_path: Path, monkeypatch) -> None:
    perturbed = run_part_b.apply_query_typos("What minimum attendance percentage is required?", seed=42)
    assert perturbed != "What minimum attendance percentage is required?"

    monkeypatch.setenv("RETRIEVER", "stub")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    with TestClient(app) as tc:
        class _ClientWrap:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json=None):
                path = "/" + url.split("://", 1)[1].split("/", 1)[1]
                return tc.post(path, json=json)

        monkeypatch.setattr(run_part_b.httpx, "Client", _ClientWrap)

        small_golden = tmp_path / "golden.jsonl"
        small_golden.write_text(
            "\n".join(
                [
                    json.dumps({"id": "t1", "question": "About the stub?", "must_abstain": False, "relevant": [{"source": "stub-handbook.pdf", "section": "1.1 About the stub", "page": 1}], "expected_answer_points": ["stub"]}),
                    json.dumps({"id": "t2", "question": "Unrelated hostel?", "must_abstain": True, "relevant": []}),
                ]
            ),
            encoding="utf-8",
        )

        report, out_file = run_part_b.run_suite(
            api_url="http://testserver",
            golden_path=small_golden,
            results_dir=tmp_path / "results",
            label="test",
        )
        assert out_file.exists()
        assert report["run1"]["summary"]["recall@5"] == 1.0
        assert report["run1"]["summary"]["citation_validity"] == 1.0
        assert "stochasticity_diff" in report
        assert "noise_test" in report
