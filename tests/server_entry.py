"""Shared helpers for the stdio MCP server tests.

``run_as_main`` executes a script the way ``python path/to/script.py`` does,
with the repo roots removed from ``sys.path`` so the path bootstrap runs too.
``McpServerContract`` holds the JSON-RPC checks every step server must pass.
"""

import contextlib
import io
import json
import runpy
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def run_as_main(script, argv=(), stdin_text=""):
    """Run ``script`` as ``__main__`` and return what it wrote to stdout."""
    drop = {ROOT.resolve(), (ROOT / "src").resolve()}
    kept = [entry for entry in sys.path if Path(entry or ".").resolve() not in drop]
    out = io.StringIO()
    with (
        mock.patch.object(sys, "path", kept),
        mock.patch.object(sys, "argv", [str(script), *argv]),
        mock.patch.object(sys, "stdin", io.StringIO(stdin_text)),
        contextlib.redirect_stdout(out),
    ):
        runpy.run_path(str(script), run_name="__main__")
    return out.getvalue()


class McpServerContract:
    """Mixin for unittest.TestCase subclasses. Subclasses set the class attributes."""

    module = None
    script = None
    tool_name = None
    function_name = None
    server_name = None
    http_name = None
    takes_args = True

    def _call(self, params=None, msg_id=3):
        message = {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call"}
        if params is not None:
            message["params"] = params
        return self.module.handle(message)

    def test_rejects_non_object_message(self):
        response = self.module.handle([])
        self.assertEqual(response["error"]["code"], -32600)
        self.assertIsNone(response["id"])

    def test_rejects_message_without_method(self):
        response = self.module.handle({"jsonrpc": "2.0", "id": 7})
        self.assertEqual(response["error"]["code"], -32600)
        self.assertEqual(response["id"], 7)

    def test_notification_gets_no_response(self):
        self.assertIsNone(
            self.module.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_initialize_reports_server_info(self):
        response = self.module.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        result = response["result"]
        self.assertEqual(result["serverInfo"]["name"], self.server_name)
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        self.assertEqual(result["capabilities"], {"tools": {}})

    def test_ping_returns_empty_result(self):
        response = self.module.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(response, {"jsonrpc": "2.0", "id": 2, "result": {}})

    def test_unknown_method(self):
        response = self.module.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})
        self.assertEqual(response["error"]["code"], -32601)
        self.assertEqual(response["id"], 4)

    def test_unknown_tool(self):
        response = self._call({"name": "other"})
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(response["error"]["message"], "unknown tool")

    def test_call_without_params_is_an_unknown_tool(self):
        self.assertEqual(self._call()["error"]["message"], "unknown tool")

    def test_tool_failure_is_reported_as_tool_error(self):
        params = {"name": self.tool_name, "arguments": {}}
        with mock.patch.object(self.module, self.function_name, side_effect=RuntimeError("boom")):
            response = self._call(params)
        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], "RuntimeError: boom")

    def test_tool_success_returns_summary_json(self):
        params = {"name": self.tool_name, "arguments": {"case_id": "AUTO-D01"}}
        with mock.patch.object(
            self.module, self.function_name, return_value={"case_count": 1}
        ) as tool:
            response = self._call(params)
        if self.takes_args:
            tool.assert_called_once_with(case_id="AUTO-D01")
        else:
            tool.assert_called_once_with()
        result = response["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), {"case_count": 1})

    def test_missing_arguments_default_to_all_cases(self):
        with mock.patch.object(self.module, self.function_name, return_value={}) as tool:
            self._call({"name": self.tool_name})
        if self.takes_args:
            tool.assert_called_once_with(case_id=None)
        else:
            tool.assert_called_once_with()

    def test_arguments_must_be_an_object(self):
        if not self.takes_args:
            self.skipTest("this tool takes no arguments")
        response = self._call({"name": self.tool_name, "arguments": ["AUTO-D01"]})
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(response["error"]["message"], "arguments must be an object")

    def test_serve_skips_blank_lines_and_reports_parse_errors(self):
        incoming = io.StringIO(
            "\n".join([
                "",
                "not json",
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 5, "method": "ping"}),
            ]) + "\n"
        )
        outgoing = io.StringIO()
        self.module.serve(stdin=incoming, stdout=outgoing)
        lines = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1]["result"], {})

    def test_serve_http_registers_the_tool(self):
        with mock.patch("server.http_serve.serve_tools") as serve_tools:
            self.module.serve_http()
        serve_tools.assert_called_once()
        name, tools = serve_tools.call_args.args
        self.assertEqual(name, self.http_name)
        self.assertEqual(list(tools), [self.tool_name])
        self.assertIs(tools[self.tool_name], getattr(self.module, self.function_name))

    def test_script_serves_stdio_by_default(self):
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
        output = run_as_main(self.script, stdin_text=request)
        self.assertEqual(json.loads(output), {"jsonrpc": "2.0", "id": 1, "result": {}})

    def test_script_ignores_other_flags_and_serves_stdio(self):
        output = run_as_main(self.script, ["--other"], stdin_text="")
        self.assertEqual(output, "")

    def test_script_serves_http_with_flag(self):
        with mock.patch("server.http_serve.serve_tools") as serve_tools:
            run_as_main(self.script, ["--http"])
        serve_tools.assert_called_once()
        self.assertEqual(serve_tools.call_args.args[0], self.http_name)
        self.assertEqual(list(serve_tools.call_args.args[1]), [self.tool_name])
