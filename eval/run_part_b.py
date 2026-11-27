#!/usr/bin/env python3
"""Part B unified 3-level offline evaluation suite (paper 4.2.5, Table 1).

Integrates:
1. Module level (embedding): loads B1 outputs from eval/results/module_embedding_eval.json
2. Component level (retrieval): recall@3, recall@5, MRR, nDCG@5 + noise test (typos / distractors)
3. End-to-end level (answers): faithfulness (grounding + optional LLM judge & human agreement),
   abstention correctness, citation validity, latency p50 and p95
4. Stochasticity: fixed seed double-run comparison (Run 1 vs Run 2)

Writes a timestamped result file + summary table under eval/results/.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLDEN = REPO_ROOT / "eval" / "golden.jsonl"
DEFAULT_MODULE = REPO_ROOT / "eval" / "results" / "module_embedding_eval.json"
DEFAULT_RESULTS_DIR = REPO_ROOT / "eval" / "results"
DEFAULT_HUMAN_LABELS = REPO_ROOT / "eval" / "faithfulness_human_labels.json"


def load_golden_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Golden dataset not found: {path}")
    rows: list[dict[str, Any]] = []
    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        s = line.strip()
        if not s:
            continue
        item = json.loads(s)
        if "id" not in item or "question" not in item:
            raise ValueError(f"Line {idx} in {path} missing 'id' or 'question'")
        must_abstain = bool(item.get("must_abstain", False))
        if not must_abstain and not item.get("relevant"):
            raise ValueError(f"Question {item['id']} is answerable but has empty 'relevant' ground truth")
        rows.append(item)
    if not rows:
        raise ValueError(f"No questions found in {path}")
    return rows


def _matches_relevant(rel: dict[str, Any], passage: dict[str, Any]) -> bool:
    rel_src = str(rel.get("source", "")).lower()
    rel_sec = str(rel.get("section", "")).lower()
    rel_page = rel.get("page")
    p_src = str(passage.get("source", "")).lower()
    p_sec = str(passage.get("section", "")).lower()
    p_page = passage.get("page")

    src_ok = bool(rel_src) and (rel_src in p_src or p_src in rel_src)
    if not src_ok:
        return False
    if rel_sec and not (rel_sec in p_sec or p_sec in rel_sec):
        return False
    if rel_page is not None and p_page is not None and int(rel_page) != int(p_page):
        return False
    return True


def recall_at_k(relevant: list[dict[str, Any]], retrieved: list[dict[str, Any]], k: int) -> float | None:
    if not relevant:
        return None
    top_k = retrieved[:k]
    matched = sum(1 for rel in relevant if any(_matches_relevant(rel, p) for p in top_k))
    return matched / len(relevant)


def reciprocal_rank(relevant: list[dict[str, Any]], retrieved: list[dict[str, Any]], k: int = 5) -> float | None:
    if not relevant:
        return None
    for rank, p in enumerate(retrieved[:k], start=1):
        if any(_matches_relevant(rel, p) for rel in relevant):
            return 1.0 / rank
    return 0.0


def ndcg_at_k(relevant: list[dict[str, Any]], retrieved: list[dict[str, Any]], k: int = 5) -> float | None:
    if not relevant:
        return None
    # Binary relevance gain per unique matched relevant item
    matched_rels: set[int] = set()
    dcg = 0.0
    for rank, p in enumerate(retrieved[:k], start=1):
        hit = False
        for r_idx, rel in enumerate(relevant):
            if r_idx not in matched_rels and _matches_relevant(rel, p):
                matched_rels.add(r_idx)
                hit = True
                break
        if hit:
            dcg += 1.0 / math.log2(rank + 1)
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(r + 1) for r in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


def citation_validity(response: dict[str, Any]) -> float | None:
    citations = response.get("citations") or []
    if not citations:
        return None
    retrieved = response.get("retrieved") or []
    valid = 0
    for c in citations:
        c_src = str(c.get("source", "")).lower()
        c_sec = str(c.get("section", "")).lower()
        for p in retrieved:
            p_src = str(p.get("source", "")).lower()
            p_sec = str(p.get("section", "")).lower()
            if (c_src in p_src or p_src in c_src) and (not c_sec or c_sec in p_sec or p_sec in c_sec):
                valid += 1
                break
    return valid / len(citations)


def lexical_faithfulness(response: dict[str, Any], expected_points: list[str] | None = None) -> float | None:
    """Measure whether an answered response is grounded in retrieved passages (and matches expected facts)."""
    if not response.get("answered_from_context", False):
        return None
    ans = str(response.get("answer", "")).strip()
    retrieved = response.get("retrieved") or []
    if not ans or not retrieved:
        return 0.0
    # When retrieved items don't include text (API RetrievedRef), check citations + expected points
    context_blob = " ".join(
        f"{p.get('source', '')} {p.get('section', '')} {p.get('text', '')}" for p in retrieved
    ).lower()
    ans_lower = ans.lower()
    cit_ok = citation_validity(response)
    cit_score = cit_ok if cit_ok is not None else 0.0

    if expected_points:
        hits = sum(1 for pt in expected_points if pt.lower() in ans_lower)
        fact_score = hits / len(expected_points)
        return round(0.5 * cit_score + 0.5 * fact_score, 4)

    # Fallback: token overlap against context metadata/text
    tokens = [t for t in re.findall(r"[a-z0-9]{4,}", ans_lower)]
    if not tokens:
        return cit_score
    overlap = sum(1 for t in tokens if t in context_blob) / len(tokens)
    return round(max(cit_score * 0.7 + overlap * 0.3, overlap), 4)


def llm_judge_faithfulness(
    question: str,
    answer: str,
    passages_text: str,
    ollama_url: str,
    judge_model: str = "llama3.2:3b",
) -> tuple[bool, str]:
    """Use local Ollama LLM to judge if answer is strictly supported by retrieved context."""
    prompt = (
        f"Context:\n{passages_text}\n\n"
        f"Question: {question}\nAnswer: {answer}\n\n"
        'Is every factual claim in Answer supported by Context? Respond strictly as JSON: {"faithful": true or false, "reason": "short"}'
    )
    url = f"{ollama_url.rstrip('/')}/api/chat"
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                url,
                json={
                    "model": judge_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "format": "json",
                    "stream": False,
                    "options": {"temperature": 0.0},
                },
            )
            resp.raise_for_status()
            raw = resp.json()["message"]["content"]
            parsed = json.loads(raw)
            return bool(parsed.get("faithful", False)), str(parsed.get("reason", ""))
    except Exception as exc:
        return False, f"judge_error: {exc}"


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = (len(ordered) - 1) * (pct / 100.0)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (idx - lo)


def apply_query_typos(question: str, seed: int = 42) -> str:
    """Deterministically inject realistic character typos into content words for noise testing."""
    rng = random.Random(seed + len(question))
    words = question.split()
    out: list[str] = []
    perturbed = 0
    for w in words:
        alpha = re.sub(r"[^A-Za-z]", "", w)
        if len(alpha) >= 6 and perturbed < 2:
            # Swap two adjacent inner characters
            chars = list(w)
            idx = rng.randint(1, len(chars) - 3)
            chars[idx], chars[idx + 1] = chars[idx + 1], chars[idx]
            out.append("".join(chars))
            perturbed += 1
        else:
            out.append(w)
    return " ".join(out)


def ask_api(client: httpx.Client, api_url: str, question: str) -> tuple[dict[str, Any], float]:
    t0 = time.perf_counter()
    resp = client.post(f"{api_url.rstrip('/')}/api/ask", json={"question": question})
    resp.raise_for_status()
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return resp.json(), elapsed_ms


def evaluate_once(
    questions: list[dict[str, Any]],
    api_url: str,
    *,
    host_header: str | None = None,
    noise_typos: bool = False,
    seed: int = 42,
    ollama_judge_url: str | None = None,
    judge_model: str = "llama3.2:3b",
    passage_lookup: dict[str, str] | None = None,
    direct_retriever: Any = None,
) -> dict[str, Any]:
    headers = {"Host": host_header} if host_header else {}
    rows: list[dict[str, Any]] = []
    r3_scores: list[float] = []
    r5_scores: list[float] = []
    mrr_scores: list[float] = []
    ndcg5_scores: list[float] = []
    cit_scores: list[float] = []
    faith_scores: list[float] = []
    judge_scores: list[float] = []
    latencies_ms: list[float] = []
    abstain_ok = 0
    abstain_total = 0

    with httpx.Client(timeout=120.0, headers=headers) as client:
        for q in questions:
            query_text = apply_query_typos(q["question"], seed=seed) if noise_typos else q["question"]
            if direct_retriever is not None:
                t0 = time.perf_counter()
                passages = direct_retriever.retrieve(query_text, direct_retriever.settings.top_k, include_superseded=False)
                latency_ms = (time.perf_counter() - t0) * 1000.0
                retrieved_list = [
                    {
                        "chunk_id": p.chunk_id,
                        "source": p.source,
                        "section": p.section,
                        "page": p.page,
                        "score": p.score,
                    }
                    for p in passages
                ]
                answered = bool(passages and passages[0].score >= direct_retriever.settings.relevance_floor)
                response = {
                    "answered_from_context": answered,
                    "answer": "",
                    "citations": [],
                    "retrieved": retrieved_list,
                }
            else:
                response, latency_ms = ask_api(client, api_url, query_text)
            latencies_ms.append(latency_ms)
            relevant = q.get("relevant") or []
            retrieved = response.get("retrieved") or []
            must_abstain = bool(q.get("must_abstain", False))

            r3 = recall_at_k(relevant, retrieved, 3)
            r5 = recall_at_k(relevant, retrieved, 5)
            mrr = reciprocal_rank(relevant, retrieved, 5)
            ndcg5 = ndcg_at_k(relevant, retrieved, 5)
            cv = citation_validity(response)
            lf = lexical_faithfulness(response, q.get("expected_answer_points"))

            if r3 is not None:
                r3_scores.append(r3)
            if r5 is not None:
                r5_scores.append(r5)
            if mrr is not None:
                mrr_scores.append(mrr)
            if ndcg5 is not None:
                ndcg5_scores.append(ndcg5)
            if cv is not None:
                cit_scores.append(cv)
            if lf is not None:
                faith_scores.append(lf)

            if must_abstain:
                abstain_total += 1
                if not response.get("answered_from_context", False):
                    abstain_ok += 1

            judge_verdict: bool | None = None
            judge_reason = ""
            if ollama_judge_url and response.get("answered_from_context", False):
                ctx_lines = []
                for p in retrieved:
                    cid = str(p.get("chunk_id", ""))
                    txt = (passage_lookup or {}).get(cid) or f"{p.get('source')} {p.get('section')}"
                    ctx_lines.append(f"[{p.get('source')} / {p.get('section')}] {txt}")
                judge_verdict, judge_reason = llm_judge_faithfulness(
                    query_text,
                    str(response.get("answer", "")),
                    "\n".join(ctx_lines),
                    ollama_judge_url,
                    judge_model=judge_model,
                )
                judge_scores.append(1.0 if judge_verdict else 0.0)

            rows.append(
                {
                    "id": q["id"],
                    "query": query_text,
                    "difficulty": q.get("difficulty", "standard"),
                    "must_abstain": must_abstain,
                    "answered_from_context": response.get("answered_from_context"),
                    "answer": response.get("answer"),
                    "citations": response.get("citations"),
                    "retrieved_ids": [p.get("chunk_id") for p in retrieved],
                    "retrieved_sources": [p.get("source") for p in retrieved],
                    "recall@3": r3,
                    "recall@5": r5,
                    "mrr": mrr,
                    "ndcg@5": ndcg5,
                    "citation_validity": cv,
                    "lexical_faithfulness": lf,
                    "judge_faithful": judge_verdict,
                    "judge_reason": judge_reason,
                    "latency_ms": round(latency_ms, 2),
                }
            )

    summary = {
        "questions_total": len(questions),
        "answerable_count": len(r5_scores),
        "unanswerable_count": abstain_total,
        "recall@3": round(sum(r3_scores) / len(r3_scores), 4) if r3_scores else 0.0,
        "recall@5": round(sum(r5_scores) / len(r5_scores), 4) if r5_scores else 0.0,
        "mrr": round(sum(mrr_scores) / len(mrr_scores), 4) if mrr_scores else 0.0,
        "ndcg@5": round(sum(ndcg5_scores) / len(ndcg5_scores), 4) if ndcg5_scores else 0.0,
        "faithfulness_lexical": round(sum(faith_scores) / len(faith_scores), 4) if faith_scores else 0.0,
        "faithfulness_llm_judge": round(sum(judge_scores) / len(judge_scores), 4) if judge_scores else None,
        "abstention_correctness": round(abstain_ok / abstain_total, 4) if abstain_total else 1.0,
        "citation_validity": round(sum(cit_scores) / len(cit_scores), 4) if cit_scores else 1.0,
        "latency_p50_ms": round(percentile(latencies_ms, 50), 2),
        "latency_p95_ms": round(percentile(latencies_ms, 95), 2),
    }
    return {"summary": summary, "rows": rows}


def compute_judge_human_agreement(rows: list[dict[str, Any]], labels_path: Path) -> dict[str, Any] | None:
    if not labels_path.exists():
        return None
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in rows}
    compared = 0
    agreed = 0
    details: list[dict[str, Any]] = []
    for item in labels.get("labels", []):
        qid = item["id"]
        row = by_id.get(qid)
        if not row or row.get("judge_faithful") is None:
            continue
        human = bool(item["human_faithful"])
        judge = bool(row["judge_faithful"])
        compared += 1
        if human == judge:
            agreed += 1
        details.append({"id": qid, "human_faithful": human, "judge_faithful": judge, "agree": human == judge})
    if compared == 0:
        return None
    return {
        "labeled_count": compared,
        "agreed_count": agreed,
        "agreement_rate": round(agreed / compared, 4),
        "details": details,
    }


def format_summary_table(report: dict[str, Any]) -> str:
    mod = report.get("module_embedding") or {}
    c1 = report["run1"]["summary"]
    noise = report["noise_test"]["summary"]
    diff = report["stochasticity_diff"]
    lines = [
        "| Level | Metric | Run 1 | Noise (Typos) | Run 2 - Run 1 |",
        "| --- | --- | ---: | ---: | ---: |",
        f"| Module (Embedding) | Paraphrase / Unrelated / Gap | {mod.get('paraphrase_mean_cosine', '-')}/{mod.get('unrelated_mean_cosine', '-')}/{mod.get('cosine_similarity_gap', '-')} | — | max_diff={mod.get('determinism', {}).get('max_abs_diff', '-')} |",
        f"| Module (Embedding) | Speed (ms/chunk) | {mod.get('ms_per_chunk', '-')} | — | — |",
        f"| Component (Retrieval) | Recall@3 | {c1['recall@3']} | {noise['recall@3']} | {diff['recall@3']:+.4f} |",
        f"| Component (Retrieval) | Recall@5 | {c1['recall@5']} | {noise['recall@5']} | {diff['recall@5']:+.4f} |",
        f"| Component (Retrieval) | MRR | {c1['mrr']} | {noise['mrr']} | {diff['mrr']:+.4f} |",
        f"| Component (Retrieval) | nDCG@5 | {c1['ndcg@5']} | {noise['ndcg@5']} | {diff['ndcg@5']:+.4f} |",
        f"| End-to-End | Faithfulness (lexical) | {c1['faithfulness_lexical']} | {noise['faithfulness_lexical']} | {diff['faithfulness_lexical']:+.4f} |",
        f"| End-to-End | Faithfulness (LLM judge) | {c1['faithfulness_llm_judge']} | — | — |",
        f"| End-to-End | Abstention Correctness | {c1['abstention_correctness']} | {noise['abstention_correctness']} | {diff['abstention_correctness']:+.4f} |",
        f"| End-to-End | Citation Validity | {c1['citation_validity']} | {noise['citation_validity']} | {diff['citation_validity']:+.4f} |",
        f"| End-to-End | Latency p50 / p95 (ms) | {c1['latency_p50_ms']} / {c1['latency_p95_ms']} | {noise['latency_p50_ms']} / {noise['latency_p95_ms']} | {diff['latency_p50_ms']:+.2f} / {diff['latency_p95_ms']:+.2f} |",
    ]
    return "\n".join(lines)


def run_suite(
    *,
    api_url: str,
    golden_path: Path = DEFAULT_GOLDEN,
    module_path: Path = DEFAULT_MODULE,
    results_dir: Path = DEFAULT_RESULTS_DIR,
    human_labels_path: Path = DEFAULT_HUMAN_LABELS,
    host_header: str | None = None,
    seed: int = 42,
    ollama_judge_url: str | None = None,
    judge_model: str = "llama3.2:3b",
    passage_lookup: dict[str, str] | None = None,
    noise_retriever: Any = None,
    label: str = "baseline",
) -> tuple[dict[str, Any], Path]:
    random.seed(seed)
    questions = load_golden_jsonl(golden_path)
    module_data = json.loads(module_path.read_text(encoding="utf-8")) if module_path.exists() else {}

    run1 = evaluate_once(
        questions,
        api_url,
        host_header=host_header,
        noise_typos=False,
        seed=seed,
        ollama_judge_url=ollama_judge_url,
        judge_model=judge_model,
        passage_lookup=passage_lookup,
    )
    run2 = evaluate_once(
        questions,
        api_url,
        host_header=host_header,
        noise_typos=False,
        seed=seed,
        ollama_judge_url=None,
        passage_lookup=passage_lookup,
    )
    noise_res = evaluate_once(
        questions,
        api_url,
        host_header=host_header,
        noise_typos=True,
        seed=seed,
        ollama_judge_url=None,
        passage_lookup=passage_lookup,
        direct_retriever=noise_retriever,
    )

    s1, s2, sn = run1["summary"], run2["summary"], noise_res["summary"]
    stoch_diff = {
        k: round(float(s2[k]) - float(s1[k]), 4)
        for k in ("recall@3", "recall@5", "mrr", "ndcg@5", "faithfulness_lexical", "abstention_correctness", "citation_validity", "latency_p50_ms", "latency_p95_ms")
    }
    noise_delta = {
        k: round(float(sn[k]) - float(s1[k]), 4)
        for k in ("recall@3", "recall@5", "mrr", "ndcg@5")
    }
    agreement = compute_judge_human_agreement(run1["rows"], human_labels_path)

    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "label": label,
        "seed": seed,
        "api_url": api_url,
        "golden_path": str(golden_path),
        "module_embedding": module_data,
        "run1": run1,
        "run2": run2,
        "stochasticity_diff": stoch_diff,
        "noise_test": noise_res,
        "noise_delta": noise_delta,
        "judge_human_agreement": agreement,
    }
    table = format_summary_table(report)
    report["summary_table_markdown"] = table

    results_dir.mkdir(parents=True, exist_ok=True)
    out_file = results_dir / f"part_b_{label}_{ts}.json"
    out_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    (results_dir / f"part_b_{label}_latest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report, out_file


def main() -> int:
    parser = argparse.ArgumentParser(description="Run full 3-level Part B evaluation suite.")
    parser.add_argument("--api", required=True, help="API base URL, e.g. http://127.0.0.1:8000")
    parser.add_argument("--host", help="Optional Host header for Ingress")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--module", type=Path, default=DEFAULT_MODULE)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--human-labels", type=Path, default=DEFAULT_HUMAN_LABELS)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--judge-ollama-url", default=None)
    parser.add_argument("--judge-model", default="llama3.2:3b")
    parser.add_argument("--noise-via-chroma", action="store_true", help="run component-level typo noise test directly against ChromaRetriever")
    parser.add_argument("--chroma-host", default="127.0.0.1")
    parser.add_argument("--chroma-port", type=int, default=18001)
    parser.add_argument("--collection", default="rahbar_b2")
    parser.add_argument("--relevance-floor", type=float, default=0.65)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--label", default="baseline")
    args = parser.parse_args()

    noise_ret = None
    passage_lookup: dict[str, str] | None = None
    if args.noise_via_chroma or args.judge_ollama_url:
        sys.path.insert(0, str(REPO_ROOT / "api"))
        from app.config import Settings  # noqa: E402
        from app.retrieval.chroma import ChromaRetriever  # noqa: E402

        c_settings = Settings(
            retriever="chroma",
            chroma_host=args.chroma_host,
            chroma_port=args.chroma_port,
            collection=args.collection,
            ollama_url=args.judge_ollama_url or "http://172.28.112.1:11434",
            top_k=args.top_k,
            relevance_floor=args.relevance_floor,
        )
        ret_obj = ChromaRetriever(c_settings)
        if args.noise_via_chroma:
            noise_ret = ret_obj
        try:
            col_data = ret_obj._get_client().get_collection(args.collection).get(include=["documents"])
            passage_lookup = dict(zip(col_data.get("ids") or [], col_data.get("documents") or []))
        except Exception:
            passage_lookup = None

    report, out_file = run_suite(
        api_url=args.api,
        golden_path=args.golden,
        module_path=args.module,
        results_dir=args.results_dir,
        human_labels_path=args.human_labels,
        host_header=args.host,
        seed=args.seed,
        ollama_judge_url=args.judge_ollama_url,
        judge_model=args.judge_model,
        passage_lookup=passage_lookup,
        noise_retriever=noise_ret,
        label=args.label,
    )
    print(report["summary_table_markdown"])
    print(f"\nTimestamped Part B report written to: {out_file}")
    if report["judge_human_agreement"]:
        ag = report["judge_human_agreement"]
        print(f"LLM-judge vs human agreement: {ag['agreed_count']}/{ag['labeled_count']} ({ag['agreement_rate']*100:.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
