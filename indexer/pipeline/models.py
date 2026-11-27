"""The shapes passed between stages. Metadata fields are required (section 3.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Page:
    """Text of one page, as extraction produced it."""

    source: str              # file name, e.g. Academic_Rules_Handbook_2026.pdf
    page: int                # 1-based
    text: str
    ocr: bool = False        # True when this page had no text layer and was OCR'd


@dataclass
class Document:
    source: str
    doc_version: str         # e.g. "2026-06"; from corpus/sources.yaml
    superseded: bool         # True for an older version of a policy
    pages: list[Page] = field(default_factory=list)
    doc_id: str = ""
    title: str = ""
    date: str = ""
    content_hash: str = ""
    extraction_error: str | None = None
    metadata_present: dict[str, bool] = field(default_factory=dict)


@dataclass
class Chunk:
    id: str                  # stable: same document + same position gives the same id
    text: str
    source: str
    section: str             # the heading this chunk sits under, e.g. "5.4 Academic Warning"
    page: int
    doc_version: str
    superseded: bool
    doc_id: str = ""

    def metadata(self) -> dict[str, str | int | bool]:
        """Chroma metadata values must be str, int, float or bool."""
        meta: dict[str, str | int | bool] = {
            "source": self.source,
            "section": self.section,
            "page": self.page,
            "doc_version": self.doc_version,
            "superseded": self.superseded,
        }
        if self.doc_id:
            meta["doc_id"] = self.doc_id
        return meta


@dataclass
class CheckResult:
    """Outcome of a single verification check on one document."""

    status: str              # "pass" | "fail"
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "reason": self.reason}


@dataclass
class DocumentVerification:
    """Full A1 verification record for one document."""

    doc_id: str
    source: str
    title: str
    doc_version: str
    date: str
    page_count: int
    file_sha256: str
    text_sha256: str
    status: str              # "pass" | "fail"
    quarantined: bool
    quarantine_path: str | None
    checks: dict[str, CheckResult]
    superseded_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "source": self.source,
            "title": self.title,
            "doc_version": self.doc_version,
            "date": self.date,
            "page_count": self.page_count,
            "file_sha256": self.file_sha256,
            "text_sha256": self.text_sha256,
            "status": self.status,
            "quarantined": self.quarantined,
            "quarantine_path": self.quarantine_path,
            "superseded_by": self.superseded_by,
            "checks": {k: v.to_dict() for k, v in self.checks.items()},
        }


@dataclass
class VerificationReport:
    """Machine-readable verification report emitted per ingestion run."""

    run_id: str
    timestamp: str
    batch_name: str
    corpus_dir: str
    quarantine_dir: str
    total_documents: int
    passed_count: int
    failed_count: int
    quarantined_count: int
    check_fail_counts: dict[str, int]
    documents: list[DocumentVerification]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "batch_name": self.batch_name,
            "corpus_dir": self.corpus_dir,
            "quarantine_dir": self.quarantine_dir,
            "summary": {
                "total_documents": self.total_documents,
                "passed_count": self.passed_count,
                "failed_count": self.failed_count,
                "quarantined_count": self.quarantined_count,
                "check_fail_counts": self.check_fail_counts,
            },
            "documents": [d.to_dict() for d in self.documents],
        }
