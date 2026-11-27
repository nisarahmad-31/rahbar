#!/usr/bin/env python3
"""Run and record Package A2 incremental indexing, deletion, archive, and restore evidence."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "indexer"))

from pipeline.clean import clean  # noqa: E402
from pipeline.config import get_settings  # noqa: E402
from pipeline.extract import extract  # noqa: E402
from pipeline.run import discover_entries  # noqa: E402
from pipeline.store import (  # noqa: E402
    count_chunks_by_doc_and_status,
    count_chunks_by_doc_id,
    get_client,
    incremental_sync,
    restore_document_version,
)
from pipeline.verify import verify_documents  # noqa: E402


def _run_step(corpus_dir: Path, settings, batch_name: str, mode: str, client) -> dict:
    entries = discover_entries(str(corpus_dir))
    extracted = [
        (
            e.path,
            clean(extract(str(e.path), e.doc_version, e.superseded, doc_id=e.doc_id, title=e.title, date=e.date)),
        )
        for e in entries
    ]
    passed, v_report = verify_documents(extracted, settings, batch_name=batch_name, corpus_dir=str(corpus_dir))
    inc = incremental_sync(passed, settings, client=client, incremental_mode=mode)
    return {
        "verification_summary": v_report.to_dict()["summary"],
        "incremental": inc,
    }


def main() -> int:
    batch_a_src = REPO_ROOT / "corpus" / "batches" / "batch_a"
    batch_b_src = REPO_ROOT / "corpus" / "batches" / "batch_b"
    out_path = REPO_ROOT / "eval" / "results" / "a2_incremental_evidence.json"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        corpus_a = tmp_path / "corpus_a"
        shutil.copytree(batch_a_src, corpus_a)

        base = get_settings()
        settings = replace(
            base,
            corpus_dir=str(corpus_a),
            data_lake_dir=str(tmp_path / "lake"),
            quarantine_dir=str(tmp_path / "quarantine"),
            archive_dir=str(tmp_path / "archive"),
            verification_report_dir=str(tmp_path / "reports"),
            near_dup_threshold=0.85,
            quarantine_mode="copy",
        )
        client = get_client(settings)
        try:
            client.delete_collection(settings.collection)
        except Exception:
            pass

        step1 = _run_step(corpus_a, settings, "batch-a-initial", "sync", client)
        step2 = _run_step(corpus_a, settings, "batch-a-unchanged-rerun", "sync", client)

        # Modify 1 document
        lib_file = corpus_a / "library-rules-2026.md"
        lib_file.write_text(
            lib_file.read_text(encoding="utf-8") + "\n2.2 Lost library cards incur a PKR 250 replacement fee.\n",
            encoding="utf-8",
        )
        step3 = _run_step(corpus_a, settings, "batch-a-one-changed", "sync", client)

        # Delete 1 document
        lib_file.unlink()
        step4 = _run_step(corpus_a, settings, "batch-a-one-deleted", "sync", client)

        # Merge Batch B (supersedes academic-rules@2024-08 with 2026-11)
        settings_b = replace(settings, corpus_dir=str(batch_b_src))
        step5 = _run_step(batch_b_src, settings_b, "batch-b-merge", "merge", client)

        col = client.get_collection(settings.collection)
        active_before_restore = sorted({m["source"] for m in col.get(where={"superseded": False})["metadatas"]})

        # Rollback/restore academic-rules to 2024-08
        restore_info = restore_document_version("academic-rules", "2024-08", settings_b, client=client)
        active_after_restore = sorted({m["source"] for m in col.get(where={"superseded": False})["metadatas"]})

        evidence = {
            "chroma": f"{settings.chroma_host}:{settings.chroma_port}",
            "collection": settings.collection,
            "step1_initial_batch_a": step1,
            "step2_unchanged_rerun": step2,
            "step3_changed_one_doc": step3,
            "step4_deleted_one_doc": step4,
            "step5_batch_b_merge_and_supersede": {
                **step5,
                "active_sources_filtered": active_before_restore,
                "counts_by_doc_id": count_chunks_by_doc_id(col),
                "counts_by_doc_and_status": count_chunks_by_doc_and_status(col),
            },
            "step6_restore_academic_rules_2024_08": {
                **restore_info,
                "active_sources_filtered": active_after_restore,
            },
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(f"Wrote A2 evidence to {out_path}")
        print(f"Step2 unchanged chunks_embedded={step2['incremental']['chunks_embedded']}")
        print(f"Step3 changed chunks_embedded={step3['incremental']['chunks_embedded']}")
        print(f"Step4 deleted doc_ids={step4['incremental']['deleted_doc_ids']}, counts_by_doc_id={step4['incremental']['counts_by_doc_id']}")
        print(f"Step5 superseded={step5['incremental']['superseded_versions']}, active_has_2024={'academic-rules-2024.md' in active_before_restore}")
        print(f"Step6 restored={restore_info['restored']}, active_has_2024={'academic-rules-2024.md' in active_after_restore}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
