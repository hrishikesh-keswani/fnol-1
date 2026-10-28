"""A human decision webhook copies that one case forward."""

from types import SimpleNamespace
from unittest import TestCase

from fnol_analyst.follow import advance_case


class _Output:
    def __init__(self, record):
        self.record = record

    def latest_draft(self, case_id):
        if self.record is not None and self.record.case_id == case_id:
            return self.record
        return None


class _Client:
    def __init__(self, record, decision):
        self.stores = SimpleNamespace(output=_Output(record))
        self.calls = []
        self._decision = decision

    def review_status(self, case_id):
        self.calls.append(("review", case_id))
        return {"status": self._decision, "case_id": case_id}

    def file(self, case_id):
        self.calls.append(("file", case_id))
        return {"status": "created", "case_id": case_id}


class FollowTests(TestCase):
    def test_approval_is_copied_and_filed(self):
        client = _Client(
            SimpleNamespace(case_id="AUTO-D01", review_id="rev_1", status="in_review", outcome=None),
            "approved",
        )
        done = advance_case(client, "AUTO-D01")
        self.assertEqual(client.calls, [("review", "AUTO-D01"), ("file", "AUTO-D01")])
        self.assertEqual(done, {"case_id": "AUTO-D01", "status": "created", "filed": True})

    def test_rejection_updates_the_draft_and_does_not_file(self):
        client = _Client(
            SimpleNamespace(case_id="AUTO-D02", review_id="rev_2", status="in_review", outcome=None),
            "not_submitted",
        )
        done = advance_case(client, "AUTO-D02")
        self.assertEqual(client.calls, [("review", "AUTO-D02")])
        self.assertFalse(done["filed"])

    def test_pending_stays_put(self):
        client = _Client(
            SimpleNamespace(case_id="AUTO-D03", review_id="rev_3", status="in_review", outcome=None),
            "in_review",
        )
        advance_case(client, "AUTO-D03")
        self.assertEqual(client.calls, [("review", "AUTO-D03")])


import io
import json
import threading
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from unittest import mock

from fnol_analyst import follow
from fnol_analyst.client import AnalystError


class _RaisingClient(_Client):
    def __init__(self, record, decision, review_exc=None, file_exc=None):
        super().__init__(record, decision)
        self.review_exc = review_exc
        self.file_exc = file_exc

    def review_status(self, case_id):
        if self.review_exc:
            raise self.review_exc
        return super().review_status(case_id)

    def file(self, case_id):
        if self.file_exc:
            raise self.file_exc
        return super().file(case_id)


class AdvanceCaseEdgeTests(TestCase):
    def test_missing_draft(self):
        client = _Client(None, "approved")
        self.assertEqual(
            advance_case(client, "X"), {"case_id": "X", "error": "draft_missing"}
        )

    def test_review_status_error(self):
        record = SimpleNamespace(case_id="A", review_id="r", status="in_review", outcome=None)
        client = _RaisingClient(record, "approved", review_exc=AnalystError("review_down", "boom"))
        self.assertEqual(advance_case(client, "A"), {"case_id": "A", "error": "review_down"})

    def test_already_approved_draft_is_filed(self):
        record = SimpleNamespace(case_id="A", review_id=None, status="approved", outcome="approved")
        client = _Client(record, "approved")
        done = advance_case(client, "A")
        self.assertTrue(done["filed"])
        self.assertEqual(client.calls, [("file", "A")])

    def test_non_review_non_approved_draft_is_not_filed(self):
        record = SimpleNamespace(case_id="A", review_id=None, status="draft", outcome=None)
        client = _Client(record, "approved")
        self.assertEqual(
            advance_case(client, "A"), {"case_id": "A", "status": "draft", "filed": False}
        )

    def test_file_error(self):
        record = SimpleNamespace(case_id="A", review_id="r", status="in_review", outcome=None)
        client = _RaisingClient(record, "approved", file_exc=AnalystError("file_down", "boom"))
        self.assertEqual(
            advance_case(client, "A"),
            {"case_id": "A", "status": "approved", "error": "file_down"},
        )


class NotifyAnalystTests(TestCase):
    def _run(self, case_id="C1"):
        buf = io.StringIO()
        with redirect_stdout(buf):
            follow.notify_analyst(case_id)
        return buf.getvalue()

    def test_skipped_when_url_unset(self):
        with mock.patch.dict("os.environ", {"FNOL_ANALYST_URL": ""}):
            self.assertIn("skipped", self._run())

    def test_skipped_without_case_id(self):
        with mock.patch.dict("os.environ", {"FNOL_ANALYST_URL": "http://x"}):
            self.assertIn("skipped", self._run(""))

    def test_posts_case(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        with mock.patch.dict("os.environ", {"FNOL_ANALYST_URL": "http://x/"}), mock.patch(
            "urllib.request.urlopen", return_value=response
        ) as urlopen:
            out = self._run()
        self.assertIn('{"ok": true}', out)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://x/v1/reviews/decided")
        self.assertEqual(json.loads(request.data), {"case_id": "C1"})

    def test_failure_is_reported(self):
        with mock.patch.dict("os.environ", {"FNOL_ANALYST_URL": "http://x"}), mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("down")
        ):
            self.assertIn("webhook failed", self._run())


class _FakeClientCM:
    def __init__(self, exc=None):
        self.exc = exc

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _ReviewOutput:
    def __init__(self, in_review=(), approved=()):
        self._in_review = list(in_review)
        self._approved = list(approved)

    def in_review(self):
        return list(self._in_review)

    def approved_unfiled(self):
        return list(self._approved)


class _PollClient:
    def __init__(self, records, decisions):
        self.stores = SimpleNamespace(output=_ReviewOutput(records))
        self.mock_url = "http://enterprise"
        self.decisions = decisions
        self.advanced = []

    def _call_transport(self, method, url, body, headers):
        review_id = url.rsplit("/", 1)[-1]
        return 200, {"status": self.decisions.get(review_id, "pending")}


class ReviewPollTests(TestCase):
    def test_pending_is_counted_and_not_advanced(self):
        record = SimpleNamespace(case_id="AUTO-D01", review_id="rev_1", status="in_review", outcome=None)
        client = _PollClient([record], {"rev_1": "pending"})
        with mock.patch.object(follow, "advance_case") as advance:
            result = follow.poll_open_reviews(client)
        advance.assert_not_called()
        self.assertEqual(result["waiting"], ["AUTO-D01"])
        self.assertEqual(result["moved"], [])

    def test_approved_review_is_copied_forward(self):
        record = SimpleNamespace(case_id="AUTO-D12", review_id="rev_12", status="in_review", outcome=None)
        client = _PollClient([record], {"rev_12": "approved"})
        with mock.patch.object(
            follow, "advance_case", return_value={"case_id": "AUTO-D12", "filed": True}
        ) as advance:
            result = follow.poll_open_reviews(client)
        advance.assert_called_once_with(client, "AUTO-D12")
        self.assertEqual(result["waiting"], [])
        self.assertEqual(result["moved"], [{"case_id": "AUTO-D12", "filed": True}])

    def test_approved_unfiled_draft_is_filed_without_another_peek(self):
        record = SimpleNamespace(case_id="AUTO-D03", review_id="rev_3", status="approved", outcome="approved")
        client = _PollClient([], {"rev_3": "pending"})
        client.stores.output = _ReviewOutput(approved=[record])
        with mock.patch.object(
            follow, "advance_case", return_value={"case_id": "AUTO-D03", "filed": True}
        ) as advance:
            follow.poll_open_reviews(client)
        advance.assert_called_once_with(client, "AUTO-D03")

    def test_duplicate_rows_for_one_case_are_checked_once(self):
        first = SimpleNamespace(case_id="AUTO-D02", review_id="rev_a", status="in_review", outcome=None)
        second = SimpleNamespace(case_id="AUTO-D02", review_id="rev_b", status="in_review", outcome=None)
        client = _PollClient([first, second], {"rev_a": "pending", "rev_b": "approved"})
        with mock.patch.object(follow, "advance_case") as advance:
            result = follow.poll_open_reviews(client)
        advance.assert_not_called()
        self.assertEqual(result["waiting"], ["AUTO-D02"])

    def test_missing_review_id_is_recorded(self):
        record = SimpleNamespace(case_id="AUTO-D09", review_id=None, status="in_review", outcome=None)
        client = _PollClient([record], {})
        with mock.patch.object(follow, "advance_case") as advance, redirect_stdout(io.StringIO()):
            result = follow.poll_open_reviews(client)
        advance.assert_not_called()
        self.assertEqual(result["moved"], [{"case_id": "AUTO-D09", "error": "review_missing"}])
        self.assertEqual(result["waiting"], [])

    def test_poll_http_failure_is_recorded(self):
        record = SimpleNamespace(case_id="AUTO-D04", review_id="rev_x", status="in_review", outcome=None)
        client = _PollClient([record], {})
        client._call_transport = lambda method, url, body, headers: (503, None)
        with redirect_stdout(io.StringIO()):
            result = follow.poll_open_reviews(client)
        self.assertEqual(result["moved"], [{"case_id": "AUTO-D04", "error": "review_poll_failed"}])
        self.assertEqual(result["waiting"], [])

    def test_poll_exception_without_error_code_uses_the_class_name(self):
        record = SimpleNamespace(case_id="AUTO-D05", review_id="rev_y", status="in_review", outcome=None)
        client = _PollClient([record], {"rev_y": "approved"})
        with mock.patch.object(follow, "advance_case", side_effect=RuntimeError("boom")), redirect_stdout(
            io.StringIO()
        ):
            result = follow.poll_open_reviews(client)
        self.assertEqual(result["moved"], [{"case_id": "AUTO-D05", "error": "RuntimeError"}])


class _QueueClient:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def enqueue_pending(self):
        return self.rows


class QueueLoopTests(TestCase):
    def test_bad_interval_falls_back_to_thirty_seconds(self):
        with mock.patch.dict("os.environ", {"FNOL_QUEUE_POLL_SECONDS": "soon"}):
            self.assertEqual(follow.queue_interval(), 30.0)

    def test_run_queue_pass_prints_work_and_hides_skips(self):
        rows = [
            {"case_id": "A", "action": "queued"},
            {"case_id": "B", "action": "skipped"},
        ]
        buf = io.StringIO()
        with mock.patch.object(follow, "AnalystClient", return_value=_QueueClient(rows)), redirect_stdout(buf):
            self.assertEqual(follow.run_queue_pass(), rows)
        text = buf.getvalue()
        self.assertIn("{'case_id': 'A', 'action': 'queued'}", text)
        self.assertNotIn("skipped", text)

    def test_run_review_pass_reads_open_reviews(self):
        client = object()
        opened = mock.MagicMock()
        opened.__enter__.return_value = client
        expected = {"waiting": ["C"], "moved": []}
        with mock.patch.object(follow, "AnalystClient", return_value=opened), mock.patch.object(
            follow, "poll_open_reviews", return_value=expected
        ) as poll:
            self.assertEqual(follow.run_review_pass(), expected)
        poll.assert_called_once_with(client)

    def test_queue_loop_keeps_going_after_a_failed_pass(self):
        calls = []

        def queue():
            calls.append("queue")
            if calls.count("queue") == 1:
                raise RuntimeError("queue down")

        def review():
            calls.append("review")
            if calls.count("review") == 1:
                raise RuntimeError("review down")

        def sleep(_interval):
            if calls.count("review") >= 2:
                raise SystemExit

        buf = io.StringIO()
        with mock.patch.object(follow, "run_queue_pass", side_effect=queue), mock.patch.object(
            follow, "run_review_pass", side_effect=review
        ), mock.patch.object(follow.time, "sleep", side_effect=sleep), redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                follow.queue_loop(5)
        text = buf.getvalue()
        self.assertIn("queue pass failed: queue down", text)
        self.assertIn("review poll failed: review down", text)
        self.assertEqual(calls, ["queue", "review", "queue", "review"])

    def test_start_queue_loop_starts_a_daemon(self):
        created = []

        class FakeThread:
            def __init__(self, *, target, args, name, daemon):
                self.target = target
                self.args = args
                self.name = name
                self.daemon = daemon
                created.append(self)

            def start(self):
                self.started = True

        buf = io.StringIO()
        with mock.patch.dict("os.environ", {"FNOL_QUEUE_POLL_SECONDS": "15"}), mock.patch.object(
            follow.threading, "Thread", FakeThread
        ), redirect_stdout(buf):
            follow.start_queue_loop()
        self.assertEqual(len(created), 1)
        thread = created[0]
        self.assertIs(thread.target, follow.queue_loop)
        self.assertEqual(thread.args, (15.0,))
        self.assertEqual(thread.name, "fnol-queue")
        self.assertTrue(thread.daemon)
        self.assertTrue(thread.started)
        self.assertIn("queue and review poll every 15s", buf.getvalue())


class ServeTests(TestCase):
    def setUp(self):
        env = mock.patch.dict("os.environ", {"FNOL_QUEUE_POLL_SECONDS": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.servers = []
        real = follow.ThreadingHTTPServer
        servers = self.servers

        class Capture(real):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                servers.append(self)

        patcher = mock.patch.object(follow, "ThreadingHTTPServer", Capture)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.buf = io.StringIO()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.servers:
                break
            threading.Event().wait(0.01)
        self.port = self.servers[0].server_address[1]
        self.addCleanup(self._stop)

    def _serve(self):
        with redirect_stdout(self.buf):
            follow.serve("127.0.0.1", 0)

    def _stop(self):
        self.servers[0].shutdown()
        self.thread.join(5)

    def _req(self, method, path, body=None, headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_health_and_not_found(self):
        self.assertEqual(self._req("GET", "/health")[0], 200)
        self.assertEqual(self._req("GET", "/nope")[0], 404)
        self.assertEqual(self._req("POST", "/nope", b"{}")[0], 404)

    def test_bad_payload(self):
        self.assertEqual(self._req("POST", "/v1/reviews/decided", b"not json")[0], 400)
        self.assertEqual(self._req("POST", "/v1/reviews/decided", b"{}")[0], 400)

    def test_decided_ok_and_conflict_and_crash(self):
        post = lambda: self._req(
            "POST", "/v1/reviews/decided", json.dumps({"case_id": "A"}).encode()
        )
        with mock.patch.object(follow, "AnalystClient", _FakeClientCM()), mock.patch.object(
            follow, "advance_case", return_value={"case_id": "A", "filed": True}
        ):
            self.assertEqual(post()[0], 200)
        with mock.patch.object(follow, "AnalystClient", _FakeClientCM()), mock.patch.object(
            follow, "advance_case", return_value={"case_id": "A", "error": "x"}
        ):
            self.assertEqual(post()[0], 409)
        with mock.patch.object(follow, "AnalystClient", _FakeClientCM()), mock.patch.object(
            follow, "advance_case", side_effect=RuntimeError("kaboom")
        ):
            status, body = post()
            self.assertEqual(status, 500)
            self.assertEqual(body["error"], "kaboom")


class ServeLifecycleTests(TestCase):
    def test_keyboard_interrupt_closes_server(self):
        server = mock.MagicMock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(follow, "ThreadingHTTPServer", return_value=server), mock.patch.dict(
            "os.environ",
            {"FNOL_HTTP_HOST": "127.0.0.1", "FNOL_HTTP_PORT": "0", "FNOL_QUEUE_POLL_SECONDS": "0"},
        ), redirect_stdout(io.StringIO()):
            follow.serve()
        server.server_close.assert_called_once()

    def test_main_guard_runs_serve(self):
        import runpy

        server = mock.MagicMock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch("http.server.ThreadingHTTPServer", return_value=server), mock.patch.dict(
            "os.environ", {"FNOL_QUEUE_POLL_SECONDS": "0"}
        ), redirect_stdout(io.StringIO()):
            runpy.run_module("fnol_analyst.follow", run_name="__main__")
        server.server_close.assert_called_once()
