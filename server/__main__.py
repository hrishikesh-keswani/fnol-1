"""Run the input MCP server on stdio: python -m server"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from server.input_server import serve

if __name__ == "__main__":
    serve()
