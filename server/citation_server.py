"""Stdio MCP server for Step 4. Tool: start_citations."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from fnol_citations.pipeline import start_citations
from server.rpc import json_error, serve as serve_rpc, tool_text

PROTOCOL = "2024-11-05"
SERVER_INFO = {"name": "fnol-citations", "version": "1.0.0"}

START_CITATIONS = {
    "name": "start_citations",
    "description": (
        "Slice exact quotes from the raw text and raw JSON already stored for "
        "each intake revision. Requires the policy decision and the route "
        "decision for that revision. Omits a claims quote when rules stored "
        "no claims snapshot. Refuses any quote that contains private_token "
        "or SYNTHETIC-PRIVATE-."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "case_id": {
                "type": "string",
                "description": "Optional. Omit to cite every case that has a latest input revision.",
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
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [START_CITATIONS]}}
    if method == "tools/call":
        return _call_tool(msg_id, message.get("params") or {})
    return json_error(msg_id, -32601, "method not found")


def _call_tool(msg_id, params):
    if params.get("name") != "start_citations":
        return json_error(msg_id, -32602, "unknown tool")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return json_error(msg_id, -32602, "arguments must be an object")
    try:
        summary = start_citations(case_id=args.get("case_id"))
    except Exception as exc:
        return tool_text(msg_id, f"{exc.__class__.__name__}: {exc}", is_error=True)
    return tool_text(msg_id, json.dumps(summary))


def serve(stdin=None, stdout=None):
    serve_rpc(handle, stdin=stdin, stdout=stdout)


def serve_http():
    from server.http_serve import serve_tools

    serve_tools("citations", {"start_citations": start_citations})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--http":
        serve_http()
    else:
        serve()
