"""Policy outage is a stored decision. Skipping the policy step is an error."""

import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from fnol_foundation.http import HttpAttempt
from fnol_foundation.stores import Stores
from fnol_intake.pipeline import start_ingestion
from fnol_policy.pipeline import start_policy
from fnol_policy.service import run_policy
from fnol_rules.claims import ClaimsAttempt, ClaimsFetchResult
from fnol_rules.pipeline import RulesError, start_routing
from fnol_rules.types import ClaimsSnapshot


def _claims_ok(case_id, base_url, **_kw):
    raw = {"source_id": "X-CLAIMS", "existing_claims": [], "intake_deliveries": 1}
    return ClaimsFetchResult(
        snapshot=ClaimsSnapshot(available=True, existing_claims=[], intake_deliveries=1),
        attempts=(ClaimsAttempt(1, 200, raw),),
        raw_json=raw,
    )


def test_policy_outage_is_manual_review_with_no_invented_version():
    def fetch(case_id, base_url):
        return (HttpAttempt(1, 503, {"error": "upstream_timeout"}, 3),), None

    with tempfile.TemporaryDirectory() as tmp:
        stores = Stores(tmp)
        revision = stores.input.append_revision(
            "AUTO-D01",
            {
                "loss_type": "collision",
                "vehicle_id": "VEH-D01",
                "incident_timestamp": "2026-10-01T12:00:00Z",
                "driver_name": "Alex",
                "incident_location": "Cedar",
                "injury_reported": False,
                "vehicle_drivable": True,
            },
            "sha",
            trace_id="trace-outage",
        )
        decision = run_policy(
            "AUTO-D01",
            "2026-10-01T12:00:00Z",
            base_url="http://mock",
            stores=stores,
            input_revision=revision.revision,
            trace_id="trace-outage",
            fetch=fetch,
        )
        assert decision["reason"] == "unavailable"
        assert decision["policy_available"] is False
        assert decision["selected_policy_version"] is None
        assert stores.input.get_tool_payload("AUTO-D01", "policy", revision.revision) is None
        summary = start_routing(case_id="AUTO-D01", mock_url="http://mock", state_dir_path=tmp, fetch=_claims_ok)

    row = summary["routes"][0]
    assert row["route"] == "manual_review"
    assert row["selected_policy_version"] is None
    assert "policy_unavailable" in row["reasons"]


def test_not_found_is_one_attempt_and_stores_nothing():
    def fetch(case_id, base_url):
        return (HttpAttempt(1, 404, {"error": "not_found"}, 1),), None

    with tempfile.TemporaryDirectory() as tmp:
        stores = Stores(tmp)
        revision = stores.input.append_revision(
            "AUTO-D01", {"loss_type": "collision", "incident_timestamp": "2026-10-01T12:00:00Z"}, "sha", trace_id="t"
        )
        decision = run_policy(
            "AUTO-D01",
            "2026-10-01T12:00:00Z",
            base_url="http://mock",
            stores=stores,
            input_revision=revision.revision,
            trace_id="t",
            fetch=fetch,
        )
        assert decision["reason"] == "not_found"
        assert decision["policy_ids"] == ["AUTO-TRIAGE-01"]
        assert stores.input.get_tool_payload("AUTO-D01", "policy", revision.revision) is None


def test_policy_faults_retry_then_stop():
    root = Path(__file__).resolve().parents[1]
    port = _free_port()
    work = tempfile.TemporaryDirectory()
    try:
        data = Path(work.name) / "data"
        data.mkdir()
        dataset = json.loads((root / "data" / "dev" / "dataset.json").read_text())
        for case in dataset["cases"]:
            if case["case_id"] == "AUTO-D02":
                case["faults"] = {"policy": {"failures": 2}}
            if case["case_id"] == "AUTO-D03":
                case["faults"] = {"policy": {"failures": "always"}}
        (data / "dataset.json").write_text(json.dumps(dataset))
        state = Path(work.name) / "fnol"
        process = subprocess.Popen(
            [
                sys.executable, "tools/runtime.py", "serve",
                "--host", "127.0.0.1", "--port", str(port),
                "--data", str(data),
                "--state", str(Path(work.name) / "mock.sqlite"),
            ],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait(port)
            url = f"http://127.0.0.1:{port}"
            start_ingestion(
                mock_url=url,
                documents_dir=root / "data" / "dev" / "documents",
                policies_dir=root / "data" / "dev" / "policies",
                state_dir_path=state,
            )
            summary = start_policy(mock_url=url, state_dir_path=state)
        finally:
            process.terminate()
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
        by_id = {row["case_id"]: row for row in summary["policies"]}
        assert by_id["AUTO-D02"]["reason"] == "matched"
        assert by_id["AUTO-D02"]["selected_policy_version"] == "2"
        assert by_id["AUTO-D02"]["policy_ids"] == ["AUTO-TRIAGE-01", "AUTO-WORDING-V2"]
        assert by_id["AUTO-D03"]["reason"] == "unavailable"
        assert by_id["AUTO-D03"]["selected_policy_version"] is None
        stores = Stores(state)
        assert stores.input.get_tool_payload("AUTO-D03", "policy", 1) is None
        stored = stores.input.get_tool_payload("AUTO-D02", "policy", 1)
        assert "private_token" not in stored["raw_json"]
        assert "policies" in stored["raw_json"]
        with stores.trace.connect() as db:
            rows = db.execute(
                """SELECT case_id, mock_http_status FROM trace_events
                   WHERE step = '2' AND event = 'tool_read' AND case_id IN ('AUTO-D02', 'AUTO-D03')
                   ORDER BY case_id, rowid"""
            ).fetchall()
        statuses = {}
        for case_id, status in rows:
            statuses.setdefault(case_id, []).append(status)
        assert statuses["AUTO-D02"] == [503, 503, 200]
        assert statuses["AUTO-D03"] == [503, 503, 503]
    finally:
        work.cleanup()


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait(port):
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("mock did not become healthy")


def test_routing_without_a_policy_decision_is_an_error():
    with tempfile.TemporaryDirectory() as tmp:
        stores = Stores(tmp)
        stores.input.append_revision("AUTO-D01", {"loss_type": "collision"}, "sha", trace_id="t")
        try:
            start_routing(case_id="AUTO-D01", mock_url="http://mock", state_dir_path=tmp, fetch=_claims_ok)
        except RulesError as exc:
            assert exc.error_code == "policy_decision_missing"
        else:
            raise AssertionError("expected RulesError")
