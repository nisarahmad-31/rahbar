"""Part E2/E4 Query Coverage: measure how well the golden set covers live queries.

For each live query, finds the nearest golden question by cosine similarity,
reports the fraction above a configured threshold, groups uncovered queries by
topic, and exits non-zero when coverage is below the minimum.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                rows.append(json.loads(stripped))
    return rows


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _embed_texts(texts: list[str], ollama_url: str, model: str) -> list[list[float]]:
    import httpx

    vectors: list[list[float]] = []
    batch_size = 32
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        resp = httpx.post(
            f"{ollama_url.rstrip('/')}/api/embed",
            json={"model": model, "input": batch},
            timeout=30.0,
        )
        resp.raise_for_status()
        embs = resp.json().get("embeddings", [])
        vectors.extend(embs)
    return vectors


def compute_coverage(
    live_queries: list[dict],
    golden_questions: list[dict],
    *,
    ollama_url: str,
    embed_model: str,
    threshold: float,
) -> dict:
    golden_texts = [g["question"] for g in golden_questions]
    live_texts = [lq["query"] for lq in live_queries]

    golden_vecs = _embed_texts(golden_texts, ollama_url, embed_model)
    live_vecs = _embed_texts(live_texts, ollama_url, embed_model)

    results: list[dict] = []
    covered_count = 0
    uncovered: list[dict] = []

    for lq, lv in zip(live_queries, live_vecs):
        best_sim = -1.0
        best_gid = ""
        best_gq = ""
        for gq, gv in zip(golden_questions, golden_vecs):
            sim = _cosine(lv, gv)
            if sim > best_sim:
                best_sim = sim
                best_gid = gq["id"]
                best_gq = gq["question"]

        is_covered = best_sim >= threshold
        if is_covered:
            covered_count += 1
        else:
            uncovered.append({
                "id": lq["id"],
                "query": lq["query"],
                "domain": lq.get("domain", "unknown"),
                "topic": lq.get("topic", "unknown"),
                "nearest_golden": best_gid,
                "nearest_golden_question": best_gq,
                "similarity": round(best_sim, 4),
            })
        results.append({
            "id": lq["id"],
            "query": lq["query"],
            "domain": lq.get("domain", "unknown"),
            "topic": lq.get("topic", "unknown"),
            "nearest_golden_id": best_gid,
            "nearest_golden_question": best_gq,
            "similarity": round(best_sim, 4),
            "covered": is_covered,
        })

    # Group uncovered by topic
    topic_groups: dict[str, list[str]] = defaultdict(list)
    for u in uncovered:
        topic_groups[u["topic"]].append(u["query"])

    coverage_rate = covered_count / len(live_queries) if live_queries else 0.0
    return {
        "total_live_queries": len(live_queries),
        "total_golden_questions": len(golden_questions),
        "threshold": threshold,
        "covered_count": covered_count,
        "uncovered_count": len(uncovered),
        "coverage_rate": round(coverage_rate, 4),
        "per_query": results,
        "uncovered_topic_groups": dict(topic_groups),
        "uncovered_details": uncovered,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Part E2/E4 query coverage checker.")
    parser.add_argument(
        "--live-queries",
        default=str(REPO_ROOT / "eval" / "live_queries.jsonl"),
        help="JSONL file of live queries.",
    )
    parser.add_argument(
        "--golden",
        default=str(REPO_ROOT / "eval" / "golden.jsonl"),
        help="JSONL file of golden questions.",
    )
    parser.add_argument(
        "--ollama-url",
        default=os.environ.get("OLLAMA_URL", "http://172.28.112.1:11434"),
    )
    parser.add_argument(
        "--embed-model",
        default=os.environ.get("EMBED_MODEL", "nomic-embed-text"),
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=float(os.environ.get("COVERAGE_THRESHOLD", "0.80")),
        help="Cosine similarity threshold for a live query to be 'covered'. Default 0.80.",
    )
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=float(os.environ.get("MIN_COVERAGE_RATE", "0.85")),
        help="Minimum coverage rate; exits non-zero if below. Default 0.85.",
    )
    parser.add_argument("--out", default=None, help="Optional JSON output path.")
    parser.add_argument("--label", default="coverage", help="Label for the report.")
    args = parser.parse_args(argv)

    live_queries = _load_jsonl(Path(args.live_queries))
    golden_questions = _load_jsonl(Path(args.golden))

    report = compute_coverage(
        live_queries,
        golden_questions,
        ollama_url=args.ollama_url,
        embed_model=args.embed_model,
        threshold=args.threshold,
    )
    report["label"] = args.label
    report["min_coverage_rate"] = args.min_coverage

    out_path = args.out
    if not out_path:
        out_path = str(REPO_ROOT / "eval" / "results" / f"coverage_{args.label}_latest.json")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"=== COVERAGE ({args.label}) ===")
    print(f"Live queries:    {report['total_live_queries']}")
    print(f"Golden questions: {report['total_golden_questions']}")
    print(f"Threshold:       {report['threshold']}")
    print(f"Covered:         {report['covered_count']}/{report['total_live_queries']} = {report['coverage_rate']}")
    print(f"Min coverage:    {args.min_coverage}")

    if report["uncovered_topic_groups"]:
        print("\nUncovered topic groups:")
        for topic, queries in sorted(report["uncovered_topic_groups"].items()):
            print(f"  [{topic}] ({len(queries)} queries)")
            for q in queries:
                print(f"    - {q}")

    if report["coverage_rate"] < args.min_coverage:
        print(f"\nFAIL: coverage {report['coverage_rate']} < min {args.min_coverage}")
        return 1
    print(f"\nPASS: coverage {report['coverage_rate']} >= min {args.min_coverage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
