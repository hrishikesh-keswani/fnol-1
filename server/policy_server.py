"""Stdio MCP server for Step 2. Tool: start_policy."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from fnol_policy.pipeline import start_policy
from server.rpc import json_error, serve as serve_rpc, tool_text

PROTOCOL = "2024-11-05"
SERVER_INFO = {"name": "fnol-policy", "version": "1.0.0"}

START_POLICY = {
    "name": "start_policy",
    "description": (
        "GET policy for each intake revision, drop private_token, and store the "
        "canonical JSON on that revision. Select the single interval containing "
        "the incident. Zero or several matches store a null version. A wording "
        "file that intake did not load is omitted from policy_ids."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "case_id": {
                "type": "string",
                "description": "Optional. Omit to select a policy for every latest intake revision.",
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
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [START_POLICY]}}
    if method == "tools/call":
        return _call_tool(msg_id, message.get("params") or {})
    return json_error(msg_id, -32601, "method not found")


def _call_tool(msg_id, params):
    if params.get("name") != "start_policy":
        return json_error(msg_id, -32602, "unknown tool")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return json_error(msg_id, -32602, "arguments must be an object")
    try:
        summary = start_policy(case_id=args.get("case_id"))
    except Exception as exc:
        return tool_text(msg_id, f"{exc.__class__.__name__}: {exc}", is_error=True)
    return tool_text(msg_id, json.dumps(summary))


def serve(stdin=None, stdout=None):
    serve_rpc(handle, stdin=stdin, stdout=stdout)


def serve_http():
    from server.http_serve import serve_tools

    serve_tools("policy", {"start_policy": start_policy})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--http":
        serve_http()
    else:
        serve()
