"""Hosted free-tier provider (Groq, Google AI Studio, OpenRouter).

The key arrives as LLM_API_KEY from a Kubernetes Secret.
"""

from __future__ import annotations

import json
import logging
import httpx
from pydantic import ValidationError

from ...config import Settings
from ...schemas import Answer, Citation, Passage

log = logging.getLogger("rahbar.llm.hosted")

SYSTEM_PROMPT = """You are RAHBAR, an official assistant answering questions strictly grounded on university policies.
Rules:
1. Base your answer strictly on the provided numbered passages.
2. If the passages do not contain the answer, set answered_from_context to false and state that the corpus does not cover the question.
3. Every factual sentence in your answer must cite its source and section as [source / section].
4. Output your entire response as a valid JSON object matching the requested Answer schema. Never output markdown outside the JSON."""


class HostedProvider:
    name = "hosted"

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
        """Call hosted OpenAI-compatible chat completions endpoint and validate with Pydantic."""
        if not passages:
            return Answer(
                reasoning="No passages available to answer from.",
                answer="The university documents do not contain information to answer this question.",
                citations=[],
                answered_from_context=False,
                confidence="low",
            )

        prompt = self._build_prompt(question, passages)
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

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                return Answer.model_validate_json(content)
        except (httpx.HTTPError, KeyError, ValidationError, json.JSONDecodeError) as exc:
            log.warning(f"Hosted provider call failed or invalid JSON: {exc}. Retrying once...")
            try:
                with httpx.Client(timeout=30.0) as client:
                    resp = client.post(url, headers=headers, json=payload)
                    resp.raise_for_status()
                    content = resp.json()["choices"][0]["message"]["content"]
                    return Answer.model_validate_json(content)
            except Exception as retry_exc:
                log.error(f"Hosted provider retry failed: {retry_exc}. Returning safe fallback.")
                top = passages[0]
                return Answer(
                    reasoning=f"Hosted provider fallback: {retry_exc}",
                    answer=f"Information from {top.source}: {top.text[:200]}...",
                    citations=[Citation(source=top.source, section=top.section, page=top.page)],
                    answered_from_context=True,
                    confidence="low",
                )
