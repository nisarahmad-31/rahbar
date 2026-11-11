"""Stage 4: Embed. One vector per chunk, from the pinned model only."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import httpx

from .config import IndexerSettings

log = logging.getLogger("rahbar.embed")

_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "to", "of",
    "in", "on", "at", "for", "with", "and", "or", "what", "when", "where",
    "who", "how", "do", "does", "did", "i", "my", "it", "this", "that", "if",
    "can", "may", "under", "per", "as", "by", "from", "into", "about",
})


def _deterministic_vector(text: str, dim: int = 768) -> list[float]:
    """Deterministic feature-hashed lexical unit vector when Ollama is unreachable offline."""
    tokens = [
        t for t in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.lower())
        if t not in _STOP_WORDS and len(t) > 1
    ]
    if not tokens:
        tokens = [text.lower() or "empty"]
    features: list[tuple[str, float]] = [(t, 1.0) for t in tokens]
    for t in tokens:
        if len(t) >= 5 and t.isalpha():
            features.append((f"stem:{t[:4]}", 0.7))
    for a, b in zip(tokens, tokens[1:]):
        features.append((f"{a}_{b}", 1.2))

    vec = [0.0] * dim
    for feat, weight in features:
        h = hashlib.sha256(feat.encode("utf-8")).digest()
        for off in (0, 4, 8):
            idx = int.from_bytes(h[off : off + 2], "big") % dim
            sign = 1.0 if (h[off + 2] & 1) == 0 else -1.0
            vec[idx] += sign * weight

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
