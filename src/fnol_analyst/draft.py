"""Deterministic draft for the human handler (step 5).

Input is the stored route, policy, and citations decisions plus the structured
columns of the input revision. Raw document text is never read here. Output is
one object in the shape of docs/OUTPUT_CONTRACT.md; its canonical hash is the
value the mock binds approval to.
"""

from __future__ import annotations

import re
from typing import Any

from fnol_foundation.canonical import assert_no_restricted

FACT_COLUMNS = (
    "policy_number",
    "incident_timestamp",
    "vehicle_id",
    "loss_type",
    "referral_indicator",
)

FOLLOW_UP = {
    "driver_name": (
        "Who was driving the vehicle at the time of the incident? "
        "Please give the driver's full name."
    ),
    "incident_location": (
        "Where did the incident happen? Please give the street address "
        "or nearest intersection and the city."
    ),
}

REASON_TEXT = {
    "unsupported_loss": "the reported loss type is not one of collision, theft, or glass",
    "missing_information": "required intake fields are missing",
    "conflicting_facts": "supplied sources disagree on a fact",
    "claims_unavailable": "the claims history service did not answer after three attempts",
    "policy_unavailable": "the policy service did not answer or returned an unusable payload",
    "policy_version_null": "no single policy interval contains the incident timestamp",
    "wording_missing": "the policy wording named by the selected version is not on file",
    "injury": "an injury is reported",
    "not_drivable": "the vehicle is reported as not drivable",
    "theft": "the loss is a theft",
    "repair_amount_conflict": "a repair-amount discrepancy was supplied as a referral indicator",
    "dangerous_content": "supplied document text may contain instructions to the system",
}

# The label is supplied text: keep it short and plain so it reads as a name, not a statement.
_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9 _-]+")
_LABEL_LIMIT = 40

POLICY_REASON_TEXT = {
    "zero": "no policy interval contains the incident timestamp",
    "ambiguous": "more than one policy interval contains the incident timestamp",
    "unparsed": "the incident timestamp could not be parsed",
    "malformed": "the policy payload was malformed",
    "not_found": "the policy service returned 404",
    "unavailable": "the policy service was unavailable",
    "restricted": "the policy payload was refused because it carried a restricted value",
    "wording_missing": "the wording file named by the selected version is not on file",
}

BOUNDARY = (
    "Cited wording is potentially relevant only. No coverage, liability, fraud, reserve, "
    "settlement, denial or payment decision is made here. A referral indicator is not a "
    "fraud finding. A policy-period mismatch requires human review, not automatic denial. "
    "This is a draft; a human must approve this exact draft before any record is created."
)


def _yes_no(value: bool | None) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "not stated"


def _policy_ids(policy: dict[str, Any], citations: dict[str, Any]) -> list[str]:
    """Prefer the policy decision. An empty list is kept; only a missing list falls back."""
    ids = policy.get("policy_ids")
    if isinstance(ids, list):
        return list(ids)
    ids = citations.get("policy_ids")
    if isinstance(ids, list):
        return list(ids)
    return []


def build_draft(
    *,
    case_id: str,
    input_revision: int,
    facts_row: dict[str, Any],
    policy: dict[str, Any],
    route: dict[str, Any],
    citations: dict[str, Any],
    evidence_summary: str | None = None,
    safety_status: str = "skipped",
) -> dict[str, Any]:
    """Build the contract object. Refuses restricted values before returning.

    ``narrative`` is always the deterministic skeleton. ``evidence_summary`` is the
    optional model slot; ``safety_status`` is skipped, passed, or fallback.
    """
    facts_row = {key: facts_row.get(key) for key in FACT_COLUMNS}
    missing = [item for item in route.get("missing_information", []) if isinstance(item, str)]
    version = route.get("selected_policy_version")
    if safety_status not in ("skipped", "passed", "fallback"):
        raise ValueError("safety_status must be skipped, passed, or fallback")
    if evidence_summary is not None and not isinstance(evidence_summary, str):
        raise TypeError("evidence_summary must be a string or null")
    draft = {
        "case_id": case_id,
        "classification": route.get("classification"),
        "routing": route.get("route"),
        # Copied from the route decision, which was set from the claims snapshot.
        "duplicate_delivery": route.get("duplicate_delivery") is True,
        "policy_ids": _policy_ids(policy, citations),
        "facts": {
            "policy_number": facts_row["policy_number"],
            "incident_timestamp": facts_row["incident_timestamp"],
            "vehicle_id": facts_row["vehicle_id"],
            "injury_reported": route.get("injury_reported"),
            "selected_policy_version": version,
        },
        "missing_information": missing,
        "deadline": None,
        "citations": [dict(item) for item in citations.get("citations", [])],
        "follow_up_questions": [
            {"field": field, "question": FOLLOW_UP[field]} for field in missing if field in FOLLOW_UP
        ],
        "narrative": _narrative(case_id, input_revision, facts_row, policy, route, citations),
        "evidence_summary": evidence_summary,
        "safety_status": safety_status,
    }
    assert_no_restricted(draft)
    return draft


def _safe_label(label: Any) -> str:
    text = _LABEL_UNSAFE.sub(" ", str(label or "")).strip()
    text = " ".join(text.split())[:_LABEL_LIMIT].strip()
    return text or "unnamed"


def _content_check_notes(route: dict[str, Any]) -> list[str]:
    """Name each extra field by source id and label. The supplied line is never repeated."""
    if "dangerous_content" not in (route.get("reasons") or []):
        return []
    notes = []
    for item in route.get("extra_fields") or []:
        if not isinstance(item, dict):
            continue
        notes.append(
            f"Content check: {item.get('source_id') or 'a supplied document'} has an unexpected "
            f"field labelled \"{_safe_label(item.get('label'))}\". Its text was treated as "
            "untrusted data and was not followed."
        )
    model = route.get("injection_model_status")
    if model == "injection":
        notes.append(
            "Content check: the model screen flagged leftover document text as instructions. "
            "That text was not followed."
        )
    elif model != "clean":
        notes.append(
            "Content check: the model screen of leftover document text did not complete, "
            "so the text was not cleared. It was not followed."
        )
    if not notes:
        notes.append("Content check: supplied document text was not cleared and was not followed.")
    return notes


def _narrative(case_id, input_revision, facts, policy, route, citations) -> str:
    reasons = [item for item in route.get("reasons", []) if isinstance(item, str)]
    conflicts = [item for item in route.get("conflicts", []) if isinstance(item, str)]
    missing = [item for item in route.get("missing_information", []) if isinstance(item, str)]
    omissions = citations.get("omissions") or []
    version = route.get("selected_policy_version")
    lines = [
        f"Draft FNOL for the human handler. Case {case_id}, input revision {input_revision}.",
        f"Classification: {route.get('classification')}. Recommended route: {route.get('route')} "
        f"under {route.get('route_rule') or 'AUTO-TRIAGE-01'}.",
        f"Facts: policy {facts.get('policy_number') or 'unknown'}, vehicle {facts.get('vehicle_id') or 'unknown'}, "
        f"incident {facts.get('incident_timestamp') or 'unknown'}, selected policy version "
        f"{version if version is not None else 'null'}.",
        f"Flags: injury reported {_yes_no(route.get('injury_reported'))}; vehicle drivable "
        f"{_yes_no(route.get('vehicle_drivable'))}; referral indicator {facts.get('referral_indicator') or 'none'}; "
        f"duplicate intake delivery {'yes' if route.get('duplicate_delivery') else 'no'}.",
    ]
    if reasons:
        lines.append("Route basis: " + "; ".join(REASON_TEXT.get(item, item) for item in reasons) + ".")
    else:
        lines.append("Route basis: no manual-review, urgency, or specialist trigger applies.")
    if route.get("injury_reported") is True or route.get("vehicle_drivable") is False:
        lines.append(
            "Urgency: injury or non-drivable vehicle is flagged for the handler regardless of route."
        )
    lines.extend(_content_check_notes(route))
    if missing:
        lines.append("Missing information: " + ", ".join(missing) + ". See follow-up questions.")
    if conflicts:
        lines.append("Conflicting facts across sources: " + ", ".join(conflicts) + ". Each side is cited.")
    policy_reason = policy.get("reason")
    if version is None and policy_reason in POLICY_REASON_TEXT:
        lines.append(
            "Policy selection: " + POLICY_REASON_TEXT[policy_reason] + "; only AUTO-TRIAGE-01 is cited."
        )
    elif policy_reason == "wording_missing":
        lines.append("Policy selection: " + POLICY_REASON_TEXT[policy_reason] + ".")
    limits = []
    if not route.get("claims_available", True):
        limits.append("claims history could not be retrieved, so duplicate status is unverified")
    for item in omissions:
        if item.get("tool") == "claims":
            continue
        if item.get("tool") == "policy":
            limits.append("the policy snapshot is unavailable and is not quoted")
        elif item.get("reason") == "wording_unavailable":
            limits.append(f"wording {item.get('source_id')} could not be quoted")
        elif item.get("reason") == "no_safe_excerpt":
            limits.append(f"{item.get('source_id')} has no safe excerpt to quote")
    if limits:
        lines.append("Limitations: " + "; ".join(limits) + ".")
    lines.append(f"Evidence: {len(citations.get('citations', []))} exact citations from the named sources.")
    lines.append(BOUNDARY)
    return "\n".join(lines)
