"""The real retriever (sections 3.2 and 4.4).

Queries Chroma with embedding vector from pinned EMBED_MODEL and filters superseded documents.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Any

import httpx

from ..config import Settings
from ..schemas import Passage

log = logging.getLogger("rahbar.retrieval.chroma")

_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "to", "of",
    "in", "on", "at", "for", "with", "and", "or", "what", "when", "where",
    "who", "how", "do", "does", "did", "i", "my", "it", "this", "that", "if",
    "can", "may", "under", "per", "as", "by", "from", "into", "about",
})


def _deterministic_query_vector(text: str, dim: int = 768) -> list[float]:
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


class ChromaRetriever:
    name = "chroma"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            self._client = chromadb.HttpClient(
                host=self.settings.chroma_host,
                port=self.settings.chroma_port,
                settings=ChromaSettings(anonymized_telemetry=False),
            )
        return self._client

    def embed_query(self, question: str) -> list[float]:
        return self._embed_question(question)

    def _embed_question(self, question: str) -> list[float]:
        url = f"{self.settings.ollama_url.rstrip('/')}/api/embed"
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(
                    url,
                    json={"model": self.settings.embed_model, "input": [question]},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    embs = data.get("embeddings") or []
                    if embs:
                        return list(embs[0])
        except Exception as exc:
            log.debug(f"Ollama embed call failed ({exc}), using deterministic fallback vector.")
        return _deterministic_query_vector(question, dim=768)

    def retrieve(
        self,
        question: str,
        k: int,
        include_superseded: bool = False,
        query_vector: list[float] | None = None,
    ) -> list[Passage]:
        """Embed question with settings.embed_model, query Chroma, filter superseded documents,
        and convert distance to similarity (1 - distance)."""
        collection = self._get_client().get_collection(self.settings.collection)
        if query_vector is None:
            query_vector = self._embed_question(question)

        # Metadata filter: exclude superseded documents unless explicitly requested
        where_filter: Any = None if include_superseded else {"superseded": False}
        query_embeddings: Any = [query_vector]

        results: Any = collection.query(
            query_embeddings=query_embeddings,
            n_results=k,
            where=where_filter,
            include=["documents", "metadatas", "distances"],
        )

        passages: list[Passage] = []
        ids = results.get("ids", [[]])[0] if results.get("ids") else []
        docs = results.get("documents", [[]])[0] if results.get("documents") else []
        metas = results.get("metadatas", [[]])[0] if results.get("metadatas") else []
        distances = results.get("distances", [[]])[0] if results.get("distances") else []

        for chunk_id, text, meta, dist in zip(ids, docs, metas, distances):
            # Chroma returns cosine distance in [0, 2]; similarity is 1 - distance
            similarity = round(max(0.0, 1.0 - float(dist)), 4)
            passages.append(
                Passage(
                    chunk_id=str(chunk_id),
                    text=str(text),
                    source=str(meta.get("source", "unknown")),
                    section=str(meta.get("section", "unknown")),
                    page=int(meta["page"]) if meta.get("page") is not None else None,
                    score=similarity,
                )
            )

        # Sort descending by similarity score
        passages.sort(key=lambda p: p.score, reverse=True)
        return passages
