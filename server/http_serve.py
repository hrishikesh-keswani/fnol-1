"""HTTP front door for the four FNOL step tools.

Same tool functions as the stdio MCP servers. POST /v1/tools/{tool_name}
with an optional JSON body ``{"case_id": "..."}``. GET /health returns ok.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

ToolFn = Callable[..., dict[str, Any]]


def create_server(
    name: str,
    tools: dict[str, ToolFn],
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> ThreadingHTTPServer:
    tool_map = dict(tools)

    class Handler(BaseHTTPRequestHandler):
        server_version = f"fnol-{name}/1.0"

        def log_message(self, fmt, *args):
            return

        def _json(self, status: int, body: dict):
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/health":
                self._json(200, {"status": "ok", "service": name})
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/") or "/"
            prefix = "/v1/tools/"
            if not path.startswith(prefix):
                self._json(404, {"error": "not_found"})
                return
            tool = path[len(prefix) :]
            if tool not in tool_map:
                self._json(404, {"error": "unknown_tool", "tool": tool})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            args: dict[str, Any] = {}
            if raw:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._json(400, {"error": "invalid_json"})
                    return
                if not isinstance(parsed, dict):
                    self._json(400, {"error": "body_must_be_object"})
                    return
                args = parsed
            try:
                if tool == "start_ingestion":
                    summary = tool_map[tool]()
                else:
                    summary = tool_map[tool](case_id=args.get("case_id"))
            except Exception as exc:
                code = getattr(exc, "error_code", "server_error")
                self._json(500, {"error": code, "message": str(exc)})
                return
            self._json(200, summary)

    return ThreadingHTTPServer((host, port), Handler)


def serve_tools(name: str, tools: dict[str, ToolFn]) -> None:
    from fnol_foundation.db import ensure_ready, using_postgres

    if using_postgres():
        ensure_ready()
    host = os.environ.get("FNOL_HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("FNOL_HTTP_PORT", "8080"))
    server = create_server(name, tools, host=host, port=port)
    print(f"{name} HTTP on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
