"""Analyst client: drives the four step servers and builds the deterministic draft.

The client never reads raw document text. Review, record creation, and the
Bedrock shadow path are out of this package.
"""

from fnol_analyst.client import AnalystClient, AnalystError
from fnol_analyst.servers import PrepareError

__all__ = ["AnalystClient", "AnalystError", "PrepareError"]
