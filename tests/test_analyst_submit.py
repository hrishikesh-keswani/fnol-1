"""Error and edge branches of the analyst review/record submission helpers."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fnol_analyst import submit
from fnol_analyst.submit import SubmitError
from fnol_foundation.stores import Stores
from test_analyst_pipeline import GOOD_SUMMARY, FakeTransport, _seed_case

BASE = "http://mock.test/"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stores = Stores(Path(self.tmp.name))
        _seed_case(self.stores)
        self.transport = FakeTransport()
        from fnol_analyst.client import AnalystClient

        with mock.patch.dict(os.environ, {"FNOL_AI_ENABLED": "0"}):
            client = AnalystClient(
                state_dir_path=Path(self.tmp.name), mock_url="http://mock.test", transport=self.transport
            )
            self.record = client.build("AUTO-D01")

    def _review(self, case="AUTO-D01"):
        return submit.submit_review(
            self.stores, case_id=case, base_url=BASE, transport=self.transport
        )

    def _poll(self):
        return submit.poll_review(
            self.stores, case_id="AUTO-D01", base_url=BASE, transport=self.transport
        )

    def _file(self):
        return submit.file_record(
            self.stores, case_id="AUTO-D01", base_url=BASE, transport=self.transport
        )

    def _in_review(self, status="in_review"):
        self.stores.output.update_status(self.record.draft_id, status, review_id="rev_1")

    def _approved(self):
        self.stores.output.update_status(
            self.record.draft_id, "approved", review_id="rev_1", outcome="approved"
        )


class SubmitReviewErrors(_Base):
    def test_draft_missing(self):
        with self.assertRaises(SubmitError) as ctx:
            self._review("NOPE")
        self.assertEqual(ctx.exception.error_code, "draft_missing")

    def test_http_error(self):
        self.transport.script = [(500, {"error": "boom"})]
        with self.assertRaises(SubmitError) as ctx:
            self._review()
        self.assertEqual(ctx.exception.error_code, "boom")

    def test_http_error_with_non_dict_body(self):
        self.transport.script = [(502, None)]
        with self.assertRaises(SubmitError) as ctx:
            self._review()
        self.assertEqual(ctx.exception.error_code, "http_error")

    def test_http_error_with_blank_error(self):
        self.transport.script = [(500, {"error": ""})]
        with self.assertRaises(SubmitError) as ctx:
            self._review()
        self.assertEqual(ctx.exception.error_code, "http_error")

    def test_bad_response(self):
        self.transport.script = [(201, {"status": "pending"})]
        with self.assertRaises(SubmitError) as ctx:
            self._review()
        self.assertEqual(ctx.exception.error_code, "bad_review_response")


class PollReviewTests(_Base):
    def test_review_missing(self):
        with self.assertRaises(SubmitError) as ctx:
            self._poll()
        self.assertEqual(ctx.exception.error_code, "review_missing")

    def test_poll_http_error(self):
        self._in_review()
        self.transport.script = [(404, {"error": "review_not_found"})]
        with self.assertRaises(SubmitError) as ctx:
            self._poll()
        self.assertEqual(ctx.exception.error_code, "review_not_found")

    def test_approval_with_wrong_hash(self):
        self._in_review()
        self.transport.script = [(200, {"status": "approved", "hash": "other"})]
        result = self._poll()
        self.assertEqual(result["outcome"], "approval_mismatch")
        self.assertEqual(result["status"], "in_review")
        self.assertIsNone(result["human_command"])

    def test_approval_with_matching_hash(self):
        self._in_review()
        self.transport.script = [(200, {"status": "approved", "hash": self.record.draft_hash})]
        result = self._poll()
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["outcome"], "approved")

    def test_unknown_status(self):
        self._in_review()
        self.transport.script = [(200, {"status": "weird"})]
        with self.assertRaises(SubmitError) as ctx:
            self._poll()
        self.assertEqual(ctx.exception.error_code, "unknown_review_status")


class EditDraftTests(_Base):
    def _edit(self, **kwargs):
        return submit.edit_draft(self.stores, case_id="AUTO-D01", **kwargs)

    def assertEditError(self, code, **kwargs):
        with self.assertRaises(SubmitError) as ctx:
            self._edit(**kwargs)
        self.assertEqual(ctx.exception.error_code, code)

    def test_empty_edit(self):
        self.assertEditError("edit_empty")

    def test_narrative_must_be_string(self):
        self.assertEditError("edit_invalid", narrative=5)

    def test_narrative_restricted(self):
        self.assertEditError("restricted_value", narrative="has private_token inside it")

    def test_narrative_unsafe(self):
        self.assertEditError("safety_blocked", narrative="Coverage is approved for this loss, sorry.")

    def test_narrative_ok(self):
        record = self._edit(narrative="A neutral handler narrative that says nothing risky.")
        self.assertEqual(
            record.draft["narrative"], "A neutral handler narrative that says nothing risky."
        )

    def test_evidence_summary_must_be_string(self):
        self.assertEditError("edit_invalid", evidence_summary=5)

    def test_evidence_summary_restricted(self):
        self.assertEditError("restricted_value", evidence_summary="SYNTHETIC-PRIVATE-1 here ok")

    def test_evidence_summary_unsafe(self):
        self.assertEditError(
            "safety_blocked", evidence_summary="We will pay the repair estimate in full."
        )

    def test_evidence_summary_ok_then_cleared(self):
        record = self._edit(evidence_summary=GOOD_SUMMARY)
        self.assertEqual(record.draft["safety_status"], "passed")
        cleared = self._edit(clear_evidence_summary=True)
        self.assertIsNone(cleared.draft["evidence_summary"])
        self.assertEqual(cleared.draft["safety_status"], "skipped")

    def test_whole_draft_restricted(self):
        # Another field of the stored draft carries a marker, so the final check refuses it.
        with mock.patch.object(
            submit,
            "assert_no_restricted",
            side_effect=[None, submit.RestrictedValueError("x")],
        ):
            self.assertEditError("restricted_value", narrative="A neutral handler narrative here.")


class FileRecordErrors(_Base):
    def test_not_approved(self):
        with self.assertRaises(SubmitError) as ctx:
            self._file()
        self.assertEqual(ctx.exception.error_code, "not_approved")

    def test_review_missing(self):
        self.stores.output.update_status(self.record.draft_id, "approved")
        with self.assertRaises(SubmitError) as ctx:
            self._file()
        self.assertEqual(ctx.exception.error_code, "review_missing")

    def test_lookup_error(self):
        self._approved()
        self.transport.script = [(503, {"error": "lookup_down"})]
        with self.assertRaises(SubmitError) as ctx:
            self._file()
        self.assertEqual(ctx.exception.error_code, "lookup_down")

    def test_create_unexpected_status(self):
        self._approved()
        self.transport.script = [(404, {}), (500, {"error": "create_down"})]
        with self.assertRaises(SubmitError) as ctx:
            self._file()
        self.assertEqual(ctx.exception.error_code, "create_down")

    def test_409_idempotency_conflict(self):
        self._approved()
        self.transport.script = [(404, {}), (409, {"error": "key_reused"})]
        result = self._file()
        self.assertEqual(result["outcome"], "idempotency_conflict")
        self.assertFalse(result["posted"])


if __name__ == "__main__":
    unittest.main()
