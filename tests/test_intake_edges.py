"""Intake run/parse edge branches."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fnol_foundation.canonical import RestrictedValueError
from fnol_foundation.stores import Stores
from fnol_intake import parse, run
from fnol_intake.run import DocumentMissing, IntakeError, run_intake

ROOT = Path(__file__).resolve().parents[1]
DEV_DOCUMENTS = ROOT / "data" / "dev" / "documents"
DEV_POLICIES = ROOT / "data" / "dev" / "policies"


class ParseHelperTests(unittest.TestCase):
    def test_document_kind_and_labels(self):
        self.assertEqual(parse.document_kind("X-NOTES", "X-INTAKE"), "unknown")
        self.assertEqual(parse.document_kind("A-REPORT", "X-INTAKE"), "report")
        self.assertEqual(parse.document_kind("A-ESTIMATE", "X-INTAKE"), "estimate")
        self.assertEqual(parse.document_kind("whatever", "whatever"), "intake")
        self.assertEqual(parse.labels_for("unknown"), {})
        self.assertIs(parse.labels_for("report"), parse.REPORT_LABELS)
        self.assertIs(parse.labels_for("estimate"), parse.ESTIMATE_LABELS)
        self.assertIs(parse.labels_for("intake"), parse.INTAKE_LABELS)

    def test_yes_no_and_supplied_value(self):
        self.assertEqual(parse.yes_no(None), (None, None))
        self.assertEqual(parse.yes_no(" Yes "), (" Yes ", True))
        self.assertEqual(parse.yes_no("NO"), ("NO", False))
        self.assertEqual(parse.yes_no("maybe"), ("maybe", None))
        self.assertEqual(parse.supplied_value(None), (None, False))
        self.assertEqual(parse.supplied_value("  "), (None, False))
        self.assertEqual(parse.supplied_value("Not Supplied"), ("Not Supplied", False))
        self.assertEqual(parse.supplied_value(" Ada "), ("Ada", True))


class ScrubTests(unittest.TestCase):
    def test_restricted_structured_fields_are_dropped(self):
        payload = {
            "driver_name": "SYNTHETIC-PRIVATE-1",
            "incident_location": "private_token here",
            "injury_reported_raw": "SYNTHETIC-PRIVATE-2",
            "vehicle_drivable_raw": "SYNTHETIC-PRIVATE-3",
            "injury_reported": True,
            "vehicle_drivable": True,
            "raw_intake": "SYNTHETIC-PRIVATE-4 stays as evidence",
            "loss_type": "collision",
        }
        cleaned = run._scrub_structured(payload)
        self.assertIsNone(cleaned["driver_name"])
        self.assertFalse(cleaned["driver_name_supplied"])
        self.assertFalse(cleaned["incident_location_supplied"])
        self.assertIsNone(cleaned["injury_reported"])
        self.assertIsNone(cleaned["vehicle_drivable"])
        self.assertIn("SYNTHETIC-PRIVATE-4", cleaned["raw_intake"])
        self.assertEqual(cleaned["loss_type"], "collision")


class PayloadTests(unittest.TestCase):
    def test_case_without_an_intake_document(self):
        payload = run._payload({"case_id": "C", "intake_source": "C-INTAKE", "documents": []})
        self.assertEqual(payload["case_id"], "C")
        self.assertEqual(payload["account_text"], "")
        self.assertIsNone(payload["policy_number"])

    def test_second_estimate_and_report_are_ignored(self):
        def doc(source_id, text):
            return {"source_id": source_id, "sha256": "x", "raw_text": text}

        payload = run._payload({
            "case_id": "C",
            "intake_source": "C-INTAKE",
            "documents": [
                doc("C-INTAKE", "Policy number: P\n"),
                doc("C-ESTIMATE", "Repair amount: 100 XCR\n"),
                doc("C2-ESTIMATE", "Repair amount: 999 XCR\n"),
                doc("C-REPORT", "Reported loss type: glass\n"),
                doc("C2-REPORT", "Reported loss type: theft\n"),
            ],
        })
        self.assertEqual(payload["policy_number"], "P")
        self.assertEqual(payload["repair_amount"], "100 XCR")
        self.assertEqual(payload["reported_loss_type"], "glass")


class UniqueCaseTests(unittest.TestCase):
    def test_validation_and_dedupe(self):
        ok = {"case_id": "A", "documents": ["A-INTAKE"], "intake_source": "A-INTAKE"}
        self.assertEqual(run._unique_cases([ok, dict(ok)]), [ok])
        for bad in (
            "text",
            {"case_id": 1, "documents": [], "intake_source": "x"},
            {"case_id": "A", "documents": "no", "intake_source": "x"},
            {"case_id": "A", "documents": [], "intake_source": None},
        ):
            with self.assertRaises(IntakeError) as caught:
                run._unique_cases([bad])
            self.assertEqual(caught.exception.error_code, "cases_unavailable")

    def test_non_string_document_id_is_missing(self):
        loaded, missing = run._read_cases(
            [{"case_id": "A", "documents": [5], "intake_source": "x"}], DEV_DOCUMENTS
        )
        self.assertEqual(missing, [{"case_id": "A", "source_id": "5"}])
        self.assertEqual(loaded[0]["documents"], [])


class RunFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stores = Stores(Path(self.tmp.name))

    def _run(self, transport):
        return run_intake(self.stores, "http://mock", DEV_DOCUMENTS, DEV_POLICIES, transport=transport)

    def _codes(self):
        with self.stores.trace.connect() as db:
            return [r["error_code"] for r in db.execute("SELECT error_code FROM trace_events ORDER BY rowid")]

    def test_health_failure(self):
        with self.assertRaises(IntakeError) as caught:
            self._run(lambda url: (503, {"status": "down"}))
        self.assertEqual(caught.exception.error_code, "health_failed")
        self.assertEqual(self._codes(), ["health_failed"])

    def test_case_list_failure(self):
        def fetch(url):
            return (200, {"status": "ok"}) if url.endswith("/health") else (200, {"cases": "nope"})

        with self.assertRaises(IntakeError) as caught:
            self._run(fetch)
        self.assertEqual(caught.exception.error_code, "cases_unavailable")

    def test_missing_document_is_reported_per_case(self):
        def fetch(url):
            if url.endswith("/health"):
                return 200, {"status": "ok"}
            return 200, {"cases": [{"case_id": "AUTO-ZZ", "documents": ["AUTO-ZZ-INTAKE"], "intake_source": "AUTO-ZZ-INTAKE"}]}

        with self.assertRaises(DocumentMissing) as caught:
            self._run(fetch)
        self.assertEqual(caught.exception.missing, [{"case_id": "AUTO-ZZ", "source_id": "AUTO-ZZ-INTAKE"}])
        self.assertIn("document_missing", self._codes())

    def test_refused_revision_does_not_stop_the_batch(self):
        def fetch(url):
            if url.endswith("/health"):
                return 200, {"status": "ok"}
            return 200, {"cases": [{"case_id": "AUTO-D01", "documents": ["AUTO-D01-INTAKE"], "intake_source": "AUTO-D01-INTAKE"}]}

        with mock.patch.object(
            self.stores.input, "append_revision", side_effect=RestrictedValueError("x")
        ):
            result = self._run(fetch)
        self.assertEqual(result["refused"], ["AUTO-D01"])
        self.assertEqual(result["revisions"], [])
        self.assertIn("restricted_value", self._codes())


class DefaultTransportTests(unittest.TestCase):
    def setUp(self):
        outer = self
        self.reply = (200, b"{}")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, raw = outer.reply
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/x"

    def test_success(self):
        self.reply = (200, b'{"a": 1}')
        self.assertEqual(run.default_transport(self.url), (200, {"a": 1}))

    def test_http_error_with_json(self):
        self.reply = (404, json.dumps({"error": "not_found"}).encode())
        self.assertEqual(run.default_transport(self.url), (404, {"error": "not_found"}))

    def test_http_error_with_non_json(self):
        self.reply = (500, b"<html>")
        self.assertEqual(run.default_transport(self.url), (500, {"error": "invalid_json"}))


if __name__ == "__main__":
    unittest.main()
