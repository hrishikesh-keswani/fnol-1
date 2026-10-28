"""Stdio MCP client: timeouts and stderr on a stalled child."""

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fnol_analyst.mcp import ServerError, StdioServer


class McpClientTests(unittest.TestCase):
    def test_timeout_includes_stderr_from_a_hanging_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "hang.py"
            script.write_text(textwrap.dedent("""\
                import json
                import sys
                import time

                sys.stderr.write("child-ready\\n")
                sys.stderr.flush()
                for line in sys.stdin:
                    message = json.loads(line)
                    method = message.get("method")
                    if method == "initialize":
                        print(json.dumps({
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "result": {"serverInfo": {"name": "hang"}, "capabilities": {}},
                        }), flush=True)
                    elif method == "notifications/initialized":
                        continue
                    else:
                        time.sleep(60)
            """))
            server = StdioServer(
                "hang",
                [sys.executable, str(script)],
                timeout=0.4,
            )
            try:
                with self.assertRaises(ServerError) as caught:
                    server.list_tools()
                message = str(caught.exception)
                self.assertIn("timed out", message)
                self.assertIn("child-ready", message)
            finally:
                server.close()


CHILD = textwrap.dedent("""\
    import json
    import os
    import subprocess
    import sys

    def send(obj):
        print(json.dumps(obj), flush=True)

    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        mid = message.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {"serverInfo": {"name": "t"}}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "a"}, {"name": "b"}]}})
        elif method == "tools/call":
            name = message["params"]["name"]
            def text(value, error=False):
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "isError": error,
                    "content": [{"type": "image"}, {"type": "text", "text": value}],
                }})
            if name == "ok":
                text(json.dumps({"fine": True}))
            elif name == "tool_error":
                text("tool blew up", error=True)
            elif name == "non_json":
                text("not json")
            elif name == "non_object":
                text("[1]")
            elif name == "garbage":
                print("garbage line", flush=True)
            elif name == "wrong_id":
                send({"jsonrpc": "2.0", "id": mid + 100, "result": {}})
            elif name == "rpc_error_dict":
                send({"jsonrpc": "2.0", "id": mid, "error": {"message": "rpc says no"}})
            elif name == "rpc_error_str":
                send({"jsonrpc": "2.0", "id": mid, "error": "plain failure"})
            elif name == "close":
                sys.stdout.close()
                os._exit(0)
            elif name == "exit_quietly":
                subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
                os._exit(3)
""")


class McpBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        script = Path(self.tmp.name) / "child.py"
        script.write_text(CHILD)
        self.script = script

    def _server(self, **kwargs):
        server = StdioServer("t", [sys.executable, str(self.script)], timeout=5, **kwargs)
        self.addCleanup(server.close)
        return server

    def assertFails(self, server, tool, text):
        with self.assertRaises(ServerError) as caught:
            server.call(tool)
        self.assertIn(text, str(caught.exception))
        self.assertEqual(caught.exception.error_code, "server_error")
        return caught.exception

    def test_timeout_must_be_positive(self):
        with self.assertRaises(ValueError):
            StdioServer("t", [sys.executable, "-c", "pass"], timeout=0)

    def test_list_and_call(self):
        server = self._server()
        self.assertEqual(server.server_info, {"name": "t"})
        self.assertEqual(server.list_tools(), ["a", "b"])
        self.assertEqual(server.call("ok", None), {"fine": True})

    def test_notify_with_params(self):
        server = self._server()
        server._notify("notifications/custom", {"a": 1})
        self.assertEqual(server.call("ok"), {"fine": True})

    def test_tool_and_protocol_errors(self):
        server = self._server()
        self.assertFails(server, "tool_error", "tool blew up")
        self.assertFails(server, "non_json", "non-JSON")
        self.assertFails(server, "non_object", "not an object")
        self.assertFails(server, "garbage", "not JSON")
        self.assertFails(server, "wrong_id", "did not match")
        self.assertFails(server, "rpc_error_dict", "rpc says no")
        self.assertFails(server, "rpc_error_str", "plain failure")

    def test_server_closes_output(self):
        server = self._server()
        exc = self.assertFails(server, "close", "closed its output")
        self.assertEqual(str(exc), "t.tools/call: server closed its output")

    def test_server_exits_without_closing_output(self):
        server = self._server()
        self.assertFails(server, "exit_quietly", "exited with code 3")

    def test_initialize_failure_closes_child(self):
        with self.assertRaises(ServerError) as caught:
            StdioServer("bad", [sys.executable, "-c", "import sys; sys.stderr.write('oops\\n')"], timeout=5)
        self.assertIn("closed its output", str(caught.exception))

    def test_close_tolerates_stderr_oserror(self):
        server = self._server()
        real = server._proc.stderr
        fake = mock.MagicMock()
        fake.close.side_effect = OSError("bad")
        server._proc.stderr = fake
        server.close()
        real.close()
        server._proc.stderr = real

    def test_close_kills_a_child_that_ignores_stdin_close(self):
        server = self._server()
        with mock.patch.object(
            server._proc, "wait", side_effect=[subprocess.TimeoutExpired("x", 5), 0]
        ):
            server.close()
        self.assertIsNotNone(server._proc.poll())


if __name__ == "__main__":
    unittest.main()
