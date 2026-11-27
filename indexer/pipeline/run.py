"""The indexer Job's entry point. It runs once and exits (section 4.2).

    python -m pipeline.run --dummy    # week 1: three fake chunks, no model needed
    python -m pipeline.run            # the real pipeline: extract, clean, chunk, embed, store

Exit code 0 means the index was written; anything else fails the Job, and
Kubernetes retries it up to the Job's backoffLimit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from .config import IndexerSettings, get_settings
from .models import Chunk


@dataclass(frozen=True)
class DiscoveredEntry:
    path: Path
    doc_version: str
    superseded: bool
    doc_id: str = ""
    title: str = ""
    date: str = ""

_PAUSE_SECONDS = 2.0
_DRIVE_VIEW = re.compile(r"https?://drive\.google\.com/file/d/([^/?#]+)")


def _normalise_url(url: str) -> str:
    m = _DRIVE_VIEW.match(url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return quote(url, safe=":/?&=%#+,;@!$'()*~")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80] or "document"


def _guess_ext(url: str, content_type: str) -> str:
    ct = content_type.lower()
    if "pdf" in ct:
        return ".pdf"
    if "html" in ct:
        return ".html"
    suffix = Path(urlsplit(url).path).suffix.lower()
    return suffix if suffix in {".pdf", ".html", ".htm", ".txt", ".md"} else ".bin"


def _bootstrap_corpus_from_sources(sources_path: Path, dest_dir: Path) -> None:
    """Respectful one-at-a-time fallback fetch into /tmp/corpus when /corpus is empty."""
    import httpx
    import yaml  # type: ignore[import-untyped]

    data = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    docs = data.get("documents") or []
    dest_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    headers = {"User-Agent": "RAHBAR-coursework-fetcher/1.0 (CS4032 student project; one request at a time)"}

    with httpx.Client(headers=headers, follow_redirects=True, timeout=60.0) as client:
        for i, doc in enumerate(docs):
            url = _normalise_url(str(doc["url"]))
            name = str(doc["name"])
            try:
                resp = client.get(url)
                if resp.status_code == 200:
                    ext = _guess_ext(url, resp.headers.get("content-type", ""))
                    fname = f"{_slug(name)}{ext}"
                    (dest_dir / fname).write_bytes(resp.content)
                    entries.append({
                        "name": name,
                        "file": fname,
                        "doc_version": str(doc.get("doc_version", "unknown")),
                        "superseded": bool(doc.get("superseded", False)),
                    })
                    log("fetched", file=fname, bytes=len(resp.content))
                else:
                    log("fetch failed", name=name, status=resp.status_code)
            except Exception as exc:
                log("fetch error", name=name, error=str(exc))
            if i + 1 < len(docs):
                time.sleep(_PAUSE_SECONDS)

    (dest_dir / "_index.json").write_text(json.dumps(entries, indent=2), encoding="utf-8")


def log(msg: str, **fields: object) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "logger": "rahbar.indexer", "msg": msg, **fields}), flush=True)


def _front_matter(path: Path) -> dict[str, str]:
    """Read `key: value` lines between leading '---' markers (used by eval/fixtures)."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        if key.strip():
            meta[key.strip()] = value.strip()
    return meta


def discover_entries(corpus_dir: str) -> list[DiscoveredEntry]:
    """List structured metadata entries for every document in `corpus_dir`."""
    root = Path(corpus_dir)
    index = root / "_index.json"
    if index.exists():
        entries = json.loads(index.read_text(encoding="utf-8"))
        return [
            DiscoveredEntry(
                path=root / e["file"],
                doc_version=str(e.get("doc_version", "unknown")),
                superseded=bool(e.get("superseded", False)),
                doc_id=str(e.get("doc_id", "")),
                title=str(e.get("title", e.get("name", ""))),
                date=str(e.get("date", "")),
            )
            for e in entries
        ]
    found: list[DiscoveredEntry] = []
    for path in sorted(root.glob("*")):
        if path.suffix.lower() not in {".pdf", ".md", ".html", ".txt"}:
            continue
        if path.name.lower().startswith("readme"):
            continue
        meta = _front_matter(path) if path.suffix.lower() == ".md" else {}
        found.append(
            DiscoveredEntry(
                path=path,
                doc_version=meta.get("doc_version", "unknown"),
                superseded=meta.get("superseded", "false").lower() == "true",
                doc_id=meta.get("doc_id", ""),
                title=meta.get("title", ""),
                date=meta.get("date", ""),
            )
        )
    return found


def discover(corpus_dir: str) -> list[tuple[Path, str, bool]]:
    """List (path, doc_version, superseded) for every document to index."""
    return [(e.path, e.doc_version, e.superseded) for e in discover_entries(corpus_dir)]


def run_pipeline(
    settings: IndexerSettings,
    *,
    batch_name: str = "default",
    verify_only: bool = False,
    incremental_mode: str = "sync",
    full_rebuild: bool = False,
    manifest_path: Path | None = None,
    report_path: Path | None = None,
    update_manifest: bool = True,
    client: Any = None,
) -> int:
    from .chunk import chunk
    from .clean import clean
    from .embed import embed
    from .extract import extract
    from .store import incremental_sync, store
    from .verify import verify_documents

    entries = discover_entries(settings.corpus_dir)
    if not entries:
        sources_file = Path(os.environ.get("CORPUS_SOURCES_FILE", "/app/sources.yaml"))
        if sources_file.exists():
            tmp_dir = Path(os.environ.get("TMP_CORPUS_DIR", "/tmp/corpus"))
            log("corpus_dir empty, bootstrapping politely from sources.yaml", dest=str(tmp_dir))
            _bootstrap_corpus_from_sources(sources_file, tmp_dir)
            entries = discover_entries(str(tmp_dir))
    if not entries:
        raise SystemExit(f"no documents found in {settings.corpus_dir}")

    extracted_docs = [
        (
            entry.path,
            clean(
                extract(
                    str(entry.path),
                    entry.doc_version,
                    entry.superseded,
                    doc_id=entry.doc_id,
                    title=entry.title,
                    date=entry.date,
                )
            ),
        )
        for entry in entries
    ]

    passed_docs, report = verify_documents(
        extracted_docs,
        settings,
        batch_name=batch_name,
        corpus_dir=settings.corpus_dir,
        manifest_path=manifest_path,
        report_path=report_path,
        update_manifest=update_manifest,
    )
    log(
        "verified",
        run_id=report.run_id,
        batch=batch_name,
        total=report.total_documents,
        passed=report.passed_count,
        quarantined=report.quarantined_count,
        fail_counts=report.check_fail_counts,
    )

    if verify_only:
        return len(passed_docs)

    if full_rebuild:
        all_chunks: list[Chunk] = []
        for path, doc in passed_docs:
            pieces = chunk(doc, settings.chunk_size, settings.chunk_overlap)
            log(
                "chunked",
                source=path.name,
                doc_id=doc.doc_id,
                pages=len(doc.pages),
                ocr_pages=sum(1 for p in doc.pages if p.ocr),
                chunks=len(pieces),
            )
            all_chunks.extend(pieces)
        vectors = embed([c.text for c in all_chunks], settings)
        return store(all_chunks, vectors, settings)

    inc_report = incremental_sync(
        passed_docs,
        settings,
        client=client,
        incremental_mode=incremental_mode,
    )
    log("incremental_update", **inc_report)
    return int(inc_report["total_chunks_in_store"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the RAHBAR index, then exit.")
    parser.add_argument("--dummy", action="store_true", help="write three fake chunks")
    parser.add_argument("--corpus-dir", help="override CORPUS_DIR")
    parser.add_argument("--batch-name", default="default", help="label for verification reports")
    parser.add_argument("--verify-only", action="store_true", help="run extraction + verification only (no Chroma write)")
    parser.add_argument("--incremental-mode", choices=["sync", "merge"], default="sync", help="sync deletes removed docs; merge keeps existing active docs")
    parser.add_argument("--full-rebuild", action="store_true", help="wipe and recreate collection instead of incremental update")
    parser.add_argument("--restore-doc", help="logical doc_id to restore from archive")
    parser.add_argument("--restore-version", help="archived version to restore")
    parser.add_argument("--counts", action="store_true", help="print chunk counts grouped by doc_id in Chroma")
    parser.add_argument("--report-out", help="optional path for JSON verification report")
    parser.add_argument("--manifest-path", help="optional path for data lake manifest JSON")
    parser.add_argument("--quarantine-dir", help="override QUARANTINE_DIR")
    parser.add_argument("--quarantine-mode", choices=["copy", "move"], help="copy or move failed files to quarantine")
    args = parser.parse_args()
    settings = get_settings()
    overrides: dict[str, object] = {}
    if args.corpus_dir:
        overrides["corpus_dir"] = args.corpus_dir
    if args.quarantine_dir:
        overrides["quarantine_dir"] = args.quarantine_dir
    if args.quarantine_mode:
        overrides["quarantine_mode"] = args.quarantine_mode
    if overrides:
        from dataclasses import replace
        settings = replace(settings, **overrides)  # type: ignore[arg-type]

    log("indexer starting", dummy=args.dummy, embed_model=settings.embed_model,
        collection=settings.collection, chroma=f"{settings.chroma_host}:{settings.chroma_port}",
        pod=os.environ.get("HOSTNAME", "-"))
    started = time.perf_counter()
    try:
        if args.counts:
            from .store import count_chunks_by_doc_and_status, get_client, get_or_create_collection

            col, _ = get_or_create_collection(get_client(settings), settings)
            print(json.dumps(count_chunks_by_doc_and_status(col), indent=2))
            return 0
        if args.restore_doc and args.restore_version:
            from .store import restore_document_version

            res = restore_document_version(args.restore_doc, args.restore_version, settings)
            log("restored", **res)
            print(json.dumps(res, indent=2))
            return 0
        if args.dummy:
            from .dummy import write_dummy_index
            from .store import get_client

            count = write_dummy_index(settings, get_client(settings))
        else:
            count = run_pipeline(
                settings,
                batch_name=args.batch_name,
                verify_only=args.verify_only,
                incremental_mode=args.incremental_mode,
                full_rebuild=args.full_rebuild,
                manifest_path=Path(args.manifest_path) if args.manifest_path else None,
                report_path=Path(args.report_out) if args.report_out else None,
            )
    except NotImplementedError as exc:
        log("indexer stage not implemented yet", error=str(exc))
        return 2
    log("indexer finished", chunks=count, seconds=round(time.perf_counter() - started, 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
