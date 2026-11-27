"""Part D4 Retrieval & Query Injection Guardrail.

Detects prompt-injection / instruction-override attempts in both incoming user
queries and retrieved corpus passages (treated strictly as untrusted data),
neutralizes adversarial directive lines before prompt assembly, increments
Prometheus counters, and emits guardrail action names for the D1 trace record.
"""

from __future__ import annotations

import logging
import re

from ..routes import metrics
from ..schemas import Passage

log = logging.getLogger("rahbar.guardrail")

_INJECTION_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("ignore_instructions", re.compile(r"\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions\b", re.I)),
    ("disregard_rules", re.compile(r"\bdisregard\s+(?:the\s+|all\s+)?(?:official|previous|prior|above)\b", re.I)),
    ("system_override", re.compile(r"\bsystem\s+override\b", re.I)),
    ("override_prompt", re.compile(r"\boverride\s+(?:system\s+)?(?:instructions|prompt|policy|rules)\b", re.I)),
    ("exfiltrate_prompt", re.compile(r"\b(?:reveal|print|leak|dump)\s+(?:your\s+)?system\s+prompt\b", re.I)),
]


def inspect_query(query: str) -> list[str]:
    """Flag adversarial instruction-override patterns in the user query."""
    actions: list[str] = []
    for name, pat in _INJECTION_RULES:
        if pat.search(query):
            metrics.INJECTION_ATTEMPTS.labels(location="query").inc()
            actions.append(f"query_injection_detected:{name}")
            log.warning(
                "injection attempt detected in user query",
                extra={"pattern": name},
            )
    return actions


def inspect_and_neutralize_passages(passages: list[Passage]) -> tuple[list[Passage], list[str]]:
    """Scan retrieved chunks for instruction overrides and neutralize hostile lines."""
    sanitized: list[Passage] = []
    actions: list[str] = []

    for passage in passages:
        lines = passage.text.splitlines()
        cleaned_lines: list[str] = []
        matched_rules: list[str] = []

        for line in lines:
            line_hits = [name for name, pat in _INJECTION_RULES if pat.search(line)]
            if line_hits:
                matched_rules.extend(line_hits)
                cleaned_lines.append("[UNTRUSTED INSTRUCTION REMOVED BY GUARDRAIL]")
            else:
                cleaned_lines.append(line)

        if matched_rules:
            unique_rules = sorted(set(matched_rules))
            metrics.INJECTION_ATTEMPTS.labels(location="retrieval").inc()
            log.warning(
                "injection attempt neutralized in retrieved passage",
                extra={
                    "chunk_id": passage.chunk_id,
                    "doc_id": passage.doc_id or passage.source,
                    "source": passage.source,
                    "patterns": unique_rules,
                },
            )
            actions.append(f"retrieval_injection_detected:{passage.chunk_id}")
            actions.append(f"retrieval_injection_neutralized:{passage.chunk_id}")
            for r in unique_rules:
                actions.append(f"injection_rule:{r}")
            sanitized.append(passage.model_copy(update={"text": "\n".join(cleaned_lines)}))
        else:
            sanitized.append(passage)

    return sanitized, actions
