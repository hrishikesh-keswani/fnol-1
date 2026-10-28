"""Local system prompt. A managed prompt replaces this only after a successful GetPrompt."""

from pathlib import Path


def local_prompt_text() -> str:
    return Path(__file__).with_name("prompt.md").read_text(encoding="utf-8")
