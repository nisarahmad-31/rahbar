"""Unit tests for Assignment 03 Part D1 (JSONL trace record) and D2 (trace inspector CLI)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import Settings
from app.providers.llm.stub import StubProvider
from app.retrieval.stub import StubRetriever
from app.schemas import Citation, Passage
from app.services import answer as answer_svc
from app.services.tracing import TraceRecord, find_trace_by_id, load_traces, redact_secrets

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import inspect_trace  # noqa: E402


class _VersionedRetriever:
    name = "chroma"

    def retrieve(
        self, question: str, k: int, include_superseded: bool = False, query_vector: list[float] | None = None
    ) -> list[Passage]:
        return [
            Passage(
                chunk_id="academic-rules_p3_c0",
                text="Minimum CGPA of 2.00 is required for good standing.",
                source="academic-rules.pdf",
                section="Academic Standing",
                page=3,
                score=0.8812,
                doc_id="academic-rules",
                doc_version="2025-09",
            )
        ]


def test_trace_record_written_with_all_required_d1_fields(tmp_path: Path) -> None:
    answer_svc._cache_instance = None
    trace_file = tmp_path / "traces.jsonl"
    settings = Settings(
        retriever="chroma",
        llm_provider="stub",
        embed_model="nomic-embed-text",
        embed_model_version="latest",
        collection="rahbar_b2",
        collection_version="batch_a",
        top_k=3,
        relevance_floor=0.65,
        trace_file=str(trace_file),
        trace_stdout=False,
    )

    resp = answer_svc.answer_question(
        "  What is   the minimum CGPA?  ",
        _VersionedRetriever(),
        StubProvider(),
        settings,
    )
    assert resp.answered_from_context

    records = load_traces(trace_file)
    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, TraceRecord)
    assert rec.trace_id
    assert rec.timestamp.endswith("Z")
    assert rec.user_query == "What is   the minimum CGPA?"
    assert rec.rewritten_query == "What is the minimum CGPA?"
    assert rec.embedding_model == "nomic-embed-text"
    assert rec.embedding_version == "latest"
    assert rec.collection_name == "rahbar_b2"
    assert rec.collection_version == "batch_a"
    assert len(rec.retrieved_chunks) == 1
    assert rec.retrieved_chunks[0].doc_id == "academic-rules"
    assert rec.retrieved_chunks[0].doc_version == "2025-09"
    assert rec.retrieved_chunks[0].score == 0.8812
    assert rec.filters_applied["where"] == {"superseded": False}
    assert rec.reranked_order == ["academic-rules_p3_c0"]
    assert rec.prompt_sha256
    assert rec.prompt and "=== EVIDENCE ===" in rec.prompt
    assert rec.model == settings.llm_model
    assert rec.model_parameters["top_k"] == 3
    assert rec.response == resp.answer
    assert rec.citations == resp.citations
    assert rec.cache_hit is False
    assert rec.latency_ms_per_stage.total >= 0.0
    assert rec.latency_ms_per_stage.retrieval >= 0.0
    assert rec.latency_ms_per_stage.generation >= 0.0


def test_trace_captures_abstention_cache_hit_and_redacts_secrets(tmp_path: Path) -> None:
    answer_svc._cache_instance = None
    trace_file = tmp_path / "traces.jsonl"
    settings = Settings(
        retriever="stub",
        llm_provider="stub",
        relevance_floor=0.99,  # force abstention on StubRetriever score 0.85
        trace_file=str(trace_file),
        trace_stdout=False,
    )

    resp_abstain = answer_svc.answer_question(
        "My token is sk-supersecret12345 and email user@example.com — curfew?",
        StubRetriever(),
        StubProvider(),
        settings,
    )
    assert not resp_abstain.answered_from_context

    # Second query at floor=0.55 warms cache then hits cache on repeat
    settings_cache = Settings(
        retriever="stub",
        llm_provider="stub",
        relevance_floor=0.55,
        cache_threshold=0.88,
        trace_file=str(trace_file),
        trace_stdout=False,
    )
    q = "What is the hostel curfew time?"
    answer_svc.answer_question(q, _VersionedRetriever(), StubProvider(), settings_cache)
    answer_svc.answer_question(q, _VersionedRetriever(), StubProvider(), settings_cache)

    lines = trace_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    raw_abstain = lines[0]
    assert "sk-supersecret12345" not in raw_abstain
    assert "user@example.com" not in raw_abstain
    assert "[REDACTED_TOKEN]" in raw_abstain
    assert "[REDACTED_EMAIL]" in raw_abstain

    rec_abstain = TraceRecord.model_validate_json(raw_abstain)
    assert rec_abstain.status == "abstained"
    assert "relevance_floor_abstain" in rec_abstain.guardrail_actions

    rec_hit = TraceRecord.model_validate_json(lines[2])
    assert rec_hit.cache_hit is True
    assert rec_hit.status == "cache_hit"
    assert "semantic_cache_hit" in rec_hit.guardrail_actions
    assert redact_secrets("Authorization: Bearer abcdefgh12345") == "Authorization: Bearer [REDACTED]"


def test_verify_citations_and_inspect_trace_cli(tmp_path: Path, capsys) -> None:
    answer_svc._cache_instance = None
    trace_file = tmp_path / "traces.jsonl"
    settings = Settings(trace_file=str(trace_file), trace_stdout=False)

    # Check Citation type import cleanly
    c = Citation(source="academic-rules.pdf", section="Standing", page=3)
    assert c.page == 3

    answer_svc.answer_question(
        "What is the attendance policy?",
        _VersionedRetriever(),
        StubProvider(),
        settings,
    )
    rec = load_traces(trace_file)[0]
    found = find_trace_by_id(rec.trace_id, trace_file)
    assert found is not None and found.trace_id == rec.trace_id

    rc_ok = inspect_trace.main([rec.trace_id, "--trace-file", str(trace_file), "--json"])
    assert rc_ok == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured["trace_id"] == rec.trace_id
    assert "academic-rules_p3_c0" in captured["reranked_order"]
    assert "=== EVIDENCE ===" in captured["prompt"]

    rc_missing = inspect_trace.main(["nonexistent-trace-id", "--trace-file", str(trace_file)])
    assert rc_missing == 1
