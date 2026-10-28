"""Rules MCP server: one tool, pipeline writes claims + returns routes."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from fnol_foundation.stores import Stores
from fnol_rules.claims import ClaimsAttempt, ClaimsFetchResult
from fnol_rules.pipeline import start_routing
from fnol_rules.types import ClaimsSnapshot
import server.rules_server as rules_server
from server.rules_server import handle, serve
from server_entry import McpServerContract


def _claims_ok(case_id, base_url, **_kw):
    raw = {
        "source_id": f"{case_id}-CLAIMS",
        "existing_claims": [],
        "intake_deliveries": 1,
        "loss_reference": f"loss-{case_id}",
    }
    return ClaimsFetchResult(
        snapshot=ClaimsSnapshot(available=True, intake_deliveries=1),
        attempts=(ClaimsAttempt(1, 200, raw),),
        raw_json=raw,
    )


class RulesServerTests(unittest.TestCase):
    def test_lists_start_routing_tool(self):
        listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = listed["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["start_routing"])
        self.assertIn("case_id", tools[0]["inputSchema"]["properties"])
        self.assertNotIn("selected_policy_version", tools[0]["inputSchema"]["properties"])

    def test_pipeline_routes_from_intake_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            stores = Stores(tmp)
            revision = stores.input.append_revision(
                "AUTO-D01",
                {
                    "loss_type": "collision",
                    "vehicle_id": "VEH-D01",
                    "incident_timestamp": "2026-10-01T12:00:00Z",
                    "driver_name": "Alex Rowan D01",
                    "driver_name_supplied": True,
                    "incident_location": "Cedar Street",
                    "incident_location_supplied": True,
                    "injury_reported": False,
                    "vehicle_drivable": True,
                    "referral_indicator": "none",
                    "loss_reference": "auto-loss-D01",
                    "injection_field_status": "clean",
                    "injection_model_status": "clean",
                },
                document_set_sha256="abc",
                trace_id="trace-d01",
            )
            stores.input.put_decision(
                "AUTO-D01",
                revision.revision,
                "policy",
                {
                    "selected_policy_version": "2",
                    "policy_available": True,
                    "reason": "matched",
                    "wording_source": "AUTO-WORDING-V2",
                    "endorsements": [],
                    "source_id": "AUTO-D01-POLICY",
                },
                policy_sha256="policy-sha",
            )
            summary = start_routing(
                case_id="AUTO-D01",
                mock_url="http://mock",
                state_dir_path=tmp,
                fetch=_claims_ok,
            )
            self.assertEqual(summary["case_count"], 1)
            row = summary["routes"][0]
            self.assertEqual(row["route"], "standard")
            self.assertEqual(row["route_rule"], "AUTO-TRIAGE-01")
            self.assertFalse(row["duplicate_delivery"])
            stored = Stores(tmp).input.get_tool_payload("AUTO-D01", "claims", 1)
            self.assertIsNotNone(stored)

    def test_stdio_calls_pipeline(self):
        import server.rules_server as mcp

        original = mcp.start_routing
        mcp.start_routing = lambda **_kw: {
            "case_count": 1,
            "routes": [{"case_id": "AUTO-D01", "route": "standard"}],
        }
        try:
            incoming = io.StringIO(
                "\n".join([
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                    json.dumps({
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "start_routing",
                            "arguments": {"case_id": "AUTO-D01"},
                        },
                    }),
                ]) + "\n"
            )
            outgoing = io.StringIO()
            serve(stdin=incoming, stdout=outgoing)
        finally:
            mcp.start_routing = original
        lines = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual(lines[0]["result"]["serverInfo"]["name"], "fnol-rules")
        body = json.loads(lines[1]["result"]["content"][0]["text"])
        self.assertEqual(body["routes"][0]["route"], "standard")
        self.assertFalse(lines[1]["result"]["isError"])


class RulesServerContractTests(McpServerContract, unittest.TestCase):
    module = rules_server
    script = ROOT / "server" / "rules_server.py"
    tool_name = "start_routing"
    function_name = "start_routing"
    server_name = "fnol-rules"
    http_name = "rules"


if __name__ == "__main__":
    unittest.main()
