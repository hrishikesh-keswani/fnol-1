"""Deterministic AUTO-TRIAGE-01 routing from structured fields only."""
from __future__ import annotations

from fnol_rules.types import (
    INJECTION_CLEAN,
    MISSING_SENTINEL,
    RULE_VERSION,
    SUPPORTED_LOSSES,
    ClaimsSnapshot,
    PolicyResult,
    RouteResult,
    SourceFacts,
    StructuredFields,
)


def _is_missing(value: str | None) -> bool:
    if value is None:
        return True
    text = value.strip()
    return text == "" or text.lower() == MISSING_SENTINEL


def _missing_information(fields: StructuredFields) -> list[str]:
    missing: list[str] = []
    if _is_missing(fields.driver_name):
        missing.append("driver_name")
    if _is_missing(fields.incident_location):
        missing.append("incident_location")
    return missing


def _sources(fields: StructuredFields) -> list[SourceFacts]:
    return [s for s in (fields.intake, fields.report, fields.estimate) if s is not None]


def _conflict_names(fields: StructuredFields) -> list[str]:
    """Fields that disagree across sources, plus labels repeated inside one document."""
    names: list[str] = []
    srcs = _sources(fields)
    if len(srcs) >= 2:
        for attr in ("vehicle_id", "incident_timestamp", "loss_type"):
            vals = [getattr(source, attr) for source in srcs if getattr(source, attr) is not None]
            if len(set(vals)) > 1:
                names.append(attr)
    for name in fields.repeated_fields:
        if name not in names:
            names.append(name)
    return names


def route(
    fields: StructuredFields,
    policy: PolicyResult,
    claims: ClaimsSnapshot,
) -> RouteResult:
    """Apply AUTO-TRIAGE-01 precedence. Never accepts raw_text."""
    classification = (
        fields.loss_type if fields.loss_type in SUPPORTED_LOSSES else "unsupported"
    )
    missing = _missing_information(fields)
    conflicts = _conflict_names(fields)
    duplicate = (
        claims.available
        and (claims.intake_deliveries > 1 or len(claims.existing_claims) > 0)
    )
    reasons: list[str] = []
    if (
        fields.injection_field_status != INJECTION_CLEAN
        or fields.injection_model_status != INJECTION_CLEAN
    ):
        reasons.append("dangerous_content")
    if classification == "unsupported":
        reasons.append("unsupported_loss")
    if missing:
        reasons.append("missing_information")
    if conflicts:
        reasons.append("conflicting_facts")
    if not claims.available:
        reasons.append("claims_unavailable")
    if policy.reason == "wording_missing":
        reasons.append("wording_missing")
    elif not policy.policy_available:
        reasons.append("policy_unavailable")
    elif policy.selected_policy_version is None:
        reasons.append("policy_version_null")

    if reasons:
        chosen = "manual_review"
    elif fields.injury_reported is True or fields.vehicle_drivable is False:
        chosen = "urgent"
        if fields.injury_reported is True:
            reasons.append("injury")
        if fields.vehicle_drivable is False:
            reasons.append("not_drivable")
    elif classification == "theft" or fields.referral_indicator == "repair_amount_conflict":
        chosen = "specialist"
        if classification == "theft":
            reasons.append("theft")
        if fields.referral_indicator == "repair_amount_conflict":
            reasons.append("repair_amount_conflict")
    else:
        chosen = "standard"

    return RouteResult(
        classification=classification,
        route=chosen,
        route_rule=RULE_VERSION,
        missing_information=missing,
        duplicate_delivery=duplicate,
        injury_reported=fields.injury_reported,
        vehicle_drivable=fields.vehicle_drivable,
        selected_policy_version=policy.selected_policy_version,
        claims_available=claims.available,
        reasons=reasons,
        conflicts=conflicts,
        injection_field_status=fields.injection_field_status,
        injection_model_status=fields.injection_model_status,
        extra_fields=[dict(item) for item in fields.extra_fields],
    )
