"""Shared stdio JSON-RPC loop for the FNOL MCP shells."""

import json
import sys


def json_error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def tool_text(msg_id, text, is_error=False):
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
    }


def serve(handle, stdin=None, stdout=None):
    """Read one JSON-RPC message per line and write one response per line."""
    incoming = sys.stdin if stdin is None else stdin
    outgoing = sys.stdout if stdout is None else stdout
    for line in incoming:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = json_error(None, -32700, "parse error")
        else:
            response = handle(message)
        if response is None:
            continue
        outgoing.write(json.dumps(response, ensure_ascii=False) + "\n")
        outgoing.flush()
