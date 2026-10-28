"""One-call intake pipeline. MCP and local callers share this entry."""

import os
from pathlib import Path

from fnol_foundation.stores import Stores, state_dir
from fnol_intake.run import run_intake

ROOT = Path(__file__).resolve().parents[2]


def start_ingestion(
    mock_url=None,
    documents_dir=None,
    policies_dir=None,
    state_dir_path=None,
    transport=None,
):
    """Read the mock case list and write input.sqlite. Returns the file path and case rows."""
    mock_url = mock_url or os.environ.get("FNOL_MOCK_URL", "http://127.0.0.1:8767")
    documents = Path(documents_dir or os.environ.get("FNOL_DOCUMENTS_DIR", ROOT / "data" / "dev" / "documents"))
    policies = Path(policies_dir or os.environ.get("FNOL_POLICIES_DIR", ROOT / "data" / "dev" / "policies"))
    directory = Path(state_dir_path) if state_dir_path is not None else state_dir()
    stores = Stores(directory)
    result = run_intake(stores, mock_url, documents, policies, transport=transport)
    return {
        "input_sqlite": stores.input.locator,
        "case_count": len(result["revisions"]),
        "revisions": [
            {"case_id": revision.case_id, "revision": revision.revision}
            for revision in result["revisions"]
        ],
        "refused": list(result["refused"]),
    }
