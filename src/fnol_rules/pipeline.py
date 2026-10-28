"""One-call rules pipeline. MCP and local callers share this entry."""

from __future__ import annotations

import os
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from fnol_foundation.stores import Stores, state_dir
from fnol_rules.adapter import fields_from_payload
from fnol_rules.claims import ClaimsFetchResult
from fnol_rules.screen import USE_DEFAULT
from fnol_rules.service import policy_from_decision, run_rules


class RulesError(Exception):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def start_routing(
    case_id: str | None = None,
    *,
    mock_url: str | None = None,
    state_dir_path: str | Path | None = None,
    fetch: Callable[..., ClaimsFetchResult] | None = None,
    screen_client: Any = USE_DEFAULT,
) -> dict[str, Any]:
    """Route latest intake revisions using the policy decision already stored for each one.

    Policy version is not an argument. Run the policy step first.
    """
    mock_url = mock_url or os.environ.get("FNOL_MOCK_URL", "http://127.0.0.1:8767")
    directory = Path(state_dir_path) if state_dir_path is not None else state_dir()
    stores = Stores(directory)
    case_ids = _case_ids(stores, case_id)
    routed = []
    for cid in case_ids:
        revision = stores.input.get_revision(cid)
        if revision is None:
            raise RulesError("revision_missing", f"no input revision for {cid}")
        decision = stores.input.get_decision(cid, revision.revision, "policy")
        if decision is None:
            raise RulesError(
                "policy_decision_missing",
                f"no policy decision for {cid} revision {revision.revision}",
            )
        result = run_rules(
            cid,
            fields_from_payload(revision.payload),
            policy_from_decision(decision),
            base_url=mock_url,
            stores=stores,
            input_revision=revision.revision,
            trace_id=revision.trace_id or uuid.uuid4().hex,
            input_sha256=revision.document_set_sha256,
            fetch=fetch,
            payload=revision.payload,
            screen_client=screen_client,
        )
        routed.append({
            "case_id": cid,
            "input_revision": revision.revision,
            **asdict(result),
        })
    return {
        "input_sqlite": stores.input.locator,
        "trace_sqlite": stores.trace.locator,
        "case_count": len(routed),
        "routes": routed,
    }


def _case_ids(stores: Stores, case_id: str | None) -> list[str]:
    if case_id:
        return [case_id]
    ids = [row["case_id"] for row in stores.input.latest_loss_keys() if row.get("case_id")]
    if not ids:
        raise RulesError("no_cases", "input.sqlite has no revisions; run intake first")
    return ids
