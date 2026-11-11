"""Unit tests for the real indexing pipeline stages over fixture documents."""

from __future__ import annotations

from pathlib import Path

from pipeline.chunk import chunk
from pipeline.clean import clean
from pipeline.embed import _deterministic_vector
from pipeline.extract import extract
from pipeline.run import discover

FIXTURES = Path(__file__).resolve().parents[2] / "eval" / "fixtures"


def test_extract_clean_and_chunk_preserve_fixture_sections() -> None:
    docs = {p.name: (p, ver, sup) for p, ver, sup in discover(str(FIXTURES))}
    rules_path, ver, sup = docs["academic-rules-2026.md"]
    doc = clean(extract(str(rules_path), ver, sup))
    assert "doc_version:" not in doc.pages[0].text

    pieces = chunk(doc, size=400, overlap=50)
    sections = {c.section for c in pieces}
    assert "1. Attendance" in sections
    assert "2. Academic warning" in sections
    assert "3. Repeating a course" in sections

    fees_path, fver, fsup = docs["fees-2026.md"]
    fees_doc = clean(extract(str(fees_path), fver, fsup))
    fees_pieces = chunk(fees_doc, size=400, overlap=50)
    assert any(c.section == "1. Tuition" and "10,000" in c.text for c in fees_pieces)


def test_deterministic_fallback_embedding_separates_relevant_from_abstain() -> None:
    q_rel = _deterministic_vector("When is a student placed on academic warning?")
    p_rel = _deterministic_vector(
        "2.1 A student whose cumulative grade point average (CGPA) falls below 2.00 is placed on academic warning."
    )
    q_abs = _deterministic_vector("How do I apply for a hostel room?")

    sim_rel = sum(a * b for a, b in zip(q_rel, p_rel))
    sim_abs = sum(a * b for a, b in zip(q_abs, p_rel))
    assert sim_rel > 0.4
    assert sim_abs < 0.15
