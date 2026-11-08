"""Ollama provider: ollama-host or ollama-incluster (section 3.5)."""

from __future__ import annotations

import json
import logging
import httpx
from pydantic import ValidationError

from ...config import Settings
from ...schemas import Answer, Citation, Passage

log = logging.getLogger("rahbar.llm.ollama")

SYSTEM_PROMPT = """You are RAHBAR, an official assistant answering questions strictly grounded on university policies.
Rules:
1. Base your answer strictly on the provided numbered passages.
2. If the passages do not contain the answer, set answered_from_context to false and state that the corpus does not cover the question.
3. Every factual sentence in your answer must cite its source and section as [source / section].
4. Output your entire response as a valid JSON object matching the requested Answer schema. Never output markdown outside the JSON."""


class OllamaProvider:
    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _build_prompt(self, question: str, passages: list[Passage]) -> str:
        evidence_lines = []
        for i, p in enumerate(passages, start=1):
            evidence_lines.append(f"[{i}] (Source: {p.source}, Section: {p.section}, Page: {p.page})\n{p.text}")
        evidence_text = "\n\n".join(evidence_lines)

        return (
            f"=== EVIDENCE ===\n{evidence_text}\n=== END EVIDENCE ===\n\n"
            f"Question: {question}\n\n"
            f"Respond with a JSON object conforming to the schema."
        )

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        """Call Ollama with constrained decoding format and validate using Pydantic."""
        if not passages:
            return Answer(
                reasoning="No passages available to answer from.",
                answer="The university documents do not contain information to answer this question.",
                citations=[],
                answered_from_context=False,
                confidence="low",
            )

        prompt = self._build_prompt(question, passages)
        url = f"{self.settings.ollama_url.rstrip('/')}/api/chat"
        schema = Answer.model_json_schema()

        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "format": schema,
            "stream": False,
            "options": {
                "temperature": self.settings.temperature,
            },
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                content = resp.json()["message"]["content"]
                return Answer.model_validate_json(content)
        except (httpx.HTTPError, KeyError, ValidationError, json.JSONDecodeError) as exc:
            log.warning(f"Ollama generation failed or invalid JSON: {exc}. Retrying once...")
            # Retry once on failure
            try:
                with httpx.Client(timeout=30.0) as client:
                    resp = client.post(url, json=payload)
                    resp.raise_for_status()
                    content = resp.json()["message"]["content"]
                    return Answer.model_validate_json(content)
            except Exception as retry_exc:
                log.error(f"Ollama retry also failed: {retry_exc}. Returning safe fallback.")
                top = passages[0]
                return Answer(
                    reasoning=f"Provider error fallback: {retry_exc}",
                    answer=f"Information from {top.source}: {top.text[:200]}...",
                    citations=[Citation(source=top.source, section=top.section, page=top.page)],
                    answered_from_context=True,
                    confidence="low",
                )
