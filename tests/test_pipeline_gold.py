"""Intake, policy, and rules against the live mock and the dev gold set."""

import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fake_model import FakeModel
from fnol_foundation.stores import Stores
from fnol_intake.pipeline import start_ingestion
from fnol_policy.pipeline import start_policy
from fnol_rules.pipeline import start_routing

DEV_DOCUMENTS = ROOT / "data" / "dev" / "documents"
DEV_POLICIES = ROOT / "data" / "dev" / "policies"
GOLD = json.loads((ROOT / "feedback" / "dev_gold.json").read_text())
# dev_gold.json still says standard for AUTO-D08; its instruction line forces manual_review.
ROUTE_OVERRIDES = {"AUTO-D08": "manual_review"}


class PipelineGoldTests(unittest.TestCase):
    def test_dev_cases_match_gold_routes_versions_and_duplicates(self):
        port = _free_port()
        fnol_state = tempfile.TemporaryDirectory()
        mock_dir = tempfile.TemporaryDirectory()
        process = subprocess.Popen(
            [
                sys.executable,
                "tools/runtime.py",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--state",
                str(Path(mock_dir.name) / "mock.sqlite"),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for_health(port)
            url = f"http://127.0.0.1:{port}"
            start_ingestion(
                mock_url=url,
                documents_dir=DEV_DOCUMENTS,
                policies_dir=DEV_POLICIES,
                state_dir_path=fnol_state.name,
            )
            policies = start_policy(mock_url=url, state_dir_path=fnol_state.name)
            routes = start_routing(
                mock_url=url,
                state_dir_path=fnol_state.name,
                screen_client=FakeModel("clean"),
            )
            by_policy = {row["case_id"]: row for row in policies["policies"]}
            by_route = {row["case_id"]: row for row in routes["routes"]}
            self.assertEqual(set(by_route), set(GOLD))
            stores = Stores(fnol_state.name)
            for case_id, gold in GOLD.items():
                route = by_route[case_id]
                policy = by_policy[case_id]
                expected = ROUTE_OVERRIDES.get(case_id, gold["routing"][0])
                self.assertEqual(route["route"], expected, case_id)
                self.assertEqual(
                    "dangerous_content" in route["reasons"], case_id == "AUTO-D08", case_id
                )
                self.assertEqual(route["classification"], gold["classification"][0], case_id)
                self.assertEqual(
                    route["selected_policy_version"],
                    gold["facts"]["selected_policy_version"],
                    case_id,
                )
                self.assertEqual(route["missing_information"], gold["missing_information"], case_id)
                self.assertEqual(route["duplicate_delivery"], case_id == "AUTO-D10", case_id)
                self.assertEqual(policy["policy_ids"], gold["policy_ids"], case_id)
                self.assertEqual(policy["missing_sources"], [], case_id)
                payload = stores.input.get_tool_payload(case_id, "policy", route["input_revision"])
                self.assertIsNotNone(payload, case_id)
                self.assertNotIn("private_token", payload["raw_json"])
                self.assertIn("policies", payload["raw_json"])
            self.assertEqual(by_policy["AUTO-D05"]["reason"], "ambiguous")
            self.assertTrue(by_policy["AUTO-D05"]["policy_available"])
            self.assertEqual(by_policy["AUTO-D06"]["reason"], "zero")
            self.assertEqual(by_policy["AUTO-D12"]["selected_policy_version"], "1")
            self.assertIn("claims_unavailable", by_route["AUTO-D07"]["reasons"])
            self.assertEqual(by_route["AUTO-D07"]["selected_policy_version"], "2")
            self.assertFalse(by_route["AUTO-D09"]["duplicate_delivery"])
            self._assert_attempt_counts(stores)
            for store in (stores.input, stores.trace, stores.output):
                blob = store.stored_blob()
                self.assertNotIn(b"private_token", blob)
                self.assertNotIn(b"SYNTHETIC-PRIVATE-", blob)
        finally:
            process.terminate()
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            fnol_state.cleanup()
            mock_dir.cleanup()

    def _assert_attempt_counts(self, stores):
        with stores.trace.connect() as db:
            rows = db.execute(
                """SELECT case_id, attempt, mock_http_status, trace_id, input_revision
                   FROM trace_events
                   WHERE step = '3' AND event = 'tool_read'
                   ORDER BY case_id, attempt"""
            ).fetchall()
        by_case = {}
        for case_id, attempt, status, trace_id, revision in rows:
            by_case.setdefault(case_id, []).append((attempt, status, trace_id, revision))
        self.assertEqual([item[1] for item in by_case["AUTO-D07"]], [503, 503, 503])
        self.assertEqual([item[1] for item in by_case["AUTO-D11"]], [503, 200])
        revision = stores.input.get_revision("AUTO-D01")
        self.assertEqual(by_case["AUTO-D01"][0][2], revision.trace_id)
        self.assertEqual(by_case["AUTO-D01"][0][3], revision.revision)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(port):
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


if __name__ == "__main__":
    unittest.main()
