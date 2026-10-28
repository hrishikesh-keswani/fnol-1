"""Exact excerpts from stored raw text and raw JSON. No I/O."""

from __future__ import annotations

import json

from fnol_foundation.canonical import RestrictedValueError

from fnol_citations.guard import contains_restricted, reject_restricted_quote

CLAIM_PROCEDURE = "triage_and_potential_coverage"
CLAIM_INCIDENT = "incident_evidence"
CLAIM_CONFLICT = "fact_conflict"
CLAIM_REPAIR = "repair_amount_conflict"
CLAIM_HISTORY = "policy_or_claim_history"
CLAIM_MISSING = "missing_information"

# Allowed missing_information identifiers and the intake label that carries each one.
_MISSING_LABELS = {
    "driver_name": "Driver name: ",
    "incident_location": "Incident location: ",
}

# Used when no selected interval can be quoted. The first safe member wins.
POLICY_JSON_KEYS = ("policies", "policy_number", "vehicle_id", "source_id")

_SECTION_PREFIX = {
    "collision": "Collision section:",
    "theft": "Theft section:",
    "glass": "Glass section:",
}

# Internal field name, then each document that can repeat that label.
_REPEATED_LABELS = {
    "driver_name": (("raw_intake", "intake_source", "Driver name: "),),
    "incident_location": (("raw_intake", "intake_source", "Incident location: "),),
    "incident_timestamp": (
        ("raw_intake", "intake_source", "Incident timestamp: "),
        ("raw_report", "report_source_id", "Incident timestamp: "),
    ),
    "vehicle_id": (("raw_intake", "intake_source", "Vehicle ID: "),),
    "loss_type": (("raw_intake", "intake_source", "Loss type: "),),
    "policy_number": (("raw_intake", "intake_source", "Policy number: "),),
    "referral_indicator": (("raw_intake", "intake_source", "Referral indicator: "),),
    "loss_reference": (("raw_intake", "intake_source", "Loss reference: "),),
    "injury_reported_raw": (("raw_intake", "intake_source", "Injury reported: "),),
    "vehicle_drivable_raw": (("raw_intake", "intake_source", "Vehicle drivable: "),),
    "vehicle": (("raw_estimate", "estimate_source_id", "Vehicle: "),),
    "repair_amount": (("raw_estimate", "estimate_source_id", "Repair amount: "),),
    "estimate_reference": (("raw_estimate", "estimate_source_id", "Estimate reference: "),),
    "prepared_by": (("raw_estimate", "estimate_source_id", "Prepared by: "),),
    "vehicle_reference": (("raw_report", "report_source_id", "Vehicle reference: "),),
    "reported_loss_type": (("raw_report", "report_source_id", "Reported loss type: "),),
}

# Structured disagreement the rules server already treats as a fact conflict.
# Each side is (payload field, raw-text field, source-id field, exact line prefix).
_CONFLICT_GROUPS = (
    (
        ("vehicle_id", "raw_intake", "intake_source", "Vehicle ID: "),
        ("report_vehicle_reference", "raw_report", "report_source_id", "Vehicle reference: "),
        ("estimate_vehicle", "raw_estimate", "estimate_source_id", "Vehicle: "),
    ),
    (
        ("incident_timestamp", "raw_intake", "intake_source", "Incident timestamp: "),
        ("report_incident_timestamp", "raw_report", "report_source_id", "Incident timestamp: "),
    ),
    (
        ("loss_type", "raw_intake", "intake_source", "Loss type: "),
        ("reported_loss_type", "raw_report", "report_source_id", "Reported loss type: "),
    ),
)


def json_member_quote(payload: dict, key: str) -> str:
    """One JSON member, sorted keys and standard spaces.

    The evaluator compares quotes to ``json.dumps(source, sort_keys=True)``,
    which keeps a space after ``:`` and ``,``. The stored snapshot is compact
    canonical JSON, so the quote is rendered again here. Stripping the outer
    braces leaves the member as it appears inside the full object, including
    when that object still has a sibling ``private_token`` key.
    """
    rendered = json.dumps({key: payload[key]}, sort_keys=True)
    if len(rendered) < 2 or not rendered.startswith("{") or not rendered.endswith("}"):
        raise ValueError("json member did not render as an object")
    return reject_restricted_quote(rendered[1:-1])


def first_json_quote(payload: dict | None, keys: tuple[str, ...]) -> str | None:
    """First preferred member whose rendered text passes the restricted-token guard."""
    if not payload:
        return None
    for key in keys:
        if key not in payload:
            continue
        try:
            return json_member_quote(payload, key)
        except RestrictedValueError:
            continue
    return None


def policy_json_quote(payload: dict | None, version: str | None) -> str | None:
    """The selected interval when one was chosen, otherwise the policies member.

    The interval object is rendered with sorted keys and standard spaces, which
    is how it appears inside the evaluator's full payload. An ambiguous or
    empty selection quotes the policies member so both intervals stay visible.
    """
    if not payload:
        return None
    policies = payload.get("policies")
    if isinstance(version, str) and version and isinstance(policies, list):
        for item in policies:
            if isinstance(item, dict) and item.get("version") == version:
                try:
                    return reject_restricted_quote(json.dumps(item, sort_keys=True))
                except RestrictedValueError:
                    break
    return first_json_quote(payload, POLICY_JSON_KEYS)


def claims_json_quote(payload: dict | None) -> str | None:
    """The claims member that carries the fact.

    A duplicate delivery count is quoted ahead of an empty claims list.
    A non-empty claims list is quoted ahead of the loss reference.
    """
    if not payload:
        return None
    deliveries = payload.get("intake_deliveries")
    existing = payload.get("existing_claims")
    duplicate = isinstance(deliveries, int) and not isinstance(deliveries, bool) and deliveries > 1
    if duplicate:
        keys = ("intake_deliveries", "existing_claims", "loss_reference", "source_id")
    elif isinstance(existing, list) and existing:
        keys = ("existing_claims", "loss_reference", "intake_deliveries", "source_id")
    else:
        keys = ("loss_reference", "intake_deliveries", "source_id")
    return first_json_quote(payload, keys)


def _usable(line: str) -> bool:
    text = line.strip()
    if not text or text.startswith("#") or text.startswith("Source ID:") or text.startswith("SYSTEM:"):
        return False
    return not contains_restricted(line)


def first_safe_line(raw: str) -> str | None:
    for line in raw.splitlines():
        if _usable(line):
            return line
    return None


def intake_quote(raw: str | None, account: str | None) -> str | None:
    """Account prose when it sits inside the stored file; otherwise a safe line."""
    if not raw:
        return None
    if account and not contains_restricted(account) and account in raw:
        return account
    if account:
        for part in account.splitlines():
            if part and not contains_restricted(part) and part in raw:
                return part
    return first_safe_line(raw)


def _continues(line: str) -> bool:
    text = line.lstrip()
    return bool(text) and text[0].islower() and _usable(line)


def _with_continuation(lines: list[str], index: int) -> str:
    """The heading line plus the wrapped sentence that follows it."""
    chunk = [lines[index]]
    cursor = index + 1
    while cursor < len(lines) and len(chunk) < 3 and _continues(lines[cursor]):
        chunk.append(lines[cursor])
        cursor += 1
    return "\n".join(chunk)


def procedure_quote(source_id: str, text: str, loss_type: str | None = None) -> str | None:
    """An exact excerpt of the wording that applies, not the form stamp.

    AUTO-TRIAGE-01 contributes the supported-losses line. That file names the
    restricted marker later, and that sentence is never selected. A wording
    file contributes the section for the loss type, including a wrapped
    continuation. An endorsement contributes the sentence that says what the
    endorsement changes.
    """
    lines = text.splitlines()
    if source_id == "AUTO-TRIAGE-01":
        for line in lines:
            if line.startswith("Supported losses:") and _usable(line):
                return line
    if any(line.startswith("Endorsement:") for line in lines):
        for index, line in enumerate(lines):
            if "this endorsement modifies" in line and _usable(line):
                return _with_continuation(lines, index)
    prefix = _SECTION_PREFIX.get(loss_type) if isinstance(loss_type, str) else None
    if prefix:
        for index, line in enumerate(lines):
            if line.startswith(prefix) and _usable(line):
                return _with_continuation(lines, index)
    for index, line in enumerate(lines):
        if line.startswith("Use the individual policy's effective interval") and _usable(line):
            return _with_continuation(lines, index)
    for line in lines:
        if _usable(line):
            return line
    return None


def repair_estimate_quote(raw: str | None) -> str | None:
    """The estimate line that shows a second amount, else the labeled repair amount.

    Instruction lines (``SYSTEM:``) are not evidence of the amount.
    """
    if not raw:
        return None
    usable = [line for line in raw.splitlines() if _usable(line)]
    for line in usable:
        if "XCR" in line and not line.startswith("Repair amount:"):
            return line
    for line in usable:
        if line.startswith("Repair amount:"):
            return line
    for line in usable:
        if line.startswith("Estimate reference:"):
            return line
    return None


def repeated_label_quotes(revision: dict) -> list[tuple[str, str]]:
    """Every safe labeled line from a document that repeated a field.

    Rules already treat the repeated name as a conflict. The second value is
    only in the raw file, so the quote has to come from there.
    """
    found: list[tuple[str, str]] = []
    names = revision.get("repeated_fields") or ()
    for name in names:
        for raw_key, sid_key, prefix in _REPEATED_LABELS.get(name, ()):
            raw = revision.get(raw_key) or ""
            source_id = revision.get(sid_key)
            labeled = [line for line in raw.splitlines() if line.startswith(prefix)]
            if len(labeled) < 2 or not isinstance(source_id, str):
                continue
            for line in labeled:
                if not contains_restricted(line):
                    found.append((source_id, line))
    return found


def conflict_quotes(revision: dict) -> list[tuple[str, str]]:
    """Exact labeled lines from every source that disagrees on a fact."""
    found: list[tuple[str, str]] = []
    for group in _CONFLICT_GROUPS:
        present = []
        for field, raw_key, sid_key, prefix in group:
            value = revision.get(field)
            if value is None or value == "":
                continue
            present.append((str(value), raw_key, sid_key, prefix))
        if len({item[0] for item in present}) < 2:
            continue
        for value, raw_key, sid_key, prefix in present:
            source_id = revision.get(sid_key)
            raw = revision.get(raw_key) or ""
            line = prefix + value
            if isinstance(source_id, str) and line in raw.splitlines() and not contains_restricted(line):
                found.append((source_id, line))
    return found


def missing_field_quotes(revision: dict, missing_information: list[str]) -> list[tuple[str, str]]:
    """The intake line that shows each required field was not supplied.

    A field the parser never found has no line to quote; the omission stays
    visible through the route decision instead.
    """
    found: list[tuple[str, str]] = []
    raw = revision.get("raw_intake") or ""
    source_id = revision.get("intake_source")
    if not isinstance(source_id, str) or not source_id:
        return found
    lines = raw.splitlines()
    for name in missing_information:
        prefix = _MISSING_LABELS.get(name)
        if prefix is None:
            continue
        for line in lines:
            if line.startswith(prefix) and _usable(line):
                found.append((source_id, line))
                break
    return found


def _tool_source_id(payload: dict, case_id: str, kind: str) -> str:
    source_id = payload.get("source_id")
    if isinstance(source_id, str) and source_id:
        return source_id
    return f"{case_id}-{kind}"


def build_citations(
    *,
    case_id: str,
    revision: dict,
    policy_ids: list[str],
    procedure_text: dict[str, str],
    policy_payload: dict | None,
    claims_payload: dict | None,
    selected_policy_version: str | None = None,
    missing_information: list[str] | None = None,
) -> list[dict]:
    """Citations for one case.

    Each quote is refused at the point it is added when it contains a
    restricted marker. ``seal`` checks the finished list again.

    ``missing_information`` is the route decision's list. Each entry is
    grounded by the intake line that says the field was not supplied.

    Report and estimate are included only when they are relevant: the estimate
    when ``referral_indicator`` is ``repair_amount_conflict``, and each
    disagreeing source when vehicle, timestamp, or loss type conflicts.
    A repeated label inside one file is quoted the same way. Claims are
    omitted when ``claims_payload`` is missing.
    """
    citations: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(claim: str, source_id: str | None, quote: str | None) -> None:
        if not source_id or not quote:
            return
        quote = reject_restricted_quote(quote)
        key = (source_id, quote)
        if key in seen:
            return
        seen.add(key)
        citations.append({"claim": claim, "source_id": source_id, "quote": quote})

    loss_type = revision.get("loss_type")
    loss_type = loss_type if isinstance(loss_type, str) else None
    for source_id in policy_ids:
        text = procedure_text.get(source_id)
        if text:
            add(CLAIM_PROCEDURE, source_id, procedure_quote(source_id, text, loss_type))

    add(
        CLAIM_INCIDENT,
        revision.get("intake_source"),
        intake_quote(revision.get("raw_intake"), revision.get("account_text")),
    )
    for source_id, quote in missing_field_quotes(revision, missing_information or []):
        add(CLAIM_MISSING, source_id, quote)
    for source_id, quote in conflict_quotes(revision):
        add(CLAIM_CONFLICT, source_id, quote)
    for source_id, quote in repeated_label_quotes(revision):
        add(CLAIM_CONFLICT, source_id, quote)
    if revision.get("referral_indicator") == "repair_amount_conflict":
        add(
            CLAIM_REPAIR,
            revision.get("estimate_source_id"),
            repair_estimate_quote(revision.get("raw_estimate")),
        )
    if policy_payload:
        add(
            CLAIM_HISTORY,
            _tool_source_id(policy_payload, case_id, "POLICY"),
            policy_json_quote(policy_payload, selected_policy_version),
        )
    if claims_payload:
        add(
            CLAIM_HISTORY,
            _tool_source_id(claims_payload, case_id, "CLAIMS"),
            claims_json_quote(claims_payload),
        )
    return citations
