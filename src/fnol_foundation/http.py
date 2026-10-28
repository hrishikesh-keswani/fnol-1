"""JSON GET for case-scoped tools.

A transport failure or an HTTP 5xx is retried, up to three attempts total.
404 and every other 4xx stop on the first response.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from fnol_foundation.canonical import RESTRICTED_MARKERS

MAX_ATTEMPTS = 3
_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ERROR_CODE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_TOOLS = frozenset({"policy", "claims"})

Get = Callable[[str], tuple[int, dict[str, Any] | None]]


@dataclass(frozen=True)
class HttpAttempt:
    attempt: int
    status: int
    body: dict[str, Any] | None
    duration_ms: int


def tool_url(base_url: str, case_id: str, tool: str) -> str:
    """Build one case-scoped tool URL. Rejects path characters in case_id."""
    if tool not in _TOOLS:
        raise ValueError("tool is not available")
    if not isinstance(case_id, str) or _CASE_ID.fullmatch(case_id) is None:
        raise ValueError("case_id is not a safe token")
    if not isinstance(base_url, str) or not base_url:
        raise ValueError("base_url is required")
    return f"{base_url.rstrip('/')}/v1/cases/{case_id}/{tool}"


def public_error(body: dict[str, Any] | None) -> str:
    """Short error code safe to store. Never the response message or a restricted value."""
    if not isinstance(body, dict):
        return "http_error"
    code = body.get("error")
    if not isinstance(code, str) or _ERROR_CODE.fullmatch(code) is None:
        return "http_error"
    if any(marker in code for marker in RESTRICTED_MARKERS):
        return "restricted_value"
    return code


def default_get(url: str, timeout: float = 10) -> tuple[int, dict[str, Any] | None]:
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _object(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _object(exc.read())
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, {"error": "transport_error"}


def _object(raw: bytes) -> dict[str, Any] | None:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _retryable(status: int) -> bool:
    return status == 0 or status >= 500


def fetch_with_retries(
    url: str,
    *,
    max_attempts: int = MAX_ATTEMPTS,
    get: Get | None = None,
) -> tuple[tuple[HttpAttempt, ...], dict[str, Any] | None]:
    """GET url. Retry transport errors and HTTP 5xx. Stop immediately on 4xx."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    getter = get or default_get
    attempts: list[HttpAttempt] = []
    for number in range(1, max_attempts + 1):
        started = time.perf_counter()
        status, body = getter(url)
        if body is not None and not isinstance(body, dict):
            body = None
        duration_ms = int((time.perf_counter() - started) * 1000)
        attempts.append(HttpAttempt(number, status, body, duration_ms))
        if status == 200 and isinstance(body, dict):
            return tuple(attempts), body
        if not _retryable(status) or number == max_attempts:
            break
    return tuple(attempts), None
