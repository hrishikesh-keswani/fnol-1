"""HTTP step client, default transport, and ServerSet wiring against a stub server."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fnol_analyst.http import default_transport
from fnol_analyst.http_steps import HttpStepClient
from fnol_analyst.mcp import ServerError
from fnol_analyst.servers import ServerSet


class _Stub(unittest.TestCase):
    """Answers every request with whatever ``self.reply`` holds: (status, bytes)."""

    def setUp(self):
        outer = self
        self.reply = (200, b"{}")
        self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.seen.append((self.command, self.path, dict(self.headers), body))
                status, raw = outer.reply
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = _handle

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()


class HttpStepClientTests(_Stub):
    def _call(self, tool="t"):
        return HttpStepClient("policy", self.base + "/").call(tool)

    def assertFails(self, text):
        with self.assertRaises(ServerError) as caught:
            self._call()
        self.assertIn(text, str(caught.exception))

    def test_success_and_close(self):
        self.reply = (200, b'{"case_count": 1}')
        client = HttpStepClient("policy", self.base)
        self.assertEqual(client.call("t", {"a": 1}), {"case_count": 1})
        self.assertEqual(self.seen[0][1], "/v1/tools/t")
        self.assertIsNone(client.close())

    def test_http_error_with_message(self):
        self.reply = (500, b'{"message": "kaboom"}')
        self.assertFails("kaboom")

    def test_http_error_with_error_key(self):
        self.reply = (400, b'{"error": "bad_case"}')
        self.assertFails("bad_case")

    def test_http_error_with_non_json_body(self):
        self.reply = (502, b"gateway text")
        self.assertFails("gateway text")

    def test_http_error_with_empty_body(self):
        self.reply = (503, b"")
        self.assertFails("HTTP 503")

    def test_transport_error(self):
        self.server.shutdown()
        self.server.server_close()
        self.assertFails("transport error")
        # Stop the cleanup from shutting down twice.
        self.server = mock.MagicMock()

    def test_non_json_success(self):
        self.reply = (200, b"not json")
        self.assertFails("not JSON")

    def test_non_200_success_status(self):
        self.reply = (201, b'{"error": "odd"}')
        self.assertFails("odd")

    def test_non_object_summary(self):
        self.reply = (200, b"[1]")
        self.assertFails("not an object")

    def test_error_summary(self):
        self.reply = (200, b'{"error": "nope", "message": "explained"}')
        self.assertFails("explained")
        self.reply = (200, b'{"error": "nope"}')
        self.assertFails("nope")


class DefaultTransportTests(_Stub):
    def test_get_and_post(self):
        self.reply = (200, b'{"ok": true}')
        self.assertEqual(default_transport("GET", self.base + "/x"), (200, {"ok": True}))
        self.assertNotIn("Content-Type", self.seen[0][2])
        status, body = default_transport(
            "POST", self.base + "/x", {"a": 1}, {"Idempotency-Key": "k"}
        )
        self.assertEqual((status, body), (200, {"ok": True}))
        self.assertEqual(self.seen[1][2]["Idempotency-Key"], "k")
        self.assertEqual(self.seen[1][3], b'{"a": 1}')

    def test_http_error_is_parsed(self):
        self.reply = (404, b'{"error": "not_found"}')
        self.assertEqual(default_transport("GET", self.base), (404, {"error": "not_found"}))

    def test_empty_bad_and_non_object_bodies(self):
        self.reply = (200, b"")
        self.assertEqual(default_transport("GET", self.base), (200, {}))
        self.reply = (200, b"\xff\xfe")
        self.assertEqual(default_transport("GET", self.base), (200, {}))
        self.reply = (200, b"not json")
        self.assertEqual(default_transport("GET", self.base), (200, {}))
        self.reply = (200, b"[1]")
        self.assertEqual(default_transport("GET", self.base), (200, {}))

    def test_transport_error(self):
        self.server.shutdown()
        self.server.server_close()
        self.assertEqual(default_transport("GET", self.base), (0, {"error": "transport_error"}))
        self.server = mock.MagicMock()


class ServerSetTests(unittest.TestCase):
    def _http_set(self):
        env = {"FNOL_STEP_TRANSPORT": "http", "FNOL_POLICY_URL": "http://policy.test:1/"}
        with mock.patch.dict(os.environ, env):
            return ServerSet(state_dir=Path("."), mock_url="http://mock")

    def test_http_mode_builds_http_clients(self):
        servers = self._http_set()
        self.assertEqual(set(servers.servers), {"intake", "policy", "rules", "citations"})
        self.assertEqual(servers.servers["policy"].base_url, "http://policy.test:1")

    def test_context_manager_closes(self):
        with self._http_set() as servers:
            with mock.patch.object(servers.servers["intake"], "close") as close:
                servers.close()
            close.assert_called_once()

    def test_stdio_mode_passes_directories(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"FNOL_STEP_TRANSPORT": ""}
        ), mock.patch("fnol_analyst.servers.StdioServer") as stdio:
            ServerSet(
                state_dir=Path(tmp),
                mock_url="http://mock",
                documents_dir=Path("/docs"),
                policies_dir=Path("/pols"),
                python="py",
            )
        self.assertEqual(stdio.call_count, 4)
        env = stdio.call_args.kwargs["env"]
        self.assertEqual(env["FNOL_DOCUMENTS_DIR"], "/docs")
        self.assertEqual(env["FNOL_POLICIES_DIR"], "/pols")
        self.assertEqual(stdio.call_args.args[1][0], "py")

    def test_one_row_validation(self):
        with self.assertRaisesRegex(RuntimeError, "returned no row"):
            ServerSet._one_row({"policies": []}, "policy", "C1")
        with self.assertRaisesRegex(RuntimeError, "not an object"):
            ServerSet._one_row({"policies": ["x"]}, "policy", "C1")
        self.assertEqual(ServerSet._one_row({"policies": [{"a": 1}]}, "policy", "C1"), {"a": 1})

    def test_unknown_step(self):
        with self.assertRaises(KeyError):
            self._http_set().call("nope")


if __name__ == "__main__":
    unittest.main()
