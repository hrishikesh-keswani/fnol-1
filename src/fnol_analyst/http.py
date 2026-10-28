"""JSON HTTP for the mock enterprise API. No bearer token; used by submit only."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

Transport = Callable[[str, str, dict | None, dict[str, str] | None], tuple[int, dict[str, Any]]]


def default_transport(
    method: str,
    url: str,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    *,
    timeout: float = 10,
) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req_headers = {"Accept": "application/json"}
    if data is not None:
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _parsed(response.status, response.read())
    except urllib.error.HTTPError as exc:
        return _parsed(exc.code, exc.read())
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, {"error": "transport_error"}


def _parsed(status: int, raw: bytes) -> tuple[int, dict[str, Any]]:
    if not raw:
        return status, {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, {}
    return status, payload if isinstance(payload, dict) else {}
