"""Semantic cache service using Redis (sections 3.3 and 4.1).

Stores question embeddings and verified answers. Applies similarity threshold and
strictly enforces never-cache rules (follow-ups, abstentions, validation failures).
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from ..config import Settings
from ..schemas import AskResponse

log = logging.getLogger("rahbar.cache")


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    if len(v1) != len(v2) or not v1:
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a, b in zip(v1, v2)))
    norm2 = math.sqrt(sum(b * b for a, b in zip(v1, v2)))
    return dot / (norm1 * norm2) if norm1 and norm2 else 0.0


class SemanticCache:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._local_cache: list[dict[str, Any]] = []
        self._redis_client: Any = None
        self._init_redis()

    def _init_redis(self) -> None:
        try:
            import redis
            self._redis_client = redis.Redis(
                host=self.settings.redis_host,
                port=self.settings.redis_port,
                socket_timeout=2,
                decode_responses=True,
            )
            self._redis_client.ping()
        except Exception as exc:
            log.debug(f"Redis not available ({exc}), using local memory cache.")
            self._redis_client = None

    def is_followup(self, question: str) -> bool:
        """Detect follow-up questions which must never be looked up or stored in cache."""
        lower = question.lower().strip()
        signals = [
            lower.startswith("why "),
            lower.startswith("what about "),
            lower.startswith("and "),
            "what did you mean" in lower,
            "tell me more" in lower,
            "earlier you said" in lower,
            "previous answer" in lower,
        ]
        return any(signals)

    def should_never_cache(
        self, question: str, response: AskResponse, fallback_used: bool = False
    ) -> bool:
        """Never-cache rules: follow-ups, abstentions, fallbacks, and low-confidence uncited replies."""
        if fallback_used or self.is_followup(question):
            return True
        if not response.answered_from_context:
            return True
        if response.confidence == "low" and not response.citations:
            return True
        return False

    def get(self, question: str, query_vector: list[float]) -> AskResponse | None:
        """Look up answer in cache if similarity >= cache_threshold."""
        if self.is_followup(question):
            return None

        # Check in Redis
        if self._redis_client:
            try:
                keys = self._redis_client.keys("cache:*")
                for key in keys:
                    raw = self._redis_client.get(key)
                    if not raw:
                        continue
                    item = json.loads(raw)
                    cached_vec = item.get("vector")
                    if cached_vec:
                        sim = cosine_similarity(query_vector, cached_vec)
                        if sim >= self.settings.cache_threshold:
                            log.info(f"semantic cache hit (similarity {sim:.3f} >= {self.settings.cache_threshold})")
                            res_data = item["response"]
                            return AskResponse.model_validate(res_data)
            except Exception as exc:
                log.debug(f"Redis get failed: {exc}")

        # Local fallback cache
        for item in self._local_cache:
            cached_vec = item["vector"]
            sim = cosine_similarity(query_vector, cached_vec)
            if sim >= self.settings.cache_threshold:
                log.info(f"semantic local cache hit (similarity {sim:.3f})")
                return AskResponse.model_validate(item["response"])

        return None

    def set(
        self,
        question: str,
        query_vector: list[float],
        response: AskResponse,
        fallback_used: bool = False,
    ) -> None:
        """Store response in semantic cache unless prohibited by never-cache rules."""
        if self.should_never_cache(question, response, fallback_used=fallback_used):
            log.debug("Skipping cache according to never-cache rules.")
            return

        payload = {
            "question": question,
            "vector": query_vector,
            "response": response.model_dump(),
        }

        if self._redis_client:
            try:
                key = f"cache:{abs(hash(question))}"
                self._redis_client.set(key, json.dumps(payload), ex=86400)
                return
            except Exception as exc:
                log.debug(f"Redis set failed: {exc}")

        self._local_cache.append(payload)

    def invalidate(self) -> None:
        """Flush the semantic cache on re-index."""
        self._local_cache.clear()
        if self._redis_client:
            try:
                self._redis_client.flushdb()
                log.info("cleared semantic cache in redis")
            except Exception as exc:
                log.debug(f"Redis clear_cache failed: {exc}")
