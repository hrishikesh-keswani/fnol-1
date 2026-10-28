"""The four step servers as one set the analyst can call independently or in sequence."""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from fnol_analyst.http_steps import HttpStepClient, step_urls
from fnol_analyst.mcp import StdioServer

ROOT = Path(__file__).resolve().parents[2]

SERVER_FILES = {
    "intake": "server/input_server.py",
    "policy": "server/policy_server.py",
    "rules": "server/rules_server.py",
    "citations": "server/citation_server.py",
}
TOOLS = {
    "intake": "start_ingestion",
    "policy": "start_policy",
    "rules": "start_routing",
    "citations": "start_citations",
}
_ROW_KEYS = {
    "policy": "policies",
    "rules": "routes",
    "citations": "cases",
}


def http_transport() -> bool:
    return os.environ.get("FNOL_STEP_TRANSPORT", "").strip().lower() == "http"


class PrepareError(RuntimeError):
    """One or more cases failed during prepare_all. Successful cases stay recorded."""

    def __init__(self, failures: dict[str, Exception]):
        self.failures = dict(failures)
        self.error_code = "prepare_failed"
        detail = "; ".join(f"{case_id}: {exc}" for case_id, exc in self.failures.items())
        super().__init__(f"prepare failed for {detail}")


class ServerSet:
    """Spawns the four stdio servers, or calls their HTTP peers when configured."""

    def __init__(
        self,
        *,
        state_dir: Path,
        mock_url: str,
        documents_dir: Path | None = None,
        policies_dir: Path | None = None,
        python: str | None = None,
    ):
        self._http = http_transport()
        if self._http:
            urls = step_urls()
            self.servers = {
                name: HttpStepClient(name, urls[name]) for name in SERVER_FILES
            }
            return
        env = dict(os.environ)
        env["FNOL_STATE_DIR"] = str(state_dir)
        env["FNOL_MOCK_URL"] = mock_url
        if documents_dir is not None:
            env["FNOL_DOCUMENTS_DIR"] = str(documents_dir)
        if policies_dir is not None:
            env["FNOL_POLICIES_DIR"] = str(policies_dir)
        interpreter = python or sys.executable
        self.servers: dict[str, StdioServer] = {
            name: StdioServer(name, [interpreter, str(ROOT / path)], cwd=str(ROOT), env=env)
            for name, path in SERVER_FILES.items()
        }

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self) -> None:
        for server in self.servers.values():
            server.close()

    def call(self, step: str, case_id: str | None = None) -> dict[str, Any]:
        """Call one step server. intake takes no case id; the others accept one."""
        if step not in TOOLS:
            raise KeyError(step)
        arguments = {} if step == "intake" or case_id is None else {"case_id": case_id}
        return self.servers[step].call(TOOLS[step], arguments)

    def ingest(self) -> dict[str, Any]:
        return self.call("intake")

    def select_policy(self, case_id: str | None = None) -> dict[str, Any]:
        return self.call("policy", case_id)

    def route(self, case_id: str | None = None) -> dict[str, Any]:
        return self.call("rules", case_id)

    def cite(self, case_id: str | None = None) -> dict[str, Any]:
        return self.call("citations", case_id)

    def prepare_case(self, case_id: str) -> dict[str, Any]:
        """Policy, then rules, then citations for one already-ingested case.

        Those three steps share one revision and must stay in order. Across
        cases they overlap: one case can sit in policy while another is in
        rules and a third is in citations.
        """
        policy = self._one_row(self.select_policy(case_id), "policy", case_id)
        route = self._one_row(self.route(case_id), "rules", case_id)
        cited = self._one_row(self.cite(case_id), "citations", case_id)
        return {"case_id": case_id, "policy": policy, "route": route, "citations": cited}

    def prepare_all(self, case_ids: list[str], *, workers: int = 3) -> dict[str, dict[str, Any]]:
        """Run prepare_case for many cases. Each server still handles one call at a time.

        Every case is attempted. If any fail, successful writes stay on disk and
        PrepareError lists every failure.
        """
        results: dict[str, dict[str, Any]] = {}
        failures: dict[str, Exception] = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(self.prepare_case, case_id): case_id for case_id in case_ids}
            for future in as_completed(futures):
                case_id = futures[future]
                try:
                    results[case_id] = future.result()
                except Exception as exc:
                    failures[case_id] = exc
        if failures:
            raise PrepareError(failures)
        return results

    @staticmethod
    def _one_row(summary: dict[str, Any], step: str, case_id: str) -> dict[str, Any]:
        key = _ROW_KEYS[step]
        rows = summary.get(key)
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"{step} returned no row for {case_id}")
        row = rows[0]
        if not isinstance(row, dict):
            raise RuntimeError(f"{step} row for {case_id} was not an object")
        return row
