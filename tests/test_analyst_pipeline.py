"""AI gate, safety fallback, review/record submit — no live Bedrock."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fnol_analyst.client import AnalystClient, ai_enabled  # noqa: E402
from fnol_foundation.canonical import digest  # noqa: E402
from fnol_foundation.stores import Stores  # noqa: E402
from fnol_shadow.bedrock import ChatReply  # noqa: E402

GOOD_SUMMARY = (
    "Draft for the human handler. Case AUTO-D01, input revision 1. "
    "Policy PA-D01, vehicle VEH-D01, incident 2026-10-01T12:00:00Z, loss type collision. "
    "Injury reported: no. Vehicle drivable: yes. Referral indicator: none. "
    "Recommended route: standard under AUTO-TRIAGE-01. Selected policy version 2. "
    "One attached citation is potentially relevant only. "
    "No coverage, liability, fraud, reserve, settlement, denial, or payment decision is made. "
    "A referral indicator is not a fraud finding. "
    "A policy-period mismatch would be human review, not an automatic denial. "
    "A person must approve the exact draft before any record is created."
)
BAD_SUMMARY = (
    "Draft for the human handler reviewing case AUTO-D01 revision 1. "
    "Coverage is approved for this collision. We will pay the repair estimate. "
    "Fraud has been confirmed."
)


def _seed_case(stores: Stores, case_id: str = "AUTO-D01") -> None:
    stores.input.append_revision(
        case_id,
        {
            "policy_number": "PA-D01",
            "incident_timestamp": "2026-10-01T12:00:00Z",
            "vehicle_id": "VEH-D01",
            "loss_type": "collision",
            "referral_indicator": "none",
            "driver_name": "Pat Doe",
            "incident_location": "Main St",
            "raw_intake": "RAW FILE TEXT MUST NOT LEAK",
        },
        "sha-seed",
        trace_id="trace-seed",
    )
    stores.input.put_decision(
        case_id,
        1,
        "policy",
        {
            "selected_policy_version": "2",
            "policy_available": True,
            "reason": "matched",
            "policy_ids": ["AUTO-TRIAGE-01", "AUTO-WORDING-V2"],
            "wording_source": "AUTO-WORDING-V2",
            "endorsements": [],
            "source_id": "AUTO-D01-POLICY",
        },
    )
    stores.input.put_decision(
        case_id,
        1,
        "route",
        {
            "classification": "collision",
            "route": "standard",
            "route_rule": "AUTO-TRIAGE-01",
            "missing_information": [],
            "duplicate_delivery": False,
            "injury_reported": False,
            "vehicle_drivable": True,
            "selected_policy_version": "2",
            "claims_available": True,
            "reasons": [],
            "conflicts": [],
        },
    )
    stores.input.put_decision(
        case_id,
        1,
        "citations",
        {
            "policy_ids": ["AUTO-TRIAGE-01", "AUTO-WORDING-V2"],
            "citations": [
                {
                    "claim": "incident_evidence",
                    "source_id": "AUTO-D01-INTAKE",
                    "quote": "The car struck a post.",
                }
            ],
            "omissions": [],
        },
    )


class FakeTransport:
    def __init__(self):
        self.calls: list[tuple] = []
        self.script: list[tuple[int, dict]] = []

    def __call__(self, method, path, body, headers):
        self.calls.append((method, path, body, headers))
        if not self.script:
            return 500, {"error": "no_script"}
        return self.script.pop(0)


class AiGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _client(self, **kwargs):
        return AnalystClient(state_dir_path=self.state, mock_url="http://mock.test", **kwargs)

    def _chat(self, text: str, *, tokens=(11, 22), guardrail=None):
        def chat(_system, _user):
            self.calls.append((_system, _user))
            return ChatReply(
                text=text,
                input_tokens=tokens[0],
                output_tokens=tokens[1],
                guardrail_action=guardrail,
            )

        return chat

    def test_ai_off_skips_model_and_keeps_null_slot(self):
        stores = Stores(self.state)
        _seed_case(stores)
        with mock.patch.dict(os.environ, {"FNOL_AI_ENABLED": "0"}, clear=False):
            self.assertFalse(ai_enabled())
            client = self._client(shadow_chat=self._chat(GOOD_SUMMARY))
            record = client.build("AUTO-D01")
        self.assertIsNone(record.draft["evidence_summary"])
        self.assertEqual(record.draft["safety_status"], "skipped")
        self.assertEqual(self.calls, [])
        with stores.trace.connect() as db:
            steps = [row[0] for row in db.execute(
                "SELECT step FROM trace_events WHERE actor='analyst' ORDER BY rowid"
            )]
        self.assertEqual(steps, ["5", "8"])
        second = client.build("AUTO-D01")
        self.assertEqual(second.draft_id, record.draft_id)

    def test_ai_on_passing_summary_stays_out_of_the_hash(self):
        stores = Stores(self.state)
        _seed_case(stores)
        with mock.patch.dict(os.environ, {"FNOL_AI_ENABLED": "1"}, clear=False):
            client = self._client(shadow_chat=self._chat(GOOD_SUMMARY), shadow_model_id="gemma-test")
            record = client.build("AUTO-D01")
        self.assertIsNone(record.draft["evidence_summary"])
        self.assertEqual(record.draft["safety_status"], "skipped")
        self.assertNotIn(GOOD_SUMMARY[:40], json.dumps(record.draft))
        self.assertEqual(record.draft_hash, digest(record.draft))
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("RAW FILE TEXT", self.calls[0][1])
        with stores.trace.connect() as db:
            rows = list(db.execute(
                """SELECT step, event, model_id, input_tokens, output_tokens,
                          shadow_narrative, shadow_safety_status, output_sha256, draft_id
                   FROM trace_events WHERE actor='analyst' ORDER BY rowid"""
            ))
        steps = [row[0] for row in rows]
        self.assertEqual(steps, ["5", "8", "6", "7"])
        step6 = rows[2]
        self.assertEqual(step6[1], "shadow_summary")
        self.assertEqual(step6[2], "gemma-test")
        self.assertEqual(step6[3], 11)
        self.assertEqual(step6[4], 22)
        self.assertIsNone(step6[5])
        self.assertEqual(step6[7], digest(GOOD_SUMMARY))
        self.assertEqual(step6[8], record.draft_id)
        step7 = rows[3]
        self.assertEqual(step7[1], "safety_scan")
        self.assertEqual(step7[5], GOOD_SUMMARY)
        self.assertEqual(step7[6], "passed")
        self.assertEqual(step7[8], record.draft_id)
        blob = " ".join(str(cell) for row in rows for cell in row)
        self.assertNotIn("RAW FILE TEXT", blob)
        self.assertNotIn(self.calls[0][0][:40], blob)

    def test_ai_on_blocked_falls_back_and_keeps_narrative(self):
        stores = Stores(self.state)
        _seed_case(stores)
        with mock.patch.dict(os.environ, {"FNOL_AI_ENABLED": "1"}, clear=False):
            client = self._client(shadow_chat=self._chat(BAD_SUMMARY))
            record = client.build("AUTO-D01")
        self.assertIsNone(record.draft["evidence_summary"])
        self.assertEqual(record.draft["safety_status"], "skipped")
        self.assertIn("Draft FNOL for the human handler", record.draft["narrative"])
        self.assertNotIn("Coverage is approved", record.draft["narrative"])
        self.assertNotIn(b"Coverage is approved", stores.trace.stored_blob())
        self.assertEqual(record.status, "recorded")

    def test_ai_on_model_error_falls_back(self):
        stores = Stores(self.state)
        _seed_case(stores)

        def boom(_system, _user):
            self.calls.append(1)
            raise RuntimeError("network down")

        with mock.patch.dict(os.environ, {"FNOL_AI_ENABLED": "1"}, clear=False):
            client = self._client(shadow_chat=boom)
            record = client.build("AUTO-D01")
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(record.draft["evidence_summary"])
        self.assertEqual(record.draft["safety_status"], "skipped")

    def test_second_build_does_not_call_model(self):
        stores = Stores(self.state)
        _seed_case(stores)
        with mock.patch.dict(os.environ, {"FNOL_AI_ENABLED": "1"}, clear=False):
            client = self._client(shadow_chat=self._chat(GOOD_SUMMARY))
            first = client.build("AUTO-D01")
            self.assertEqual(len(self.calls), 1)
            second = client.build("AUTO-D01")
        self.assertEqual(second.draft_id, first.draft_id)
        self.assertEqual(len(self.calls), 1)


class SubmitFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.stores = Stores(self.state)
        _seed_case(self.stores)
        self.transport = FakeTransport()
        with mock.patch.dict(os.environ, {"FNOL_AI_ENABLED": "0"}, clear=False):
            self.client = AnalystClient(
                state_dir_path=self.state,
                mock_url="http://mock.test",
                transport=self.transport,
            )
            self.record = self.client.build("AUTO-D01")

    def tearDown(self):
        self.tmp.cleanup()

    def test_submit_hash_match(self):
        self.transport.script = [
            (201, {"review_id": "rev_abc", "status": "pending", "draft_hash": self.record.draft_hash}),
        ]
        updated = self.client.submit("AUTO-D01")
        self.assertEqual(updated.status, "in_review")
        self.assertEqual(updated.review_id, "rev_abc")
        method, path, body, _headers = self.transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/v1/reviews")
        self.assertEqual(body["draft"], self.record.draft)

    def test_submit_hash_mismatch(self):
        self.transport.script = [
            (201, {"review_id": "rev_bad", "status": "pending", "draft_hash": "deadbeef"}),
        ]
        updated = self.client.submit("AUTO-D01")
        self.assertEqual(updated.status, "recorded")
        self.assertEqual(updated.outcome, "hash_mismatch")
        self.assertIsNone(updated.review_id)

    def test_reject_does_not_post_records(self):
        self.transport.script = [
            (201, {"review_id": "rev_1", "status": "pending", "draft_hash": self.record.draft_hash}),
            (200, {
                "id": "rev_1",
                "case_id": "AUTO-D01",
                "hash": self.record.draft_hash,
                "status": "rejected",
                "reviewer": "r1",
            }),
        ]
        self.client.submit("AUTO-D01")
        result = self.client.review_status("AUTO-D01")
        self.assertEqual(result["status"], "not_submitted")
        self.assertEqual(result["outcome"], "rejected")
        paths = [call[1] for call in self.transport.calls]
        self.assertNotIn("/v1/records", paths)
        self.assertTrue(all(not p.startswith("/v1/records") for p in paths))

    def test_pending_prints_human_command_without_invoking_approve(self):
        self.transport.script = [
            (201, {"review_id": "rev_wait", "status": "pending", "draft_hash": self.record.draft_hash}),
            (200, {
                "id": "rev_wait",
                "case_id": "AUTO-D01",
                "hash": self.record.draft_hash,
                "status": "pending",
                "reviewer": None,
            }),
        ]
        self.client.submit("AUTO-D01")
        result = self.client.review_status("AUTO-D01")
        self.assertIn("runtime.py approve rev_wait", result["human_command"])
        self.assertEqual(len(self.transport.calls), 2)

    def test_edit_creates_new_draft_and_voids_old_review(self):
        self.transport.script = [
            (201, {"review_id": "rev_old", "status": "pending", "draft_hash": self.record.draft_hash}),
        ]
        submitted = self.client.submit("AUTO-D01")
        self.assertEqual(submitted.review_id, "rev_old")
        edited = self.client.edit(
            "AUTO-D01",
            evidence_summary=GOOD_SUMMARY,
        )
        self.assertNotEqual(edited.draft_id, submitted.draft_id)
        self.assertNotEqual(edited.draft_hash, submitted.draft_hash)
        self.assertEqual(edited.draft["evidence_summary"], GOOD_SUMMARY)
        self.assertIsNone(edited.review_id)
        old = self.stores.output.get_draft(submitted.draft_id)
        self.assertEqual(old.status, "superseded")
        self.assertIsNone(old.review_id)
        self.assertEqual(old.outcome, "edited")

    def test_file_skips_post_when_record_exists(self):
        self.stores.output.update_status(
            self.record.draft_id, "approved", review_id="rev_ok", outcome="approved"
        )
        self.transport.script = [
            (200, {"id": "logical-1", "case_id": "AUTO-D01", "review_id": "rev_other"}),
        ]
        result = self.client.file("AUTO-D01")
        self.assertEqual(result["outcome"], "duplicate")
        self.assertFalse(result["posted"])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0][0], "GET")

    def test_file_again_after_created_is_duplicate(self):
        self.stores.output.update_status(
            self.record.draft_id, "created", review_id="rev_ok", outcome="created"
        )
        self.transport.script = [
            (200, {"id": "logical-1", "case_id": "AUTO-D01", "review_id": "rev_ok"}),
        ]
        result = self.client.file("AUTO-D01")
        self.assertEqual(result["outcome"], "duplicate")
        self.assertFalse(result["posted"])
        self.assertEqual(len(self.transport.calls), 1)

    def test_file_posts_on_404_and_accepts_201(self):
        self.stores.output.update_status(
            self.record.draft_id, "approved", review_id="rev_ok", outcome="approved"
        )
        self.transport.script = [
            (404, {"error": "not_found"}),
            (201, {"record_id": "logical-1", "case_id": "AUTO-D01", "status": "created", "review_id": "rev_ok"}),
        ]
        result = self.client.file("AUTO-D01")
        self.assertEqual(result["outcome"], "created")
        self.assertTrue(result["posted"])
        self.assertEqual(result["record_id"], "logical-1")
        post = self.transport.calls[1]
        self.assertEqual(post[0], "POST")
        self.assertEqual(post[1], "/v1/records")
        self.assertEqual(post[3]["Idempotency-Key"], self.record.draft_id)

    def test_file_replay_200_is_success(self):
        self.stores.output.update_status(
            self.record.draft_id, "approved", review_id="rev_ok", outcome="approved"
        )
        self.transport.script = [
            (404, {"error": "not_found"}),
            (200, {"record_id": "logical-1", "case_id": "AUTO-D01", "status": "created", "review_id": "rev_ok"}),
        ]
        result = self.client.file("AUTO-D01")
        self.assertEqual(result["outcome"], "created")
        self.assertEqual(result["http_status"], 200)

    def test_file_409_and_403_stop(self):
        self.stores.output.update_status(
            self.record.draft_id, "approved", review_id="rev_ok", outcome="approved"
        )
        self.transport.script = [
            (404, {"error": "not_found"}),
            (409, {"error": "duplicate_case", "existing_record_id": "logical-1"}),
        ]
        result = self.client.file("AUTO-D01")
        self.assertEqual(result["outcome"], "duplicate")
        self.assertFalse(result["posted"])

        self.transport.script = [
            (404, {"error": "not_found"}),
            (403, {"error": "approval_missing_or_draft_changed"}),
        ]
        # fresh approved state for second attempt
        self.stores.output.update_status(self.record.draft_id, "approved", outcome="approved")
        result = self.client.file("AUTO-D01")
        self.assertEqual(result["outcome"], "approval_mismatch")
        self.assertFalse(result["posted"])

    def test_submit_path_does_not_invoke_approve(self):
        for name in ("client.py", "submit.py", "cli.py"):
            text = (ROOT / "src" / "fnol_analyst" / name).read_text(encoding="utf-8")
            self.assertNotIn("subprocess", text)
            self.assertNotIn("os.system", text)
            self.assertNotIn("runtime.main", text)
            self.assertNotIn("Popen", text)
            self.assertNotIn('command == "approve"', text)
            self.assertNotIn("tools.runtime", text)


if __name__ == "__main__":
    unittest.main()
