"""Stage 5: Store. The only code in the whole system that writes to Chroma."""

from __future__ import annotations

import logging
import os
from typing import Any

from .config import IndexerSettings
from .models import Chunk

log = logging.getLogger("rahbar.store")


def get_client(settings: IndexerSettings) -> Any:
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    return chromadb.HttpClient(
        host=settings.chroma_host,
        port=settings.chroma_port,
        settings=ChromaSettings(anonymized_telemetry=False),
    )


def _invalidate_cache(settings: IndexerSettings) -> None:
    """Invalidate semantic cache when re-indexing occurs (section 3.3)."""
    redis_host = os.environ.get("REDIS_HOST", "redis")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    try:
        import redis
        r = redis.Redis(host=redis_host, port=redis_port, socket_timeout=2)
        r.flushdb()
        log.info("invalidated semantic cache in redis upon re-index")
    except Exception as exc:
        log.debug(f"Redis cache invalidation skipped: {exc}")


def store(chunks: list[Chunk], vectors: list[list[float]], settings: IndexerSettings) -> int:
    """Write every chunk with its vector and metadata, and record the embedding model
    and vector dimension in the collection metadata (section 4.4). Return the count."""
    client = get_client(settings)
    dimension = len(vectors[0]) if vectors else 768

    col_meta = {
        "embed_model": settings.embed_model,
        "dimension": dimension,
    }

    try:
        client.delete_collection(settings.collection)
    except Exception:
        pass

    collection = client.create_collection(
        name=settings.collection,
        metadata=col_meta,
    )

    if chunks:
        ids = [c.id for c in chunks]
        documents = [c.text for c in chunks]
        metadatas = [c.metadata() for c in chunks]

        batch_size = 100
        for i in range(0, len(chunks), batch_size):
            collection.add(
                ids=ids[i : i + batch_size],
                documents=documents[i : i + batch_size],
                embeddings=vectors[i : i + batch_size],
                metadatas=metadatas[i : i + batch_size],
            )

    _invalidate_cache(settings)
    log.info(f"stored {len(chunks)} chunks in collection {settings.collection} with model {settings.embed_model}")
    return len(chunks)
