"""Shared stdio JSON-RPC loop."""

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from server.rpc import json_error, serve, tool_text


class RpcTests(unittest.TestCase):
    def test_json_error_shape(self):
        self.assertEqual(
            json_error(9, -32601, "method not found"),
            {"jsonrpc": "2.0", "id": 9, "error": {"code": -32601, "message": "method not found"}},
        )

    def test_tool_text_marks_errors(self):
        ok = tool_text(1, "fine")
        self.assertFalse(ok["result"]["isError"])
        bad = tool_text(2, "broken", is_error=True)
        self.assertTrue(bad["result"]["isError"])
        self.assertEqual(bad["result"]["content"], [{"type": "text", "text": "broken"}])

    def test_serve_writes_one_line_per_response_and_skips_none(self):
        seen = []

        def handle(message):
            seen.append(message)
            return None if "id" not in message else {"id": message["id"], "text": "é"}

        incoming = io.StringIO('\n  \n{"method": "note"}\n{"id": 1}\n')
        outgoing = io.StringIO()
        serve(handle, stdin=incoming, stdout=outgoing)
        self.assertEqual(seen, [{"method": "note"}, {"id": 1}])
        self.assertEqual(outgoing.getvalue(), '{"id": 1, "text": "é"}\n')

    def test_serve_reports_parse_errors_without_calling_handler(self):
        handler = mock.Mock()
        outgoing = io.StringIO()
        serve(handler, stdin=io.StringIO("{oops\n"), stdout=outgoing)
        handler.assert_not_called()
        response = json.loads(outgoing.getvalue())
        self.assertIsNone(response["id"])
        self.assertEqual(response["error"]["code"], -32700)

    def test_serve_defaults_to_process_streams(self):
        outgoing = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", io.StringIO('{"id": 4}\n')),
            mock.patch.object(sys, "stdout", outgoing),
        ):
            serve(lambda message: {"id": message["id"]})
        self.assertEqual(json.loads(outgoing.getvalue()), {"id": 4})


if __name__ == "__main__":
    unittest.main()
