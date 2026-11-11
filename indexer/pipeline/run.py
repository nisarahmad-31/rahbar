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
from pathlib import Path
from urllib.parse import quote, urlsplit

from .config import IndexerSettings, get_settings
from .models import Chunk

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
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta


def discover(corpus_dir: str) -> list[tuple[Path, str, bool]]:
    """List (path, doc_version, superseded) for every document to index.

    corpus/fetch.py writes _index.json next to the files it downloads; use it when
    present. Otherwise read Markdown front matter (the CI fixtures work this way).
    """
    root = Path(corpus_dir)
    index = root / "_index.json"
    if index.exists():
        entries = json.loads(index.read_text(encoding="utf-8"))
        return [(root / e["file"], str(e.get("doc_version", "unknown")),
                 bool(e.get("superseded", False))) for e in entries]
    found = []
    for path in sorted(root.glob("*")):
        if path.suffix.lower() not in {".pdf", ".md", ".html", ".txt"}:
            continue
        if path.name.lower().startswith("readme"):
            continue
        meta = _front_matter(path) if path.suffix.lower() == ".md" else {}
        found.append((path, meta.get("doc_version", "unknown"),
                      meta.get("superseded", "false").lower() == "true"))
    return found


def run_pipeline(settings: IndexerSettings) -> int:
    from .chunk import chunk
    from .clean import clean
    from .embed import embed
    from .extract import extract
    from .store import store

    documents = discover(settings.corpus_dir)
    if not documents:
        sources_file = Path(os.environ.get("CORPUS_SOURCES_FILE", "/app/sources.yaml"))
        if sources_file.exists():
            tmp_dir = Path(os.environ.get("TMP_CORPUS_DIR", "/tmp/corpus"))
            log("corpus_dir empty, bootstrapping politely from sources.yaml", dest=str(tmp_dir))
            _bootstrap_corpus_from_sources(sources_file, tmp_dir)
            documents = discover(str(tmp_dir))
    if not documents:
        raise SystemExit(f"no documents found in {settings.corpus_dir}")
    all_chunks: list[Chunk] = []
    for path, doc_version, superseded in documents:
        doc = clean(extract(str(path), doc_version, superseded))
        pieces = chunk(doc, settings.chunk_size, settings.chunk_overlap)
        log("chunked", source=path.name, chunks=len(pieces))
        all_chunks.extend(pieces)
    vectors = embed([c.text for c in all_chunks], settings)
    return store(all_chunks, vectors, settings)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the RAHBAR index, then exit.")
    parser.add_argument("--dummy", action="store_true", help="write three fake chunks")
    args = parser.parse_args()
    settings = get_settings()
    log("indexer starting", dummy=args.dummy, embed_model=settings.embed_model,
        collection=settings.collection, chroma=f"{settings.chroma_host}:{settings.chroma_port}",
        pod=os.environ.get("HOSTNAME", "-"))
    started = time.perf_counter()
    try:
        if args.dummy:
            from .dummy import write_dummy_index
            from .store import get_client

            count = write_dummy_index(settings, get_client(settings))
        else:
            count = run_pipeline(settings)
    except NotImplementedError as exc:
        log("indexer stage not implemented yet", error=str(exc))
        return 2
    log("indexer finished", chunks=count, seconds=round(time.perf_counter() - started, 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
