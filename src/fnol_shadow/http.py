"""JSON HTTP for Bedrock.

A bearer token is sent only when the caller does not sign with the task role.
The token is never returned or logged.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

MAX_ATTEMPTS = 3

Sleeper = Callable[[int], None]


def _default_sleeper(attempt: int) -> None:
    time.sleep(0.25 * attempt)


Signer = Callable[[bytes | None], dict[str, str]]


def request_json(
    url: str,
    *,
    method: str,
    api_key: str,
    body: dict | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float = 60,
    sleeper: Sleeper | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    signer: Signer | None = None,
) -> tuple[int, dict[str, str], dict]:
    """Call url. Retry transport errors, 429, and HTTP 5xx at most three times."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    pause = sleeper or _default_sleeper
    last = (0, {}, {})
    for attempt in range(1, max_attempts + 1):
        status, headers, payload = _once(
            url,
            method=method,
            api_key=api_key,
            body=body,
            extra_headers=extra_headers,
            timeout=timeout,
            signer=signer,
        )
        last = (status, headers, payload)
        if not _retryable(status) or attempt == max_attempts:
            return last
        pause(attempt)
    return last  # pragma: no cover — loop always returns on the final attempt


def _retryable(status: int) -> bool:
    return status == 0 or status == 429 or status >= 500


def _once(
    url: str,
    *,
    method: str,
    api_key: str,
    body: dict | None,
    extra_headers: dict[str, str] | None,
    timeout: float,
    signer: Signer | None = None,
) -> tuple[int, dict[str, str], dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    if signer is not None:
        headers = signer(data)
    else:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _parsed(response.status, response.headers, response.read())
    except urllib.error.HTTPError as exc:
        return _parsed(exc.code, exc.headers, exc.read())
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, {}, {}


def _parsed(status: int, header_items, raw: bytes) -> tuple[int, dict[str, str], dict]:
    headers = {key.lower(): value for key, value in header_items.items()}
    if not raw:
        return status, headers, {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, headers, {}
    if not isinstance(parsed, dict):
        return status, headers, {}
    return status, headers, parsed
