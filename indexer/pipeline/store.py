"""Stage 5: Store. The only code in the whole system that writes to Chroma.

Supports both full collection replacement (legacy `store()`) and A2 incremental
data-lake + retrieval-source updates:
- Zero re-embeddings on unchanged corpus reruns
- Single-document chunk replacement on changed documents
- Clean orphan-free deletion by doc_id
- Archival + metadata exclusion (`superseded=True`) for old versions
- Version rollback / restore from archive
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .chunk import chunk
from .config import IndexerSettings
from .embed import embed_with_stats
from .models import Chunk, Document

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
    """Invalidate semantic cache when index changes occur (section 3.3)."""
    redis_host = os.environ.get("REDIS_HOST", "redis")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    try:
        import redis
        r = redis.Redis(host=redis_host, port=redis_port, socket_timeout=2)
        r.flushdb()
        log.info("invalidated semantic cache in redis upon re-index")
    except Exception as exc:
        log.debug(f"Redis cache invalidation skipped: {exc}")


def _text_sha256(doc: Document) -> str:
    text = re.sub(r"\s+", " ", "\n".join(p.text for p in doc.pages)).strip().lower()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_index_state(state_path: Path) -> dict[str, Any]:
    if state_path.exists():
        return dict(json.loads(state_path.read_text(encoding="utf-8")))
    return {"embed_model": "", "chunk_size": 0, "chunk_overlap": 0, "documents": {}}


def _save_index_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_or_create_collection(client: Any, settings: IndexerSettings, dimension: int = 768) -> tuple[Any, bool]:
    """Return (collection, reset_occurred). Recreates only if missing or EMBED_MODEL changed."""
    col_meta = {
        "embed_model": settings.embed_model,
        "dimension": dimension,
        "hnsw:space": "cosine",
    }
    try:
        col = client.get_collection(settings.collection)
        existing_meta = getattr(col, "metadata", None) or {}
        stored_model = existing_meta.get("embed_model")
        if stored_model and stored_model != settings.embed_model:
            log.info(f"embed_model changed ({stored_model} -> {settings.embed_model}); recreating collection")
            client.delete_collection(settings.collection)
            return client.create_collection(name=settings.collection, metadata=col_meta), True
        return col, False
    except Exception:
        return client.create_collection(name=settings.collection, metadata=col_meta), True


def count_chunks_by_doc_id(collection: Any) -> dict[str, int]:
    """Return total chunk counts in the vector store grouped by doc_id."""
    data = collection.get(include=["metadatas"])
    metas = data.get("metadatas") or []
    counts: Counter[str] = Counter()
    for m in metas:
        if not isinstance(m, dict):
            continue
        did = str(m.get("doc_id") or m.get("source") or "unknown")
        counts[did] += 1
    return dict(sorted(counts.items()))


def count_chunks_by_doc_and_status(collection: Any) -> dict[str, dict[str, int]]:
    """Return active vs superseded chunk counts grouped by doc_id."""
    data = collection.get(include=["metadatas"])
    metas = data.get("metadatas") or []
    summary: dict[str, dict[str, int]] = {}
    for m in metas:
        if not isinstance(m, dict):
            continue
        did = str(m.get("doc_id") or m.get("source") or "unknown")
        bucket = summary.setdefault(did, {"active": 0, "superseded": 0, "total": 0})
        if m.get("superseded"):
            bucket["superseded"] += 1
        else:
            bucket["active"] += 1
        bucket["total"] += 1
    return dict(sorted(summary.items()))


def _copy_to_data_lake(src_path: Path, doc: Document, lake_dir: Path) -> str:
    dest_dir = lake_dir / "active" / doc.doc_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Remove any older active file in this doc_id folder with a different filename
    for old in dest_dir.iterdir():
        if old.is_file() and old.name != src_path.name:
            old.unlink()
    dest = dest_dir / src_path.name
    if src_path.exists():
        shutil.copy2(str(src_path), str(dest))
    return str(dest)


def _archive_version(
    doc_id: str,
    version: str,
    source_name: str,
    lake_path: str | None,
    archive_dir: Path,
    meta: dict[str, Any],
) -> str:
    safe_ver = re.sub(r"[^a-zA-Z0-9._-]+", "_", version or "unknown")
    ver_dir = archive_dir / doc_id / safe_ver
    ver_dir.mkdir(parents=True, exist_ok=True)
    archived_file = ver_dir / source_name
    if lake_path and Path(lake_path).exists():
        shutil.copy2(lake_path, str(archived_file))
    meta_out = dict(meta)
    meta_out["archived_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta_out["archived_file"] = str(archived_file)
    (ver_dir / "metadata.json").write_text(json.dumps(meta_out, indent=2), encoding="utf-8")
    return str(archived_file)


def _set_chunks_superseded(collection: Any, chunk_ids: list[str], superseded: bool) -> int:
    if not chunk_ids:
        return 0
    existing = collection.get(ids=chunk_ids, include=["metadatas"])
    got_ids = existing.get("ids") or []
    got_metas = existing.get("metadatas") or []
    updated_ids: list[str] = []
    updated_metas: list[dict[str, Any]] = []
    for cid, meta in zip(got_ids, got_metas):
        new_meta = dict(meta or {})
        new_meta["superseded"] = superseded
        updated_ids.append(cid)
        updated_metas.append(new_meta)
    if updated_ids:
        collection.update(ids=updated_ids, metadatas=updated_metas)
    return len(updated_ids)


def _add_chunks_batched(collection: Any, chunks: list[Chunk], vectors: list[list[float]]) -> None:
    if not chunks:
        return
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


def incremental_sync(
    passed_items: list[tuple[Path, Document]],
    settings: IndexerSettings,
    *,
    client: Any = None,
    incremental_mode: str = "sync",
    state_path: Path | None = None,
) -> dict[str, Any]:
    """Apply incremental A2 updates to the data lake and Chroma vector store."""
    client = client or get_client(settings)
    lake_dir = Path(settings.data_lake_dir)
    archive_dir = Path(settings.archive_dir)
    idx_state_path = state_path or (lake_dir / "index_state.json")
    state = _load_index_state(idx_state_path)
    col, reset_occurred = get_or_create_collection(client, settings)

    if reset_occurred or state.get("embed_model") != settings.embed_model or (
        state.get("chunk_size") and (
            state.get("chunk_size") != settings.chunk_size or state.get("chunk_overlap") != settings.chunk_overlap
        )
    ):
        # Model or chunking params changed -> clear tracked active chunk states
        state["documents"] = {}

    state["embed_model"] = settings.embed_model
    state["chunk_size"] = settings.chunk_size
    state["chunk_overlap"] = settings.chunk_overlap

    docs_state: dict[str, dict[str, Any]] = state.setdefault("documents", {})
    archived_versions: dict[str, dict[str, Any]] = state.setdefault("archived", {})

    incoming_by_id: dict[str, tuple[Path, Document]] = {doc.doc_id: (p, doc) for p, doc in passed_items}

    unchanged_docs: list[str] = []
    new_docs: list[str] = []
    changed_docs: list[str] = []
    superseded_docs: list[str] = []
    deleted_docs: list[str] = []

    chunks_to_add: list[Chunk] = []
    ids_to_delete: list[str] = []
    chunks_superseded_count = 0

    # 1. Handle deletions in "sync" mode (documents previously active in state but removed from corpus_dir)
    if incremental_mode == "sync":
        for prev_did in list(docs_state.keys()):
            prev_entry = docs_state[prev_did]
            if prev_did not in incoming_by_id:
                old_ids = list(prev_entry.get("chunk_ids") or [])
                if old_ids:
                    ids_to_delete.extend(old_ids)
                # Also remove active data-lake copy and manifest entry
                active_dir = lake_dir / "active" / prev_did
                if active_dir.exists():
                    shutil.rmtree(active_dir)
                manifest_file = lake_dir / "manifest.json"
                if manifest_file.exists():
                    mdata = json.loads(manifest_file.read_text(encoding="utf-8"))
                    mdocs = mdata.get("documents", {})
                    for k in [src for src, ent in mdocs.items() if ent.get("doc_id") == prev_did and not ent.get("superseded")]:
                        del mdocs[k]
                    manifest_file.write_text(json.dumps(mdata, indent=2), encoding="utf-8")
                del docs_state[prev_did]
                deleted_docs.append(prev_did)

    if ids_to_delete:
        col.delete(ids=ids_to_delete)

    # 2. Process incoming verified documents
    for did, (src_path, doc) in incoming_by_id.items():
        text_hash = _text_sha256(doc)
        file_hash = doc.content_hash
        prev = docs_state.get(did)

        if prev and prev.get("doc_version") == doc.doc_version and prev.get("text_sha256") == text_hash and not prev.get("superseded"):
            unchanged_docs.append(did)
            continue

        if prev and prev.get("doc_version") != doc.doc_version:
            # Older version superseded by newer incoming version: archive it & exclude via metadata
            prev_ver = str(prev.get("doc_version", "unknown"))
            prev_src = str(prev.get("source", ""))
            prev_ids = list(prev.get("chunk_ids") or [])
            archived_path = _archive_version(
                did,
                prev_ver,
                prev_src,
                prev.get("lake_path"),
                archive_dir,
                prev,
            )
            chunks_superseded_count += _set_chunks_superseded(col, prev_ids, True)
            archived_versions[f"{did}@{prev_ver}"] = {
                **prev,
                "superseded": True,
                "superseded_by": doc.doc_version,
                "archived_file": archived_path,
            }
            superseded_docs.append(f"{did}@{prev_ver}")
        elif prev:
            # Same version but modified content -> replace only this doc's chunks
            old_ids = list(prev.get("chunk_ids") or [])
            if old_ids:
                col.delete(ids=old_ids)
                ids_to_delete.extend(old_ids)
            changed_docs.append(did)
        else:
            new_docs.append(did)

        lake_file = _copy_to_data_lake(src_path, doc, lake_dir)
        pieces = chunk(doc, settings.chunk_size, settings.chunk_overlap)
        chunks_to_add.extend(pieces)
        docs_state[did] = {
            "doc_id": did,
            "source": doc.source,
            "title": doc.title,
            "doc_version": doc.doc_version,
            "date": doc.date,
            "file_sha256": file_hash,
            "text_sha256": text_hash,
            "superseded": False,
            "lake_path": lake_file,
            "chunk_ids": [c.id for c in pieces],
        }

    # 3. Embed ONLY new/changed chunks (0 embeddings when unchanged!)
    vectors, embed_stats = embed_with_stats([c.text for c in chunks_to_add], settings)
    _add_chunks_batched(col, chunks_to_add, vectors)

    _save_index_state(idx_state_path, state)

    mutated = bool(chunks_to_add or ids_to_delete or chunks_superseded_count)
    if mutated:
        _invalidate_cache(settings)

    counts_by_doc = count_chunks_by_doc_id(col)
    status_counts = count_chunks_by_doc_and_status(col)

    return {
        "mode": incremental_mode,
        "collection": settings.collection,
        "embed_model": settings.embed_model,
        "documents_unchanged": len(unchanged_docs),
        "documents_new": len(new_docs),
        "documents_changed": len(changed_docs),
        "documents_deleted": len(deleted_docs),
        "documents_superseded": len(superseded_docs),
        "unchanged_doc_ids": unchanged_docs,
        "new_doc_ids": new_docs,
        "changed_doc_ids": changed_docs,
        "deleted_doc_ids": deleted_docs,
        "superseded_versions": superseded_docs,
        "chunks_embedded": embed_stats["chunks_embedded"],
        "embed_api_calls": embed_stats["embed_api_calls"],
        "chunks_added": len(chunks_to_add),
        "chunks_deleted": len(ids_to_delete),
        "chunks_superseded": chunks_superseded_count,
        "total_chunks_in_store": col.count(),
        "counts_by_doc_id": counts_by_doc,
        "counts_by_doc_and_status": status_counts,
    }


def restore_document_version(
    doc_id: str,
    target_version: str,
    settings: IndexerSettings,
    *,
    client: Any = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    """Rollback/restore an archived document version and mark the current active one superseded."""
    client = client or get_client(settings)
    lake_dir = Path(settings.data_lake_dir)
    archive_dir = Path(settings.archive_dir)
    idx_state_path = state_path or (lake_dir / "index_state.json")
    state = _load_index_state(idx_state_path)
    col, _ = get_or_create_collection(client, settings)

    key = f"{doc_id}@{target_version}"
    archived_versions = state.get("archived", {})
    docs_state = state.get("documents", {})
    target_entry = archived_versions.get(key)
    if not target_entry:
        raise ValueError(f"Archived version not found: {key}")

    # Archive and supersede the currently active version of doc_id (if any)
    current = docs_state.get(doc_id)
    demoted_version: str | None = None
    if current and current.get("doc_version") != target_version:
        cur_ver = str(current.get("doc_version", "unknown"))
        cur_src = str(current.get("source", ""))
        demoted_version = cur_ver
        cur_archived = _archive_version(
            doc_id,
            cur_ver,
            cur_src,
            current.get("lake_path"),
            archive_dir,
            current,
        )
        _set_chunks_superseded(col, list(current.get("chunk_ids") or []), True)
        archived_versions[f"{doc_id}@{cur_ver}"] = {
            **current,
            "superseded": True,
            "superseded_by": target_version,
            "archived_file": cur_archived,
        }

    # Restore target archived file back into active data lake and un-supersede its chunks
    active_dir = lake_dir / "active" / doc_id
    active_dir.mkdir(parents=True, exist_ok=True)
    for old in active_dir.iterdir():
        if old.is_file():
            old.unlink()
    restored_src = Path(str(target_entry.get("archived_file", "")))
    dest_file = active_dir / str(target_entry["source"])
    if restored_src.exists():
        shutil.copy2(str(restored_src), str(dest_file))

    restored_chunk_ids = list(target_entry.get("chunk_ids") or [])
    reactivated = _set_chunks_superseded(col, restored_chunk_ids, False)

    restored_record = dict(target_entry)
    restored_record["superseded"] = False
    restored_record["lake_path"] = str(dest_file)
    restored_record.pop("archived_file", None)
    restored_record.pop("superseded_by", None)
    docs_state[doc_id] = restored_record
    del archived_versions[key]

    # Also keep A1 manifest.json aligned if present
    manifest_file = lake_dir / "manifest.json"
    if manifest_file.exists():
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        for src, entry in manifest.get("documents", {}).items():
            if entry.get("doc_id") == doc_id:
                entry["superseded"] = src != restored_record["source"]
        manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    _save_index_state(idx_state_path, state)
    _invalidate_cache(settings)

    return {
        "restored": key,
        "demoted_version": demoted_version,
        "chunks_reactivated": reactivated,
        "counts_by_doc_and_status": count_chunks_by_doc_and_status(col),
    }


def store(chunks: list[Chunk], vectors: list[list[float]], settings: IndexerSettings) -> int:
    """Write every chunk with its vector and metadata, and record the embedding model
    and vector dimension in the collection metadata (section 4.4). Return the count."""
    client = get_client(settings)
    dimension = len(vectors[0]) if vectors else 768

    col_meta = {
        "embed_model": settings.embed_model,
        "dimension": dimension,
        "hnsw:space": "cosine",
    }

    try:
        client.delete_collection(settings.collection)
    except Exception:
        pass

    collection = client.create_collection(
        name=settings.collection,
        metadata=col_meta,
    )

    _add_chunks_batched(collection, chunks, vectors)

    _invalidate_cache(settings)
    log.info(f"stored {len(chunks)} chunks in collection {settings.collection} with model {settings.embed_model}")
    return len(chunks)
