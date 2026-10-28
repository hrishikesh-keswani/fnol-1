"""Hard guard for citation quotes.

A quote is refused when it contains ``private_token`` or ``SYNTHETIC-PRIVATE-``.
The error does not echo the quote.
"""

from fnol_foundation.canonical import RESTRICTED_MARKERS, RestrictedValueError, assert_no_restricted

__all__ = ["RestrictedValueError", "contains_restricted", "reject_restricted_quote", "seal"]


def contains_restricted(text: str) -> bool:
    return any(marker in text for marker in RESTRICTED_MARKERS)


def reject_restricted_quote(quote: str) -> str:
    """Return ``quote`` when it is a non-empty excerpt with no restricted marker."""
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError("quote must be a non-empty excerpt")
    assert_no_restricted(quote)
    return quote


def seal(citations: list[dict]) -> list[dict]:
    """Refuse the whole list if any quote, claim, or source id carries a marker."""
    assert_no_restricted(citations)
    for item in citations:
        reject_restricted_quote(item.get("quote", ""))
    return citations
