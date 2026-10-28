"""Reviewer HTTP boundary. Local role header stands in for Easy Auth."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fnol_foundation.canonical import canonical, digest
from fnol_foundation.db import using_postgres
from fnol_foundation.events import record_event
from fnol_foundation.stores import Stores
from server import reviewer
from server.review_page import PAGE
from server.reviewer import create_server
from server_entry import ROOT, run_as_main
from tools.runtime import connect


def _draft(narrative: str, case_id: str = "AUTO-D01") -> dict:
    return {
        "case_id": case_id,
        "classification": "collision",
        "routing": "standard",
        "narrative": narrative,
        "evidence_summary": None,
    }


class ReviewerHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "mock.sqlite"
        self.state_dir = Path(self.tmp.name) / "state"
        env = mock.patch.dict(
            os.environ, {"FNOL_STATE_DIR": str(self.state_dir), "FNOL_ANALYST_URL": ""}
        )
        env.start()
        self.addCleanup(env.stop)
        self._add_review("rev_1", _draft("Original text."))
        self.server = create_server(host="127.0.0.1", port=0, state=self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.host, self.port = self.server.server_address[:2]
        self.base = f"http://{self.host}:{self.port}"

    def _add_review(self, review_id, draft, status="pending"):
        with connect(self.state) as db:
            db.execute(
                "INSERT INTO reviews (id, case_id, draft, hash, status, reviewer) VALUES (?,?,?,?,?,?)",
                (review_id, draft["case_id"], canonical(draft), digest(draft), status, None),
            )

    def _request(self, method, path, roles="Claims.Reviewer", reviewer="alice"):
        headers = {"Accept": "application/json"}
        if roles is not None:
            headers["X-Fnol-Roles"] = roles
        if reviewer is not None:
            headers["X-Fnol-Reviewer"] = reviewer
        req = urllib.request.Request(self.base + path, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            return exc.code, body

    def _queue(self) -> dict:
        status, rows = self._request("GET", "/v1/queue")
        self.assertEqual(status, 200)
        return {row["review_id"]: row for row in rows}

    def test_page_loads(self):
        req = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, PAGE)
        self.assertIn("Pending cases", body)

    def test_forbidden_without_role(self):
        status, body = self._request("GET", "/v1/pending", roles="")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")

    def test_pending_and_approve(self):
        status, rows = self._request("GET", "/v1/pending")
        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 1)
        status, body = self._request("POST", "/v1/approve/rev_1")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "approved")
        self.assertEqual(body["reviewer"], "alice")
        with connect(self.state) as db:
            row = db.execute("SELECT status, reviewer FROM reviews WHERE id=?", ("rev_1",)).fetchone()
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["reviewer"], "alice")

    def test_second_decision_conflicts(self):
        self.assertEqual(self._request("POST", "/v1/approve/rev_1")[0], 200)
        status, body = self._request("POST", "/v1/reject/rev_1", reviewer="bob")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "review_not_pending")
        self.assertEqual(self._queue()["rev_1"]["reviewer"], "alice")

    def test_queue_shows_the_draft_on_the_review_not_a_later_edit(self):
        reviewed = _draft("Original text.")
        stores = Stores(self.state_dir)
        old = stores.output.record_draft(
            "AUTO-D01", reviewed, input_revision=1, created_at="2026-01-01T00:00:00+00:00"
        )
        stores.output.update_status(old.draft_id, "superseded", outcome="edited", clear_review_id=True)
        stores.output.record_draft(
            "AUTO-D01", _draft("Edited text."), input_revision=1, created_at="2026-01-02T00:00:00+00:00"
        )

        row = self._queue()["rev_1"]
        self.assertEqual(row["narrative"], "Original text.")
        self.assertEqual(row["draft_hash"], digest(reviewed))
        self.assertEqual(row["classification"], "collision")
        self.assertEqual(row["routing"], "standard")
        self.assertEqual(row["draft_status"], "superseded")
        self.assertEqual(row["outcome"], "edited")

    def test_gemma_paragraph_matches_the_draft_input_revision(self):
        stores = Stores(self.state_dir)
        stores.output.record_draft("AUTO-D01", _draft("Original text."), input_revision=1)
        for revision, text in ((1, "Summary of revision one."), (2, "Summary of revision two.")):
            record_event(
                stores,
                trace_id="trace-1",
                step="7",
                event="safety_scan",
                actor="test",
                status="ok",
                case_id="AUTO-D01",
                input_revision=revision,
                shadow_narrative=text,
            )

        self.assertEqual(self._queue()["rev_1"]["shadow_narrative"], "Summary of revision one.")

    def test_gemma_paragraph_without_a_revision_is_shown(self):
        record_event(
            Stores(self.state_dir),
            trace_id="trace-1",
            step="6",
            event="shadow_summary",
            actor="shadow",
            status="ok",
            case_id="AUTO-D01",
            shadow_narrative="Handler summary with no revision recorded.",
        )
        self.assertEqual(
            self._queue()["rev_1"]["shadow_narrative"],
            "Handler summary with no revision recorded.",
        )

    def test_gemma_paragraph_from_another_revision_is_not_substituted(self):
        stores = Stores(self.state_dir)
        stores.output.record_draft("AUTO-D01", _draft("Original text."), input_revision=1)
        record_event(
            stores,
            trace_id="trace-1",
            step="6",
            event="shadow_summary",
            actor="shadow",
            status="ok",
            case_id="AUTO-D01",
            input_revision=2,
            shadow_narrative="Summary of a later revision.",
        )
        self.assertIsNone(self._queue()["rev_1"]["shadow_narrative"])

    def test_queue_is_newest_first(self):
        self._add_review("rev_9", _draft("Later review.", case_id="AUTO-D02"))
        status, rows = self._request("GET", "/v1/queue")
        self.assertEqual(status, 200)
        self.assertEqual([row["review_id"] for row in rows], ["rev_9", "rev_1"])

    def test_queue_without_output_or_trace_stores(self):
        row = self._queue()["rev_1"]
        self.assertEqual(row["narrative"], "Original text.")
        self.assertIsNone(row["draft_status"])
        self.assertIsNone(row["shadow_narrative"])
        if not using_postgres():
            self.assertFalse((self.state_dir / "output.sqlite").exists())
            self.assertFalse((self.state_dir / "trace.sqlite").exists())

    def test_decision_does_not_wait_for_the_analyst_webhook(self):
        received = []
        delivered = threading.Event()

        class SlowAnalyst(BaseHTTPRequestHandler):
            def log_message(self, *_):
                return

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                time.sleep(2)
                received.append(json.loads(body))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                delivered.set()

        analyst = ThreadingHTTPServer(("127.0.0.1", 0), SlowAnalyst)
        threading.Thread(target=analyst.serve_forever, daemon=True).start()
        self.addCleanup(analyst.server_close)
        self.addCleanup(analyst.shutdown)
        host, port = analyst.server_address[:2]

        with mock.patch.dict(os.environ, {"FNOL_ANALYST_URL": f"http://{host}:{port}"}):
            started = time.monotonic()
            status, _ = self._request("POST", "/v1/approve/rev_1")
            elapsed = time.monotonic() - started
            self.assertEqual(status, 200)
            self.assertLess(elapsed, 1.5)
            self.assertTrue(delivered.wait(10))
        self.assertEqual(received, [{"case_id": "AUTO-D01"}])

    def test_health_and_review_alias_need_no_role(self):
        self.assertEqual(
            self._request("GET", "/health", roles=None),
            (200, {"status": "ok", "service": "reviewer"}),
        )
        req = urllib.request.Request(self.base + "/review")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.read().decode("utf-8"), PAGE)

    def test_audit_lists_decisions_in_order(self):
        self.assertEqual(self._request("GET", "/v1/audit")[1], [])
        self.assertEqual(self._request("POST", "/v1/reject/rev_1", reviewer="carol")[0], 200)
        status, rows = self._request("GET", "/v1/audit")
        self.assertEqual(status, 200)
        self.assertEqual(rows[-1]["case_id"], "AUTO-D01")
        self.assertIn("human_rejected", json.dumps(rows))

    def test_unknown_get_path_is_not_found(self):
        self.assertEqual(self._request("GET", "/v1/nothing"), (404, {"error": "not_found"}))

    def test_post_without_role_is_forbidden_and_changes_nothing(self):
        status, body = self._request("POST", "/v1/approve/rev_1", roles=None)
        self.assertEqual(status, 403)
        self.assertEqual(body["required_role"], "Claims.Reviewer")
        self.assertEqual(self._queue()["rev_1"]["status"], "pending")

    def test_unknown_post_path_is_not_found(self):
        self.assertEqual(self._request("POST", "/v1/delete/rev_1"), (404, {"error": "not_found"}))
        self.assertEqual(self._request("POST", "/v1/approve"), (404, {"error": "not_found"}))

    def test_decision_on_a_missing_review_conflicts(self):
        status, body = self._request("POST", "/v1/approve/rev_missing")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "review_not_pending")

    def test_role_check_can_be_switched_off(self):
        self.server.shutdown()
        with mock.patch.dict(os.environ, {"FNOL_REVIEWER_REQUIRE_ROLE": "0"}):
            server = create_server(host="127.0.0.1", port=0, state=self.state)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        self.base = f"http://{host}:{port}"
        status, rows = self._request("GET", "/v1/pending", roles=None)
        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 1)
        status, body = self._request("POST", "/v1/approve/rev_1", roles=None, reviewer=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["reviewer"], "unknown-reviewer")

    def test_state_path_comes_from_the_environment_or_the_repo(self):
        with mock.patch.dict(os.environ, {"FNOL_MOCK_STATE": "/tmp/custom.sqlite"}):
            self.assertEqual(reviewer._state_path(), Path("/tmp/custom.sqlite"))
        with mock.patch.dict(os.environ):
            os.environ.pop("FNOL_MOCK_STATE", None)
            self.assertEqual(reviewer._state_path(), reviewer.ROOT / ".state" / "mock.sqlite")

    def test_create_server_defaults_to_the_environment_state_path(self):
        with mock.patch.dict(os.environ, {"FNOL_MOCK_STATE": str(self.state)}):
            server = create_server(host="127.0.0.1", port=0)
        self.addCleanup(server.server_close)
        self.assertIsInstance(server, ThreadingHTTPServer)


def _principal(claims):
    return base64.b64encode(json.dumps({"claims": claims}).encode()).decode().rstrip("=")


class RoleAndNameHeaderTests(unittest.TestCase):
    def test_roles_from_easy_auth_principal(self):
        header = _principal([
            {"typ": "roles", "val": "Claims.Reviewer"},
            {"typ": "http://schemas.microsoft.com/ws/2008/06/identity/claims/role", "val": "Other"},
            {"typ": "name", "val": "ignored"},
            {"typ": "roles", "val": ""},
            {"typ": "roles", "val": 7},
            "not-a-claim",
        ])
        roles = reviewer._roles_from_headers({"X-Ms-Client-Principal": header})
        self.assertEqual(roles, {"Claims.Reviewer", "Other"})

    def test_lowercase_principal_header_and_missing_claims(self):
        header = base64.b64encode(b"{}").decode()
        self.assertEqual(reviewer._roles_from_headers({"x-ms-client-principal": header}), set())

    def test_garbage_principal_is_ignored(self):
        self.assertEqual(reviewer._roles_from_headers({"X-Ms-Client-Principal": "%%%"}), set())
        self.assertEqual(
            reviewer._roles_from_headers({"X-Ms-Client-Principal": _principal([]) + "x"}), set()
        )

    def test_explicit_roles_header_is_split_and_merged(self):
        headers = {
            "x-fnol-roles": " Claims.Reviewer, ,Audit ",
            "X-Ms-Client-Principal": _principal([{"typ": "roles", "val": "Easy.Auth"}]),
        }
        self.assertEqual(
            reviewer._roles_from_headers(headers), {"Claims.Reviewer", "Audit", "Easy.Auth"}
        )
        self.assertEqual(reviewer._roles_from_headers({}), set())

    def test_reviewer_name_header_precedence(self):
        self.assertEqual(
            reviewer._reviewer_name(
                {"X-Ms-Client-Principal-Name": " dana ", "X-Fnol-Reviewer": "erin"}
            ),
            "dana",
        )
        self.assertEqual(reviewer._reviewer_name({"x-fnol-reviewer": "erin"}), "erin")
        self.assertEqual(reviewer._reviewer_name({"X-Fnol-Reviewer": "   "}), "unknown-reviewer")
        self.assertEqual(reviewer._reviewer_name({}), "unknown-reviewer")


class ReviewerMainTests(unittest.TestCase):
    def _run(self, argv, *, postgres=False, serve_forever=None, env=None):
        fake_server = mock.Mock()
        if serve_forever is not None:
            fake_server.serve_forever.side_effect = serve_forever
        out = io.StringIO()
        merged = {"FNOL_ALLOW_LOCAL_STATE": "1"}
        if env:
            merged.update(env)
        with (
            mock.patch.dict(os.environ, merged),
            mock.patch("fnol_foundation.db.using_postgres", return_value=postgres),
            mock.patch("fnol_foundation.db.ensure_ready") as ensure_ready,
            mock.patch.object(reviewer, "create_server", return_value=fake_server) as create,
            contextlib.redirect_stdout(out),
        ):
            code = reviewer.main(argv)
        return code, fake_server, create, ensure_ready, out.getvalue()

    def test_main_serves_with_flags_and_closes(self):
        code, server, create, ensure_ready, output = self._run(
            ["--host", "127.0.0.1", "--port", "9099", "--state", "/tmp/mock.sqlite"]
        )
        self.assertEqual(code, 0)
        ensure_ready.assert_not_called()
        create.assert_called_once_with(host="127.0.0.1", port=9099, state=Path("/tmp/mock.sqlite"))
        server.serve_forever.assert_called_once_with()
        server.server_close.assert_called_once_with()
        self.assertIn("reviewer HTTP on http://127.0.0.1:9099 (sqlite)", output)

    def test_main_defaults_come_from_the_environment(self):
        _code, _server, create, _ready, _out = self._run(
            [], env={"FNOL_HTTP_HOST": "127.0.0.2", "FNOL_HTTP_PORT": "9100"}
        )
        create.assert_called_once_with(host="127.0.0.2", port=9100, state=None)

    def test_main_checks_postgres_and_survives_ctrl_c(self):
        code, server, _create, ensure_ready, _out = self._run(
            [], postgres=True, serve_forever=KeyboardInterrupt
        )
        self.assertEqual(code, 0)
        ensure_ready.assert_called_once_with()
        server.server_close.assert_called_once_with()

    def test_main_refuses_sqlite_without_explicit_local_opt_in(self):
        fake_server = mock.Mock()
        err = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"FNOL_ALLOW_LOCAL_STATE": "0"}, clear=False),
            mock.patch("fnol_foundation.db.using_postgres", return_value=False),
            mock.patch.object(reviewer, "create_server", return_value=fake_server) as create,
            contextlib.redirect_stderr(err),
        ):
            # Force the opt-in off even if the process inherited ALLOW_LOCAL.
            with mock.patch.dict(os.environ, {"FNOL_ALLOW_LOCAL_STATE": ""}):
                code = reviewer.main(["--host", "127.0.0.1", "--port", "9099"])
        self.assertEqual(code, 2)
        create.assert_not_called()
        self.assertIn("refusing local SQLite", err.getvalue())

    def test_script_exits_zero_after_interrupt(self):
        script = ROOT / "server" / "reviewer.py"
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"FNOL_ALLOW_LOCAL_STATE": "1"}):
                with mock.patch.object(ThreadingHTTPServer, "serve_forever", side_effect=KeyboardInterrupt):
                    with self.assertRaises(SystemExit) as ctx:
                        run_as_main(
                            script, ["--host", "127.0.0.1", "--port", "0", "--state", f"{tmp}/m.sqlite"]
                        )
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
