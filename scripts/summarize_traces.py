"""Part D3 Trace Summary CLI: summarize JSONL traces over a configurable time window."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "api") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "api"))

from app.services.tracing import TraceRecord, load_traces  # noqa: E402


def _parse_iso(ts: str) -> datetime:
    cleaned = ts.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = (len(ordered) - 1) * (pct / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return round(ordered[lo] * (1.0 - frac) + ordered[hi] * frac, 4)


def filter_traces_by_window(
    records: list[TraceRecord],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    window_minutes: int | None = None,
    anchor_to_latest: bool = False,
) -> tuple[list[TraceRecord], datetime | None, datetime | None]:
    if not records:
        return [], since, until

    effective_until = until
    effective_since = since
    if window_minutes is not None:
        if effective_until is None:
            effective_until = (
                max(_parse_iso(r.timestamp) for r in records)
                if anchor_to_latest
                else datetime.now(timezone.utc)
            )
        effective_since = effective_until - timedelta(minutes=window_minutes)

    filtered: list[TraceRecord] = []
    for rec in records:
        ts = _parse_iso(rec.timestamp)
        if effective_since is not None and ts < effective_since:
            continue
        if effective_until is not None and ts > effective_until:
            continue
        filtered.append(rec)
    return filtered, effective_since, effective_until


def summarize_traces(records: list[TraceRecord]) -> dict[str, Any]:
    total = len(records)
    top1_scores = [
        float(rec.retrieved_chunks[0].score)
        for rec in records
        if rec.retrieved_chunks
    ]
    buckets = {
        "<0.55": sum(1 for s in top1_scores if s < 0.55),
        "0.55-0.65": sum(1 for s in top1_scores if 0.55 <= s < 0.65),
        "0.65-0.75": sum(1 for s in top1_scores if 0.65 <= s < 0.75),
        "0.75-0.85": sum(1 for s in top1_scores if 0.75 <= s < 0.85),
        ">=0.85": sum(1 for s in top1_scores if s >= 0.85),
    }

    abstained_count = sum(
        1 for rec in records if rec.status == "abstained" or not rec.answered_from_context
    )
    cache_hit_count = sum(1 for rec in records if rec.cache_hit)

    injection_traces: list[str] = []
    injection_events = 0
    flagged_chunks: list[str] = []
    for rec in records:
        hits = [a for a in rec.guardrail_actions if "injection" in a.lower()]
        if hits:
            injection_traces.append(rec.trace_id)
            detection_hits = [a for a in hits if "injection_detected:" in a.lower()]
            injection_events += len(detection_hits) if detection_hits else 1
            for a in hits:
                if a.startswith("retrieval_injection_detected:"):
                    flagged_chunks.append(a.split(":", 1)[1])

    stage_names = ("total", "embed", "cache", "retrieval", "guardrail", "generation")
    p95_latency_ms: dict[str, float] = {}
    p50_latency_ms: dict[str, float] = {}
    for stage in stage_names:
        vals = [float(getattr(rec.latency_ms_per_stage, stage, 0.0)) for rec in records]
        p50_latency_ms[stage] = round(_percentile(vals, 50.0), 2)
        p95_latency_ms[stage] = round(_percentile(vals, 95.0), 2)

    return {
        "total_queries": total,
        "status_breakdown": {
            s: sum(1 for r in records if r.status == s)
            for s in ("answered", "abstained", "cache_hit", "fallback", "rejected")
        },
        "top1_similarity_distribution": {
            "count": len(top1_scores),
            "min": round(min(top1_scores), 4) if top1_scores else None,
            "p25": _percentile(top1_scores, 25.0) if top1_scores else None,
            "p50": _percentile(top1_scores, 50.0) if top1_scores else None,
            "p75": _percentile(top1_scores, 75.0) if top1_scores else None,
            "p95": _percentile(top1_scores, 95.0) if top1_scores else None,
            "max": round(max(top1_scores), 4) if top1_scores else None,
            "mean": round(sum(top1_scores) / len(top1_scores), 4) if top1_scores else None,
            "buckets": buckets,
        },
        "abstention_count": abstained_count,
        "abstention_rate": round(abstained_count / total, 4) if total else 0.0,
        "cache_hit_count": cache_hit_count,
        "cache_hit_rate": round(cache_hit_count / total, 4) if total else 0.0,
        "p50_latency_ms_per_stage": p50_latency_ms,
        "p95_latency_ms_per_stage": p95_latency_ms,
        "injection_attempts_flagged": len(injection_traces),
        "injection_events_count": injection_events,
        "injection_trace_ids": injection_traces,
        "injection_flagged_chunks": sorted(set(flagged_chunks)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize Part D3 JSONL trace metrics over a time window.")
    parser.add_argument(
        "--trace-file",
        action="append",
        default=None,
        help="Trace JSONL file path (repeatable; use '-' for stdin). Defaults to TRACE_LOG_PATH or traces/traces.jsonl.",
    )
    parser.add_argument("--since", default=None, help="Start UTC ISO-8601 timestamp (inclusive).")
    parser.add_argument("--until", default=None, help="End UTC ISO-8601 timestamp (inclusive).")
    parser.add_argument("--window-minutes", type=int, default=None, help="Trailing window in minutes.")
    parser.add_argument(
        "--anchor-to-latest",
        action="store_true",
        help="Anchor --window-minutes to latest trace timestamp instead of current UTC time.",
    )
    parser.add_argument("--out", default=None, help="Optional output JSON report path.")
    args = parser.parse_args(argv)

    sources = args.trace_file or [os.environ.get("TRACE_LOG_PATH", str(REPO_ROOT / "traces" / "traces.jsonl"))]
    all_records: list[TraceRecord] = []
    for src in sources:
        if src == "-":
            all_records.extend(load_traces(sys.stdin.read().splitlines()))
        else:
            p = Path(src)
            if p.exists():
                all_records.extend(load_traces(p))

    since_dt = _parse_iso(args.since) if args.since else None
    until_dt = _parse_iso(args.until) if args.until else None
    filtered, win_start, win_end = filter_traces_by_window(
        all_records,
        since=since_dt,
        until=until_dt,
        window_minutes=args.window_minutes,
        anchor_to_latest=args.anchor_to_latest,
    )

    summary = summarize_traces(filtered)
    report: dict[str, Any] = {
        "window": {
            "since": win_start.strftime("%Y-%m-%dT%H:%M:%SZ") if win_start else None,
            "until": win_end.strftime("%Y-%m-%dT%H:%M:%SZ") if win_end else None,
            "raw_records_loaded": len(all_records),
            "records_in_window": len(filtered),
        },
        "summary": summary,
    }

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("=== TRACE WINDOW SUMMARY (Part D3) ===")
    print(f"Queries in window:        {summary['total_queries']} (loaded={len(all_records)})")
    print(f"Abstention rate:          {summary['abstention_rate']} ({summary['abstention_count']}/{summary['total_queries']})")
    print(f"Cache hit rate:           {summary['cache_hit_rate']} ({summary['cache_hit_count']}/{summary['total_queries']})")
    print(f"Injection attempts:       {summary['injection_attempts_flagged']} queries ({summary['injection_events_count']} events)")
    dist = summary["top1_similarity_distribution"]
    print(
        f"Top-1 similarity dist:    n={dist['count']} min={dist['min']} p50={dist['p50']} "
        f"p95={dist['p95']} max={dist['max']} mean={dist['mean']} buckets={dist['buckets']}"
    )
    print(f"p95 latency/stage (ms):   {summary['p95_latency_ms_per_stage']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
