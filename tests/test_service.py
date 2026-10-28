"""run_rules writes claims, the route decision, and one trace row per attempt."""

import sqlite3
import tempfile

from fnol_foundation.canonical import canonical
from fnol_foundation.stores import Stores
from fnol_rules.claims import ClaimsAttempt, ClaimsFetchResult
from fnol_rules.service import run_rules
from fnol_rules.types import ClaimsSnapshot, PolicyResult, StructuredFields


def _fields() -> StructuredFields:
    return StructuredFields(
        loss_type="collision",
        vehicle_id="VEH-1",
        incident_timestamp="2026-10-01T12:00:00Z",
        driver_name="Alex",
        incident_location="Cedar",
        injury_reported=False,
        vehicle_drivable=True,
        injection_field_status="clean",
        injection_model_status="clean",
    )


def _events(stores, case_id):
    with stores.trace.connect() as db:
        return db.execute(
            """SELECT event, step, status, attempt, mock_http_status, input_revision, trace_id
               FROM trace_events WHERE case_id = ? ORDER BY rowid""",
            (case_id,),
        ).fetchall()


def test_stores_canonical_claims_and_logs_attempts():
    raw = {
        "source_id": "AUTO-D01-CLAIMS",
        "existing_claims": [],
        "intake_deliveries": 1,
        "loss_reference": "auto-loss-D01",
    }

    def fake_fetch(case_id, base_url, **_kw):
        return ClaimsFetchResult(
            snapshot=ClaimsSnapshot(existing_claims=[], intake_deliveries=1, available=True),
            attempts=(ClaimsAttempt(attempt=1, status=200, body=raw, duration_ms=4),),
            raw_json=raw,
        )

    with tempfile.TemporaryDirectory() as tmp:
        stores = Stores(tmp)
        revision = stores.input.append_revision(
            "AUTO-D01", {"loss_type": "collision"}, "sha-a", trace_id="trace-1"
        )
        result = run_rules(
            "AUTO-D01",
            _fields(),
            PolicyResult(selected_policy_version="2", payload_sha256="policy-sha"),
            base_url="http://example",
            stores=stores,
            input_revision=revision.revision,
            trace_id="trace-1",
            fetch=fake_fetch,
        )
        stored = stores.input.get_tool_payload("AUTO-D01", "claims", revision.revision)
        decision = stores.input.get_decision("AUTO-D01", revision.revision, "route")
        events = _events(stores, "AUTO-D01")

    assert result.route == "standard"
    assert canonical(stored["raw_json"]) == canonical(raw)
    assert "private_token" not in stored["raw_json"]
    assert decision["body"]["route"] == "standard"
    assert decision["claims_sha256"] == stored["sha256"]
    assert decision["policy_sha256"] == "policy-sha"
    assert [row["event"] for row in events] == [
        "tool_read",
        "claims_stored",
        "injection_check",
        "route_decision",
    ]
    assert events[0]["attempt"] == 1
    assert events[0]["mock_http_status"] == 200
    assert events[0]["input_revision"] == 1
    assert events[0]["trace_id"] == "trace-1"
    assert all("private_token" not in row["event"] for row in events)


def test_outage_skips_store_logs_each_attempt():
    def fake_fetch(case_id, base_url, **_kw):
        attempts = tuple(
            ClaimsAttempt(
                attempt=i,
                status=503,
                body={"error": "upstream_timeout", "retryable": True},
                duration_ms=2,
            )
            for i in range(1, 4)
        )
        return ClaimsFetchResult(
            snapshot=ClaimsSnapshot(available=False, existing_claims=[], intake_deliveries=0),
            attempts=attempts,
            raw_json=None,
        )

    with tempfile.TemporaryDirectory() as tmp:
        stores = Stores(tmp)
        revision = stores.input.append_revision(
            "AUTO-D07", {"loss_type": "collision"}, "sha-b", trace_id="trace-7"
        )
        result = run_rules(
            "AUTO-D07",
            _fields(),
            PolicyResult(selected_policy_version="2"),
            base_url="http://example",
            stores=stores,
            input_revision=revision.revision,
            trace_id="trace-7",
            fetch=fake_fetch,
        )
        stored = stores.input.get_tool_payload("AUTO-D07", "claims", revision.revision)
        decision = stores.input.get_decision("AUTO-D07", revision.revision, "route")
        events = _events(stores, "AUTO-D07")

    assert result.route == "manual_review"
    assert result.selected_policy_version == "2"
    assert "claims_unavailable" in result.reasons
    assert stored is None
    assert decision["body"]["route"] == "manual_review"
    assert decision["claims_sha256"] is None
    reads = [row for row in events if row["event"] == "tool_read"]
    assert len(reads) == 3
    assert [row["attempt"] for row in reads] == [1, 2, 3]
    assert events[-1]["event"] == "route_decision"
