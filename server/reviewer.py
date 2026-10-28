"""HTTP human-review boundary.

Exposes the review page plus queue / pending / approve / reject / audit against
the same mock state that ``tools/runtime.py`` uses locally.

The Claims.Reviewer check reads X-Ms-Client-Principal or X-Fnol-Roles. Nothing
in front of this service sets those on AWS, and the review page sends
X-Fnol-Roles itself, so the check is not an identity control. Access is
controlled by the network: port 8090 accepts only the fnol-tasks security group
and the dashboard tunnel. The reviewer name is whatever the caller types.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from server.review_page import PAGE, queue_rows  # noqa: E402
from tools.runtime import audit, connect  # noqa: E402

REQUIRED_ROLE = "Claims.Reviewer"


def _state_path() -> Path:
    return Path(os.environ.get("FNOL_MOCK_STATE", ROOT / ".state" / "mock.sqlite"))


def _roles_from_headers(headers) -> set[str]:
    """Read roles Easy Auth injects. Local/dev may set X-Fnol-Roles for tests."""
    roles: set[str] = set()
    raw = headers.get("X-Ms-Client-Principal") or headers.get("x-ms-client-principal")
    if raw:
        try:
            import base64

            decoded = json.loads(base64.b64decode(raw + "=="))
            claims = decoded.get("claims") or []
            for claim in claims:
                if isinstance(claim, dict) and claim.get("typ") in {"roles", "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"}:
                    val = claim.get("val")
                    if isinstance(val, str) and val:
                        roles.add(val)
        except Exception:
            pass
    explicit = headers.get("X-Fnol-Roles") or headers.get("x-fnol-roles")
    if explicit:
        roles.update(part.strip() for part in explicit.split(",") if part.strip())
    return roles


def _reviewer_name(headers) -> str:
    for key in (
        "X-Ms-Client-Principal-Name",
        "x-ms-client-principal-name",
        "X-Fnol-Reviewer",
        "x-fnol-reviewer",
    ):
        value = headers.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown-reviewer"


def create_server(host: str = "0.0.0.0", port: int = 8090, state: Path | None = None) -> ThreadingHTTPServer:
    state_path = Path(state) if state is not None else _state_path()
    require_role = os.environ.get("FNOL_REVIEWER_REQUIRE_ROLE", "1").strip() != "0"

    class Handler(BaseHTTPRequestHandler):
        server_version = "fnol-reviewer/1.0"

        def log_message(self, fmt, *args):
            return

        def _json(self, status: int, body):
            raw = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _html(self, status: int, text: str):
            raw = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authorized(self) -> bool:
            if not require_role:
                return True
            return REQUIRED_ROLE in _roles_from_headers(self.headers)

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/health":
                self._json(200, {"status": "ok", "service": "reviewer"})
                return
            if path in {"/", "/review"}:
                self._html(200, PAGE)
                return
            if not self._authorized():
                self._json(403, {"error": "forbidden", "required_role": REQUIRED_ROLE})
                return
            if path == "/v1/queue":
                with connect(state_path) as db:
                    rows = queue_rows(db)
                self._json(200, rows)
                return
            if path == "/v1/pending":
                with connect(state_path) as db:
                    rows = [dict(x) for x in db.execute(
                        "SELECT id, case_id, draft, hash, status, reviewer FROM reviews WHERE status='pending'"
                    )]
                self._json(200, rows)
                return
            if path == "/v1/audit":
                with connect(state_path) as db:
                    rows = [dict(x) for x in db.execute("SELECT * FROM audit ORDER BY seq")]
                self._json(200, rows)
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/") or "/"
            if not self._authorized():
                self._json(403, {"error": "forbidden", "required_role": REQUIRED_ROLE})
                return
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "v1" and parts[1] in {"approve", "reject"}:
                review_id = parts[2]
                action = parts[1]
                status = "approved" if action == "approve" else "rejected"
                reviewer = _reviewer_name(self.headers)
                with connect(state_path) as db:
                    db.begin_immediate(f"review:{review_id}")
                    row = db.execute(
                        "SELECT id, case_id, draft, hash, status, reviewer FROM reviews WHERE id=?",
                        (review_id,),
                    ).fetchone()
                    if not row or row["status"] != "pending":
                        self._json(409, {"error": "review_not_pending"})
                        return
                    db.execute(
                        "UPDATE reviews SET status=?,reviewer=? WHERE id=?",
                        (status, reviewer, review_id),
                    )
                    audit(
                        db,
                        "human_" + status,
                        row["case_id"],
                        review_id=review_id,
                        reviewer=reviewer,
                        draft_hash=row["hash"],
                    )
                    decided_case = row["case_id"]
                from fnol_analyst.follow import notify_analyst

                threading.Thread(
                    target=notify_analyst, args=(decided_case,), name=f"notify-{review_id}"
                ).start()
                self._json(200, {"status": status, "review_id": review_id, "reviewer": reviewer})
                return
            self._json(404, {"error": "not_found"})

    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="FNOL reviewer HTTP boundary")
    parser.add_argument("--host", default=os.environ.get("FNOL_HTTP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FNOL_HTTP_PORT", "8090")))
    parser.add_argument("--state", type=Path, default=None)
    args = parser.parse_args(argv)
    from fnol_foundation.db import ensure_ready, using_postgres

    aws_only = os.environ.get("FNOL_REVIEWER_AWS_ONLY", "").strip() in {"1", "true", "yes"}
    if using_postgres():
        ensure_ready()
    elif aws_only or os.environ.get("FNOL_ALLOW_LOCAL_STATE", "").strip() not in {"1", "true", "yes"}:
        # Default CLI path is AWS Postgres. Local SQLite needs an explicit opt-in
        # so a leftover .state/mock.sqlite cannot silently drive the demo UI.
        print(
            "reviewer: refusing local SQLite. "
            "Run: AWS_PROFILE=atharv uv run --locked --group postgres python tools/reviewer_aws.py\n"
            "Or set FNOL_ALLOW_LOCAL_STATE=1 for an intentional local mock.",
            file=sys.stderr,
            flush=True,
        )
        return 2
    server = create_server(host=args.host, port=args.port, state=args.state)
    backend = "postgres" if using_postgres() else "sqlite"
    print(f"reviewer HTTP on http://{args.host}:{args.port} ({backend})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
