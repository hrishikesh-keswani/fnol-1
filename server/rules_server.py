"""Stdio MCP server for Step 3. Tool: start_routing."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from fnol_rules.pipeline import start_routing
from server.rpc import json_error, serve as serve_rpc, tool_text

PROTOCOL = "2024-11-05"
SERVER_INFO = {"name": "fnol-rules", "version": "1.0.0"}

START_ROUTING = {
    "name": "start_routing",
    "description": (
        "Fetch claims, retry a transport error or HTTP 5xx at most three times, "
        "screen extra-field labels and leftover lines, store the canonical claims "
        "JSON, and route with AUTO-TRIAGE-01. Requires an intake revision and the "
        "policy decision for that same revision. The model screen does not change "
        "the stored revision."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "case_id": {
                "type": "string",
                "description": "Optional. Omit to route every case that has a latest input revision.",
            },
        },
    },
}


def handle(message):
    if not isinstance(message, dict) or "method" not in message:
        return json_error(message.get("id") if isinstance(message, dict) else None, -32600, "invalid request")
    method = message["method"]
    if "id" not in message:
        return None
    msg_id = message["id"]
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [START_ROUTING]}}
    if method == "tools/call":
        return _call_tool(msg_id, message.get("params") or {})
    return json_error(msg_id, -32601, "method not found")


def _call_tool(msg_id, params):
    if params.get("name") != "start_routing":
        return json_error(msg_id, -32602, "unknown tool")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return json_error(msg_id, -32602, "arguments must be an object")
    try:
        summary = start_routing(case_id=args.get("case_id"))
    except Exception as exc:
        return tool_text(msg_id, f"{exc.__class__.__name__}: {exc}", is_error=True)
    return tool_text(msg_id, json.dumps(summary))


def serve(stdin=None, stdout=None):
    serve_rpc(handle, stdin=stdin, stdout=stdout)


def serve_http():
    from server.http_serve import serve_tools

    serve_tools("rules", {"start_routing": start_routing})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--http":
        serve_http()
    else:
        serve()
