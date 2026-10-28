"""Bedrock shadow summary. The deterministic draft generator is not used here."""

from fnol_shadow.errors import ShadowError
from fnol_shadow.pipeline import summarize_case

__all__ = ["ShadowError", "summarize_case"]
