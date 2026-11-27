#!/usr/bin/env python3
"""Module-level embedding evaluation for Assignment 03 (Part B1, paper Table 1).

Measures:
- Cosine similarity across >= 15 paraphrase pairs vs >= 15 unrelated pairs & gap
- Embedding time per chunk (ms/chunk)
- Determinism (embed the exact same text twice and report max abs & L2 diff)
- Full provenance (model, dimensions, backend mode, seed, hardware/OS)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import random
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "indexer"))

from pipeline.chunk import chunk  # noqa: E402
from pipeline.clean import clean  # noqa: E402
from pipeline.config import get_settings  # noqa: E402
from pipeline.embed import embed_with_stats  # noqa: E402
from pipeline.extract import extract  # noqa: E402
from pipeline.run import discover_entries  # noqa: E402


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    if len(v1) != len(v2) or not v1:
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = math.sqrt(sum(a * a for a in v1))
    n2 = math.sqrt(sum(b * b for b in v2))
    return dot / (n1 * n2) if n1 and n2 else 0.0


def run_embedding_eval(
    *,
    pairs_path: Path = REPO_ROOT / "eval" / "embed_pairs.json",
    corpus_dir: Path = REPO_ROOT / "corpus" / "batches" / "batch_a",
    out_path: Path = REPO_ROOT / "eval" / "results" / "module_embedding_eval.json",
    ollama_url: str | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    random.seed(seed)
    base = get_settings()
    settings = replace(base, ollama_url=ollama_url) if ollama_url else base

    pairs_data = json.loads(pairs_path.read_text(encoding="utf-8"))
    para_pairs: list[list[str]] = pairs_data["paraphrase_pairs"]
    unrel_pairs: list[list[str]] = pairs_data["unrelated_pairs"]

    para_texts = [t for pair in para_pairs for t in pair]
    unrel_texts = [t for pair in unrel_pairs for t in pair]

    para_vecs, para_stats = embed_with_stats(para_texts, settings)
    unrel_vecs, unrel_stats = embed_with_stats(unrel_texts, settings)

    para_sims = [
        round(cosine_similarity(para_vecs[2 * i], para_vecs[2 * i + 1]), 6)
        for i in range(len(para_pairs))
    ]
    unrel_sims = [
        round(cosine_similarity(unrel_vecs[2 * i], unrel_vecs[2 * i + 1]), 6)
        for i in range(len(unrel_pairs))
    ]

    mean_para = sum(para_sims) / len(para_sims)
    mean_unrel = sum(unrel_sims) / len(unrel_sims)
    gap = mean_para - mean_unrel

    # Real chunks timing from Batch A
    chunk_texts: list[str] = []
    for e in discover_entries(str(corpus_dir)):
        doc = clean(extract(str(e.path), e.doc_version, e.superseded, doc_id=e.doc_id, title=e.title, date=e.date))
        chunk_texts.extend(c.text for c in chunk(doc, settings.chunk_size, settings.chunk_overlap))

    t0 = time.perf_counter()
    chunk_vecs, chunk_stats = embed_with_stats(chunk_texts, settings)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    ms_per_chunk = elapsed_ms / len(chunk_texts) if chunk_texts else 0.0

    # Determinism check: embed exact same text twice
    sample_text = "A student whose cumulative grade point average falls below 2.00 is placed on Academic Warning."
    vec_a, _ = embed_with_stats([sample_text], settings)
    vec_b, _ = embed_with_stats([sample_text], settings)
    abs_diffs = [abs(a - b) for a, b in zip(vec_a[0], vec_b[0])]
    max_abs_diff = max(abs_diffs) if abs_diffs else 0.0
    l2_diff = math.sqrt(sum(d * d for d in abs_diffs))

    backend_used = "ollama" if (para_stats["ollama_calls"] > 0 and para_stats["fallback_calls"] == 0) else "deterministic_fallback"
    dim = len(para_vecs[0]) if para_vecs else 0

    result = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": settings.embed_model,
        "backend": backend_used,
        "ollama_url": settings.ollama_url,
        "dimensions": dim,
        "seed": seed,
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "hardware_context": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
        "paraphrase_pairs_count": len(para_pairs),
        "unrelated_pairs_count": len(unrel_pairs),
        "paraphrase_mean_cosine": round(mean_para, 6),
        "unrelated_mean_cosine": round(mean_unrel, 6),
        "cosine_similarity_gap": round(gap, 6),
        "paraphrase_similarities": para_sims,
        "unrelated_similarities": unrel_sims,
        "chunks_timed": len(chunk_texts),
        "total_embed_ms": round(elapsed_ms, 3),
        "ms_per_chunk": round(ms_per_chunk, 3),
        "determinism": {
            "sample_text": sample_text,
            "max_abs_diff": max_abs_diff,
            "l2_diff": l2_diff,
            "exact_match": max_abs_diff == 0.0,
        },
        "chunk_embed_stats": chunk_stats,
        "actual_chunk_vectors_count": len(chunk_vecs),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Part B1 module-level embedding evaluation.")
    parser.add_argument("--pairs", type=Path, default=REPO_ROOT / "eval" / "embed_pairs.json")
    parser.add_argument("--corpus-dir", type=Path, default=REPO_ROOT / "corpus" / "batches" / "batch_a")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "eval" / "results" / "module_embedding_eval.json")
    parser.add_argument("--ollama-url", default=None)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    res = run_embedding_eval(
        pairs_path=args.pairs,
        corpus_dir=args.corpus_dir,
        out_path=args.out,
        ollama_url=args.ollama_url,
        seed=args.seed,
    )
    print(f"Model: {res['model']} ({res['backend']}, dim={res['dimensions']})")
    print(f"Paraphrase mean cosine ({res['paraphrase_pairs_count']} pairs): {res['paraphrase_mean_cosine']:.4f}")
    print(f"Unrelated  mean cosine ({res['unrelated_pairs_count']} pairs): {res['unrelated_mean_cosine']:.4f}")
    print(f"Similarity gap: {res['cosine_similarity_gap']:.4f}")
    print(f"Embedding speed: {res['ms_per_chunk']:.3f} ms/chunk ({res['chunks_timed']} chunks)")
    print(f"Determinism max abs diff: {res['determinism']['max_abs_diff']}")
    print(f"Results written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
