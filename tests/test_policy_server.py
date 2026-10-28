"""Policy MCP server: one tool, pipeline stores the policy decision."""

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import server.policy_server as policy_server
from server.policy_server import handle, serve
from server_entry import McpServerContract


class PolicyServerTests(McpServerContract, unittest.TestCase):
    module = policy_server
    script = ROOT / "server" / "policy_server.py"
    tool_name = "start_policy"
    function_name = "start_policy"
    server_name = "fnol-policy"
    http_name = "policy"

    def test_lists_start_policy_tool(self):
        listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = listed["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["start_policy"])
        self.assertIn("case_id", tools[0]["inputSchema"]["properties"])
        self.assertEqual(tools[0]["inputSchema"]["type"], "object")

    def test_stdio_calls_pipeline(self):
        calls = []

        def fake_start_policy(**kwargs):
            calls.append(kwargs)
            return {
                "case_count": 1,
                "policies": [{"case_id": "AUTO-D01", "selected_policy_version": "2"}],
            }

        with mock.patch.object(policy_server, "start_policy", fake_start_policy):
            incoming = io.StringIO(
                "\n".join([
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                    json.dumps({
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "start_policy",
                            "arguments": {"case_id": "AUTO-D01"},
                        },
                    }),
                ]) + "\n"
            )
            outgoing = io.StringIO()
            serve(stdin=incoming, stdout=outgoing)
        lines = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["result"]["serverInfo"]["name"], "fnol-policy")
        self.assertEqual(lines[1]["result"]["tools"][0]["name"], "start_policy")
        body = json.loads(lines[2]["result"]["content"][0]["text"])
        self.assertEqual(body["policies"][0]["selected_policy_version"], "2")
        self.assertFalse(lines[2]["result"]["isError"])
        self.assertEqual(calls, [{"case_id": "AUTO-D01"}])

    def test_pipeline_error_is_a_tool_error_over_stdio(self):
        with mock.patch.object(
            policy_server, "start_policy", side_effect=ValueError("no intake revision")
        ):
            incoming = io.StringIO(
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "start_policy", "arguments": {}},
                }) + "\n"
            )
            outgoing = io.StringIO()
            serve(stdin=incoming, stdout=outgoing)
        response = json.loads(outgoing.getvalue())
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(
            response["result"]["content"][0]["text"], "ValueError: no intake revision"
        )


if __name__ == "__main__":
    unittest.main()
