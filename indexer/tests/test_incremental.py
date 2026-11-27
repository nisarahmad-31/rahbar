"""Automated tests for Package A2: incremental update, deletion, archive & restore."""

from __future__ import annotations

import math
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from pipeline.config import get_settings
from pipeline.embed import embed_with_stats as real_embed_with_stats
from pipeline.run import run_pipeline
from pipeline.store import (
    count_chunks_by_doc_and_status,
    count_chunks_by_doc_id,
    restore_document_version,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BATCH_A = REPO_ROOT / "corpus" / "batches" / "batch_a"
BATCH_B = REPO_ROOT / "corpus" / "batches" / "batch_b"


class InMemoryCollection:
    def __init__(self, name: str, metadata: dict[str, Any]) -> None:
        self.name = name
        self.metadata = dict(metadata)
        self.records: dict[str, dict[str, Any]] = {}

    def add(self, ids, documents, embeddings, metadatas) -> None:
        for i, d, e, m in zip(ids, documents, embeddings, metadatas):
            self.records[i] = {"document": d, "embedding": list(e), "metadata": dict(m)}

    def update(self, ids, metadatas=None) -> None:
        if metadatas is None:
            return
        for i, m in zip(ids, metadatas):
            if i in self.records:
                self.records[i]["metadata"] = dict(m)

    def delete(self, ids=None, where=None) -> None:
        if ids is not None:
            for i in ids:
                self.records.pop(i, None)
        if where is not None:
            to_del = [
                k for k, v in self.records.items()
                if all(v["metadata"].get(wk) == wv for wk, wv in where.items())
            ]
            for k in to_del:
                del self.records[k]

    def get(self, ids=None, where=None, include=None) -> dict[str, Any]:
        selected = []
        target_ids = list(ids) if ids is not None else list(self.records.keys())
        for k in target_ids:
            rec = self.records.get(k)
            if not rec:
                continue
            if where and not all(rec["metadata"].get(wk) == wv for wk, wv in where.items()):
                continue
            selected.append((k, rec))
        return {
            "ids": [k for k, _ in selected],
            "documents": [r["document"] for _, r in selected],
            "metadatas": [r["metadata"] for _, r in selected],
        }

    def query(self, query_embeddings, n_results=5, where=None, include=None) -> dict[str, Any]:
        qv = query_embeddings[0]
        scored = []
        for k, rec in self.records.items():
            if where and not all(rec["metadata"].get(wk) == wv for wk, wv in where.items()):
                continue
            dv = rec["embedding"]
            dot = sum(a * b for a, b in zip(qv, dv))
            n1 = math.sqrt(sum(a * a for a in qv)) or 1.0
            n2 = math.sqrt(sum(b * b for b in dv)) or 1.0
            sim = dot / (n1 * n2)
            scored.append((1.0 - sim, k, rec))
        scored.sort(key=lambda x: x[0])
        top = scored[:n_results]
        return {
            "ids": [[k for _, k, _ in top]],
            "documents": [[r["document"] for _, _, r in top]],
            "metadatas": [[r["metadata"] for _, _, r in top]],
            "distances": [[d for d, _, _ in top]],
        }

    def count(self) -> int:
        return len(self.records)


class InMemoryChromaClient:
    def __init__(self) -> None:
        self.collections: dict[str, InMemoryCollection] = {}

    def get_collection(self, name: str) -> InMemoryCollection:
        if name not in self.collections:
            raise ValueError(f"Collection {name} not found")
        return self.collections[name]

    def create_collection(self, name: str, metadata: dict[str, Any]) -> InMemoryCollection:
        col = InMemoryCollection(name, metadata)
        self.collections[name] = col
        return col

    def delete_collection(self, name: str) -> None:
        self.collections.pop(name, None)


def _settings_for(tmp_path: Path, corpus_dir: Path):
    return replace(
        get_settings(),
        corpus_dir=str(corpus_dir),
        data_lake_dir=str(tmp_path / "lake"),
        quarantine_dir=str(tmp_path / "quarantine"),
        archive_dir=str(tmp_path / "archive"),
        verification_report_dir=str(tmp_path / "reports"),
        near_dup_threshold=0.85,
        quarantine_mode="copy",
        ollama_url="http://127.0.0.1:11434",
    )


def test_unchanged_rerun_embeds_zero_chunks_and_handles_change_and_delete(tmp_path: Path, monkeypatch) -> None:
    corpus = tmp_path / "corpus_a"
    shutil.copytree(BATCH_A, corpus)
    client = InMemoryChromaClient()

    embed_calls: list[int] = []

    def spy_embed(texts: list[str], settings):
        embed_calls.append(len(texts))
        return real_embed_with_stats(texts, settings)

    monkeypatch.setattr("pipeline.store.embed_with_stats", spy_embed)

    # 1. Initial ingestion of Batch A
    s1 = _settings_for(tmp_path, corpus)
    total_1 = run_pipeline(s1, batch_name="a-init", client=client)
    assert total_1 >= 5
    assert sum(embed_calls) == total_1
    counts_1 = count_chunks_by_doc_id(client.get_collection("rahbar"))
    assert set(counts_1) == {"academic-rules", "attendance-policy", "fee-schedule", "grading-scale", "library-rules"}

    # 2. Re-run on unchanged corpus -> ZERO chunks embedded
    embed_calls.clear()
    total_2 = run_pipeline(s1, batch_name="a-unchanged", client=client)
    assert total_2 == total_1
    assert embed_calls == [0]

    # 3. Modify ONE document -> only that doc's chunks re-embedded
    embed_calls.clear()
    lib_file = corpus / "library-rules-2026.md"
    lib_file.write_text(
        lib_file.read_text(encoding="utf-8") + "\n2.2 Lost library cards incur a PKR 250 replacement fee.\n",
        encoding="utf-8",
    )
    run_pipeline(s1, batch_name="a-modified", client=client)
    assert len(embed_calls) == 1
    assert embed_calls[0] == count_chunks_by_doc_id(client.get_collection("rahbar"))["library-rules"]

    # 4. Delete ONE document -> zero orphan chunks left in vector store
    embed_calls.clear()
    lib_file.unlink()
    run_pipeline(s1, batch_name="a-deleted", client=client)
    assert embed_calls == [0]
    counts_after_del = count_chunks_by_doc_id(client.get_collection("rahbar"))
    assert "library-rules" not in counts_after_del
    assert set(counts_after_del) == {"academic-rules", "attendance-policy", "fee-schedule", "grading-scale"}


def test_superseded_archive_metadata_exclusion_and_restore(tmp_path: Path) -> None:
    corpus_a = tmp_path / "corpus_a"
    shutil.copytree(BATCH_A, corpus_a)
    client = InMemoryChromaClient()

    # Ingest Batch A
    s_a = _settings_for(tmp_path, corpus_a)
    run_pipeline(s_a, batch_name="batch-a", client=client)

    # Merge Batch B (b03_academic_rules_2026.md supersedes academic-rules-2024.md)
    s_b = _settings_for(tmp_path, BATCH_B)
    run_pipeline(s_b, batch_name="batch-b", incremental_mode="merge", client=client)

    col = client.get_collection("rahbar")
    status_counts = count_chunks_by_doc_and_status(col)
    assert status_counts["academic-rules"]["active"] >= 1
    assert status_counts["academic-rules"]["superseded"] >= 1

    # Archived file exists on disk
    archived_2024 = tmp_path / "archive" / "academic-rules" / "2024-08" / "academic-rules-2024.md"
    assert archived_2024.exists()

    # Active-only query (where={"superseded": False}) excludes 2024-08
    active_rows = col.get(where={"superseded": False})
    active_sources = {m["source"] for m in active_rows["metadatas"]}
    assert "b03_academic_rules_2026.md" in active_sources
    assert "academic-rules-2024.md" not in active_sources

    # Quarantine excluded b04..b10 from vector store
    all_sources = {m["source"] for m in col.get()["metadatas"]}
    for q_src in [
        "b04_fee_schedule_2023.md",
        "b05_exact_dup_scholarship.md",
        "b06_exact_dup_grading.md",
        "b07_near_dup_exam_conduct.md",
        "b08_corrupt_lab_policy.pdf",
        "b09_truncated_internship_policy.md",
        "b10_missing_meta_hostel_draft.md",
    ]:
        assert q_src not in all_sources

    # Restore / rollback academic-rules to 2024-08
    restore_res = restore_document_version("academic-rules", "2024-08", s_b, client=client)
    assert restore_res["restored"] == "academic-rules@2024-08"
    assert restore_res["demoted_version"] == "2026-11"

    active_after_restore = col.get(where={"superseded": False})
    restored_sources = {m["source"] for m in active_after_restore["metadatas"]}
    assert "academic-rules-2024.md" in restored_sources
    assert "b03_academic_rules_2026.md" not in restored_sources
