"""Canonical JSON and SHA-256 matching tools/runtime.py.

The mock hashes review drafts with sorted keys, no extra whitespace, and
UTF-8 characters left as themselves. Draft hashes only compare equal when
this helper uses that same form.
"""

import hashlib
import json

RESTRICTED_MARKERS = ("private_token", "SYNTHETIC-PRIVATE-")

# Verbatim source text. A marker may sit in these columns because the file is the evidence.
# Every other stored field is refused, and events never receive these columns.
EVIDENCE_TEXT_COLUMNS = frozenset({
    "account_text",
    "raw_intake",
    "intake_notes",
    "raw_estimate",
    "estimate_notes",
    "raw_report",
    "report_notes",
})


def canonical(value):
    """Return the mock's canonical JSON form of value."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    """Return the hex SHA-256 of canonical(value), matching tools.runtime.digest."""
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class RestrictedValueError(ValueError):
    """A stored value contained a restricted synthetic marker."""


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def assert_no_restricted(value):
    """Refuse private_token and SYNTHETIC-PRIVATE- markers before persistence.

    The error does not echo the submitted value.
    """
    for text in _strings(value):
        if any(marker in text for marker in RESTRICTED_MARKERS):
            raise RestrictedValueError("restricted value refused")


def drop_private_token(value):
    """Return a deep copy with every private_token key removed.

    Other keys and string values are unchanged. The removed value is not returned.
    """
    if isinstance(value, dict):
        return {
            key: drop_private_token(item)
            for key, item in value.items()
            if key != "private_token"
        }
    if isinstance(value, list):
        return [drop_private_token(item) for item in value]
    return value


def redact_for_store(value):
    """Drop private_token keys, then refuse if a restricted marker remains."""
    cleaned = drop_private_token(value)
    assert_no_restricted(cleaned)
    return cleaned
