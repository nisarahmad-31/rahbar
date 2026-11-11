"""Hosted free-tier provider (Groq, Google AI Studio, OpenRouter).

The key arrives as LLM_API_KEY from a Kubernetes Secret.
"""

from __future__ import annotations

import httpx

from ...config import Settings
from ...schemas import Answer, Passage
from .base import SYSTEM_PROMPT, build_evidence_prompt


class HostedProvider:
    name = "hosted"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def generate(self, question: str, passages: list[Passage]) -> Answer:
        """Call hosted OpenAI-compatible chat completions endpoint and validate with Pydantic."""
        if not passages:
            return Answer(
                reasoning="No passages available to answer from.",
                answer="The university documents do not contain information to answer this question.",
                citations=[],
                answered_from_context=False,
                confidence="low",
            )

        prompt = build_evidence_prompt(question, passages)
        base_url = self.settings.hosted_base_url.rstrip("/")
        url = f"{base_url}/chat/completions" if base_url else "https://api.groq.com/openai/v1/chat/completions"

        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": self.settings.temperature,
        }

        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return Answer.model_validate_json(content)
