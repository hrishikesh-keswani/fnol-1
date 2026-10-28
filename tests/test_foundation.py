"""Acceptance checks for the input, output, and trace stores."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from fnol_foundation import RestrictedValueError, Stores, TRACE_COLUMNS, canonical, digest
from fnol_foundation.canonical import redact_for_store
from fnol_foundation.db import IntegrityError
from fnol_foundation.stores import ImmutableRecordError
from tools.runtime import canonical as mock_canonical
from tools.runtime import digest as mock_digest


class CanonicalTests(unittest.TestCase):
    def test_matches_mock_runtime(self):
        value = {"b": 1, "a": "café", "nested": {"z": None, "y": [3, 2]}, "flag": False}
        self.assertEqual(canonical(value), mock_canonical(value))
        self.assertEqual(digest(value), mock_digest(value))
        self.assertEqual(canonical(value), '{"a":"café","b":1,"flag":false,"nested":{"y":[3,2],"z":null}}')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stores = Stores(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_the_three_sqlite_files(self):
        if self.stores.backend == "postgres":
            for store, table in (
                (self.stores.input, "input_revisions"),
                (self.stores.output, "drafts"),
                (self.stores.trace, "trace_events"),
            ):
                with store.connect() as db:
                    db.execute(f"SELECT 1 FROM {table}")
            return
        directory = Path(self.tmp.name)
        self.assertTrue((directory / "input.sqlite").is_file())
        self.assertTrue((directory / "output.sqlite").is_file())
        self.assertTrue((directory / "trace.sqlite").is_file())

    def test_sqlite_uses_wal(self):
        if self.stores.backend != "sqlite":
            self.skipTest("WAL is the SQLite journal mode")
        with sqlite3.connect(self.stores.input.path) as db:
            mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(str(mode).lower(), "wal")

    def test_latest_loss_keys_are_ordered_by_case_id(self):
        self.stores.input.append_revision("B-CASE", {"loss_type": "collision"})
        self.stores.input.append_revision("A-CASE", {"loss_type": "theft"})
        self.stores.input.append_revision("C-CASE", {"loss_type": "glass"})
        keys = [row["case_id"] for row in self.stores.input.latest_loss_keys()]
        self.assertEqual(keys, ["A-CASE", "B-CASE", "C-CASE"])

    def test_reingest_increments_revision_and_keeps_the_prior_row(self):
        first = self.stores.input.append_revision("AUTO-D01", {"loss_type": "collision"}, "sha-a", "2026-10-01T00:00:00+00:00")
        second = self.stores.input.append_revision("AUTO-D01", {"loss_type": "collision"}, "sha-b", "2026-10-01T01:00:00+00:00")
        self.assertEqual(first.revision, 1)
        self.assertEqual(second.revision, 2)
        self.assertEqual(second.document_set_sha256, "sha-b")
        latest = self.stores.input.get_revision("AUTO-D01").payload
        self.assertEqual(latest["loss_type"], "collision")
        self.assertIsNone(latest["vehicle_id"])
        self.assertEqual(self.stores.input.get_revision("AUTO-D01", 1).document_set_sha256, "sha-a")

    def test_unique_case_revision_is_enforced(self):
        self.stores.input.append_revision("AUTO-D01", {"n": 1})
        with self.stores.input.connect() as db:
            with self.assertRaises(IntegrityError):
                db.execute(
                    """INSERT INTO input_revisions
                       (case_id, revision, ingested_at, document_set_sha256)
                       VALUES ('AUTO-D01', 1, 't', 'sha')"""
                )

    def test_input_revisions_cannot_be_updated_or_deleted(self):
        self.stores.input.append_revision("AUTO-D01", {"n": 1})
        with self.stores.input.connect() as db:
            with self.assertRaises(IntegrityError):
                db.execute("UPDATE input_revisions SET loss_type = 'theft' WHERE case_id = 'AUTO-D01'")
            with self.assertRaises(IntegrityError):
                db.execute("DELETE FROM input_revisions WHERE case_id = 'AUTO-D01'")

    def test_document_hash_defaults_to_payload_digest(self):
        payload = {"vehicle_id": "VEH-D01"}
        row = self.stores.input.append_revision("AUTO-D01", payload)
        self.assertEqual(row.document_set_sha256, digest(payload))

    def test_trace_columns_match_the_agreed_schema(self):
        self.assertEqual(self.stores.trace.column_names(), TRACE_COLUMNS)

    def test_trace_is_append_only(self):
        event = self.stores.trace.append_event(
            trace_id="trace-1",
            span_id="span-1",
            step="1",
            actor="intake",
            status="ok",
            case_id="AUTO-D01",
            input_revision=1,
            started_at="2026-10-01T00:00:00+00:00",
            duration_ms=12,
        )
        self.assertEqual(event["step"], "1")
        self.assertEqual(event["duration_ms"], 12)
        self.assertIsNone(event["prev_hash"])
        self.assertTrue(isinstance(event["event_hash"], str) and len(event["event_hash"]) == 64)
        with self.stores.trace.connect() as db:
            with self.assertRaises(IntegrityError):
                db.execute("UPDATE trace_events SET status = 'changed'")
            with self.assertRaises(IntegrityError):
                db.execute("DELETE FROM trace_events")

    def test_trace_events_are_hash_chained(self):
        first = self.stores.trace.append_event(
            trace_id="trace-1",
            span_id="span-1",
            step="1",
            actor="intake",
            status="ok",
            event="started",
        )
        second = self.stores.trace.append_event(
            trace_id="trace-1",
            span_id="span-2",
            step="2",
            actor="policy",
            status="ok",
            event="tool_read",
        )
        self.assertEqual(second["prev_hash"], first["event_hash"])
        self.assertNotEqual(second["event_hash"], first["event_hash"])

    def test_draft_hash_matches_the_mock_and_body_stays_fixed(self):
        draft = {"case_id": "AUTO-D01", "summary": "Draft FNOL for human handler."}
        record = self.stores.output.record_draft("AUTO-D01", draft, input_revision=1, draft_id="draft_test")
        self.assertEqual(record.draft_hash, mock_digest(draft))
        updated = self.stores.output.update_status("draft_test", "pending", review_id="rev_1", outcome=None)
        self.assertEqual(updated.status, "pending")
        self.assertEqual(updated.review_id, "rev_1")
        self.assertEqual(updated.draft_hash, record.draft_hash)
        with self.stores.output.connect() as db:
            with self.assertRaises(IntegrityError):
                db.execute("UPDATE drafts SET draft_json = '{}' WHERE draft_id = 'draft_test'")

    def test_latest_draft_is_the_most_recent_for_the_case(self):
        self.stores.output.record_draft("AUTO-D01", {"n": 1}, draft_id="draft_old", created_at="2026-10-01T00:00:00Z")
        self.stores.output.record_draft("AUTO-D01", {"n": 2}, draft_id="draft_new", created_at="2026-10-01T01:00:00Z")
        self.stores.output.record_draft("AUTO-D02", {"n": 3}, draft_id="draft_other")
        latest = self.stores.output.latest_draft("AUTO-D01")
        self.assertEqual(latest.draft_id, "draft_new")
        self.assertIsNone(self.stores.output.latest_draft("AUTO-D99"))

    def test_raw_evidence_may_contain_the_marker(self):
        row = self.stores.input.append_revision("AUTO-D01", {
            "loss_type": "collision",
            "raw_intake": "Account mentions SYNTHETIC-PRIVATE-AUTO-D01\nLoss type: collision\n",
        })
        self.assertIn("SYNTHETIC-PRIVATE-AUTO-D01", row.payload["raw_intake"])
        with self.assertRaises(RestrictedValueError):
            self.stores.input.append_revision("AUTO-D02", {"driver_name": "SYNTHETIC-PRIVATE-AUTO-D02"})

    def test_restricted_markers_are_refused(self):
        with self.assertRaises(RestrictedValueError):
            self.stores.input.append_revision("AUTO-D01", {"private_token": "secret"})
        with self.assertRaises(RestrictedValueError):
            self.stores.output.record_draft("AUTO-D01", {"note": "SYNTHETIC-PRIVATE-AUTO-D01"})
        with self.assertRaises(RestrictedValueError):
            self.stores.trace.append_event(
                trace_id="t",
                span_id="s",
                step="7",
                actor="shadow",
                status="ok",
                shadow_narrative="token SYNTHETIC-PRIVATE-AUTO-D01",
            )

    def test_redact_drops_the_token_key_and_refuses_a_leftover_marker(self):
        cleaned = redact_for_store({"private_token": "SYNTHETIC-PRIVATE-AUTO-D01", "version": "2"})
        self.assertEqual(cleaned, {"version": "2"})
        with self.assertRaises(RestrictedValueError):
            redact_for_store({"note": "SYNTHETIC-PRIVATE-AUTO-D01"})

    def test_tool_payload_is_redacted_and_bound_to_the_revision(self):
        revision = self.stores.input.append_revision("AUTO-D01", {"loss_type": "collision"}, "sha-a")
        raw = {"private_token": "SYNTHETIC-PRIVATE-AUTO-D01", "source_id": "AUTO-D01-POLICY", "version": "2"}
        first = self.stores.input.put_tool_payload("AUTO-D01", "policy", raw, revision.revision)
        second = self.stores.input.put_tool_payload("AUTO-D01", "policy", raw, revision.revision)
        self.assertEqual(first, second)
        stored = self.stores.input.get_tool_payload("AUTO-D01", "policy", revision.revision)
        self.assertNotIn("private_token", stored["raw_json"])
        self.assertEqual(stored["sha256"], digest({"source_id": "AUTO-D01-POLICY", "version": "2"}))
        with self.assertRaises(ImmutableRecordError):
            self.stores.input.put_tool_payload(
                "AUTO-D01", "policy", {"source_id": "OTHER", "version": "1"}, revision.revision
            )


if __name__ == "__main__":
    unittest.main()
