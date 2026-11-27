"""Turns a question into an AskResponse (sections 3.2, 3.3, 3.4).

Executes the query pipeline: validation, semantic cache check, embedding,
top-k Chroma retrieval with superseded filtering, honest abstention check,
augmented generation, Pydantic validation with retry and fallback, citation
verification against retrieved chunks, and cache writing.
"""

from __future__ import annotations

import inspect
import logging
import re
import time

from pydantic import ValidationError

from ..config import Settings
from ..logjson import request_id_var
from ..providers.llm.base import LLMProvider, build_evidence_prompt
from ..retrieval.base import Retriever
from ..retrieval.chroma import _deterministic_query_vector
from ..routes import metrics
from ..schemas import Answer, AskResponse, Citation, Passage, RetrievedRef
from .cache import SemanticCache
from .tracing import (
    TraceRecord,
    TraceStageLatencies,
    build_trace_chunks,
    emit_trace,
    normalize_trace_id,
    redact_secrets,
    sha256_text,
    system_prompt_hash,
    utc_timestamp,
)

log = logging.getLogger("rahbar.answer")
_cache_instance: SemanticCache | None = None
_HISTORICAL_RE = re.compile(
    r"\b(2019|2020|2021|2022|2023|older version|previous version|prior version|old rules|superseded|repealed)\b",
    re.I,
)


def get_cache(settings: Settings) -> SemanticCache:
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = SemanticCache(settings)
    return _cache_instance


def _approx_tokens(text: str) -> int:
    return max(1, int(len(text.split()) * 1.3)) if text.strip() else 0


def _wants_superseded(question: str) -> bool:
    """Detect whether the caller explicitly asks about an older or superseded policy version."""
    return bool(_HISTORICAL_RE.search(question))


def _retrieve_passages(
    retriever: Retriever,
    question: str,
    k: int,
    include_superseded: bool,
    query_vector: list[float],
) -> list[Passage]:
    """Call retriever.retrieve cleanly while supporting minimal custom test mocks."""
    params = inspect.signature(retriever.retrieve).parameters
    kwargs: dict[str, object] = {}
    if "include_superseded" in params:
        kwargs["include_superseded"] = include_superseded
    if "query_vector" in params:
        kwargs["query_vector"] = query_vector
    return retriever.retrieve(question, k, **kwargs)  # type: ignore[arg-type]


def _normalize_query(question: str) -> tuple[str, str | None]:
    """Strip edges and collapse excess internal whitespace; return (clean, rewritten_or_none)."""
    clean_q = question.strip()
    collapsed = " ".join(clean_q.split())
    rewritten = collapsed if collapsed != clean_q else None
    return clean_q, rewritten


def _verify_citations(
    citations: list[Citation],
    passages: list[Passage],
    answered_from_context: bool = True,
    dropped_sources: list[str] | None = None,
) -> list[Citation]:
    """Ensure every citation matches a retrieved chunk; drop fabricated citations."""
    retrieved_pairs = {(p.source, p.section) for p in passages}
    retrieved_sources = {p.source for p in passages}

    verified: list[Citation] = []
    for c in citations:
        if (c.source, c.section) in retrieved_pairs or c.source in retrieved_sources:
            verified.append(c)
        else:
            log.warning(f"Fabricated citation dropped: {c.source} / {c.section}")
            if dropped_sources is not None:
                dropped_sources.append(f"fabricated_citation_dropped:{c.source}")

    if not verified and answered_from_context and passages:
        top = passages[0]
        verified.append(Citation(source=top.source, section=top.section, page=top.page))

    return verified


def _to_retrieved_refs(passages: list[Passage]) -> list[RetrievedRef]:
    return [
        RetrievedRef(
            chunk_id=p.chunk_id,
            source=p.source,
            section=p.section,
            page=p.page,
            score=p.score,
        )
        for p in passages
    ]


def _model_params(settings: Settings) -> dict[str, object]:
    return {
        "temperature": settings.temperature,
        "top_k": settings.top_k,
        "relevance_floor": settings.relevance_floor,
        "cache_threshold": settings.cache_threshold,
    }


def _emit_query_trace(
    *,
    trace_id: str,
    settings: Settings,
    provider_name: str,
    user_query: str,
    rewritten_query: str | None,
    passages: list[Passage],
    filters_applied: dict[str, object],
    prompt: str | None,
    response: AskResponse,
    guardrail_actions: list[str],
    latencies: TraceStageLatencies,
    cache_hit: bool,
    status: str,
) -> None:
    redacted_prompt = redact_secrets(prompt) if prompt is not None else None
    record = TraceRecord(
        trace_id=trace_id,
        timestamp=utc_timestamp(),
        user_query=redact_secrets(user_query),
        rewritten_query=redact_secrets(rewritten_query) if rewritten_query is not None else None,
        embedding_model=settings.embed_model,
        embedding_version=settings.embed_model_version,
        collection_name=settings.collection,
        collection_version=settings.collection_version,
        retrieved_chunks=build_trace_chunks(passages),
        filters_applied=filters_applied,
        reranked_order=[p.chunk_id for p in passages],
        system_prompt_sha256=system_prompt_hash(),
        prompt_sha256=sha256_text(redacted_prompt) if redacted_prompt is not None else "",
        prompt=redacted_prompt,
        model=settings.llm_model,
        model_parameters={"provider": provider_name, **_model_params(settings)},
        response=redact_secrets(response.answer),
        answered_from_context=response.answered_from_context,
        confidence=response.confidence,
        citations=response.citations,
        guardrail_actions=guardrail_actions,
        latency_ms_per_stage=latencies,
        cache_hit=cache_hit,
        status=status,  # type: ignore[arg-type]
    )
    emit_trace(record, settings)


def answer_question(
    question: str, retriever: Retriever, provider: LLMProvider, settings: Settings
) -> AskResponse:
    total_start = time.perf_counter()
    raw_req_id = request_id_var.get()
    trace_id = normalize_trace_id(raw_req_id)
    req_id = trace_id if raw_req_id == "-" else raw_req_id
    cache = get_cache(settings)
    guardrail_actions: list[str] = []

    # 1. Validation of question length
    clean_q, rewritten_q = _normalize_query(question)
    effective_q = rewritten_q or clean_q
    if len(clean_q) < 3 or len(clean_q) > 500:
        guardrail_actions.append("length_validation_failed")
        resp = AskResponse(
            request_id=req_id,
            answer="Question must be between 3 and 500 characters.",
            citations=[],
            answered_from_context=False,
            confidence="low",
            retrieved=[],
            provider=provider.name,
        )
        _emit_query_trace(
            trace_id=trace_id,
            settings=settings,
            provider_name=provider.name,
            user_query=clean_q,
            rewritten_query=rewritten_q,
            passages=[],
            filters_applied={"relevance_floor": settings.relevance_floor, "top_k": settings.top_k},
            prompt=None,
            response=resp,
            guardrail_actions=guardrail_actions,
            latencies=TraceStageLatencies(total=round((time.perf_counter() - total_start) * 1000, 2)),
            cache_hit=False,
            status="rejected",
        )
        return resp

    # 2. Compute query embedding once and check semantic cache
    embed_start = time.perf_counter()
    embed_fn = getattr(retriever, "embed_query", None)
    query_vector = embed_fn(effective_q) if callable(embed_fn) else _deterministic_query_vector(effective_q)
    embed_ms = round((time.perf_counter() - embed_start) * 1000, 2)

    cache_start = time.perf_counter()
    cached_response = cache.get(effective_q, query_vector=query_vector)
    cache_ms = round((time.perf_counter() - cache_start) * 1000, 2)
    if cached_response is not None:
        metrics.CACHE_HITS.inc()
        log.info("served from semantic cache", extra={"request_id": req_id})
        resp = cached_response.model_copy(update={"request_id": req_id})
        guardrail_actions.append("semantic_cache_hit")
        _emit_query_trace(
            trace_id=trace_id,
            settings=settings,
            provider_name=provider.name,
            user_query=clean_q,
            rewritten_query=rewritten_q,
            passages=[],
            filters_applied={"relevance_floor": settings.relevance_floor, "top_k": settings.top_k},
            prompt=None,
            response=resp,
            guardrail_actions=guardrail_actions,
            latencies=TraceStageLatencies(
                total=round((time.perf_counter() - total_start) * 1000, 2),
                embed=embed_ms,
                cache=cache_ms,
            ),
            cache_hit=True,
            status="cache_hit",
        )
        return resp
    metrics.CACHE_MISSES.inc()

    # 3. Retrieve top-k chunks from Chroma (excludes superseded unless asked)
    include_old = _wants_superseded(effective_q)
    filters_applied: dict[str, object] = {
        "include_superseded": include_old,
        "where": None if include_old else {"superseded": False},
        "relevance_floor": settings.relevance_floor,
        "top_k": settings.top_k,
    }
    ret_start = time.perf_counter()
    passages = _retrieve_passages(retriever, effective_q, settings.top_k, include_old, query_vector)
    ret_sec = time.perf_counter() - ret_start
    retrieval_ms = round(ret_sec * 1000, 2)
    metrics.RETRIEVAL_LATENCY.observe(ret_sec)

    # 4. Relevance Floor & Honest Abstention (section 3.4)
    guard_start = time.perf_counter()
    if not passages or passages[0].score < settings.relevance_floor:
        guard_ms = round((time.perf_counter() - guard_start) * 1000, 2)
        guardrail_actions.append("relevance_floor_abstain")
        metrics.ABSTENTIONS.inc()
        log.info(
            "abstaining: relevance floor not reached",
            extra={
                "top_score": passages[0].score if passages else 0.0,
                "floor": settings.relevance_floor,
            },
        )
        resp = AskResponse(
            request_id=req_id,
            answer="I am sorry, but the official university documents in the corpus do not cover this question.",
            citations=[],
            answered_from_context=False,
            confidence="low",
            retrieved=_to_retrieved_refs(passages),
            provider=provider.name,
        )
        _emit_query_trace(
            trace_id=trace_id,
            settings=settings,
            provider_name=provider.name,
            user_query=clean_q,
            rewritten_query=rewritten_q,
            passages=passages,
            filters_applied=filters_applied,
            prompt=None,
            response=resp,
            guardrail_actions=guardrail_actions,
            latencies=TraceStageLatencies(
                total=round((time.perf_counter() - total_start) * 1000, 2),
                embed=embed_ms,
                cache=cache_ms,
                retrieval=retrieval_ms,
                guardrail=guard_ms,
            ),
            cache_hit=False,
            status="abstained",
        )
        return resp
    guard_ms = round((time.perf_counter() - guard_start) * 1000, 2)

    # 5. Generation with Schema Validation, Retry once, and Safe Fallback
    rendered_prompt = build_evidence_prompt(effective_q, passages)
    answer_obj: Answer
    used_fallback = False
    gen_start = time.perf_counter()
    try:
        raw_answer = provider.generate(effective_q, passages)
        answer_obj = Answer.model_validate(raw_answer)
    except (ValidationError, Exception) as exc:
        guardrail_actions.append("schema_validation_retry")
        log.warning(f"Generation validation error: {exc}. Retrying once...")
        try:
            retry_raw = provider.generate(effective_q, passages)
            answer_obj = Answer.model_validate(retry_raw)
        except Exception as retry_exc:
            log.error(f"Generation retry failed ({retry_exc}). Returning safe fallback.")
            used_fallback = True
            guardrail_actions.append("schema_validation_fallback")
            metrics.FALLBACKS.inc()
            top_p = passages[0]
            answer_obj = Answer(
                reasoning=f"Validation failure fallback: {retry_exc}",
                answer=f"Based on {top_p.source} ({top_p.section}): {top_p.text[:250]}...",
                citations=[Citation(source=top_p.source, section=top_p.section, page=top_p.page)],
                answered_from_context=True,
                confidence="low",
            )
    gen_sec = time.perf_counter() - gen_start
    generation_ms = round(gen_sec * 1000, 2)
    metrics.GENERATION_LATENCY.observe(gen_sec)

    if not answer_obj.answered_from_context:
        metrics.ABSTENTIONS.inc()
        guardrail_actions.append("model_abstained")

    tokens_in = _approx_tokens(effective_q) + sum(_approx_tokens(p.text) for p in passages)
    tokens_out = _approx_tokens(answer_obj.answer) + _approx_tokens(answer_obj.reasoning)
    metrics.TOKENS.labels(direction="in").inc(tokens_in)
    metrics.TOKENS.labels(direction="out").inc(tokens_out)

    # 6. Citation Verification: assert every citation matches a chunk actually retrieved
    verified_citations = _verify_citations(
        answer_obj.citations,
        passages,
        answered_from_context=answer_obj.answered_from_context,
        dropped_sources=guardrail_actions,
    )

    # 7. Construct Response
    response = AskResponse(
        request_id=req_id,
        answer=answer_obj.answer,
        citations=verified_citations,
        answered_from_context=answer_obj.answered_from_context,
        confidence=answer_obj.confidence,
        retrieved=_to_retrieved_refs(passages),
        provider=provider.name,
    )

    log.info(
        "answered",
        extra={
            "retriever": retriever.name,
            "provider": provider.name,
            "top_k": settings.top_k,
            "include_superseded": include_old,
            "chunk_ids": [p.chunk_id for p in passages],
            "scores": [round(p.score, 4) for p in passages],
            "answered_from_context": response.answered_from_context,
            "fallback": used_fallback,
        },
    )

    # 8. Store in Semantic Cache if safe to cache
    cache.set(effective_q, query_vector=query_vector, response=response, fallback_used=used_fallback)

    status = "fallback" if used_fallback else ("answered" if response.answered_from_context else "abstained")
    _emit_query_trace(
        trace_id=trace_id,
        settings=settings,
        provider_name=provider.name,
        user_query=clean_q,
        rewritten_query=rewritten_q,
        passages=passages,
        filters_applied=filters_applied,
        prompt=rendered_prompt,
        response=response,
        guardrail_actions=guardrail_actions,
        latencies=TraceStageLatencies(
            total=round((time.perf_counter() - total_start) * 1000, 2),
            embed=embed_ms,
            cache=cache_ms,
            retrieval=retrieval_ms,
            guardrail=guard_ms,
            generation=generation_ms,
        ),
        cache_hit=False,
        status=status,
    )

    return response
