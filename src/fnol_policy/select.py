"""Select the single policy interval that contains the incident timestamp.

Intervals include effective_from and exclude effective_to. Zero matches or more
than one match leave selected_policy_version null. A wording file or endorsement
that intake did not load is omitted from policy_ids. This module does not guess
a version and does not read the network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

PROCEDURE_ID = "AUTO-TRIAGE-01"


def parse_utc(value: object) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp. Return None when the text is not usable."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def select_policy(
    incident_timestamp: object,
    payload: dict[str, Any],
    known_sources: set[str] | None = None,
) -> dict[str, Any]:
    """Return the decision body for one redacted policy payload.

    known_sources is the set of procedure ids intake stored. When it is provided,
    a wording_source or endorsement outside that set is listed in missing_sources
    and left out of policy_ids. The matched version is still recorded.
    """
    source_id = _text(payload.get("source_id"))
    policy_number = _text(payload.get("policy_number"))
    vehicle_id = _text(payload.get("vehicle_id"))
    policies = payload.get("policies")
    if not isinstance(policies, list):
        return _body(None, False, "malformed", None, [], source_id, policy_number, vehicle_id, [])

    incident = parse_utc(incident_timestamp)
    if incident is None:
        return _body(None, True, "unparsed", None, [], source_id, policy_number, vehicle_id, [])

    matches: list[dict[str, Any]] = []
    for item in policies:
        if not isinstance(item, dict):
            return _body(None, False, "malformed", None, [], source_id, policy_number, vehicle_id, [])
        start = parse_utc(item.get("effective_from"))
        end = parse_utc(item.get("effective_to"))
        if start is None or end is None:
            return _body(None, False, "malformed", None, [], source_id, policy_number, vehicle_id, [])
        if start <= incident < end:
            matches.append(item)

    if len(matches) == 0:
        return _body(None, True, "zero", None, [], source_id, policy_number, vehicle_id, [])
    if len(matches) > 1:
        return _body(None, True, "ambiguous", None, [], source_id, policy_number, vehicle_id, [])

    chosen = matches[0]
    version = chosen.get("version")
    wording = chosen.get("wording_source")
    endorsements = chosen.get("endorsements")
    if not isinstance(version, str) or not version:
        return _body(None, False, "malformed", None, [], source_id, policy_number, vehicle_id, [])
    if wording is not None and not isinstance(wording, str):
        return _body(None, False, "malformed", None, [], source_id, policy_number, vehicle_id, [])
    if not isinstance(endorsements, list) or not all(isinstance(item, str) for item in endorsements):
        return _body(None, False, "malformed", None, [], source_id, policy_number, vehicle_id, [])

    cited = [item for item in [wording, *endorsements] if item]
    missing = [item for item in cited if known_sources is not None and item not in known_sources]
    if missing:
        return _body(
            version, False, "wording_missing", wording, list(endorsements),
            source_id, policy_number, vehicle_id, missing,
        )
    return _body(
        version, True, "matched", wording, list(endorsements),
        source_id, policy_number, vehicle_id, [],
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _body(
    version,
    available,
    reason,
    wording,
    endorsements,
    source_id,
    policy_number,
    vehicle_id,
    missing_sources,
) -> dict[str, Any]:
    cited = [item for item in [wording, *endorsements] if item]
    if missing_sources or version is None or not available:
        policy_ids = [PROCEDURE_ID]
    else:
        policy_ids = [PROCEDURE_ID, *cited]
    return {
        "selected_policy_version": version,
        "policy_available": available,
        "reason": reason,
        "wording_source": wording,
        "endorsements": endorsements,
        "source_id": source_id,
        "policy_number": policy_number,
        "vehicle_id": vehicle_id,
        "policy_ids": policy_ids,
        "missing_sources": list(missing_sources),
    }
