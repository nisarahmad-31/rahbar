"""Ollama provider: ollama-host or ollama-incluster (section 3.5)."""

from __future__ import annotations

import httpx

from ...config import Settings
from ...schemas import Answer, Passage
from .base import SYSTEM_PROMPT, build_evidence_prompt


class OllamaProvider:
    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        """Call Ollama with constrained JSON schema decoding and validate using Pydantic."""
        if not passages:
            return Answer(
                reasoning="No passages available to answer from.",
                answer="The university documents do not contain information to answer this question.",
                citations=[],
                answered_from_context=False,
                confidence="low",
            )

        prompt = build_evidence_prompt(question, passages)
        url = f"{self.settings.ollama_url.rstrip('/')}/api/chat"
        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "format": Answer.model_json_schema(),
            "stream": False,
            "options": {"temperature": self.settings.temperature},
        }

        with httpx.Client(timeout=120.0) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            content = resp.json()["message"]["content"]
            return Answer.model_validate_json(content)
