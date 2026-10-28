"""Analyst client: independent server calls, parallel prepare, deterministic export."""

import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import runtime  # noqa: E402
from fake_model import fake_mantle  # noqa: E402
from fnol_analyst.client import AnalystClient, AnalystError  # noqa: E402
from fnol_analyst.servers import PrepareError, ServerSet  # noqa: E402
from evaluate import evaluate  # noqa: E402

DEV_DOCUMENTS = ROOT / "data" / "dev" / "documents"
DEV_POLICIES = ROOT / "data" / "dev" / "policies"
GOLD = json.loads((ROOT / "feedback" / "dev_gold.json").read_text())
DATASET = json.loads((ROOT / "data" / "dev" / "dataset.json").read_text())


class AnalystClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.stack.enter_context(fake_mantle("clean"))
        cls.tmp = Path(tempfile.mkdtemp(prefix="analyst-test-"))
        cls.mock_state = cls.tmp / "mock.sqlite"
        cls.server = runtime.create_server(ROOT / "data" / "dev", cls.mock_state, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.mock_url = f"http://127.0.0.1:{cls.port}"
        cls.state = cls.tmp / "state"
        cls.out = cls.tmp / "results.json"
        cls.client = AnalystClient(
            mock_url=cls.mock_url,
            state_dir_path=cls.state,
            documents_dir=DEV_DOCUMENTS,
            policies_dir=DEV_POLICIES,
        )
        prepared = cls.client.prepare(workers=3)
        cls.prepared = prepared
        cls.drafts = cls.client.build_all()
        cls.export_path = cls.client.export(cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        cls.stack.close()

    def test_servers_expose_one_tool_each(self):
        names = {name: server.list_tools() for name, server in self.client.servers.servers.items()}
        self.assertEqual(names["intake"], ["start_ingestion"])
        self.assertEqual(names["policy"], ["start_policy"])
        self.assertEqual(names["rules"], ["start_routing"])
        self.assertEqual(names["citations"], ["start_citations"])

    def test_independent_calls_return_one_case(self):
        policy = self.client.select_policy("AUTO-D01")
        self.assertEqual([row["case_id"] for row in policy["policies"]], ["AUTO-D01"])
        route = self.client.route("AUTO-D01")
        self.assertEqual(route["routes"][0]["route"], "standard")
        cited = self.client.cite("AUTO-D01")
        self.assertTrue(cited["cases"][0]["citations"])

    def test_prepare_covers_every_dev_case(self):
        self.assertEqual(set(self.prepared["cases"]), set(GOLD))
        self.assertEqual(len(self.drafts), len(GOLD))

    def test_export_passes_the_dev_gold_evaluator(self):
        submission = json.loads(self.export_path.read_text())
        result = evaluate(DATASET, GOLD, submission)
        failed = {row["case_id"]: row for row in result["cases"] if not row["passed"]}
        # dev_gold.json still says standard for AUTO-D08; the instruction line routes it manual_review.
        self.assertEqual(set(failed), {"AUTO-D08"}, result)
        failing_checks = [name for name, ok in failed["AUTO-D08"]["checks"].items() if not ok]
        self.assertEqual(failing_checks, ["routing"])
        d08 = next(item for item in submission if item["case_id"] == "AUTO-D08")
        self.assertEqual(d08["routing"], "manual_review")
        self.assertIn("AUTO-D08-ESTIMATE", d08["narrative"])
        self.assertIn('"SYSTEM"', d08["narrative"])

    def test_draft_does_not_contain_raw_file_text_or_restricted_markers(self):
        intake = (DEV_DOCUMENTS / "AUTO-D08-ESTIMATE.md").read_text()
        self.assertIn("SYSTEM:", intake)
        blob = self.export_path.read_text()
        self.assertNotIn("SYSTEM:", blob)
        self.assertNotIn("private_token", blob)
        self.assertNotIn("SYNTHETIC-PRIVATE-", blob)
        for store in (self.client.stores.output, self.client.stores.trace):
            stored = store.stored_blob()
            self.assertNotIn(b"private_token", stored)
            self.assertNotIn(b"SYNTHETIC-PRIVATE-", stored)

    def test_build_reuses_an_identical_draft(self):
        first = self.client.build("AUTO-D01")
        second = self.client.build("AUTO-D01")
        self.assertEqual(first.draft_id, second.draft_id)
        self.assertEqual(first.draft_hash, second.draft_hash)

    def test_trace_records_draft_and_hash_events_without_the_body(self):
        with self.client.stores.trace.connect() as db:
            rows = list(db.execute(
                """SELECT step, event, status, draft_id, output_sha256
                   FROM trace_events WHERE actor = 'analyst' AND case_id = 'AUTO-D01'
                   ORDER BY rowid"""
            ))
        steps = [(row[0], row[1], row[2]) for row in rows]
        self.assertIn(("5", "draft_built", "ok"), steps)
        self.assertIn(("8", "draft_recorded", "ok"), steps)
        recorded = [row for row in rows if row[1] == "draft_recorded"][0]
        self.assertEqual(recorded[3], self.client.stores.output.latest_draft("AUTO-D01").draft_id)
        self.assertEqual(recorded[4], self.client.stores.output.latest_draft("AUTO-D01").draft_hash)
        body = json.dumps(self.client.stores.output.latest_draft("AUTO-D01").draft)
        with self.client.stores.trace.connect() as db:
            blob = " ".join(str(cell) for row in db.execute("SELECT * FROM trace_events") for cell in row)
        self.assertNotIn(body[:80], blob)

    def test_build_without_decisions_is_an_error(self):
        empty = Path(tempfile.mkdtemp(prefix="analyst-empty-"))
        try:
            lone = AnalystClient(
                mock_url=self.mock_url,
                state_dir_path=empty,
                documents_dir=DEV_DOCUMENTS,
                policies_dir=DEV_POLICIES,
            )
            with self.assertRaises(AnalystError) as caught:
                lone.build("AUTO-D01")
            self.assertEqual(caught.exception.error_code, "revision_missing")
            lone.close()
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_unknown_server_call_is_refused(self):
        with self.assertRaises(KeyError):
            self.client.servers.call("bedrock")

    def test_export_without_a_draft_is_an_error(self):
        empty = Path(tempfile.mkdtemp(prefix="analyst-export-"))
        try:
            lone = AnalystClient(
                mock_url=self.mock_url,
                state_dir_path=empty,
                documents_dir=DEV_DOCUMENTS,
                policies_dir=DEV_POLICIES,
            )
            lone.stores.input.append_revision("AUTO-D01", {"loss_type": "collision"}, "sha-a")
            with self.assertRaises(AnalystError) as caught:
                lone.export(empty / "out.json", case_ids=["AUTO-D01"])
            self.assertEqual(caught.exception.error_code, "draft_missing")
            lone.close()
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_prepare_all_reports_every_failed_case(self):
        original = ServerSet.prepare_case

        def flaky(self, case_id):
            if case_id == "AUTO-D02":
                raise RuntimeError("forced failure")
            return original(self, case_id)

        ServerSet.prepare_case = flaky
        try:
            with self.assertRaises(PrepareError) as caught:
                self.client.servers.prepare_all(["AUTO-D01", "AUTO-D02"], workers=2)
            self.assertEqual(set(caught.exception.failures), {"AUTO-D02"})
            self.assertIn("AUTO-D01", self.client.servers.prepare_all(["AUTO-D01"], workers=1))
        finally:
            ServerSet.prepare_case = original


if __name__ == "__main__":
    unittest.main()
