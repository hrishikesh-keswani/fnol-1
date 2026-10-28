"""HTTP client for the four step servers when FNOL_STEP_TRANSPORT=http.

Each step server listens on FNOL_HTTP_PORT, which defaults to 8080
(``server/http_serve.py``). On one machine the four servers cannot share
that port, so the client defaults below use 8081-8084. ECS does not use
those defaults. Cloud Map gives each step its own name on port 8080, and
the analyst task sets:

  FNOL_INTAKE_URL=http://intake.fnol.internal:8080
  FNOL_POLICY_URL=http://policy.fnol.internal:8080
  FNOL_RULES_URL=http://rules.fnol.internal:8080
  FNOL_CITATIONS_URL=http://citations.fnol.internal:8080
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from fnol_analyst.mcp import ServerError

DEFAULT_URLS = {
    "intake": "http://127.0.0.1:8081",
    "policy": "http://127.0.0.1:8082",
    "rules": "http://127.0.0.1:8083",
    "citations": "http://127.0.0.1:8084",
}

ENV_URLS = {
    "intake": "FNOL_INTAKE_URL",
    "policy": "FNOL_POLICY_URL",
    "rules": "FNOL_RULES_URL",
    "citations": "FNOL_CITATIONS_URL",
}


def step_urls() -> dict[str, str]:
    urls = dict(DEFAULT_URLS)
    for name, env_name in ENV_URLS.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            urls[name] = value.rstrip("/")
    return urls


class HttpStepClient:
    """One remote step server. Same ``call(tool, arguments)`` shape as StdioServer."""

    def __init__(self, name: str, base_url: str, *, timeout: float = 60.0):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/v1/tools/{tool}"
        body = json.dumps(arguments or {}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            status = exc.code
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {}
            message = payload.get("message") or payload.get("error") or raw or f"HTTP {status}"
            raise ServerError(self.name, tool, str(message)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ServerError(self.name, tool, f"transport error: {exc}") from exc
        try:
            summary = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ServerError(self.name, tool, "response was not JSON") from exc
        if status != 200:
            raise ServerError(self.name, tool, summary.get("error", f"HTTP {status}"))
        if not isinstance(summary, dict):
            raise ServerError(self.name, tool, "tool summary was not an object")
        if "error" in summary and "case_count" not in summary and "revisions" not in summary:
            raise ServerError(self.name, tool, str(summary.get("message") or summary["error"]))
        return summary

    def close(self) -> None:
        return
