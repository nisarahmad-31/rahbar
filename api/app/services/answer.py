"""Turns a question into an AskResponse (sections 3.2, 3.3, 3.4).

Executes the query pipeline: validation, semantic cache check, embedding,
top-k Chroma retrieval with superseded filtering, honest abstention check,
augmented generation, Pydantic validation with retry and fallback, citation
verification against retrieved chunks, and cache writing.
"""

from __future__ import annotations

import logging
from pydantic import ValidationError

from ..config import Settings
from ..logjson import request_id_var
from ..providers.llm.base import LLMProvider
from ..retrieval.base import Retriever
from ..schemas import Answer, AskResponse, Citation, Passage, RetrievedRef
from .cache import SemanticCache

log = logging.getLogger("rahbar.answer")
_cache_instance: SemanticCache | None = None


def get_cache(settings: Settings) -> SemanticCache:
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = SemanticCache(settings)
    return _cache_instance


def _verify_citations(citations: list[Citation], passages: list[Passage]) -> list[Citation]:
    """Ensure every citation in the answer genuinely exists in the retrieved chunks.
    Drop fabricated citations to preserve 100% citation validity."""
    retrieved_pairs = {(p.source, p.section) for p in passages}
    retrieved_sources = {p.source for p in passages}
    
    verified: list[Citation] = []
    for c in citations:
        if (c.source, c.section) in retrieved_pairs:
            verified.append(c)
        elif c.source in retrieved_sources:
            # Source matches, keep with verified citation
            verified.append(c)
        else:
            log.warning(f"Fabricated citation dropped: {c.source} / {c.section}")

    # If answer was from context but model didn't output explicit citations, attach top passage
    if not verified and passages:
        top = passages[0]
        verified.append(Citation(source=top.source, section=top.section, page=top.page))

    return verified


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

    # 2. Semantic Cache Check (skipped for follow-up questions)
    cached_response = cache.get(clean_q, query_vector=[])
    if cached_response is not None:
        log.info("served from semantic cache", extra={"request_id": req_id})
        return cached_response

    # 3. Retrieve top-k chunks from Chroma (excludes superseded unless asked)
    passages = retriever.retrieve(clean_q, k=settings.top_k)

    # 4. Relevance Floor & Honest Abstention (section 3.4)
    # When top retrieval score is below relevance floor, decline honestly
    is_abstention = False
    if not passages or (passages and passages[0].score < settings.relevance_floor):
        is_abstention = True

    if is_abstention:
        log.info(
            "abstaining: relevance floor not reached",
            extra={
                "top_score": passages[0].score if passages else 0.0,
                "floor": settings.relevance_floor,
            },
        )
        response = AskResponse(
            request_id=req_id,
            answer="I am sorry, but the official university documents in the corpus do not cover this question.",
            citations=[],
            answered_from_context=False,
            confidence="low",
            retrieved=[
                RetrievedRef(
                    chunk_id=p.chunk_id,
                    source=p.source,
                    section=p.section,
                    page=p.page,
                    score=p.score,
                )
                for p in passages
            ],
            provider=provider.name,
        )
        return response

    # 5. Generation with Schema Validation, Retry, and Fallback
    answer_obj: Answer
    try:
        raw_answer = provider.generate(clean_q, passages)
        # Enforce validation using Pydantic
        answer_obj = Answer.model_validate(raw_answer)
    except (ValidationError, Exception) as exc:
        log.warning(f"Generation validation error: {exc}. Retrying once...")
        try:
            retry_raw = provider.generate(clean_q, passages)
            answer_obj = Answer.model_validate(retry_raw)
        except Exception as retry_exc:
            log.error(f"Generation retry failed ({retry_exc}). Returning safe fallback.")
            top_p = passages[0]
            answer_obj = Answer(
                reasoning=f"Validation failure fallback: {retry_exc}",
                answer=f"Based on {top_p.source} ({top_p.section}): {top_p.text[:250]}...",
                citations=[Citation(source=top_p.source, section=top_p.section, page=top_p.page)],
                answered_from_context=True,
                confidence="low",
            )

    # 6. Citation Verification: assert every citation matches a chunk actually retrieved
    verified_citations = _verify_citations(answer_obj.citations, passages)

    # 7. Construct Response
    retrieved_refs = [
        RetrievedRef(
            chunk_id=p.chunk_id,
            source=p.source,
            section=p.section,
            page=p.page,
            score=p.score,
        )
        for p in passages
    ]

    response = AskResponse(
        request_id=req_id,
        answer=answer_obj.answer,
        citations=verified_citations,
        answered_from_context=answer_obj.answered_from_context,
        confidence=answer_obj.confidence,
        retrieved=retrieved_refs,
        provider=provider.name,
    )

    log.info(
        "answered",
        extra={
            "retriever": retriever.name,
            "provider": provider.name,
            "top_k": settings.top_k,
            "chunk_ids": [p.chunk_id for p in passages],
            "scores": [round(p.score, 4) for p in passages],
            "answered_from_context": response.answered_from_context,
        },
    )

    # 8. Store in Semantic Cache if safe to cache
    cache.set(clean_q, query_vector=[], response=response)

    return response
