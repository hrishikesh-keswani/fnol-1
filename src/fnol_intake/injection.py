"""Deterministic prompt-injection scan, plus an inventory of unknown labels.

A ``Label: value`` line whose label is not a known label for that document is
an extra field. The label is stored. It is not an injection by itself.
Injection is a line that tries to instruct a model, reviewer, or approver.
Intake stores that verdict on the immutable revision. The Bedrock screen of
the labels and leftover lines runs later, on the rules service.
"""

from __future__ import annotations

import re

from fnol_foundation.canonical import RESTRICTED_MARKERS

CLEAN = "clean"
INJECTION = "injection"
ERROR = "error"

RESTRICTED_LABEL = "restricted_label"

# Light patterns for instruction-shaped text. An unknown label does not match.
_INSTRUCTION = re.compile(
    r"you\s+are\s+now"
    r"|ignore\s+(?:any|all|previous|prior|every)"
    r"|disregard\s+(?:any|all|previous|prior|the)"
    r"|override\s+(?:any|the|all\s+)?(?:rules|approval|policy|human)"
    r"|print\s+all\s+private"
    r"|private\s+tokens"
    r"|reveal\s+(?:private|hidden|secret)"
    r"|approve\s+and\s+pay"
    r"|pay\s+immediately",
    re.IGNORECASE,
)


def _header(line):
    return not line.strip() or line.startswith("#") or line.startswith("Source ID:")


def _restricted(text):
    return any(marker in text for marker in RESTRICTED_MARKERS)


def extra_fields(text, labels):
    """Labels of ``Label: value`` lines that are not in this document's label map."""
    found = []
    for line in text.splitlines():
        if _header(line):
            continue
        key, sep, _value = line.partition(": ")
        if sep and key not in labels:
            found.append(RESTRICTED_LABEL if _restricted(key) else key)
    return found


def instruction_status(*texts):
    """``injection`` when any supplied document text matches an instruction pattern."""
    for text in texts:
        if isinstance(text, str) and _INSTRUCTION.search(text):
            return INJECTION
    return CLEAN
