"""HTTP front door for the step tools: routing, body parsing, errors, startup."""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from server.http_serve import create_server, serve_tools


class CodedError(Exception):
    error_code = "policy_unavailable"


class HttpServeTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def start_ingestion():
            self.calls.append(("start_ingestion", {}))
            return {"case_count": 2}

        def start_policy(case_id=None):
            self.calls.append(("start_policy", {"case_id": case_id}))
            return {"case_id": case_id}

        def start_routing(case_id=None):
            raise CodedError("policy missing")

        def start_citations(case_id=None):
            raise RuntimeError("boom")

        self.server = create_server(
            "demo",
            {
                "start_ingestion": start_ingestion,
                "start_policy": start_policy,
                "start_routing": start_routing,
                "start_citations": start_citations,
            },
            host="127.0.0.1",
            port=0,
        )
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def _request(self, method, path, data=None):
        req = urllib.request.Request(self.base + path, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self):
        self.assertEqual(self._request("GET", "/health"), (200, {"status": "ok", "service": "demo"}))
        self.assertEqual(self._request("GET", "/health/?probe=1")[0], 200)

    def test_get_unknown_path_is_not_found(self):
        self.assertEqual(self._request("GET", "/other"), (404, {"error": "not_found"}))
        self.assertEqual(self._request("GET", ""), (404, {"error": "not_found"}))

    def test_post_outside_tool_prefix_is_not_found(self):
        self.assertEqual(self._request("POST", "/health"), (404, {"error": "not_found"}))

    def test_post_unknown_tool(self):
        status, body = self._request("POST", "/v1/tools/missing")
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "unknown_tool", "tool": "missing"})

    def test_ingestion_tool_takes_no_arguments(self):
        status, body = self._request("POST", "/v1/tools/start_ingestion", b'{"case_id": "X"}')
        self.assertEqual((status, body), (200, {"case_count": 2}))
        self.assertEqual(self.calls, [("start_ingestion", {})])

    def test_tool_receives_case_id_from_body(self):
        status, body = self._request(
            "POST", "/v1/tools/start_policy/", json.dumps({"case_id": "AUTO-D01"}).encode()
        )
        self.assertEqual((status, body), (200, {"case_id": "AUTO-D01"}))
        self.assertEqual(self.calls, [("start_policy", {"case_id": "AUTO-D01"})])

    def test_empty_body_runs_every_case(self):
        status, body = self._request("POST", "/v1/tools/start_policy")
        self.assertEqual((status, body), (200, {"case_id": None}))

    def test_invalid_json_body(self):
        for raw in (b"{not json", b"\xff\xfe"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    self._request("POST", "/v1/tools/start_policy", raw),
                    (400, {"error": "invalid_json"}),
                )
        self.assertEqual(self.calls, [])

    def test_body_must_be_an_object(self):
        self.assertEqual(
            self._request("POST", "/v1/tools/start_policy", b"[1, 2]"),
            (400, {"error": "body_must_be_object"}),
        )

    def test_tool_error_code_is_returned(self):
        status, body = self._request("POST", "/v1/tools/start_routing")
        self.assertEqual(status, 500)
        self.assertEqual(body, {"error": "policy_unavailable", "message": "policy missing"})

    def test_tool_error_without_code_is_server_error(self):
        status, body = self._request("POST", "/v1/tools/start_citations")
        self.assertEqual(status, 500)
        self.assertEqual(body, {"error": "server_error", "message": "boom"})


class ServeToolsTests(unittest.TestCase):
    def _serve(self, *, postgres, env=None, serve_forever=None):
        fake_server = mock.Mock()
        if serve_forever is not None:
            fake_server.serve_forever.side_effect = serve_forever
        out = io.StringIO()
        with (
            mock.patch.dict("os.environ", env or {}, clear=False),
            mock.patch("fnol_foundation.db.using_postgres", return_value=postgres),
            mock.patch("fnol_foundation.db.ensure_ready") as ensure_ready,
            mock.patch("server.http_serve.create_server", return_value=fake_server) as create,
            redirect_stdout(out),
        ):
            serve_tools("policy", {"start_policy": lambda **_kw: {}})
        return fake_server, create, ensure_ready, out.getvalue()

    def test_serves_on_configured_host_and_port_then_closes(self):
        server, create, ensure_ready, output = self._serve(
            postgres=False,
            env={"FNOL_HTTP_HOST": "127.0.0.1", "FNOL_HTTP_PORT": "9123"},
        )
        ensure_ready.assert_not_called()
        self.assertEqual(create.call_args.args[0], "policy")
        self.assertEqual(create.call_args.kwargs, {"host": "127.0.0.1", "port": 9123})
        server.serve_forever.assert_called_once_with()
        server.server_close.assert_called_once_with()
        self.assertIn("policy HTTP on http://127.0.0.1:9123", output)

    def test_defaults_and_postgres_readiness(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("FNOL_HTTP_HOST", None)
            os.environ.pop("FNOL_HTTP_PORT", None)
            _server, create, ensure_ready, _out = self._serve(postgres=True)
        ensure_ready.assert_called_once_with()
        self.assertEqual(create.call_args.kwargs, {"host": "0.0.0.0", "port": 8080})

    def test_keyboard_interrupt_still_closes_the_server(self):
        server, _create, _ready, _out = self._serve(postgres=False, serve_forever=KeyboardInterrupt)
        server.server_close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
