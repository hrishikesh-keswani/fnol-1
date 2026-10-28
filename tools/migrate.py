"""Apply the versioned PostgreSQL migrations.

Usage:
  FNOL_DATABASE_URL=postgresql://fnol:fnol@127.0.0.1:5432/fnol \\
    uv run --group postgres python tools/migrate.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fnol_foundation.migrate import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
