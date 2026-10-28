"""One-call policy pipeline. MCP and local callers share this entry."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Callable

from fnol_foundation.stores import Stores, state_dir
from fnol_policy.service import run_policy


class PolicyError(Exception):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def start_policy(
    case_id: str | None = None,
    *,
    mock_url: str | None = None,
    state_dir_path: str | Path | None = None,
    fetch: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Select a policy interval for each latest intake revision."""
    mock_url = mock_url or os.environ.get("FNOL_MOCK_URL", "http://127.0.0.1:8767")
    directory = Path(state_dir_path) if state_dir_path is not None else state_dir()
    stores = Stores(directory)
    case_ids = _case_ids(stores, case_id)
    selected = []
    for cid in case_ids:
        revision = stores.input.get_revision(cid)
        if revision is None:
            raise PolicyError("revision_missing", f"no input revision for {cid}")
        decision = run_policy(
            cid,
            revision.payload.get("incident_timestamp"),
            base_url=mock_url,
            stores=stores,
            input_revision=revision.revision,
            trace_id=revision.trace_id or uuid.uuid4().hex,
            input_sha256=revision.document_set_sha256,
            fetch=fetch,
        )
        selected.append({
            "case_id": cid,
            "input_revision": revision.revision,
            **decision,
        })
    return {
        "input_sqlite": stores.input.locator,
        "trace_sqlite": stores.trace.locator,
        "case_count": len(selected),
        "policies": selected,
    }


def _case_ids(stores: Stores, case_id: str | None) -> list[str]:
    if case_id:
        return [case_id]
    ids = [row["case_id"] for row in stores.input.latest_loss_keys() if row.get("case_id")]
    if not ids:
        raise PolicyError("no_cases", "input.sqlite has no revisions; run intake first")
    return ids
