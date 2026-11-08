"""Prometheus metrics at /metrics (section 4.8).

Exposes request count, latency, retrieval latency, generation latency,
cache hits/misses, abstention count, fallback count, and tokens.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

router = APIRouter()

REQUESTS = Counter("rahbar_requests_total", "HTTP requests", ["path", "status"])
LATENCY = Histogram(
    "rahbar_request_seconds",
    "HTTP request latency",
    ["path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)

RETRIEVAL_LATENCY = Histogram("rahbar_retrieval_seconds", "Retrieval stage latency")
GENERATION_LATENCY = Histogram("rahbar_generation_seconds", "Generation stage latency")
CACHE_HITS = Counter("rahbar_cache_hits_total", "Semantic cache hits")
CACHE_MISSES = Counter("rahbar_cache_misses_total", "Semantic cache misses")
ABSTENTIONS = Counter("rahbar_abstentions_total", "Honest abstentions when context is absent")
FALLBACKS = Counter("rahbar_fallbacks_total", "Fallbacks triggered by schema or provider errors")
TOKENS = Counter("rahbar_tokens_total", "Tokens in and out per request", ["direction"])


@router.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
