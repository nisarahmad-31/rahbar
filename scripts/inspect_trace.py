"""Part D2 Trace Lookup CLI: reconstruct exact context and prompt for a trace ID."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "api") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "api"))

from app.providers.llm.base import SYSTEM_PROMPT, build_evidence_prompt  # noqa: E402
from app.schemas import Passage  # noqa: E402
from app.services.tracing import TraceRecord, find_trace_by_id  # noqa: E402


def _reconstruct_prompt(record: TraceRecord) -> str:
    if record.prompt is not None:
        return record.prompt
    if not record.retrieved_chunks:
        return "(no prompt rendered — query did not reach generation stage)"
    passages = [
        Passage(
            chunk_id=c.chunk_id,
            text=c.text,
            source=c.source,
            section=c.section,
            page=c.page,
            score=c.score,
            doc_id=c.doc_id,
            doc_version=c.doc_version,
        )
        for c in record.retrieved_chunks
    ]
    return build_evidence_prompt(record.rewritten_query or record.user_query, passages)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect a Part D trace by trace_id.")
    parser.add_argument("trace_id", help="Trace ID / request_id to inspect.")
    parser.add_argument(
        "--trace-file",
        default=os.environ.get("TRACE_LOG_PATH", str(REPO_ROOT / "traces" / "traces.jsonl")),
        help="Path to JSONL trace file (or '-' to read JSONL/kubectl logs from stdin).",
    )
    parser.add_argument("--json", action="store_true", help="Output reconstructed trace as JSON.")
    args = parser.parse_args(argv)

    source: Path | list[str]
    if args.trace_file == "-":
        source = sys.stdin.read().splitlines()
    else:
        path = Path(args.trace_file)
        if not path.exists():
            print(f"ERROR: trace file not found: {path}", file=sys.stderr)
            return 2
        source = path

    record = find_trace_by_id(args.trace_id, source)
    if record is None:
        print(f"ERROR: trace_id '{args.trace_id}' not found.", file=sys.stderr)
        return 1

    reconstructed_prompt = _reconstruct_prompt(record)

    if args.json:
        payload = {
            "trace_id": record.trace_id,
            "timestamp": record.timestamp,
            "status": record.status,
            "user_query": record.user_query,
            "rewritten_query": record.rewritten_query,
            "embedding_model": record.embedding_model,
            "embedding_version": record.embedding_version,
            "collection_name": record.collection_name,
            "collection_version": record.collection_version,
            "filters_applied": record.filters_applied,
            "reranked_order": record.reranked_order,
            "cache_hit": record.cache_hit,
            "guardrail_actions": record.guardrail_actions,
            "retrieved_context": [c.model_dump() for c in record.retrieved_chunks],
            "system_prompt": SYSTEM_PROMPT,
            "prompt": reconstructed_prompt,
            "prompt_sha256": record.prompt_sha256,
            "response": record.response,
            "citations": [c.model_dump() for c in record.citations],
            "latency_ms_per_stage": record.latency_ms_per_stage.model_dump(),
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(f"=== TRACE {record.trace_id} ({record.timestamp}) ===")
    print(f"Status:            {record.status} (cache_hit={record.cache_hit})")
    print(f"User Query:        {record.user_query}")
    print(f"Rewritten Query:   {record.rewritten_query or '-'}")
    print(f"Embedding Pin:     {record.embedding_model} ({record.embedding_version})")
    print(f"Collection:        {record.collection_name} ({record.collection_version})")
    print(f"Model / Params:    {record.model} {json.dumps(record.model_parameters)}")
    print(f"Filters Applied:   {json.dumps(record.filters_applied)}")
    print(f"Reranked Order:    {record.reranked_order}")
    print(f"Guardrail Actions: {record.guardrail_actions}")
    print(f"Latency (ms):      {record.latency_ms_per_stage.model_dump()}")
    print("\n--- RETRIEVED CONTEXT ---")
    if not record.retrieved_chunks:
        print("(none)")
    for idx, chunk in enumerate(record.retrieved_chunks, start=1):
        print(
            f"[{idx}] chunk_id={chunk.chunk_id} | doc_id={chunk.doc_id} | "
            f"version={chunk.doc_version} | source={chunk.source} | "
            f"section={chunk.section} | page={chunk.page} | score={chunk.score}"
        )
        print(chunk.text)
        print("-" * 60)

    print("\n--- SYSTEM PROMPT ---")
    print(f"(sha256={record.system_prompt_sha256})")
    print(SYSTEM_PROMPT)
    print("\n--- EXACT USER PROMPT ---")
    print(f"(sha256={record.prompt_sha256})")
    print(reconstructed_prompt)
    print("\n--- RESPONSE & CITATIONS ---")
    print(record.response)
    print(f"Citations: {[c.model_dump() for c in record.citations]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
