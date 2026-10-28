"""Rules server orchestration: read policy decision, fetch claims, route, persist."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable

from fnol_foundation.canonical import RestrictedValueError, digest
from fnol_foundation.events import record_event
from fnol_foundation.http import public_error
from fnol_foundation.stores import Stores
from fnol_intake.injection import CLEAN, INJECTION
from fnol_rules.claims import ClaimsFetchResult, fetch_claims
from fnol_rules.route import route
from fnol_rules.screen import USE_DEFAULT, resolve_client, screen_fields
from fnol_rules.types import RULE_VERSION, ClaimsSnapshot, PolicyResult, RouteResult, StructuredFields


def policy_from_decision(decision: dict[str, Any]) -> PolicyResult:
    body = decision["body"]
    endorsements = body.get("endorsements") or ()
    return PolicyResult(
        selected_policy_version=body.get("selected_policy_version"),
        policy_available=bool(body.get("policy_available")),
        payload_sha256=decision.get("policy_sha256"),
        reason=body.get("reason"),
        wording_source=body.get("wording_source"),
        endorsements=tuple(endorsements),
        source_id=body.get("source_id"),
    )


def run_rules(
    case_id: str,
    fields: StructuredFields,
    policy: PolicyResult,
    *,
    base_url: str,
    stores: Stores,
    input_revision: int,
    trace_id: str,
    input_sha256: str | None = None,
    fetch: Callable[..., ClaimsFetchResult] | None = None,
    payload: dict | None = None,
    screen_client: Any = USE_DEFAULT,
) -> RouteResult:
    """Fetch claims, screen leftover text, route, and store the decision.

    The model screen does not update the intake revision. A missing screen
    or three failed calls is not clean, so the route is manual_review. The
    trace records the outcome without the prompt or the line text.
    """
    fetcher = fetch or fetch_claims
    result = fetcher(case_id, base_url)
    parent = None
    for attempt in result.attempts:
        parent = record_event(
            stores,
            trace_id=trace_id,
            parent_span_id=parent,
            step="3",
            event="tool_read",
            actor="rules",
            rule_version=RULE_VERSION,
            status="ok" if attempt.status == 200 else "error",
            case_id=case_id,
            input_revision=input_revision,
            attempt=attempt.attempt,
            mock_http_status=attempt.status,
            error_code=None if attempt.status == 200 else public_error(attempt.body),
            duration_ms=attempt.duration_ms,
            input_sha256=input_sha256,
        )

    snapshot = result.snapshot
    claims_sha = None
    if result.raw_json is not None and snapshot.available:
        try:
            claims_sha = stores.input.put_tool_payload(
                case_id, "claims", result.raw_json, input_revision
            )
        except RestrictedValueError:
            snapshot = ClaimsSnapshot(existing_claims=[], intake_deliveries=0, available=False)
            parent = record_event(
                stores,
                trace_id=trace_id,
                parent_span_id=parent,
                step="3",
                event="claims_refused",
                actor="rules",
                rule_version=RULE_VERSION,
                status="error",
                case_id=case_id,
                input_revision=input_revision,
                error_code="restricted_value",
                input_sha256=input_sha256,
            )
        else:
            parent = record_event(
                stores,
                trace_id=trace_id,
                parent_span_id=parent,
                step="3",
                event="claims_stored",
                actor="rules",
                rule_version=RULE_VERSION,
                status="ok",
                case_id=case_id,
                input_revision=input_revision,
                output_sha256=claims_sha,
                input_sha256=input_sha256,
            )

    fields, verdict, check_ms = screen_fields(
        fields, payload or {}, resolve_client(screen_client)
    )
    field_status = fields.injection_field_status
    clean = field_status == CLEAN and verdict.status == CLEAN
    error_code = verdict.error_code
    if error_code is None and field_status == INJECTION:
        error_code = "instruction_text"
    parent = record_event(
        stores,
        trace_id=trace_id,
        parent_span_id=parent,
        step="3",
        event="injection_check",
        actor="rules",
        rule_version=RULE_VERSION,
        status="ok" if clean else "error",
        error_code=None if clean else error_code,
        case_id=case_id,
        input_revision=input_revision,
        model_id=verdict.model_id,
        input_tokens=verdict.input_tokens,
        output_tokens=verdict.output_tokens,
        injection_field_status=field_status,
        injection_model_status=verdict.status,
        duration_ms=check_ms,
        started_at=datetime.now(timezone.utc),
        input_sha256=input_sha256,
        attempt=verdict.attempt,
    )
    routed = route(fields, policy, snapshot)
    body = asdict(routed)
    body["policy_sha256"] = policy.payload_sha256
    body["claims_sha256"] = claims_sha
    stores.input.put_decision(
        case_id,
        input_revision,
        "route",
        body,
        policy_sha256=policy.payload_sha256,
        claims_sha256=claims_sha,
    )
    record_event(
        stores,
        trace_id=trace_id,
        parent_span_id=parent,
        step="3",
        event="route_decision",
        actor="rules",
        rule_version=RULE_VERSION,
        status="ok",
        case_id=case_id,
        input_revision=input_revision,
        output_sha256=digest(body),
        input_sha256=input_sha256,
    )
    return routed
