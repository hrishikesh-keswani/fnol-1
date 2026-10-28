"""One-call citations pipeline. MCP and local callers share this entry."""

from __future__ import annotations

import os
from pathlib import Path

from fnol_citations.service import CitationError, run_citations
from fnol_foundation.stores import Stores, state_dir

ROOT = Path(__file__).resolve().parents[2]


def start_citations(
    case_id: str | None = None,
    *,
    state_dir_path: str | Path | None = None,
    policies_dir: str | Path | None = None,
) -> dict:
    """Slice citations for one case, or for every case with an input revision.

    Reads the stored revision, the policy decision, the route decision, and the
    snapshots rules and policy already wrote. Does not call the mock.
    """
    directory = Path(state_dir_path) if state_dir_path is not None else state_dir()
    policies = Path(policies_dir or os.environ.get("FNOL_POLICIES_DIR", ROOT / "data" / "dev" / "policies"))
    stores = Stores(directory)
    if case_id:
        case_ids = [case_id]
    else:
        case_ids = [row["case_id"] for row in stores.input.latest_loss_keys() if row.get("case_id")]
        if not case_ids:
            raise CitationError("no_cases", "input.sqlite has no revisions; run intake first")
    cases = [
        run_citations(cid, stores, policies_dir=policies)
        for cid in case_ids
    ]
    return {
        "input_sqlite": stores.input.locator,
        "trace_sqlite": stores.trace.locator,
        "case_count": len(cases),
        "cases": cases,
    }
