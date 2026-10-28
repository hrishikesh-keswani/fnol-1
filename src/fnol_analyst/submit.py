"""Steps 9-12: review submission, human boundary, and record creation.

Reads the stored draft only. Does not rebuild. Does not invoke the operator
approve command. Idempotency-Key for record creation is the local draft_id.
"""

from __future__ import annotations

import copy
import time
from typing import Any, Callable

from fnol_foundation.canonical import RestrictedValueError, assert_no_restricted, digest
from fnol_foundation.events import record_event
from fnol_foundation.stores import DraftRecord, Stores
from fnol_shadow.safety import assess

ACTOR = "analyst"

Transport = Callable[[str, str, dict | None, dict[str, str] | None], tuple[int, dict[str, Any]]]


class SubmitError(Exception):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def submit_review(
    stores: Stores,
    *,
    case_id: str,
    base_url: str,
    transport: Transport,
    trace_id: str | None = None,
) -> DraftRecord:
    """Step 9. POST the stored draft; continue only when the returned hash matches."""
    record = _require_draft(stores, case_id)
    started = time.perf_counter()
    status, body = transport(
        "POST",
        f"{base_url.rstrip('/')}/v1/reviews",
        {"case_id": case_id, "draft": record.draft},
        None,
    )
    duration_ms = int((time.perf_counter() - started) * 1000)
    if status != 201:
        _event(
            stores,
            step="9",
            event="review_requested",
            case_id=case_id,
            draft_id=record.draft_id,
            input_revision=record.input_revision,
            trace_id=trace_id,
            status="error",
            error_code=_error_code(body),
            mock_http_status=status,
            duration_ms=duration_ms,
        )
        raise SubmitError(_error_code(body), f"review request failed with HTTP {status}")
    returned = body.get("draft_hash")
    review_id = body.get("review_id")
    if not isinstance(returned, str) or not isinstance(review_id, str):
        _event(
            stores,
            step="9",
            event="review_requested",
            case_id=case_id,
            draft_id=record.draft_id,
            input_revision=record.input_revision,
            trace_id=trace_id,
            status="error",
            error_code="bad_review_response",
            mock_http_status=status,
            duration_ms=duration_ms,
        )
        raise SubmitError("bad_review_response", "review response missing draft_hash or review_id")
    if returned != record.draft_hash:
        updated = stores.output.update_status(record.draft_id, "recorded", outcome="hash_mismatch")
        _event(
            stores,
            step="9",
            event="review_requested",
            case_id=case_id,
            draft_id=record.draft_id,
            input_revision=record.input_revision,
            trace_id=trace_id,
            status="error",
            error_code="hash_mismatch",
            mock_http_status=status,
            duration_ms=duration_ms,
            output_sha256=record.draft_hash,
        )
        return updated
    updated = stores.output.update_status(
        record.draft_id, "in_review", review_id=review_id
    )
    _event(
        stores,
        step="9",
        event="review_requested",
        case_id=case_id,
        draft_id=record.draft_id,
        input_revision=record.input_revision,
        trace_id=trace_id,
        status="ok",
        mock_http_status=status,
        duration_ms=duration_ms,
        output_sha256=record.draft_hash,
    )
    return updated


def poll_review(
    stores: Stores,
    *,
    case_id: str,
    base_url: str,
    transport: Transport,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Step 10. Poll the human decision. Never approves."""
    record = _require_draft(stores, case_id)
    if not record.review_id:
        raise SubmitError("review_missing", f"no review_id on draft for {case_id}")
    started = time.perf_counter()
    status, body = transport(
        "GET",
        f"{base_url.rstrip('/')}/v1/reviews/{record.review_id}",
        None,
        None,
    )
    duration_ms = int((time.perf_counter() - started) * 1000)
    if status != 200:
        _event(
            stores,
            step="10",
            event="review_polled",
            case_id=case_id,
            draft_id=record.draft_id,
            input_revision=record.input_revision,
            trace_id=trace_id,
            status="error",
            error_code=_error_code(body),
            mock_http_status=status,
            duration_ms=duration_ms,
        )
        raise SubmitError(_error_code(body), f"review poll failed with HTTP {status}")

    decision = body.get("status")
    remote_hash = body.get("hash")
    if decision == "rejected":
        updated = stores.output.update_status(record.draft_id, "not_submitted", outcome="rejected")
        _event(
            stores,
            step="10",
            event="review_polled",
            case_id=case_id,
            draft_id=record.draft_id,
            input_revision=record.input_revision,
            trace_id=trace_id,
            status="ok",
            error_code="rejected",
            mock_http_status=status,
            duration_ms=duration_ms,
        )
        return {
            "draft_id": updated.draft_id,
            "status": updated.status,
            "outcome": updated.outcome,
            "review_id": updated.review_id,
            "human_command": None,
        }
    if decision == "pending":
        _event(
            stores,
            step="10",
            event="review_polled",
            case_id=case_id,
            draft_id=record.draft_id,
            input_revision=record.input_revision,
            trace_id=trace_id,
            status="ok",
            error_code="pending",
            mock_http_status=status,
            duration_ms=duration_ms,
        )
        return {
            "draft_id": record.draft_id,
            "status": record.status,
            "outcome": record.outcome,
            "review_id": record.review_id,
            "human_command": (
                f"uv run --locked python tools/runtime.py approve {record.review_id} "
                "--reviewer NAME"
            ),
        }
    if decision == "approved":
        if remote_hash != record.draft_hash:
            updated = stores.output.update_status(
                record.draft_id, "in_review", outcome="approval_mismatch"
            )
            _event(
                stores,
                step="10",
                event="review_polled",
                case_id=case_id,
                draft_id=record.draft_id,
                input_revision=record.input_revision,
                trace_id=trace_id,
                status="error",
                error_code="approval_mismatch",
                mock_http_status=status,
                duration_ms=duration_ms,
            )
            return {
                "draft_id": updated.draft_id,
                "status": updated.status,
                "outcome": updated.outcome,
                "review_id": updated.review_id,
                "human_command": None,
            }
        updated = stores.output.update_status(record.draft_id, "approved", outcome="approved")
        _event(
            stores,
            step="10",
            event="review_polled",
            case_id=case_id,
            draft_id=record.draft_id,
            input_revision=record.input_revision,
            trace_id=trace_id,
            status="ok",
            mock_http_status=status,
            duration_ms=duration_ms,
            output_sha256=record.draft_hash,
        )
        return {
            "draft_id": updated.draft_id,
            "status": updated.status,
            "outcome": updated.outcome,
            "review_id": updated.review_id,
            "human_command": None,
        }
    _event(
        stores,
        step="10",
        event="review_polled",
        case_id=case_id,
        draft_id=record.draft_id,
        input_revision=record.input_revision,
        trace_id=trace_id,
        status="error",
        error_code="unknown_review_status",
        mock_http_status=status,
        duration_ms=duration_ms,
    )
    raise SubmitError("unknown_review_status", f"unexpected review status {decision!r}")


def edit_draft(
    stores: Stores,
    *,
    case_id: str,
    narrative: str | None = None,
    evidence_summary: str | None = None,
    clear_evidence_summary: bool = False,
    trace_id: str | None = None,
) -> DraftRecord:
    """Local edit: new draft_id, new hash. Old approval is void. Does not call Bedrock."""
    if narrative is None and evidence_summary is None and not clear_evidence_summary:
        raise SubmitError("edit_empty", "provide narrative and/or evidence_summary")
    old = _require_draft(stores, case_id)
    draft = copy.deepcopy(old.draft)
    if narrative is not None:
        if not isinstance(narrative, str):
            raise SubmitError("edit_invalid", "narrative must be a string")
        try:
            assert_no_restricted(narrative)
        except RestrictedValueError as exc:
            raise SubmitError("restricted_value", "edited narrative refused") from exc
        if assess(narrative) != "passed":
            raise SubmitError("safety_blocked", "edited narrative failed the safety scan")
        draft["narrative"] = narrative
    if clear_evidence_summary:
        draft["evidence_summary"] = None
        draft["safety_status"] = "skipped"
    elif evidence_summary is not None:
        if not isinstance(evidence_summary, str):
            raise SubmitError("edit_invalid", "evidence_summary must be a string")
        try:
            assert_no_restricted(evidence_summary)
        except RestrictedValueError as exc:
            raise SubmitError("restricted_value", "edited evidence_summary refused") from exc
        if assess(evidence_summary) != "passed":
            raise SubmitError("safety_blocked", "edited evidence_summary failed the safety scan")
        draft["evidence_summary"] = evidence_summary
        draft["safety_status"] = "passed"
    try:
        assert_no_restricted(draft)
    except RestrictedValueError as exc:
        raise SubmitError("restricted_value", "edited draft refused") from exc

    started = time.perf_counter()
    stores.output.update_status(
        old.draft_id, "superseded", outcome="edited", clear_review_id=True
    )
    record = stores.output.record_draft(
        case_id, draft, input_revision=old.input_revision, status="recorded"
    )
    _event(
        stores,
        step="8",
        event="draft_edited",
        case_id=case_id,
        draft_id=record.draft_id,
        input_revision=record.input_revision,
        trace_id=trace_id,
        status="ok",
        duration_ms=int((time.perf_counter() - started) * 1000),
        output_sha256=record.draft_hash,
    )
    return record


def file_record(
    stores: Stores,
    *,
    case_id: str,
    base_url: str,
    transport: Transport,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Steps 11-12. Duplicate check, then POST with Idempotency-Key = draft_id."""
    record = _require_draft(stores, case_id)
    if record.status not in ("approved", "created"):
        raise SubmitError("not_approved", f"draft for {case_id} is not approved")
    if not record.review_id:
        raise SubmitError("review_missing", f"no review_id on approved draft for {case_id}")

    started = time.perf_counter()
    get_status, get_body = transport(
        "GET",
        f"{base_url.rstrip('/')}/v1/records/{case_id}",
        None,
        None,
    )
    get_ms = int((time.perf_counter() - started) * 1000)
    if get_status == 200:
        updated = stores.output.update_status(
            record.draft_id, record.status, outcome="duplicate"
        )
        _event(
            stores,
            step="11",
            event="record_lookup",
            case_id=case_id,
            draft_id=record.draft_id,
            input_revision=record.input_revision,
            trace_id=trace_id,
            status="ok",
            error_code="duplicate",
            mock_http_status=get_status,
            duration_ms=get_ms,
        )
        return {
            "draft_id": updated.draft_id,
            "status": updated.status,
            "outcome": updated.outcome,
            "record_id": get_body.get("id") or get_body.get("record_id"),
            "posted": False,
        }
    if get_status != 404:
        _event(
            stores,
            step="11",
            event="record_lookup",
            case_id=case_id,
            draft_id=record.draft_id,
            input_revision=record.input_revision,
            trace_id=trace_id,
            status="error",
            error_code=_error_code(get_body),
            mock_http_status=get_status,
            duration_ms=get_ms,
        )
        raise SubmitError(_error_code(get_body), f"record lookup failed with HTTP {get_status}")
    _event(
        stores,
        step="11",
        event="record_lookup",
        case_id=case_id,
        draft_id=record.draft_id,
        input_revision=record.input_revision,
        trace_id=trace_id,
        status="ok",
        error_code="not_found",
        mock_http_status=get_status,
        duration_ms=get_ms,
    )

    started = time.perf_counter()
    post_status, post_body = transport(
        "POST",
        f"{base_url.rstrip('/')}/v1/records",
        {
            "case_id": case_id,
            "review_id": record.review_id,
            "draft": record.draft,
        },
        {"Idempotency-Key": record.draft_id},
    )
    post_ms = int((time.perf_counter() - started) * 1000)
    if post_status in (201, 200):
        updated = stores.output.update_status(record.draft_id, "created", outcome="created")
        _event(
            stores,
            step="12",
            event="record_created",
            case_id=case_id,
            draft_id=record.draft_id,
            input_revision=record.input_revision,
            trace_id=trace_id,
            status="ok",
            mock_http_status=post_status,
            duration_ms=post_ms,
            output_sha256=record.draft_hash,
        )
        return {
            "draft_id": updated.draft_id,
            "status": updated.status,
            "outcome": updated.outcome,
            "record_id": post_body.get("record_id"),
            "posted": True,
            "http_status": post_status,
        }
    if post_status == 409:
        code = _error_code(post_body)
        outcome = "duplicate" if code == "duplicate_case" else "idempotency_conflict"
        stores.output.update_status(record.draft_id, "approved", outcome=outcome)
        _event(
            stores,
            step="12",
            event="record_created",
            case_id=case_id,
            draft_id=record.draft_id,
            input_revision=record.input_revision,
            trace_id=trace_id,
            status="error",
            error_code=code,
            mock_http_status=post_status,
            duration_ms=post_ms,
        )
        return {
            "draft_id": record.draft_id,
            "status": "approved",
            "outcome": outcome,
            "record_id": post_body.get("existing_record_id"),
            "posted": False,
            "http_status": post_status,
        }
    if post_status == 403:
        stores.output.update_status(record.draft_id, "approved", outcome="approval_mismatch")
        _event(
            stores,
            step="12",
            event="record_created",
            case_id=case_id,
            draft_id=record.draft_id,
            input_revision=record.input_revision,
            trace_id=trace_id,
            status="error",
            error_code="approval_mismatch",
            mock_http_status=post_status,
            duration_ms=post_ms,
        )
        return {
            "draft_id": record.draft_id,
            "status": "approved",
            "outcome": "approval_mismatch",
            "record_id": None,
            "posted": False,
            "http_status": post_status,
        }
    _event(
        stores,
        step="12",
        event="record_created",
        case_id=case_id,
        draft_id=record.draft_id,
        input_revision=record.input_revision,
        trace_id=trace_id,
        status="error",
        error_code=_error_code(post_body),
        mock_http_status=post_status,
        duration_ms=post_ms,
    )
    raise SubmitError(_error_code(post_body), f"record create failed with HTTP {post_status}")


def _require_draft(stores: Stores, case_id: str) -> DraftRecord:
    record = stores.output.latest_draft(case_id)
    if record is None:
        raise SubmitError("draft_missing", f"no draft for {case_id}; run build first")
    return record


def _error_code(body: dict[str, Any] | None) -> str:
    if not isinstance(body, dict):
        return "http_error"
    code = body.get("error")
    if isinstance(code, str) and code:
        return code
    return "http_error"


def _event(
    stores: Stores,
    *,
    step: str,
    event: str,
    case_id: str,
    draft_id: str | None,
    input_revision: int | None,
    trace_id: str | None,
    status: str,
    error_code: str | None = None,
    mock_http_status: int | None = None,
    duration_ms: int | None = None,
    output_sha256: str | None = None,
) -> None:
    record_event(
        stores,
        trace_id=trace_id or digest({"case_id": case_id, "draft_id": draft_id}),
        step=step,
        event=event,
        actor=ACTOR,
        status=status,
        case_id=case_id,
        input_revision=input_revision,
        draft_id=draft_id,
        error_code=error_code,
        mock_http_status=mock_http_status,
        duration_ms=duration_ms,
        output_sha256=output_sha256,
    )
