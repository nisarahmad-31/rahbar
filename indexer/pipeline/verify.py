"""Stage 1.5: Verify documents after extraction/cleaning and before chunking.

Checks four RAGOps data-verification gates (paper 4.2.2, Assignment 03 A1):
1. quality: empty, garbled, or failed extraction
2. completeness: required metadata (source, title, date/version, page_count) & non-truncated text
3. recency: newer version of the same logical document detected
4. uniqueness: exact duplicates (SHA-256) and near-duplicates (embedding cosine similarity)

Failed documents are quarantined and excluded from chunking/embedding/indexing.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from .config import IndexerSettings
from .embed import embed
from .models import CheckResult, Document, DocumentVerification, VerificationReport

_TRUNCATION_MARKERS = (
    "[truncated]",
    "...truncated",
    "[incomplete]",
    "content cut off",
)


def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
    if len(v1) != len(v2) or not v1:
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = math.sqrt(sum(a * a for a in v1))
    n2 = math.sqrt(sum(b * b for b in v2))
    return dot / (n1 * n2) if n1 and n2 else 0.0


def _version_key(version: str, date_str: str = "") -> tuple[int, ...]:
    """Parse version/date strings like '2026-11', '2026', 'v2.1' into comparable tuples."""
    candidate = version.strip() if version and version.strip().lower() not in {"", "unknown"} else date_str.strip()
    nums = [int(n) for n in re.findall(r"\d+", candidate)]
    return tuple(nums) if nums else (0,)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _text_sha256(text: str) -> str:
    return hashlib.sha256(_normalize_text(text).encode("utf-8")).hexdigest()


def _check_quality(doc: Document) -> CheckResult:
    if doc.extraction_error:
        return CheckResult("fail", f"failed extraction: {doc.extraction_error}")
    full_text = "\n".join(p.text for p in doc.pages).strip()
    if not doc.pages or len(full_text) < 40:
        return CheckResult("fail", f"empty or insufficient extracted text ({len(full_text)} chars)")

    total = len(full_text)
    replacement_chars = full_text.count("\ufffd")
    control_chars = sum(1 for ch in full_text if ord(ch) < 32 and ch not in "\n\r\t")
    if (replacement_chars + control_chars) / total > 0.02:
        return CheckResult("fail", "garbled text: excessive replacement or control characters")

    alnum_ws = sum(1 for ch in full_text if ch.isalnum() or ch.isspace() or ch in ".,;:!?()-/'\"|%$#@&*+=")
    if alnum_ws / total < 0.70:
        return CheckResult("fail", f"garbled text: low readable character ratio ({alnum_ws / total:.2f})")

    words = re.findall(r"[A-Za-z]{2,}", full_text)
    if len(words) < 5:
        return CheckResult("fail", "garbled text: too few valid words extracted")

    return CheckResult("pass", f"clean extraction ({len(doc.pages)} pages, {len(full_text)} chars)")


def _check_completeness(doc: Document) -> CheckResult:
    missing: list[str] = []
    if not doc.source.strip():
        missing.append("source")
    if not doc.title.strip():
        missing.append("title")
    has_version = bool(doc.doc_version.strip()) and doc.doc_version.strip().lower() != "unknown"
    has_date = bool(doc.date.strip()) and doc.date.strip().lower() != "unknown"
    if not (has_version or has_date):
        missing.append("date_or_version")
    if len(doc.pages) <= 0:
        missing.append("page_count")

    if missing:
        return CheckResult("fail", f"missing required metadata: {', '.join(missing)}")

    full_text = "\n".join(p.text for p in doc.pages).strip()
    lower = full_text.lower()
    for marker in _TRUNCATION_MARKERS:
        if marker in lower:
            return CheckResult("fail", f"truncated text: found marker '{marker}'")

    if full_text.endswith("..."):
        return CheckResult("fail", "truncated text: ends with ellipsis '...'")

    # Detect abrupt mid-sentence cutoffs on prose endings (ignore markdown table rows/headings)
    last_line = full_text.splitlines()[-1].strip() if full_text else ""
    if (
        len(full_text) >= 40
        and last_line
        and not last_line.startswith(("#", "|", "-", "*"))
        and last_line[-1] not in ".!?:;)\"'"
    ):
        return CheckResult("fail", f"truncated text: abrupt ending without terminal punctuation ('{last_line[-30:]}')")

    return CheckResult("pass", f"metadata complete (title='{doc.title}', version='{doc.doc_version or doc.date}', pages={len(doc.pages)})")


def _quarantine_file(src_path: Path, quarantine_run_dir: Path, mode: str) -> str:
    quarantine_run_dir.mkdir(parents=True, exist_ok=True)
    dest = quarantine_run_dir / src_path.name
    if mode == "move" and src_path.exists():
        shutil.move(str(src_path), str(dest))
    elif src_path.exists():
        shutil.copy2(str(src_path), str(dest))
    return str(dest)


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    if manifest_path.exists():
        return dict(json.loads(manifest_path.read_text(encoding="utf-8")))
    return {"documents": {}}


def save_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def verify_documents(
    items: list[tuple[Path, Document]],
    settings: IndexerSettings,
    batch_name: str = "default",
    corpus_dir: str = "",
    manifest_path: Path | None = None,
    report_path: Path | None = None,
    update_manifest: bool = True,
) -> tuple[list[tuple[Path, Document]], VerificationReport]:
    """Verify every extracted/cleaned document before chunking.

    Returns (passed_items, VerificationReport).
    """
    run_id = uuid.uuid4().hex[:8]
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    quarantine_root = Path(settings.quarantine_dir)
    quarantine_run_dir = quarantine_root / f"{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}_{run_id}"
    lake_manifest_path = manifest_path or (Path(settings.data_lake_dir) / "manifest.json")
    manifest = load_manifest(lake_manifest_path)
    existing_docs: dict[str, dict[str, Any]] = manifest.setdefault("documents", {})

    # Determine highest known version per logical doc_id across existing lake + incoming valid-version items
    latest_by_doc_id: dict[str, tuple[tuple[int, ...], str, str]] = {}
    for entry in existing_docs.values():
        did = str(entry.get("doc_id", ""))
        if not did or entry.get("superseded"):
            continue
        vk = _version_key(str(entry.get("doc_version", "")), str(entry.get("date", "")))
        cur = latest_by_doc_id.get(did)
        if cur is None or vk > cur[0]:
            latest_by_doc_id[did] = (vk, str(entry.get("doc_version", "")), str(entry.get("source", "")))

    for _path, doc in items:
        if doc.extraction_error or doc.superseded:
            continue
        vk = _version_key(doc.doc_version, doc.date)
        if vk == (0,):
            continue
        cur = latest_by_doc_id.get(doc.doc_id)
        if cur is None or vk > cur[0]:
            latest_by_doc_id[doc.doc_id] = (vk, doc.doc_version or doc.date, doc.source)

    # Known accepted hashes & vectors (seeded from active existing lake documents)
    accepted_file_hashes: dict[str, tuple[str, str]] = {}
    accepted_text_hashes: dict[str, tuple[str, str]] = {}
    accepted_vectors: list[tuple[str, str, list[float]]] = []  # (source, doc_id, vec)
    cached_embeddings: dict[tuple[str, str, str], list[float]] = {}
    for entry in existing_docs.values():
        if entry.get("superseded"):
            continue
        src = str(entry.get("source", ""))
        did = str(entry.get("doc_id", ""))
        thash = str(entry.get("text_sha256", ""))
        if entry.get("file_sha256"):
            accepted_file_hashes[str(entry["file_sha256"])] = (src, did)
        if thash:
            accepted_text_hashes[thash] = (src, did)
        if entry.get("embedding"):
            vec_cached = list(entry["embedding"])
            accepted_vectors.append((src, did, vec_cached))
            if thash:
                cached_embeddings[(did, src, thash)] = vec_cached

    # Pre-compute embeddings only for documents without a cached unchanged embedding
    doc_texts: list[str] = []
    doc_text_indices: list[int] = []
    computed_vectors: dict[int, list[float]] = {}
    for idx, (_path, doc) in enumerate(items):
        full_text = "\n".join(p.text for p in doc.pages).strip()
        if not doc.extraction_error and len(full_text) >= 40:
            thash = _text_sha256(full_text)
            cached = cached_embeddings.get((doc.doc_id, doc.source, thash))
            if cached:
                computed_vectors[idx] = cached
            else:
                doc_texts.append(full_text[:2500])
                doc_text_indices.append(idx)

    if doc_texts:
        vecs = embed(doc_texts, settings)
        for idx, vec in zip(doc_text_indices, vecs):
            computed_vectors[idx] = vec

    passed_items: list[tuple[Path, Document]] = []
    verifications: list[DocumentVerification] = []
    fail_counts = {"quality": 0, "completeness": 0, "recency": 0, "uniqueness": 0}

    for idx, (path, doc) in enumerate(items):
        full_text = "\n".join(p.text for p in doc.pages).strip()
        file_hash = doc.content_hash or (hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "")
        text_hash = _text_sha256(full_text) if full_text else ""

        q_res = _check_quality(doc)
        c_res = _check_completeness(doc)

        # Recency check
        superseded_by: str | None = None
        vk = _version_key(doc.doc_version, doc.date)
        latest = latest_by_doc_id.get(doc.doc_id)
        if doc.superseded:
            newer_hint = f" ({latest[2]} v{latest[1]})" if latest and latest[2] != doc.source else ""
            superseded_by = latest[2] if latest and latest[2] != doc.source else None
            r_res = CheckResult("fail", f"marked superseded; newer policy exists{newer_hint}")
        elif latest is not None and vk < latest[0]:
            superseded_by = latest[2]
            r_res = CheckResult(
                "fail",
                f"newer version {latest[1]} ({latest[2]}) detected for doc_id '{doc.doc_id}'",
            )
        else:
            r_res = CheckResult("pass", f"latest version for doc_id '{doc.doc_id}' ({doc.doc_version or doc.date})")

        # Uniqueness check (exact hash + embedding cosine similarity)
        doc_vec = computed_vectors.get(idx)
        f_match = accepted_file_hashes.get(file_hash)
        t_match = accepted_text_hashes.get(text_hash)
        is_exact_file_dup = f_match is not None and (f_match[0] != doc.source or f_match[1] != doc.doc_id)
        is_exact_text_dup = t_match is not None and (t_match[0] != doc.source or t_match[1] != doc.doc_id)
        if q_res.status == "fail" or not text_hash:
            u_res = CheckResult("fail", "skipped uniqueness due to failed extraction/quality")
        elif is_exact_file_dup and f_match is not None:
            u_res = CheckResult("fail", f"exact file duplicate of '{f_match[0]}' (sha256={file_hash[:12]})")
        elif is_exact_text_dup and t_match is not None:
            u_res = CheckResult("fail", f"exact text duplicate of '{t_match[0]}' (sha256={text_hash[:12]})")
        else:
            max_sim = 0.0
            max_src = ""
            if doc_vec is not None:
                for ref_src, ref_did, ref_vec in accepted_vectors:
                    # Updates to the same logical doc_id are evaluated by recency, not near-dup
                    if ref_did == doc.doc_id:
                        continue
                    sim = _cosine_similarity(doc_vec, ref_vec)
                    if sim > max_sim:
                        max_sim = sim
                        max_src = ref_src
            if max_sim >= settings.near_dup_threshold:
                u_res = CheckResult(
                    "fail",
                    f"near-duplicate of '{max_src}' (cosine={max_sim:.4f} >= {settings.near_dup_threshold:.2f})",
                )
            else:
                u_res = CheckResult("pass", f"unique document (max cross-doc cosine={max_sim:.4f})")

        checks = {
            "quality": q_res,
            "completeness": c_res,
            "recency": r_res,
            "uniqueness": u_res,
        }
        overall_pass = all(ch.status == "pass" for ch in checks.values())
        for cname, cres in checks.items():
            if cres.status == "fail":
                fail_counts[cname] += 1

        q_path: str | None = None
        if overall_pass:
            passed_items.append((path, doc))
            if file_hash:
                accepted_file_hashes[file_hash] = (doc.source, doc.doc_id)
            if text_hash:
                accepted_text_hashes[text_hash] = (doc.source, doc.doc_id)
            if doc_vec is not None:
                accepted_vectors.append((doc.source, doc.doc_id, doc_vec))
            if update_manifest:
                # Mark any older version of same doc_id in manifest as superseded
                for prev_src, prev_entry in existing_docs.items():
                    if prev_entry.get("doc_id") == doc.doc_id and prev_src != doc.source:
                        prev_entry["superseded"] = True
                        prev_entry["superseded_by"] = doc.source
                existing_docs[doc.source] = {
                    "doc_id": doc.doc_id,
                    "source": doc.source,
                    "title": doc.title,
                    "doc_version": doc.doc_version,
                    "date": doc.date,
                    "page_count": len(doc.pages),
                    "file_sha256": file_hash,
                    "text_sha256": text_hash,
                    "superseded": False,
                    "embedding": doc_vec or [],
                }
        else:
            q_path = _quarantine_file(path, quarantine_run_dir, settings.quarantine_mode)

        verifications.append(
            DocumentVerification(
                doc_id=doc.doc_id,
                source=doc.source,
                title=doc.title,
                doc_version=doc.doc_version,
                date=doc.date,
                page_count=len(doc.pages),
                file_sha256=file_hash,
                text_sha256=text_hash,
                status="pass" if overall_pass else "fail",
                quarantined=not overall_pass,
                quarantine_path=q_path,
                checks=checks,
                superseded_by=superseded_by,
            )
        )

    if update_manifest:
        save_manifest(lake_manifest_path, manifest)

    report = VerificationReport(
        run_id=run_id,
        timestamp=timestamp,
        batch_name=batch_name,
        corpus_dir=corpus_dir or settings.corpus_dir,
        quarantine_dir=str(quarantine_run_dir),
        total_documents=len(items),
        passed_count=len(passed_items),
        failed_count=len(items) - len(passed_items),
        quarantined_count=len(items) - len(passed_items),
        check_fail_counts=fail_counts,
        documents=verifications,
    )

    out_file = report_path or (
        Path(settings.verification_report_dir)
        / f"verification_{batch_name}_{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}_{run_id}.json"
    )
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    return passed_items, report
