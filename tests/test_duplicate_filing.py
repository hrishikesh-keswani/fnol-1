"""AUTO-D09 creates the claim. AUTO-D10 is flagged and tied to that claim.

The duplicate flag comes from the claims snapshot at routing. Filing consults
the live ledger. Both cases still receive an evaluation object.
"""

import json
import socket
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
from fnol_analyst.client import AnalystClient
from fnol_citations.pipeline import start_citations
from fnol_intake.pipeline import start_ingestion
from fnol_policy.pipeline import start_policy
from fnol_rules.pipeline import start_routing
from tools.evaluate import evaluate
from tools.runtime import connect

DEV_DOCUMENTS = ROOT / "data" / "dev" / "documents"
DEV_POLICIES = ROOT / "data" / "dev" / "policies"
DATASET = json.loads((ROOT / "data" / "dev" / "dataset.json").read_text())
GOLD = json.loads((ROOT / "feedback" / "dev_gold.json").read_text())
PAIR = ("AUTO-D09", "AUTO-D10")


class DuplicateFilingTests(unittest.TestCase):
    def test_d10_links_to_d09_and_keeps_an_evaluation_result(self):
        port = _free_port()
        fnol_state = tempfile.TemporaryDirectory()
        mock_dir = tempfile.TemporaryDirectory()
        export_dir = tempfile.TemporaryDirectory()
        mock_state = Path(mock_dir.name) / "mock.sqlite"
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
                str(mock_state),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        client = AnalystClient(
            mock_url=f"http://127.0.0.1:{port}",
            state_dir_path=fnol_state.name,
            documents_dir=DEV_DOCUMENTS,
            policies_dir=DEV_POLICIES,
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
            start_policy(mock_url=url, state_dir_path=fnol_state.name)
            routes = start_routing(
                mock_url=url,
                state_dir_path=fnol_state.name,
                screen_client=FakeModel("clean"),
            )
            by_route = {row["case_id"]: row for row in routes["routes"]}
            self.assertEqual(by_route["AUTO-D09"]["route"], "standard")
            self.assertEqual(by_route["AUTO-D10"]["route"], "standard")
            self.assertFalse(by_route["AUTO-D09"]["duplicate_delivery"])
            self.assertTrue(by_route["AUTO-D10"]["duplicate_delivery"])
            for case_id in PAIR:
                start_citations(
                    case_id,
                    state_dir_path=fnol_state.name,
                    policies_dir=DEV_POLICIES,
                )

            drafts = {case_id: client.build(case_id) for case_id in PAIR}
            self.assertEqual(drafts["AUTO-D09"].draft["routing"], "standard")
            self.assertEqual(drafts["AUTO-D10"].draft["routing"], "standard")
            self.assertFalse(drafts["AUTO-D09"].draft["duplicate_delivery"])
            self.assertTrue(drafts["AUTO-D10"].draft["duplicate_delivery"])
            self.assertIn("duplicate intake delivery no", drafts["AUTO-D09"].draft["narrative"])
            self.assertIn("duplicate intake delivery yes", drafts["AUTO-D10"].draft["narrative"])

            exported = client.export(Path(export_dir.name) / "results.json", list(PAIR))
            submission = json.loads(exported.read_text())
            report = evaluate(DATASET, {case_id: GOLD[case_id] for case_id in PAIR}, submission)
            self.assertTrue(report["passed"], report)

            created = self._approve_and_file(client, "AUTO-D09", mock_state)
            self.assertEqual(created["outcome"], "created")
            self.assertTrue(created["posted"])
            self.assertEqual(created["record_id"], "auto-loss-nine")

            linked = self._approve_and_file(client, "AUTO-D10", mock_state)
            self.assertEqual(linked["outcome"], "duplicate")
            self.assertFalse(linked["posted"])
            self.assertEqual(linked["record_id"], "auto-loss-nine")

            with connect(mock_state) as db:
                records = db.execute("SELECT id, case_id FROM records").fetchall()
            self.assertEqual([(row["id"], row["case_id"]) for row in records], [("auto-loss-nine", "AUTO-D09")])

            again = json.loads(client.export(exported, list(PAIR)).read_text())
            self.assertEqual([item["case_id"] for item in again], list(PAIR))
            kept = evaluate(DATASET, {case_id: GOLD[case_id] for case_id in PAIR}, again)
            self.assertTrue(kept["passed"], kept)
            self.assertEqual(client.stores.output.latest_draft("AUTO-D10").outcome, "duplicate")
            self.assertEqual(
                client.stores.output.latest_draft("AUTO-D10").draft_hash,
                drafts["AUTO-D10"].draft_hash,
            )
        finally:
            client.close()
            process.terminate()
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            fnol_state.cleanup()
            mock_dir.cleanup()
            export_dir.cleanup()

    def _approve_and_file(self, client, case_id, mock_state):
        submitted = client.submit(case_id)
        completed = subprocess.run(
            [
                sys.executable,
                "tools/runtime.py",
                "approve",
                submitted.review_id,
                "--reviewer",
                "handler",
                "--state",
                str(mock_state),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        polled = client.review_status(case_id)
        self.assertEqual(polled["status"], "approved", polled)
        return client.file(case_id)


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
