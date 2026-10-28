"""Empty FNOL data tables. Leaves schemas, roles, and the migration ledger."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from fnol_foundation.db import database_url  # noqa: E402

TABLES = (
    "input.input_revisions",
    "input.procedure_sources",
    "input.tool_payloads",
    "input.decisions",
    "output.drafts",
    "trace.trace_events",
    "mock.reviews",
    "mock.records",
    "mock.keys",
    "mock.calls",
    "mock.audit",
)


def main() -> int:
    url = database_url()
    if not url:
        print("PostgreSQL is not configured", file=sys.stderr)
        return 1
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError:
        print(
            "psycopg is required; install with: uv sync --locked --group postgres",
            file=sys.stderr,
        )
        return 1

    statement = "TRUNCATE TABLE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"
    with psycopg.connect(url, connect_timeout=15, autocommit=True) as conn:
        conn.execute(statement)
    print("blanked", ", ".join(TABLES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
