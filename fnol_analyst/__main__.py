"""Start the src package. The repository root is on the path; src is not."""

import importlib
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))
for _name in list(sys.modules):
    if _name == "fnol_analyst" or _name.startswith("fnol_analyst."):
        del sys.modules[_name]
    if _name == "fnol_shadow" or _name.startswith("fnol_shadow."):
        del sys.modules[_name]
importlib.invalidate_caches()

from fnol_analyst.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
