"""Queue decisions: submit a built draft, skip a repeat, process a new revision."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from fnol_analyst.client import AnalystClient, AnalystError
from fnol_analyst.queue import queue_action


def _revision(number, sha):
    return SimpleNamespace(revision=number, document_set_sha256=sha)


def _draft(revision, status, outcome=None):
    return SimpleNamespace(input_revision=revision, status=status, outcome=outcome)


class QueueActionTests(TestCase):
    def test_missing_revision_is_skipped(self):
        self.assertEqual(queue_action(None, None, None), "skip")

    def test_recorded_draft_is_submitted(self):
        self.assertEqual(
            queue_action(_revision(1, "sha"), _draft(1, "recorded"), None),
            "submit",
        )

    def test_hash_mismatch_is_not_submitted_again(self):
        self.assertEqual(
            queue_action(
                _revision(1, "sha"), _draft(1, "recorded", outcome="hash_mismatch"), None
            ),
            "skip",
        )

    def test_review_already_open_for_this_revision_is_skipped(self):
        for status in ("in_review", "approved", "created", "not_submitted"):
            with self.subTest(status=status):
                self.assertEqual(
                    queue_action(_revision(1, "sha"), _draft(1, status), None),
                    "skip",
                )

    def test_same_documents_on_a_new_revision_are_skipped(self):
        self.assertEqual(
            queue_action(
                _revision(2, "sha"),
                _draft(1, "in_review"),
                _revision(1, "sha"),
            ),
            "skip",
        )

    def test_changed_documents_are_processed(self):
        self.assertEqual(
            queue_action(
                _revision(2, "new"),
                _draft(1, "created"),
                _revision(1, "old"),
            ),
            "process",
        )

    def test_case_with_no_draft_is_processed(self):
        self.assertEqual(queue_action(_revision(1, "sha"), None, None), "process")


class EnqueueTests(TestCase):
    def _client(self, revision, draft, prior=None):
        client = AnalystClient.__new__(AnalystClient)
        store = Mock()

        def get_revision(case_id, number=None):
            if number is None:
                return revision
            return prior

        store.get_revision.side_effect = get_revision
        output = Mock()
        output.latest_draft.return_value = draft
        client.stores = SimpleNamespace(input=store, output=output)
        client._servers = Mock()
        client.build = Mock()
        client.submit = Mock(
            return_value=SimpleNamespace(draft_id="d1", review_id="r1", status="in_review")
        )
        return client

    def test_new_case_runs_the_pipeline_then_submits(self):
        client = self._client(_revision(1, "sha"), None)
        row = client.enqueue_case("AUTO-D01")
        client.servers.prepare_case.assert_called_once_with("AUTO-D01")
        client.build.assert_called_once_with("AUTO-D01")
        client.submit.assert_called_once_with("AUTO-D01")
        self.assertEqual(row["action"], "queued")
        self.assertEqual(row["review_id"], "r1")

    def test_recorded_draft_is_only_submitted(self):
        client = self._client(_revision(1, "sha"), _draft(1, "recorded"))
        row = client.enqueue_case("AUTO-D01")
        client.servers.prepare_case.assert_not_called()
        client.build.assert_not_called()
        client.submit.assert_called_once_with("AUTO-D01")
        self.assertEqual(row["action"], "submitted")

    def test_same_documents_do_not_call_the_servers(self):
        client = self._client(
            _revision(2, "sha"), _draft(1, "in_review"), _revision(1, "sha")
        )
        row = client.enqueue_case("AUTO-D01")
        self.assertEqual(row["action"], "skipped")
        client.submit.assert_not_called()

    def test_one_failure_does_not_stop_the_next_case(self):
        client = AnalystClient.__new__(AnalystClient)
        client.case_ids = lambda: ["AUTO-D01", "AUTO-D02"]

        def enqueue(case_id):
            if case_id == "AUTO-D01":
                raise AnalystError("rules_down", "rules down")
            return {"case_id": case_id, "action": "queued"}

        client.enqueue_case = enqueue
        rows = AnalystClient.enqueue_pending(client)
        self.assertEqual(rows[0], {"case_id": "AUTO-D01", "action": "error", "error": "rules_down"})
        self.assertEqual(rows[1]["action"], "queued")
