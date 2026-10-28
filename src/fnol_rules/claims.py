"""GET /v1/cases/{case_id}/claims.

Retries a transport error or HTTP 5xx at most three times. A 404 stops.
A 200 whose body is not a claims object is unavailable and is not stored.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fnol_foundation.http import MAX_ATTEMPTS, fetch_with_retries, tool_url
from fnol_rules.types import ClaimsSnapshot


@dataclass(frozen=True)
class ClaimsAttempt:
    attempt: int
    status: int
    body: dict[str, Any] | None = None
    duration_ms: int = 0


@dataclass(frozen=True)
class ClaimsFetchResult:
    snapshot: ClaimsSnapshot
    attempts: tuple[ClaimsAttempt, ...]
    raw_json: dict[str, Any] | None = None


def _unavailable() -> ClaimsSnapshot:
    return ClaimsSnapshot(existing_claims=[], intake_deliveries=0, available=False)


def _snapshot(body: dict[str, Any]) -> ClaimsSnapshot | None:
    """Accept only a real list of claims and an integer delivery count."""
    existing = body.get("existing_claims")
    deliveries = body.get("intake_deliveries")
    if not isinstance(existing, list):
        return None
    if isinstance(deliveries, bool) or not isinstance(deliveries, int):
        return None
    source_id = body.get("source_id")
    loss_reference = body.get("loss_reference")
    return ClaimsSnapshot(
        existing_claims=existing,
        intake_deliveries=deliveries,
        source_id=source_id if isinstance(source_id, str) else None,
        loss_reference=loss_reference if isinstance(loss_reference, str) else None,
        available=True,
    )


def fetch_claims(
    case_id: str,
    base_url: str,
    *,
    max_attempts: int = MAX_ATTEMPTS,
    get: Callable[..., tuple[int, dict[str, Any] | None]] | None = None,
) -> ClaimsFetchResult:
    """Fetch claims. Retry transport errors and HTTP 5xx. A malformed 200 is unavailable."""
    url = tool_url(base_url, case_id, "claims")
    http_attempts, body = fetch_with_retries(url, max_attempts=max_attempts, get=get)
    attempts = tuple(
        ClaimsAttempt(item.attempt, item.status, item.body, item.duration_ms)
        for item in http_attempts
    )
    if body is None:
        return ClaimsFetchResult(snapshot=_unavailable(), attempts=attempts, raw_json=None)
    snapshot = _snapshot(body)
    if snapshot is None:
        return ClaimsFetchResult(snapshot=_unavailable(), attempts=attempts, raw_json=None)
    return ClaimsFetchResult(snapshot=snapshot, attempts=attempts, raw_json=body)
