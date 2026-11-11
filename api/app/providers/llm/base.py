"""The LLM provider interface (section 3.5). Swapping providers is one file."""

from __future__ import annotations

from typing import Protocol

from ...schemas import Answer, Passage

SYSTEM_PROMPT = """You are RAHBAR, an official assistant answering questions strictly grounded on university policies.
Rules:
1. Base your answer strictly on the provided numbered passages.
2. If the passages do not contain the answer, set answered_from_context to false and state that the corpus does not cover the question.
3. Every factual sentence in your answer must cite its source and section as [source / section].
4. Output your entire response as a valid JSON object matching the requested Answer schema. Never output markdown outside the JSON."""


def build_evidence_prompt(question: str, passages: list[Passage]) -> str:
    """Format numbered retrieved passages delimited as untrusted data for grounded generation."""
    evidence_lines = [
        f"[{i}] (Source: {p.source}, Section: {p.section}, Page: {p.page})\n{p.text}"
        for i, p in enumerate(passages, start=1)
    ]
    evidence_text = "\n\n".join(evidence_lines)
    return (
        f"=== EVIDENCE ===\n{evidence_text}\n=== END EVIDENCE ===\n\n"
        f"Question: {question}\n\n"
        f"Respond with a JSON object conforming to the schema."
    )


class LLMProvider(Protocol):
    name: str

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        """Answer the question from the passages, as an `Answer`."""
        ...
