"""Structured inputs/outputs for AUTO-TRIAGE-01 routing."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SUPPORTED_LOSSES = frozenset({"collision", "theft", "glass"})
MISSING_SENTINEL = "not supplied"
RULE_VERSION = "AUTO-TRIAGE-01"
INJECTION_CLEAN = "clean"


@dataclass(frozen=True)
class SourceFacts:
    """Structured fields from one document source (intake / report / estimate)."""

    loss_type: str | None = None
    vehicle_id: str | None = None
    incident_timestamp: str | None = None
    driver_name: str | None = None
    incident_location: str | None = None
    injury_reported: bool | None = None
    vehicle_drivable: bool | None = None
    referral_indicator: str | None = None


@dataclass(frozen=True)
class StructuredFields:
    """Reconciled case fields plus optional per-source accounts for conflict checks."""

    loss_type: str
    vehicle_id: str | None
    incident_timestamp: str | None
    driver_name: str | None
    incident_location: str | None
    injury_reported: bool | None
    vehicle_drivable: bool | None
    referral_indicator: str | None = None
    repeated_fields: tuple[str, ...] = ()
    intake: SourceFacts | None = None
    report: SourceFacts | None = None
    estimate: SourceFacts | None = None
    # Only "clean" passes. A missing check, an error, or "injection" is manual_review.
    injection_field_status: str | None = None
    injection_model_status: str | None = None
    extra_fields: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class PolicyResult:
    """Step 2 policy selection result — never mutated by rules."""

    selected_policy_version: str | None
    policy_available: bool = True
    payload_sha256: str | None = None
    reason: str | None = None
    wording_source: str | None = None
    endorsements: tuple[str, ...] = ()
    source_id: str | None = None


@dataclass(frozen=True)
class ClaimsSnapshot:
    """Successful claims tool payload (subset used for routing)."""

    existing_claims: list[Any] = field(default_factory=list)
    intake_deliveries: int = 1
    source_id: str | None = None
    loss_reference: str | None = None
    available: bool = True


@dataclass(frozen=True)
class RouteResult:
    classification: str
    route: str
    route_rule: str
    missing_information: list[str]
    duplicate_delivery: bool
    injury_reported: bool | None
    vehicle_drivable: bool | None
    selected_policy_version: str | None
    claims_available: bool
    reasons: list[str]
    conflicts: list[str]
    injection_field_status: str | None = None
    injection_model_status: str | None = None
    extra_fields: list[dict[str, str]] = field(default_factory=list)
