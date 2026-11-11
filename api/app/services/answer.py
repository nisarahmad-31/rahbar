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
from ..providers.llm.base import LLMProvider
from ..retrieval.base import Retriever
from ..retrieval.chroma import _deterministic_query_vector
from ..routes import metrics
from ..schemas import Answer, AskResponse, Citation, Passage, RetrievedRef
from .cache import SemanticCache

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


def _verify_citations(
    citations: list[Citation], passages: list[Passage], answered_from_context: bool = True
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


def answer_question(
    question: str, retriever: Retriever, provider: LLMProvider, settings: Settings
) -> AskResponse:
    req_id = request_id_var.get()
    cache = get_cache(settings)

    # 1. Validation of question length
    clean_q = question.strip()
    if len(clean_q) < 3 or len(clean_q) > 500:
        return AskResponse(
            request_id=req_id,
            answer="Question must be between 3 and 500 characters.",
            citations=[],
            answered_from_context=False,
            confidence="low",
            retrieved=[],
            provider=provider.name,
        )

    # 2. Compute query embedding once and check semantic cache
    embed_fn = getattr(retriever, "embed_query", None)
    query_vector = embed_fn(clean_q) if callable(embed_fn) else _deterministic_query_vector(clean_q)

    cached_response = cache.get(clean_q, query_vector=query_vector)
    if cached_response is not None:
        metrics.CACHE_HITS.inc()
        log.info("served from semantic cache", extra={"request_id": req_id})
        return cached_response.model_copy(update={"request_id": req_id})
    metrics.CACHE_MISSES.inc()

    # 3. Retrieve top-k chunks from Chroma (excludes superseded unless asked)
    include_old = _wants_superseded(clean_q)
    ret_start = time.perf_counter()
    passages = _retrieve_passages(retriever, clean_q, settings.top_k, include_old, query_vector)
    metrics.RETRIEVAL_LATENCY.observe(time.perf_counter() - ret_start)

    # 4. Relevance Floor & Honest Abstention (section 3.4)
    if not passages or passages[0].score < settings.relevance_floor:
        metrics.ABSTENTIONS.inc()
        log.info(
            "abstaining: relevance floor not reached",
            extra={
                "top_score": passages[0].score if passages else 0.0,
                "floor": settings.relevance_floor,
            },
        )
        return AskResponse(
            request_id=req_id,
            answer="I am sorry, but the official university documents in the corpus do not cover this question.",
            citations=[],
            answered_from_context=False,
            confidence="low",
            retrieved=_to_retrieved_refs(passages),
            provider=provider.name,
        )

    # 5. Generation with Schema Validation, Retry once, and Safe Fallback
    answer_obj: Answer
    used_fallback = False
    gen_start = time.perf_counter()
    try:
        raw_answer = provider.generate(clean_q, passages)
        answer_obj = Answer.model_validate(raw_answer)
    except (ValidationError, Exception) as exc:
        log.warning(f"Generation validation error: {exc}. Retrying once...")
        try:
            retry_raw = provider.generate(clean_q, passages)
            answer_obj = Answer.model_validate(retry_raw)
        except Exception as retry_exc:
            log.error(f"Generation retry failed ({retry_exc}). Returning safe fallback.")
            used_fallback = True
            metrics.FALLBACKS.inc()
            top_p = passages[0]
            answer_obj = Answer(
                reasoning=f"Validation failure fallback: {retry_exc}",
                answer=f"Based on {top_p.source} ({top_p.section}): {top_p.text[:250]}...",
                citations=[Citation(source=top_p.source, section=top_p.section, page=top_p.page)],
                answered_from_context=True,
                confidence="low",
            )
    metrics.GENERATION_LATENCY.observe(time.perf_counter() - gen_start)

    if not answer_obj.answered_from_context:
        metrics.ABSTENTIONS.inc()

    tokens_in = _approx_tokens(clean_q) + sum(_approx_tokens(p.text) for p in passages)
    tokens_out = _approx_tokens(answer_obj.answer) + _approx_tokens(answer_obj.reasoning)
    metrics.TOKENS.labels(direction="in").inc(tokens_in)
    metrics.TOKENS.labels(direction="out").inc(tokens_out)

    # 6. Citation Verification: assert every citation matches a chunk actually retrieved
    verified_citations = _verify_citations(
        answer_obj.citations, passages, answered_from_context=answer_obj.answered_from_context
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
    cache.set(clean_q, query_vector=query_vector, response=response, fallback_used=used_fallback)

    return response
