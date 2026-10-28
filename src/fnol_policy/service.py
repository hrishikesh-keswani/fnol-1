"""Policy step: fetch, redact, select one interval, persist the decision.

The stored snapshot keeps the policies array so a later citation can quote it.
private_token is removed before that write. The decision row is the only
version rules is allowed to read.
"""

from __future__ import annotations

from typing import Any, Callable

from fnol_foundation.canonical import RestrictedValueError, digest
from fnol_foundation.events import record_event
from fnol_foundation.http import MAX_ATTEMPTS, fetch_with_retries, public_error, tool_url
from fnol_foundation.stores import Stores
from fnol_policy.select import PROCEDURE_ID, select_policy

RULE_VERSION = "AUTO-TRIAGE-01"


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "selected_policy_version": None,
        "policy_available": False,
        "reason": reason,
        "wording_source": None,
        "endorsements": [],
        "source_id": None,
        "policy_number": None,
        "vehicle_id": None,
        "policy_ids": [PROCEDURE_ID],
        "missing_sources": [],
    }


def run_policy(
    case_id: str,
    incident_timestamp: object,
    *,
    base_url: str,
    stores: Stores,
    input_revision: int,
    trace_id: str,
    input_sha256: str | None = None,
    fetch: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """GET policy for one revision. A failed fetch still stores an unavailable decision."""
    if fetch is None:
        url = tool_url(base_url, case_id, "policy")
        attempts, body = fetch_with_retries(url, max_attempts=MAX_ATTEMPTS)
    else:
        attempts, body = fetch(case_id, base_url)

    parent = None
    for attempt in attempts:
        parent = record_event(
            stores,
            trace_id=trace_id,
            parent_span_id=parent,
            step="2",
            event="tool_read",
            actor="policy",
            status="ok" if attempt.status == 200 else "error",
            case_id=case_id,
            input_revision=input_revision,
            attempt=attempt.attempt,
            mock_http_status=attempt.status,
            error_code=None if attempt.status == 200 else public_error(attempt.body),
            duration_ms=attempt.duration_ms,
            input_sha256=input_sha256,
        )

    payload_sha = None
    if body is None:
        last_status = attempts[-1].status if attempts else 0
        decision = _unavailable("not_found" if last_status == 404 else "unavailable")
    else:
        try:
            payload_sha = stores.input.put_tool_payload(case_id, "policy", body, input_revision)
        except RestrictedValueError:
            decision = _unavailable("restricted")
            parent = record_event(
                stores,
                trace_id=trace_id,
                parent_span_id=parent,
                step="2",
                event="policy_refused",
                actor="policy",
                status="error",
                case_id=case_id,
                input_revision=input_revision,
                error_code="restricted_value",
                input_sha256=input_sha256,
            )
        else:
            stored = stores.input.get_tool_payload(case_id, "policy", input_revision)
            known = {row["source_id"] for row in stores.input.list_procedures()}
            decision = select_policy(incident_timestamp, stored["raw_json"], known)
            parent = record_event(
                stores,
                trace_id=trace_id,
                parent_span_id=parent,
                step="2",
                event="policy_stored",
                actor="policy",
                status="ok",
                case_id=case_id,
                input_revision=input_revision,
                output_sha256=payload_sha,
                input_sha256=input_sha256,
            )

    stores.input.put_decision(
        case_id,
        input_revision,
        "policy",
        decision,
        policy_sha256=payload_sha,
    )
    record_event(
        stores,
        trace_id=trace_id,
        parent_span_id=parent,
        step="2",
        event="policy_decision",
        actor="policy",
        rule_version=RULE_VERSION,
        status="ok" if decision["policy_available"] else "error",
        case_id=case_id,
        input_revision=input_revision,
        output_sha256=digest(decision),
        input_sha256=input_sha256,
        error_code=None if decision["policy_available"] else decision["reason"],
    )
    return decision
