"""Minimal stdio MCP client for one FNOL step server.

Calls on the same server are serialized with a lock: one request line, one
response line. Different servers are separate processes and can run at once.

Each read waits at most ``timeout`` seconds. Child stderr is kept so a crash
or import error is visible in the raised message. After initialize, the client
sends ``notifications/initialized`` per the 2024-11-05 MCP handshake.
"""

from __future__ import annotations

import json
import select
import subprocess
import threading
import time
from collections import deque
from typing import Any

DEFAULT_TIMEOUT = 60.0


class ServerError(RuntimeError):
    def __init__(self, server: str, tool: str, message: str):
        super().__init__(f"{server}.{tool}: {message}")
        self.server = server
        self.tool = tool
        self.error_code = "server_error"


class StdioServer:
    def __init__(
        self,
        name: str,
        command: list[str],
        *,
        cwd=None,
        env=None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.name = name
        self.timeout = timeout
        self._lock = threading.Lock()
        self._next_id = 0
        self._stderr: deque[str] = deque(maxlen=40)
        self._proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        try:
            info = self._request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
            self.server_info = info.get("serverInfo", {})
            self._notify("notifications/initialized")
        except Exception:
            self.close()
            raise

    def _drain_stderr(self) -> None:
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            self._stderr.append(line.rstrip("\n"))

    def _stderr_tail(self) -> str:
        if not self._stderr:
            return ""
        return "stderr: " + " | ".join(self._stderr)

    def _fail(self, tool: str, message: str) -> ServerError:
        tail = self._stderr_tail()
        if tail:
            message = f"{message} ({tail})"
        return ServerError(self.name, tool, message)

    def _readline(self, tool: str) -> str:
        stdout = self._proc.stdout
        assert stdout is not None
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._fail(tool, f"timed out after {self.timeout:g}s waiting for response")
            ready, _, _ = select.select([stdout], [], [], min(remaining, 1.0))
            if ready:
                line = stdout.readline()
                if not line:
                    raise self._fail(tool, "server closed its output")
                return line
            if self._proc.poll() is not None:
                raise self._fail(tool, f"server exited with code {self._proc.returncode}")

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                message["params"] = params
            assert self._proc.stdin is not None
            self._proc.stdin.write(json.dumps(message) + "\n")
            self._proc.stdin.flush()

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
            if params is not None:
                message["params"] = params
            assert self._proc.stdin is not None
            self._proc.stdin.write(json.dumps(message) + "\n")
            self._proc.stdin.flush()
            line = self._readline(method)
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise self._fail(method, "response was not JSON") from exc
        if response.get("id") != msg_id:
            raise self._fail(method, "response id did not match request")
        if "error" in response:
            err = response["error"]
            detail = err.get("message") if isinstance(err, dict) else err
            raise self._fail(method, str(detail))
        return response.get("result") or {}

    def list_tools(self) -> list[str]:
        return [tool["name"] for tool in self._request("tools/list").get("tools", [])]

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call one tool and return its JSON summary. A tool error is raised."""
        result = self._request("tools/call", {"name": tool, "arguments": arguments or {}})
        text = "".join(
            part.get("text", "") for part in result.get("content", []) if part.get("type") == "text"
        )
        if result.get("isError"):
            raise self._fail(tool, text)
        try:
            summary = json.loads(text)
        except json.JSONDecodeError:
            raise self._fail(tool, "tool returned non-JSON text") from None
        if not isinstance(summary, dict):
            raise self._fail(tool, "tool summary was not an object")
        return summary

    def close(self) -> None:
        if self._proc.poll() is None:
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.close()
                self._proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self._proc.kill()
                self._proc.wait()
        if self._proc.stderr is not None:
            try:
                self._proc.stderr.close()
            except OSError:
                pass
        self._stderr_thread.join(timeout=1)
