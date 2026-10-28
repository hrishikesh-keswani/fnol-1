"""Slice exact quotes for one input revision and store the citations decision.

Quotes come from the revision's raw text, the policy and claims snapshots for
that same revision, and the wording files intake hashed. The trace stores a
hash of the quotes. File text is not written to the trace.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from fnol_citations.guard import seal
from fnol_citations.slice import build_citations
from fnol_foundation.canonical import RestrictedValueError, digest
from fnol_foundation.events import record_event
from fnol_foundation.stores import Stores
from fnol_intake.parse import file_sha256, read_source
from fnol_policy.select import PROCEDURE_ID

STEP = "4"
ACTOR = "citations"
EVENT = "citations"


class CitationError(Exception):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def load_procedure_text(policies_dir: Path, policy_ids: list[str], procedures: list[dict]):
    """Read wording files for the selected ids.

    When intake recorded a sha256, a file that no longer matches is not quoted.
    The returned text is used only to slice one line; it is not stored.
    """
    known = {row["source_id"]: row["sha256"] for row in procedures}
    texts: dict[str, str] = {}
    missing: list[str] = []
    for source_id in policy_ids:
        found = read_source(policies_dir, source_id)
        if found is None:
            missing.append(source_id)
            continue
        _path, data, text = found
        expected = known.get(source_id)
        if expected is not None and file_sha256(data) != expected:
            missing.append(source_id)
            continue
        texts[source_id] = text
    return texts, missing


def run_citations(
    case_id: str,
    stores: Stores,
    *,
    policies_dir: Path,
    trace_id: str | None = None,
) -> dict:
    """Slice citations for one case from its latest revision and tool snapshots.

    Requires the policy decision and the route decision for that revision.
    A claims snapshot that rules stored as unavailable produces no claims
    citation. Citing before rules have run is refused, so a missing claims
    row is not frozen as if the service had failed.
    """
    started = time.perf_counter()
    revision = stores.input.get_revision(case_id)
    if revision is None:
        _event(
            stores,
            trace_id=trace_id or uuid.uuid4().hex,
            case_id=case_id,
            revision=None,
            citations=None,
            started=started,
            status="error",
            error_code="revision_missing",
        )
        raise CitationError("revision_missing", f"no input revision for {case_id}")

    trace_id = revision.trace_id or trace_id or uuid.uuid4().hex
    decision = stores.input.get_decision(case_id, revision.revision, "policy")
    if decision is None:
        _event(
            stores,
            trace_id=trace_id,
            case_id=case_id,
            revision=revision,
            citations=None,
            started=started,
            status="error",
            error_code="policy_decision_missing",
        )
        raise CitationError(
            "policy_decision_missing",
            f"no policy decision for {case_id} revision {revision.revision}",
        )
    policy_ids = decision["body"].get("policy_ids")
    if not isinstance(policy_ids, list) or not all(isinstance(item, str) and item for item in policy_ids):
        _event(
            stores,
            trace_id=trace_id,
            case_id=case_id,
            revision=revision,
            citations=None,
            started=started,
            status="error",
            error_code="policy_ids_missing",
        )
        raise CitationError("policy_ids_missing", f"policy decision for {case_id} has no policy_ids")

    route = stores.input.get_decision(case_id, revision.revision, "route")
    if route is None:
        _event(
            stores,
            trace_id=trace_id,
            case_id=case_id,
            revision=revision,
            citations=None,
            started=started,
            status="error",
            error_code="route_decision_missing",
        )
        raise CitationError(
            "route_decision_missing",
            f"no route decision for {case_id} revision {revision.revision}",
        )
    version = decision["body"].get("selected_policy_version")
    if not isinstance(version, str) or not version:
        version = None
    missing_information = route["body"].get("missing_information")
    if not isinstance(missing_information, list):
        missing_information = []
    missing_information = [item for item in missing_information if isinstance(item, str)]

    policy_row = stores.input.get_tool_payload(case_id, "policy", revision.revision)
    claims_row = stores.input.get_tool_payload(case_id, "claims", revision.revision)
    policy_payload = None if policy_row is None else policy_row["raw_json"]
    claims_payload = None if claims_row is None else claims_row["raw_json"]
    texts, missing_ids = load_procedure_text(
        policies_dir, policy_ids, stores.input.list_procedures(),
    )

    try:
        citations = seal(build_citations(
            case_id=case_id,
            revision=revision.payload,
            policy_ids=policy_ids,
            procedure_text=texts,
            policy_payload=policy_payload,
            claims_payload=claims_payload,
            selected_policy_version=version,
            missing_information=missing_information,
        ))
    except RestrictedValueError:
        _event(
            stores,
            trace_id=trace_id,
            case_id=case_id,
            revision=revision,
            citations=None,
            started=started,
            status="error",
            error_code="restricted_quote",
        )
        raise CitationError("restricted_quote", "restricted value refused") from None

    omissions = _omissions(
        revision.payload, citations, missing_ids, policy_payload, claims_payload,
    )
    body = {"policy_ids": list(policy_ids), "citations": citations}
    if omissions:
        body["omissions"] = omissions
    stores.input.put_decision(
        case_id,
        revision.revision,
        "citations",
        body,
        policy_sha256=None if policy_row is None else policy_row["sha256"],
        claims_sha256=None if claims_row is None else claims_row["sha256"],
    )
    _event(
        stores,
        trace_id=trace_id,
        case_id=case_id,
        revision=revision,
        citations=citations,
        started=started,
        status="ok",
    )
    return {"case_id": case_id, "input_revision": revision.revision, **body}


def _omissions(payload, citations, missing_ids, policy_payload, claims_payload):
    found = []
    if claims_payload is None:
        found.append({"tool": "claims", "reason": "unavailable"})
    if policy_payload is None:
        found.append({"tool": "policy", "reason": "unavailable"})
    for source_id in missing_ids:
        found.append({"source_id": source_id, "reason": "wording_unavailable"})
    intake_source = payload.get("intake_source")
    if intake_source and not _cited(citations, intake_source):
        found.append({"source_id": intake_source, "reason": "no_safe_excerpt"})
    for tool_payload in (policy_payload, claims_payload):
        if tool_payload is None:
            continue
        source_id = tool_payload.get("source_id")
        if isinstance(source_id, str) and source_id and not _cited(citations, source_id):
            found.append({"source_id": source_id, "reason": "no_safe_excerpt"})
    return found


def _cited(citations, source_id) -> bool:
    return isinstance(source_id, str) and any(item["source_id"] == source_id for item in citations)


def _event(stores, *, trace_id, case_id, revision, citations, started, status, error_code=None):
    record_event(
        stores,
        trace_id=trace_id,
        step=STEP,
        event=EVENT,
        actor=ACTOR,
        rule_version=PROCEDURE_ID,
        case_id=case_id,
        input_revision=None if revision is None else revision.revision,
        input_sha256=None if revision is None else revision.document_set_sha256,
        output_sha256=None if citations is None else digest(citations),
        duration_ms=int((time.perf_counter() - started) * 1000),
        status=status,
        error_code=error_code,
    )
