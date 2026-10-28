"""Local check on the model summary.

The required boundary may name coverage, liability, fraud, reserve, settlement,
denial, and payment, because it says no decision is made. Affirmative decisions
are refused. Restricted markers are refused before anything is stored.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fnol_foundation.canonical import RESTRICTED_MARKERS

WITHHELD_NOTE = "Summary withheld: model output was not a handler summary."
MIN_SUMMARY_CHARS = 20

_ADJUDICATION = (
    re.compile(r"\bcoverage\s+(is|was)\s+(approved|denied|granted|confirmed|applicable)\b", re.I),
    re.compile(r"\bcoverage\s+applies\b", re.I),
    re.compile(r"\bcoverage\s+does\s+not\s+apply\b", re.I),
    re.compile(r"\b(this|the)\s+loss\s+is\s+covered\b", re.I),
    re.compile(r"\b(this|the)\s+(loss|claim)\s+is\s+not\s+covered\b", re.I),
    re.compile(r"\bclaim\s+(is|was)\s+(denied|approved|payable|closed)\b", re.I),
    re.compile(r"\b(deny|denies|denied)\s+(the\s+|this\s+)?claim\b", re.I),
    re.compile(r"\bwe\s+will\s+pay\b", re.I),
    re.compile(r"\bpayment\s+(is|was)\s+(approved|authorized|due)\b", re.I),
    re.compile(r"\b(this|the)\s+(claim|loss)\s+is\s+fraud\b", re.I),
    re.compile(r"\bfraudulent\b", re.I),
    re.compile(r"\bfraud\s+(has\s+been|was)\s+(found|confirmed|established)\b", re.I),
    re.compile(r"\bliability\s+(rests|lies)\s+with\b", re.I),
    re.compile(r"\bsettlement\s+of\b", re.I),
    re.compile(r"\breserve\s+(of|is\s+set|set\s+at)\b", re.I),
    re.compile(r"\bset\s+the\s+reserve\b", re.I),
)


def blocked_output_message() -> str:
    spec = json.loads(Path(__file__).with_name("guardrail_spec.json").read_text(encoding="utf-8"))
    message = spec.get("blockedOutputsMessaging")
    if not isinstance(message, str) or not message.strip():
        return WITHHELD_NOTE
    return message


def assess(text: str) -> str:
    """Return 'passed' or 'blocked'. The blocked text is not returned."""
    if not isinstance(text, str):
        return "blocked"
    stripped = text.strip()
    if len(stripped) < MIN_SUMMARY_CHARS:
        return "blocked"
    if stripped == blocked_output_message() or stripped == WITHHELD_NOTE:
        return "blocked"
    if any(marker in stripped for marker in RESTRICTED_MARKERS):
        return "blocked"
    if _is_json_object(stripped):
        return "blocked"
    if any(pattern.search(stripped) for pattern in _ADJUDICATION):
        return "blocked"
    return "passed"


def _is_json_object(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    if not stripped.startswith("{"):
        return False
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return isinstance(value, dict)
