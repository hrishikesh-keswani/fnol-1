"""Discover cases from the mock API and store one immutable input revision each."""

import json
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone

from fnol_foundation.canonical import EVIDENCE_TEXT_COLUMNS, RESTRICTED_MARKERS, RestrictedValueError, digest
from fnol_foundation.events import record_event

from fnol_intake import injection
from fnol_intake.parse import (
    document_kind,
    file_sha256,
    labels_for,
    load_procedures,
    parse_document,
    read_source,
    supplied_value,
    yes_no,
)


class IntakeError(Exception):
    def __init__(self, error_code, message):
        super().__init__(message)
        self.error_code = error_code


class DocumentMissing(IntakeError):
    def __init__(self, missing):
        self.missing = missing
        pairs = ", ".join(f"{item['case_id']}:{item['source_id']}" for item in missing)
        super().__init__("document_missing", f"document_missing: {pairs}")


def default_transport(url, timeout=10):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"error": "invalid_json"}
        return exc.code, body


def _url(mock_url, path):
    return mock_url.rstrip("/") + path


def _now():
    return datetime.now(timezone.utc)


def _timed(fetch, url):
    started = time.perf_counter()
    status, body = fetch(url)
    return status, body, int((time.perf_counter() - started) * 1000)


def run_intake(stores, mock_url, documents_dir, policies_dir, transport=None):
    """Load procedure sources once, then write one revision per case id.

    Case ids and document source ids come from GET /v1/cases. A listed source
    with no file fails the run with error_code document_missing and writes no
    revision for that case. A restricted marker in a structured field is dropped.
    The raw file is still stored. One refused case does not drop the others.
    Calling this again inserts the next revision.

    Each revision carries the extra-field list and the deterministic field
    status. The model screen runs on the rules service after this write, so a
    model outage still leaves the revision in place.
    """
    fetch = transport or default_transport
    trace_id = uuid.uuid4().hex
    health_status, health, health_ms = _timed(fetch, _url(mock_url, "/health"))
    health_ok = health_status == 200 and isinstance(health, dict) and health.get("status") == "ok"
    record_event(
        stores,
        trace_id=trace_id,
        step="1",
        event="health",
        actor="intake",
        status="ok" if health_ok else "error",
        error_code=None if health_ok else "health_failed",
        mock_http_status=health_status,
        duration_ms=health_ms,
        started_at=_now(),
        attempt=1,
    )
    if not health_ok:
        raise IntakeError("health_failed", "mock health check failed")

    cases_status, cases_body, cases_ms = _timed(fetch, _url(mock_url, "/v1/cases"))
    cases_ok = cases_status == 200 and isinstance(cases_body, dict) and isinstance(cases_body.get("cases"), list)
    record_event(
        stores,
        trace_id=trace_id,
        step="1",
        event="cases",
        actor="intake",
        status="ok" if cases_ok else "error",
        error_code=None if cases_ok else "cases_unavailable",
        mock_http_status=cases_status,
        duration_ms=cases_ms,
        started_at=_now(),
        attempt=1,
    )
    if not cases_ok:
        raise IntakeError("cases_unavailable", "case list was not available")

    procedures = load_procedures(policies_dir)
    record_event(
        stores,
        trace_id=trace_id,
        step="1",
        event="procedures",
        actor="intake",
        status="ok",
        output_sha256=digest([{"sha256": item["sha256"], "source_id": item["source_id"]} for item in procedures]),
        started_at=_now(),
        attempt=1,
    )

    cases = _unique_cases(cases_body["cases"])
    loaded, missing = _read_cases(cases, documents_dir)
    if missing:
        for item in missing:
            record_event(
                stores,
                trace_id=trace_id,
                step="1",
                event="document_missing",
                actor="intake",
                status="error",
                case_id=item["case_id"],
                error_code="document_missing",
                started_at=_now(),
                attempt=1,
            )
        raise DocumentMissing(missing)

    stores.input.store_procedures(procedures)
    revisions = []
    refused = []
    for item in loaded:
        payload = _scrub_structured(_payload(item))
        started = time.perf_counter()
        try:
            revision = stores.input.append_revision(
                payload["case_id"],
                payload,
                document_set_sha256=payload["document_set_sha256"],
                trace_id=trace_id,
            )
        except RestrictedValueError:
            record_event(
                stores,
                trace_id=trace_id,
                step="1",
                event="revision_refused",
                actor="intake",
                status="error",
                case_id=payload["case_id"],
                error_code="restricted_value",
                started_at=_now(),
                attempt=1,
            )
            refused.append(payload["case_id"])
            continue
        record_event(
            stores,
            trace_id=trace_id,
            step="1",
            event="revision",
            actor="intake",
            status="ok",
            case_id=payload["case_id"],
            input_revision=revision.revision,
            output_sha256=revision.document_set_sha256,
            duration_ms=int((time.perf_counter() - started) * 1000),
            started_at=_now(),
            attempt=1,
        )
        revisions.append(revision)
    return {
        "trace_id": trace_id,
        "revisions": revisions,
        "refused": refused,
        "procedure_sources": procedures,
    }


def _unique_cases(cases):
    ordered = []
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str):
            raise IntakeError("cases_unavailable", "case list entry is missing case_id")
        if not isinstance(case.get("documents"), list) or not isinstance(case.get("intake_source"), str):
            raise IntakeError("cases_unavailable", "case list entry is missing documents or intake_source")
        case_id = case["case_id"]
        if case_id in seen:
            continue
        seen.add(case_id)
        ordered.append(case)
    return ordered


def _read_cases(cases, documents_dir):
    loaded = []
    missing = []
    for case in cases:
        documents = []
        for source_id in case["documents"]:
            found = read_source(documents_dir, source_id) if isinstance(source_id, str) else None
            if found is None:
                missing.append({"case_id": case["case_id"], "source_id": str(source_id)})
                continue
            path, data, text = found
            documents.append({
                "source_id": source_id,
                "sha256": file_sha256(data),
                "raw_text": text,
                "path": path,
            })
        loaded.append({
            "case_id": case["case_id"],
            "intake_source": case["intake_source"],
            "documents": documents,
        })
    return loaded, missing


def _notes(lines):
    if not lines:
        return None
    return "\n".join(lines)


def _restricted(value):
    return isinstance(value, str) and any(marker in value for marker in RESTRICTED_MARKERS)


def _scrub_structured(payload):
    """Drop a restricted marker from structured fields. Raw document text stays.

    A driver name or loss type that is itself the marker is not usable. The file
    text is kept so a later citation can quote the safe lines and skip the marker.
    """
    cleaned = dict(payload)
    for key, value in payload.items():
        if key in EVIDENCE_TEXT_COLUMNS or not _restricted(value):
            continue
        cleaned[key] = None
    if _restricted(payload.get("driver_name")):
        cleaned["driver_name_supplied"] = False
    if _restricted(payload.get("incident_location")):
        cleaned["incident_location_supplied"] = False
    if _restricted(payload.get("injury_reported_raw")):
        cleaned["injury_reported"] = None
    if _restricted(payload.get("vehicle_drivable_raw")):
        cleaned["vehicle_drivable"] = None
    return cleaned


def _payload(item):
    """Return the revision payload.

    Extra-field labels are stored. Leftover lines stay in the note fields for
    the rules screen. They are not a separate payload list.
    """
    parsed_docs = []
    repeated = []
    extra = []
    intake_parsed = None
    intake_doc = None
    estimate = None
    report = None
    for document in item["documents"]:
        kind = document_kind(document["source_id"], item["intake_source"])
        labels = labels_for(kind)
        parsed = parse_document(document["raw_text"], labels)
        for name in parsed["duplicate_fields"]:
            if name not in repeated:
                repeated.append(name)
        if kind in ("intake", "estimate", "report"):
            for label in injection.extra_fields(document["raw_text"], labels):
                extra.append({"source_id": document["source_id"], "label": label})
        entry = {
            "source_id": document["source_id"],
            "kind": kind,
            "sha256": document["sha256"],
            "raw_text": document["raw_text"],
            "fields": parsed["fields"],
            "unlabeled_lines": parsed["unlabeled_lines"],
        }
        parsed_docs.append(entry)
        if kind == "intake" and document["source_id"] == item["intake_source"]:
            intake_parsed = parsed
            intake_doc = entry
        elif kind == "estimate" and estimate is None:
            estimate = entry
        elif kind == "report" and report is None:
            report = entry
    if intake_parsed is None:
        intake_parsed = {"account_text": "", "fields": {}, "duplicate_fields": [], "unlabeled_lines": []}
    fields = intake_parsed["fields"]
    driver_name, driver_supplied = supplied_value(fields.get("driver_name"))
    location, location_supplied = supplied_value(fields.get("incident_location"))
    injury_raw, injury = yes_no(fields.get("injury_reported_raw"))
    drivable_raw, drivable = yes_no(fields.get("vehicle_drivable_raw"))
    estimate_fields = {} if estimate is None else estimate["fields"]
    report_fields = {} if report is None else report["fields"]
    document_set = [{"sha256": doc["sha256"], "source_id": doc["source_id"]} for doc in parsed_docs]
    payload = {
        "case_id": item["case_id"],
        "document_set_sha256": digest(document_set),
        "intake_source": item["intake_source"],
        "account_text": intake_parsed["account_text"],
        "incident_timestamp": fields.get("incident_timestamp"),
        "policy_number": fields.get("policy_number"),
        "vehicle_id": fields.get("vehicle_id"),
        "loss_type": fields.get("loss_type"),
        "driver_name": driver_name,
        "driver_name_supplied": driver_supplied,
        "incident_location": location,
        "incident_location_supplied": location_supplied,
        "injury_reported_raw": injury_raw,
        "injury_reported": injury,
        "vehicle_drivable_raw": drivable_raw,
        "vehicle_drivable": drivable,
        "referral_indicator": fields.get("referral_indicator"),
        "loss_reference": fields.get("loss_reference"),
        "intake_sha256": None if intake_doc is None else intake_doc["sha256"],
        "raw_intake": None if intake_doc is None else intake_doc["raw_text"],
        "intake_notes": _notes(intake_parsed["unlabeled_lines"]),
        "estimate_source_id": None if estimate is None else estimate["source_id"],
        "estimate_sha256": None if estimate is None else estimate["sha256"],
        "estimate_reference": estimate_fields.get("estimate_reference"),
        "estimate_vehicle": estimate_fields.get("vehicle"),
        "repair_amount": estimate_fields.get("repair_amount"),
        "estimate_prepared_by": estimate_fields.get("prepared_by"),
        "estimate_notes": None if estimate is None else _notes(estimate["unlabeled_lines"]),
        "raw_estimate": None if estimate is None else estimate["raw_text"],
        "report_source_id": None if report is None else report["source_id"],
        "report_sha256": None if report is None else report["sha256"],
        "report_vehicle_reference": report_fields.get("vehicle_reference"),
        "report_incident_timestamp": report_fields.get("incident_timestamp"),
        "reported_loss_type": report_fields.get("reported_loss_type"),
        "report_notes": None if report is None else _notes(report["unlabeled_lines"]),
        "raw_report": None if report is None else report["raw_text"],
        "repeated_fields": repeated,
        "extra_fields": extra,
        "injection_field_status": injection.instruction_status(
            *(document["raw_text"] for document in item["documents"])
        ),
    }
    return payload
