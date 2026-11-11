"""Liveness and readiness probes (sections 4.3 and 4.4).

/healthz: Liveness probe. Checks only local process health. Never touches Chroma, Redis, or LLM.
/readyz: Readiness probe. Verifies Chroma, non-empty collection, Redis, and embedding model pin.
"""

from __future__ import annotations

import logging
from fastapi import APIRouter, Request, Response, status

router = APIRouter()
log = logging.getLogger("rahbar.health")


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Is the process alive? Must never touch Chroma, Redis or the LLM."""
    return {"status": "alive"}


@router.get("/readyz")
def readyz(request: Request, response: Response) -> dict[str, object]:
    """Readiness probe. Returns 200 only if Chroma is reachable, collection is non-empty,
    Redis is reachable, and the collection embedding model matches EMBED_MODEL."""
    settings = request.app.state.settings
    checks: dict[str, str] = {}
    failures: list[str] = []

    # If running with stubs for tests/CI, bypass external cluster checks
    if settings.retriever == "stub":
        return {"status": "ready", "checks": {"retriever": "stub", "provider": "stub"}}

    # 1. Chroma & collection check (reuse client to keep probe latency <20ms)
    try:
        chroma_client = getattr(request.app.state, "_chroma_client", None)
        if chroma_client is None:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            chroma_client = chromadb.HttpClient(
                host=settings.chroma_host,
                port=settings.chroma_port,
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            request.app.state._chroma_client = chroma_client
        chroma_client.heartbeat()
        checks["chroma"] = "reachable"

        # Check collection exists and is non-empty
        collection = chroma_client.get_collection(settings.collection)
        chunk_count = collection.count()
        if chunk_count == 0:
            failures.append(f"collection {settings.collection} is empty (count: 0)")
            checks["collection"] = "empty"
        else:
            checks["collection"] = f"non-empty ({chunk_count} chunks)"

        # 2. Embedding pin guardrail check
        col_metadata = collection.metadata or {}
        stored_model = col_metadata.get("embed_model")
        if stored_model and stored_model != settings.embed_model:
            conflict_msg = (
                f"embedding_model mismatch: collection was built with '{stored_model}' "
                f"but API is configured with '{settings.embed_model}'"
            )
            log.error(conflict_msg)
            failures.append(conflict_msg)
            checks["embedding_model"] = "mismatch"
        else:
            checks["embedding_model"] = "match"

    except Exception as exc:
        request.app.state._chroma_client = None
        failures.append(f"chroma unreachable: {exc}")
        checks["chroma"] = f"unreachable: {exc}"

    # 3. Redis reachability check
    try:
        import redis
        r = redis.Redis(host=settings.redis_host, port=settings.redis_port, socket_timeout=2)
        r.ping()
        checks["redis"] = "reachable"
    except Exception as exc:
        failures.append(f"redis unreachable: {exc}")
        checks["redis"] = f"unreachable: {exc}"

    if failures:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not ready", "failed_dependencies": failures, "checks": checks}

    return {"status": "ready", "checks": checks}
