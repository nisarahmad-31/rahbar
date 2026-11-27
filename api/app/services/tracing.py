"""Structured per-query RAGOps trace schema, redaction, emission, and lookup (Part D1/D2).

Every `/api/ask` invocation validates and writes one `TraceRecord` JSON line to
stdout (for `kubectl logs`) and, when `TRACE_LOG_PATH` is configured, appends the
same record to persistent storage for offline analysis and trace lookup.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from ..config import Settings
from ..providers.llm.base import SYSTEM_PROMPT
from ..schemas import Citation, Passage

_TRACE_WRITE_LOCK = threading.Lock()

# Dedicated stdout logger emitting raw single-line TraceRecord JSON (propagate=False
# avoids double-wrapping by root JsonFormatter while staying parseable in kubectl logs).
trace_logger = logging.getLogger("rahbar.trace")
if not trace_logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    trace_logger.addHandler(_handler)
    trace_logger.setLevel(logging.INFO)
    trace_logger.propagate = False

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}", re.I), "Bearer [REDACTED]"),
    (re.compile(r"\b(?:sk|gsk|pk|xoxb|ghp|gho)_[A-Za-z0-9_\-]{8,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"), "[REDACTED_TOKEN]"),
    (
        re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?([^\s'\";,]{4,})"),
        r"\1=[REDACTED]",
    ),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
]


def redact_secrets(text: str) -> str:
    """Remove credential tokens and PII emails before writing trace records."""
    cleaned = text
    for pattern, repl in _SECRET_PATTERNS:
        cleaned = pattern.sub(repl, cleaned)
    return cleaned


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TraceChunk(BaseModel):
    """Retrieved passage recorded inside a TraceRecord."""

    chunk_id: str
    doc_id: str
    doc_version: str
    source: str
    section: str
    page: int | None = None
    score: float
    text: str = ""


class TraceStageLatencies(BaseModel):
    """Per-stage durations in milliseconds."""

    total: float = 0.0
    embed: float = 0.0
    cache: float = 0.0
    retrieval: float = 0.0
    guardrail: float = 0.0
    generation: float = 0.0


class TraceRecord(BaseModel):
    """Canonical Part D1/D3/D4 trace contract."""

    record_type: Literal["rag_trace"] = "rag_trace"
    schema_version: str = "1.0"
    trace_id: str
    timestamp: str
    user_query: str
    rewritten_query: str | None = None
    embedding_model: str
    embedding_version: str
    collection_name: str
    collection_version: str
    retrieved_chunks: list[TraceChunk] = Field(default_factory=list)
    filters_applied: dict[str, Any] = Field(default_factory=dict)
    reranked_order: list[str] = Field(default_factory=list)
    system_prompt_sha256: str = ""
    prompt_sha256: str = ""
    prompt: str | None = None
    model: str
    model_parameters: dict[str, Any] = Field(default_factory=dict)
    response: str
    answered_from_context: bool
    confidence: Literal["high", "medium", "low"]
    citations: list[Citation] = Field(default_factory=list)
    guardrail_actions: list[str] = Field(default_factory=list)
    latency_ms_per_stage: TraceStageLatencies = Field(default_factory=TraceStageLatencies)
    cache_hit: bool = False
    status: Literal["answered", "abstained", "cache_hit", "fallback", "rejected"] = "answered"


def normalize_trace_id(req_id: str) -> str:
    clean = (req_id or "").strip()
    return clean if clean and clean != "-" else uuid4().hex


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_trace_chunks(passages: list[Passage], *, max_chars: int = 2500) -> list[TraceChunk]:
    chunks: list[TraceChunk] = []
    for p in passages:
        snippet = p.text if len(p.text) <= max_chars else p.text[:max_chars] + "...[truncated]"
        chunks.append(
            TraceChunk(
                chunk_id=p.chunk_id,
                doc_id=p.doc_id or p.source,
                doc_version=p.doc_version or "unknown",
                source=p.source,
                section=p.section,
                page=p.page,
                score=round(float(p.score), 4),
                text=redact_secrets(snippet),
            )
        )
    return chunks


def emit_trace(record: TraceRecord, settings: Settings) -> TraceRecord:
    """Validate and emit one TraceRecord JSON line to stdout and optional trace file."""
    validated = TraceRecord.model_validate(record)
    line = validated.model_dump_json()

    if getattr(settings, "trace_stdout", True):
        trace_logger.info(line)

    trace_path = (getattr(settings, "trace_file", "") or "").strip()
    if trace_path:
        try:
            path = Path(trace_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with _TRACE_WRITE_LOCK:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:
            logging.getLogger("rahbar.answer").warning(
                "failed to append trace file %s: %s", trace_path, exc
            )
    return validated


def iter_trace_records_from_lines(lines: Iterable[str]) -> list[TraceRecord]:
    """Parse valid TraceRecord JSON lines from a JSONL file or mixed stdout stream."""
    records: list[TraceRecord] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or not stripped.startswith("{"):
            continue
        # Fast filter to ignore unrelated application JSON logs when scanning stdout
        if '"trace_id"' not in stripped:
            continue
        try:
            records.append(TraceRecord.model_validate_json(stripped))
        except Exception:
            continue
    return records


def load_traces(trace_source: Path | Iterable[str]) -> list[TraceRecord]:
    """Load validated TraceRecord items from a file path or line iterable."""
    if isinstance(trace_source, Path):
        if not trace_source.exists():
            return []
        with trace_source.open("r", encoding="utf-8") as fh:
            return iter_trace_records_from_lines(fh)
    return iter_trace_records_from_lines(trace_source)


def find_trace_by_id(trace_id: str, trace_source: Path | Iterable[str]) -> TraceRecord | None:
    """Return the matching TraceRecord for `trace_id`, or None if absent."""
    target = trace_id.strip()
    if not target:
        return None
    for rec in load_traces(trace_source):
        if rec.trace_id == target:
            return rec
    return None


def system_prompt_hash() -> str:
    return sha256_text(SYSTEM_PROMPT)
