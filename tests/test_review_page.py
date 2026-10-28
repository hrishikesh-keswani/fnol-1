"""Review queue helpers: draft parsing, store reads, and Gemma paragraph matching."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from server import review_page
from server.review_page import (
    _draft_body,
    _gemma_for,
    _gemma_paragraphs,
    _store_rows,
    _text,
    queue_rows,
)


class FakeDb:
    def __init__(self, reviews):
        self.reviews = reviews

    def execute(self, _sql):
        return mock.Mock(fetchall=lambda: self.reviews)


class HelperTests(unittest.TestCase):
    def test_text_keeps_only_non_blank_strings(self):
        self.assertEqual(_text("hello"), "hello")
        for value in ("", "   ", None, 5, ["x"]):
            self.assertIsNone(_text(value))

    def test_draft_body_tolerates_bad_input(self):
        self.assertEqual(_draft_body('{"narrative": "x"}'), {"narrative": "x"})
        self.assertEqual(_draft_body("{broken"), {})
        self.assertEqual(_draft_body("[1, 2]"), {})
        self.assertEqual(_draft_body(None), {})
        self.assertEqual(_draft_body(b"{}"), {})


class StoreRowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"FNOL_STATE_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_no_params_means_no_read(self):
        with mock.patch.object(review_page, "open_db") as open_db:
            self.assertEqual(_store_rows("output", "SELECT 1", []), [])
        open_db.assert_not_called()

    def test_missing_sqlite_file_means_no_rows(self):
        with (
            mock.patch.object(review_page, "using_postgres", return_value=False),
            mock.patch.object(review_page, "open_db") as open_db,
        ):
            self.assertEqual(_store_rows("output", "SELECT 1", ["x"]), [])
        open_db.assert_not_called()

    def test_postgres_skips_the_file_check(self):
        db = mock.MagicMock()
        db.__enter__.return_value.execute.return_value.fetchall.return_value = ["row"]
        with (
            mock.patch.object(review_page, "using_postgres", return_value=True),
            mock.patch.object(review_page, "open_db", return_value=db) as open_db,
        ):
            rows = _store_rows("trace", "SELECT ?", ["x"])
        self.assertEqual(rows, ["row"])
        self.assertEqual(open_db.call_args.kwargs, {"kind": "trace"})

    def test_failed_read_is_logged_not_raised(self):
        (Path(self.tmp.name) / "output.sqlite").write_bytes(b"")
        err = io.StringIO()
        with (
            mock.patch.object(review_page, "using_postgres", return_value=False),
            mock.patch.object(review_page, "open_db", side_effect=RuntimeError("locked")),
            redirect_stderr(err),
        ):
            self.assertEqual(_store_rows("output", "SELECT 1", ["x"]), [])
        self.assertIn("review queue: output store unavailable: locked", err.getvalue())


class GemmaTests(unittest.TestCase):
    def _paragraphs(self, rows):
        with mock.patch.object(review_page, "_store_rows", return_value=rows):
            return _gemma_paragraphs(["AUTO-D01"])

    def test_paragraphs_are_scoped_by_revision(self):
        by_revision, unscoped, newest = self._paragraphs([
            {"case_id": "AUTO-D01", "input_revision": 1, "shadow_narrative": "one"},
            {"case_id": "AUTO-D01", "input_revision": "2", "shadow_narrative": "two"},
            {"case_id": "AUTO-D01", "input_revision": None, "shadow_narrative": "loose"},
        ])
        self.assertEqual(by_revision, {("AUTO-D01", 1): "one", ("AUTO-D01", 2): "two"})
        self.assertEqual(unscoped, {"AUTO-D01": "loose"})
        self.assertEqual(newest, {"AUTO-D01": "loose"})

    def test_blank_text_and_missing_case_are_skipped(self):
        paragraphs = self._paragraphs([
            {"case_id": "AUTO-D01", "input_revision": 1, "shadow_narrative": "   "},
            {"case_id": "AUTO-D01", "input_revision": 1, "shadow_narrative": None},
            {"case_id": "", "input_revision": 1, "shadow_narrative": "orphan"},
        ])
        self.assertEqual(paragraphs, ({}, {}, {}))

    def test_gemma_for_prefers_the_matching_revision(self):
        paragraphs = ({("C", 1): "rev1"}, {"C": "loose"}, {"C": "newest"})
        self.assertEqual(_gemma_for("C", 1, paragraphs), "rev1")
        self.assertEqual(_gemma_for("C", "1", paragraphs), "rev1")

    def test_gemma_for_falls_back_to_an_unscoped_paragraph(self):
        paragraphs = ({("C", 1): "rev1"}, {"C": "loose"}, {"C": "newest"})
        self.assertEqual(_gemma_for("C", 2, paragraphs), "loose")
        self.assertEqual(_gemma_for("C", None, paragraphs), "loose")

    def test_gemma_for_unknown_revision_uses_newest_only_when_revision_missing(self):
        paragraphs = ({("C", 2): "rev2"}, {}, {"C": "rev2"})
        self.assertIsNone(_gemma_for("C", 1, paragraphs))
        self.assertEqual(_gemma_for("C", None, paragraphs), "rev2")
        self.assertIsNone(_gemma_for("OTHER", None, paragraphs))


class QueueRowsTests(unittest.TestCase):
    def test_empty_queue(self):
        with mock.patch.object(review_page, "_store_rows", return_value=[]) as store_rows:
            self.assertEqual(queue_rows(FakeDb([])), [])
        self.assertEqual(store_rows.call_count, 2)

    def test_rows_survive_corrupt_drafts_and_missing_hashes(self):
        reviews = [
            {"id": "rev_2", "case_id": "AUTO-D02", "draft": "{corrupt", "hash": None,
             "status": "pending", "reviewer": None},
            {"id": "rev_1", "case_id": "AUTO-D01",
             "draft": json.dumps({"classification": "collision", "narrative": " "}),
             "hash": "h1", "status": "approved", "reviewer": "alice"},
        ]
        with mock.patch.object(review_page, "_store_rows", return_value=[]):
            rows = queue_rows(FakeDb(reviews))
        self.assertEqual([row["review_id"] for row in rows], ["rev_2", "rev_1"])
        self.assertIsNone(rows[0]["classification"])
        self.assertIsNone(rows[0]["draft_hash"])
        self.assertEqual(rows[1]["classification"], "collision")
        self.assertIsNone(rows[1]["narrative"])
        self.assertIsNone(rows[1]["draft_status"])
        self.assertEqual(rows[1]["reviewer"], "alice")

    def test_queue_keeps_one_row_per_case(self):
        reviews = [
            {"id": "rev_new", "case_id": "AUTO-D03", "draft": "{}", "hash": "h2",
             "status": "pending", "reviewer": None},
            {"id": "rev_old", "case_id": "AUTO-D03", "draft": "{}", "hash": "h1",
             "status": "rejected", "reviewer": "bob"},
            {"id": "rev_other", "case_id": "AUTO-D01", "draft": "{}", "hash": "h0",
             "status": "approved", "reviewer": "alice"},
        ]
        with mock.patch.object(review_page, "_store_rows", return_value=[]):
            rows = queue_rows(FakeDb(reviews))
        self.assertEqual([row["review_id"] for row in rows], ["rev_new", "rev_other"])
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
