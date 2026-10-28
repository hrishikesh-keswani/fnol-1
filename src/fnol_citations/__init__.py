"""Citations step: exact quotes from stored raw text and raw JSON.

A quote that contains private_token or SYNTHETIC-PRIVATE- is refused.
The error does not echo the quote.
"""

from fnol_citations.pipeline import start_citations
from fnol_citations.service import CitationError, run_citations

__all__ = ["CitationError", "run_citations", "start_citations"]
