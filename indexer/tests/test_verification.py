"""Automated tests for Package A1: verification checks, quarantine, and Batch A/B."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from pipeline.config import get_settings
from pipeline.extract import extract
from pipeline.models import Document, Page
from pipeline.run import run_pipeline
from pipeline.verify import verify_documents

REPO_ROOT = Path(__file__).resolve().parents[2]
BATCH_A = REPO_ROOT / "corpus" / "batches" / "batch_a"
BATCH_B = REPO_ROOT / "corpus" / "batches" / "batch_b"


def _make_settings(tmp_path: Path, corpus_dir: Path, threshold: float = 0.85):
    return replace(
        get_settings(),
        corpus_dir=str(corpus_dir),
        data_lake_dir=str(tmp_path / "lake"),
        quarantine_dir=str(tmp_path / "quarantine"),
        archive_dir=str(tmp_path / "archive"),
        verification_report_dir=str(tmp_path / "reports"),
        near_dup_threshold=threshold,
        quarantine_mode="copy",
    )


def test_quality_check_rejects_empty_and_garbled(tmp_path: Path) -> None:
    empty_file = tmp_path / "empty.md"
    empty_file.write_text("---\nsource: empty.md\ntitle: Empty Doc\ndoc_version: 2026\n---\n\nHi.", encoding="utf-8")
    garbled_file = tmp_path / "garbled.txt"
    garbled_file.write_text("@@@###$$$%%%^^^&&&***((()))!!!~~~```" * 5, encoding="utf-8")

    settings = _make_settings(tmp_path, tmp_path)
    items = [
        (empty_file, extract(str(empty_file), "2026", False)),
        (garbled_file, Document(source="garbled.txt", doc_version="2026", superseded=False, doc_id="garbled", title="Garbled", date="2026", pages=[Page(source="garbled.txt", page=1, text=garbled_file.read_text(encoding="utf-8"))])),
    ]
    passed, report = verify_documents(items, settings, batch_name="unit-quality", update_manifest=False)

    assert passed == []
    assert report.failed_count == 2
    assert report.quarantined_count == 2
    assert report.documents[0].checks["quality"].status == "fail"
    assert report.documents[1].checks["quality"].status == "fail"
    assert Path(report.documents[0].quarantine_path or "").exists()


def test_completeness_check_rejects_missing_metadata_and_truncation(tmp_path: Path) -> None:
    missing_file = BATCH_B / "b10_missing_meta_hostel_draft.md"
    trunc_file = BATCH_B / "b09_truncated_internship_policy.md"

    settings = _make_settings(tmp_path, BATCH_B)
    items = [
        (missing_file, extract(str(missing_file), "", False)),
        (trunc_file, extract(str(trunc_file), "2026-08", False)),
    ]
    passed, report = verify_documents(items, settings, batch_name="unit-completeness", update_manifest=False)

    assert passed == []
    assert report.documents[0].checks["completeness"].status == "fail"
    assert "missing required metadata" in report.documents[0].checks["completeness"].reason
    assert report.documents[1].checks["completeness"].status == "fail"
    assert "truncated" in report.documents[1].checks["completeness"].reason


def test_batch_a_and_batch_b_verification_and_non_indexing(tmp_path: Path, monkeypatch) -> None:
    captured_chunks = []

    def fake_store(chunks, vectors, settings):
        captured_chunks.extend(chunks)
        return len(chunks)

    monkeypatch.setattr("pipeline.store.store", fake_store)

    # Run Batch A
    settings_a = _make_settings(tmp_path, BATCH_A, threshold=0.85)
    count_a = run_pipeline(settings_a, batch_name="batch-a")
    assert count_a >= 5
    reports_a = sorted((tmp_path / "reports").glob("verification_batch-a_*.json"))
    assert len(reports_a) == 1
    rep_a = json.loads(reports_a[0].read_text(encoding="utf-8"))
    assert rep_a["summary"]["total_documents"] == 5
    assert rep_a["summary"]["passed_count"] == 5
    assert rep_a["summary"]["quarantined_count"] == 0

    # Reset captured chunks and run Batch B against same data lake manifest
    captured_chunks.clear()
    settings_b = _make_settings(tmp_path, BATCH_B, threshold=0.85)
    count_b = run_pipeline(settings_b, batch_name="batch-b")
    assert count_b >= 4

    reports_b = sorted((tmp_path / "reports").glob("verification_batch-b_*.json"))
    assert len(reports_b) == 1
    rep_b = json.loads(reports_b[0].read_text(encoding="utf-8"))
    assert rep_b["summary"]["total_documents"] == 11
    assert rep_b["summary"]["passed_count"] == 4
    assert rep_b["summary"]["quarantined_count"] == 7

    by_source = {d["source"]: d for d in rep_b["documents"]}
    assert by_source["b01_scholarship_policy_2026.md"]["status"] == "pass"
    assert by_source["b02_exam_conduct_2026.md"]["status"] == "pass"
    assert by_source["b03_academic_rules_2026.md"]["status"] == "pass"
    assert by_source["b11_injected_advisor_notice.md"]["status"] == "pass"

    assert by_source["b04_fee_schedule_2023.md"]["checks"]["recency"]["status"] == "fail"
    assert by_source["b05_exact_dup_scholarship.md"]["checks"]["uniqueness"]["status"] == "fail"
    assert "exact" in by_source["b05_exact_dup_scholarship.md"]["checks"]["uniqueness"]["reason"]
    assert by_source["b06_exact_dup_grading.md"]["checks"]["uniqueness"]["status"] == "fail"
    assert "exact" in by_source["b06_exact_dup_grading.md"]["checks"]["uniqueness"]["reason"]
    assert by_source["b07_near_dup_exam_conduct.md"]["checks"]["uniqueness"]["status"] == "fail"
    assert "near-duplicate" in by_source["b07_near_dup_exam_conduct.md"]["checks"]["uniqueness"]["reason"]
    assert by_source["b08_corrupt_lab_policy.pdf"]["checks"]["quality"]["status"] == "fail"
    assert by_source["b09_truncated_internship_policy.md"]["checks"]["completeness"]["status"] == "fail"
    assert by_source["b10_missing_meta_hostel_draft.md"]["checks"]["completeness"]["status"] == "fail"

    # Prove zero quarantined documents entered chunking/indexing
    indexed_sources = {c.source for c in captured_chunks}
    quarantined_sources = {d["source"] for d in rep_b["documents"] if d["quarantined"]}
    assert indexed_sources.isdisjoint(quarantined_sources)
    assert indexed_sources == {
        "b01_scholarship_policy_2026.md",
        "b02_exam_conduct_2026.md",
        "b03_academic_rules_2026.md",
        "b11_injected_advisor_notice.md",
    }

    # Verify manifest marked Batch A's academic-rules-2024.md as superseded by b03_academic_rules_2026.md
    manifest = json.loads((tmp_path / "lake" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["documents"]["academic-rules-2024.md"]["superseded"] is True
    assert manifest["documents"]["academic-rules-2024.md"]["superseded_by"] == "b03_academic_rules_2026.md"
