"""Guardrails for the LLM layer.

The website is untrusted input. Anything scraped from it that reaches Claude is
truncated, stripped of control characters, wrapped as data, and the model's answer
is validated against the findings it was given. The LLM can explain and group;
it can never change the verdict, a severity, or trigger an action.
"""
from __future__ import annotations

import json
import re

INJECTION_RE = re.compile(
    r"(ignore (all |any )?(previous|prior|above) instructions|disregard (the )?(system|previous) prompt"
    r"|you are now [a-z ]{0,30}(assistant|ai|model)|report (this|the) (site|website) (as )?healthy)", re.I)
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+)")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_evidence(text: str, limit: int = 200) -> str:
    text = CTRL_RE.sub(" ", text or "")
    text = SECRET_RE.sub("[REDACTED]", text)
    text = INJECTION_RE.sub("[INJECTION-ATTEMPT-REMOVED]", text)
    return text[:limit]


def extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a model reply (tolerates ``` fences)."""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start: end + 1])
    except json.JSONDecodeError:
        return None


def validate_triage(raw: dict | None, valid_ids: set[str]) -> dict | None:
    """Keep only well-formed root causes that reference findings that actually exist."""
    if not isinstance(raw, dict) or not isinstance(raw.get("summary"), str):
        return None
    causes = []
    for rc in raw.get("root_causes", [])[:15]:
        if not isinstance(rc, dict):
            continue
        ids = [i for i in rc.get("finding_ids", []) if i in valid_ids]
        if not ids:
            continue                                   # hallucinated / empty group -> drop
        causes.append({
            "title": str(rc.get("title", ""))[:120],
            "finding_ids": ids,
            "likely_cause": str(rc.get("likely_cause", ""))[:400],
            "fix": str(rc.get("fix", ""))[:400],
            "priority": int(rc.get("priority", 3)) if str(rc.get("priority", "3")).isdigit() else 3,
        })
    fps = [
        {"finding_id": fp["finding_id"], "reason": str(fp.get("reason", ""))[:200]}
        for fp in raw.get("suspected_false_positives", [])
        if isinstance(fp, dict) and fp.get("finding_id") in valid_ids
    ]
    return {"summary": raw["summary"][:800], "root_causes": sorted(causes, key=lambda c: c["priority"]),
            "suspected_false_positives": fps}
