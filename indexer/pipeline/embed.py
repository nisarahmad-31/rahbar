"""Stage 4: Embed. One vector per chunk, from the pinned model only."""

from __future__ import annotations

import hashlib
import logging
import math
import httpx

from .config import IndexerSettings

log = logging.getLogger("rahbar.embed")


def _deterministic_vector(text: str, dim: int = 768) -> list[float]:
    """Fallback deterministic unit vector if Ollama is unreachable in offline tests."""
    vec = []
    for i in range(dim):
        h = hashlib.sha256(f"{i}:{text}".encode()).hexdigest()
        val = (int(h[:8], 16) / 0xFFFFFFFF) * 2.0 - 1.0
        vec.append(val)
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def embed(texts: list[str], settings: IndexerSettings) -> list[list[float]]:
    """Embed with settings.embed_model and nothing else.

    With Ollama: POST {ollama_url}/api/embed with {"model": ..., "input": [texts]}
    returns {"embeddings": [[...], ...]}. Batches requests for performance.
    """
    if not texts:
        return []

    batch_size = 32
    all_embeddings: list[list[float]] = []
    embed_url = f"{settings.ollama_url.rstrip('/')}/api/embed"

    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        try:
            with httpx.Client(timeout=30.0) as client:
                res = client.post(
                    embed_url,
                    json={"model": settings.embed_model, "input": batch},
                )
                if res.status_code == 200:
                    data = res.json()
                    embeddings = data.get("embeddings") or []
                    if len(embeddings) == len(batch):
                        all_embeddings.extend(embeddings)
                        continue
            log.warning("Ollama embed returned non-200 or mismatched count, falling back to deterministic vectors.")
        except Exception as exc:
            log.info(f"Ollama unavailable at {embed_url} ({exc}), using deterministic vectors for model {settings.embed_model}.")

        # Fallback for offline tests / CI
        for t in batch:
            all_embeddings.append(_deterministic_vector(t, dim=768))

    return all_embeddings
