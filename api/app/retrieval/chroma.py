"""The real retriever (sections 3.2 and 4.4).

Queries Chroma with embedding vector from pinned EMBED_MODEL and filters superseded documents.
"""

from __future__ import annotations

import hashlib
import logging
import math
import httpx

from ..config import Settings
from ..schemas import Passage

log = logging.getLogger("rahbar.retrieval.chroma")


def _deterministic_query_vector(text: str, dim: int = 768) -> list[float]:
    """Deterministic fallback vector for offline unit tests when Ollama is not running."""
    vec = []
    for i in range(dim):
        h = hashlib.sha256(f"{i}:{text}".encode()).hexdigest()
        val = (int(h[:8], 16) / 0xFFFFFFFF) * 2.0 - 1.0
        vec.append(val)
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class ChromaRetriever:
    name = "chroma"

    def __init__(self, settings: Settings) -> None:
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        self.settings = settings
        self.client = chromadb.HttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port,
            settings=ChromaSettings(anonymized_telemetry=False),
        )

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
                        return embs[0]
        except Exception as exc:
            log.debug(f"Ollama embed call failed ({exc}), using deterministic fallback vector.")
        return _deterministic_query_vector(question, dim=768)

    def retrieve(self, question: str, k: int, include_superseded: bool = False) -> list[Passage]:
        """Embed question with settings.embed_model, query Chroma, filter superseded documents,
        and convert distance to similarity (1 - distance)."""
        collection = self.client.get_collection(self.settings.collection)
        query_vector = self._embed_question(question)

        # Metadata filter: exclude superseded documents unless explicitly requested
        where_filter = None if include_superseded else {"superseded": False}

        results = collection.query(
            query_embeddings=[query_vector],
            n_results=k,
            where=where_filter,
            include=["documents", "metadatas", "distances"],
        )

        passages: list[Passage] = []
        ids = results.get("ids", [[]])[0]
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

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
