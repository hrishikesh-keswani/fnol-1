"""Analyst process that stays running.

Every poll does two things. It queues input revisions that have no review yet:
policy, rules, citations, the draft, the Gemma summary, and POST /v1/reviews.
Then it reads every open review. A decision that is still pending stays put.
An approval is copied onto the draft and filed. A rejection is copied and not
filed. The poll does not approve.

The HTTP server is the same copy-forward step, called once when the reviewer
can reach this process.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from fnol_analyst.client import AnalystClient, AnalystError


def advance_case(client: AnalystClient, case_id: str) -> dict[str, Any]:
    """Copy one human decision forward. File only an approval."""
    record = client.stores.output.latest_draft(case_id)
    if record is None:
        return {"case_id": case_id, "error": "draft_missing"}
    if record.status == "in_review" and record.review_id:
        try:
            polled = client.review_status(case_id)
        except AnalystError as exc:
            return {"case_id": case_id, "error": exc.error_code}
        if polled.get("status") != "approved":
            return {"case_id": case_id, "status": polled.get("status"), "filed": False}
    elif not (record.status == "approved" and record.outcome == "approved"):
        return {"case_id": case_id, "status": record.status, "filed": False}
    try:
        filed = client.file(case_id)
    except AnalystError as exc:
        return {"case_id": case_id, "status": "approved", "error": exc.error_code}
    return {"case_id": case_id, "status": filed.get("status", "approved"), "filed": True}


def notify_analyst(case_id: str) -> None:
    """POST the decided case to the analyst. The human decision is already saved."""
    base = os.environ.get("FNOL_ANALYST_URL", "").strip().rstrip("/")
    if not base or not case_id:
        print("analyst webhook skipped: FNOL_ANALYST_URL is unset", flush=True)
        return
    body = json.dumps({"case_id": case_id}).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/v1/reviews/decided",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            print(response.read().decode("utf-8"), flush=True)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"analyst webhook failed: {exc}", flush=True)


def queue_interval() -> float:
    """Seconds between queue passes. Zero or below turns the loop off."""
    raw = os.environ.get("FNOL_QUEUE_POLL_SECONDS", "30").strip()
    try:
        return float(raw)
    except ValueError:
        return 30.0


def run_queue_pass() -> list[dict[str, Any]]:
    """Queue every ingested case that is not already on the reviewer page."""
    with AnalystClient() as client:
        rows = client.enqueue_pending()
    for row in rows:
        if row.get("action") != "skipped":
            print(f"queue {row}", flush=True)
    return rows


def _review_decision(client: AnalystClient, review_id: str) -> str | None:
    """Read the human decision without writing a trace row for a still-pending review."""
    status, body = client._call_transport(
        "GET",
        f"{client.mock_url.rstrip('/')}/v1/reviews/{review_id}",
        None,
        None,
    )
    if status != 200 or not isinstance(body, dict):
        raise AnalystError("review_poll_failed", f"review poll failed with HTTP {status}")
    decision = body.get("status")
    return decision if isinstance(decision, str) else None


def poll_open_reviews(client: AnalystClient) -> dict[str, Any]:
    """Copy forward any open review a person has already decided.

    Pending reviews are only counted. Approved and rejected reviews go through
    ``advance_case``, which files an approval and leaves a rejection unfiled.
    """
    waiting: list[str] = []
    moved: list[dict[str, Any]] = []
    seen: set[str] = set()
    records = list(client.stores.output.in_review()) + list(client.stores.output.approved_unfiled())
    for record in records:
        case_id = record.case_id
        if not case_id or case_id in seen:
            continue
        seen.add(case_id)
        try:
            if record.status == "approved" and record.outcome == "approved":
                moved.append(advance_case(client, case_id))
                continue
            if not record.review_id:
                moved.append({"case_id": case_id, "error": "review_missing"})
                continue
            decision = _review_decision(client, record.review_id)
            if decision in {"approved", "rejected"}:
                moved.append(advance_case(client, case_id))
            else:
                waiting.append(case_id)
        except Exception as exc:
            code = getattr(exc, "error_code", None) or exc.__class__.__name__
            moved.append({"case_id": case_id, "error": code})
    print(f"review poll: {len(waiting)} waiting {waiting}", flush=True)
    for row in moved:
        print(f"review poll {row}", flush=True)
    return {"waiting": waiting, "moved": moved}


def run_review_pass() -> dict[str, Any]:
    """One look at the open reviews."""
    with AnalystClient() as client:
        return poll_open_reviews(client)


def queue_loop(interval: float) -> None:
    """Queue new revisions and copy human decisions forward until the process stops."""
    while True:
        try:
            run_queue_pass()
        except Exception as exc:
            print(f"queue pass failed: {exc}", flush=True)
        try:
            run_review_pass()
        except Exception as exc:
            print(f"review poll failed: {exc}", flush=True)
        time.sleep(interval)


def start_queue_loop() -> None:
    interval = queue_interval()
    if interval <= 0:
        print("queue loop off", flush=True)
        return
    threading.Thread(
        target=queue_loop, args=(interval,), name="fnol-queue", daemon=True
    ).start()
    print(f"queue and review poll every {interval:g}s", flush=True)


def serve(host: str | None = None, port: int | None = None) -> None:
    """Listen for the reviewer webhook until the process is stopped."""
    host = host or os.environ.get("FNOL_HTTP_HOST", "0.0.0.0")
    port = port if port is not None else int(os.environ.get("FNOL_HTTP_PORT", "8080"))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _json(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/health":
                self._json(200, {"status": "ok", "service": "analyst-follow"})
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path != "/v1/reviews/decided":
                self._json(404, {"error": "not_found"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(size).decode("utf-8"))
                case_id = payload["case_id"]
            except (ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"error": "case_id_required"})
                return
            try:
                with AnalystClient() as client:
                    result = advance_case(client, case_id)
            except Exception as exc:
                self._json(500, {"case_id": case_id, "error": str(exc)})
                return
            print(result, flush=True)
            status = 200 if "error" not in result else 409
            self._json(status, result)

    server = ThreadingHTTPServer((host, port), Handler)
    start_queue_loop()
    print(f"analyst webhook on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
