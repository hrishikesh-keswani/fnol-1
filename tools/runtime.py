"""Local fictional enterprise simulator. Python 3.12+.

State is a SQLite file unless FNOL_DATABASE_URL (or RDS settings) select PostgreSQL.
"""
import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from fnol_foundation.db import UniqueViolation  # noqa: E402


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


_MOCK_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY, case_id TEXT,
  draft TEXT, hash TEXT, status TEXT, reviewer TEXT);
CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, case_id TEXT UNIQUE,
  draft TEXT, review_id TEXT);
CREATE TABLE IF NOT EXISTS keys(key TEXT PRIMARY KEY, hash TEXT, response TEXT);
CREATE TABLE IF NOT EXISTS calls(case_id TEXT, tool TEXT, n INTEGER,
  PRIMARY KEY(case_id, tool));
CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT, event TEXT, case_id TEXT, detail TEXT);
"""

_REVIEW_COLUMNS = "id, case_id, draft, hash, status, reviewer"


def connect(path):
    """Open the mock state database (SQLite file, or the Postgres mock schema)."""
    from fnol_foundation.db import open_db, using_postgres

    path = Path(path)
    db = open_db(path, kind="mock")
    if not using_postgres():
        db.executescript(_MOCK_SCHEMA)
    return db


def mock_state_present(path) -> bool:
    """True when a read should open the mock database.

    PostgreSQL has no file to stat. SQLite stays closed until the file exists
    so a read does not create an empty database.
    """
    from fnol_foundation.db import using_postgres

    if using_postgres():
        return True
    return Path(path).exists()


def audit(db, event, case_id="", **detail):
    db.execute("INSERT INTO audit(at,event,case_id,detail) VALUES(?,?,?,?)",
               (datetime.now(timezone.utc).isoformat(), event, case_id, canonical(detail)))


def create_server(data_dir, state, host="127.0.0.1", port=8765):
    dataset = json.loads((Path(data_dir) / "dataset.json").read_text())
    case_map = {x["case_id"]: x for x in dataset["cases"]}
    connect(state).close()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, body):
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            from urllib.parse import urlsplit
            path = urlsplit(self.path).path
            if path == "/health":
                return self.reply(200, {"status": "ok", "dataset": dataset["name"],
                                        "fictional_training_only": True})
            if path == "/v1/cases":
                return self.reply(200, {"cases": [{"case_id": x["case_id"],
                    "intake_source": x["intake_source"], "documents": x["documents"]}
                    for x in dataset["cases"]]})
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["v1", "reviews"]:
                with connect(state) as db:
                    row = db.execute(
                        f"SELECT {_REVIEW_COLUMNS} FROM reviews WHERE id=?",
                        (parts[2],),
                    ).fetchone()
                return self.reply(200, dict(row)) if row else self.reply(404, {"error": "not_found"})
            if len(parts) == 3 and parts[:2] == ["v1", "records"]:
                case = case_map.get(parts[2])
                if not case:
                    return self.reply(404, {"error": "not_found"})
                with connect(state) as db:
                    row = db.execute("SELECT * FROM records WHERE id=?", (case["logical_key"],)).fetchone()
                return self.reply(200, dict(row)) if row else self.reply(404, {"error": "not_found"})
            if len(parts) != 4 or parts[:2] != ["v1", "cases"]:
                return self.reply(404, {"error": "not_found"})
            cid, tool = parts[2:]
            case = case_map.get(cid)
            if not case or tool not in case["tools"]:
                return self.reply(404, {"error": "not_found"})
            payload = case["tools"][tool]
            with connect(state) as db:
                db.begin_immediate("mock-write")
                row = db.execute("SELECT n FROM calls WHERE case_id=? AND tool=?", (cid, tool)).fetchone()
                n = (row[0] if row else 0) + 1
                db.execute(
                    """INSERT INTO calls (case_id, tool, n) VALUES (?,?,?)
                       ON CONFLICT (case_id, tool) DO UPDATE SET n = excluded.n""",
                    (cid, tool, n),
                )
                fault = case.get("faults", {}).get(tool)
                fails = fault and (fault["failures"] == "always" or n <= fault["failures"])
                audit(db, "tool_read", cid, tool=tool, attempt=n, status=503 if fails else 200)
            if fails:
                return self.reply(503, {"error": "upstream_timeout", "retryable": True,
                                         "message": "Simulated upstream timeout; never treat as no results."})
            return self.reply(200, payload)

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > 262144:
                    return self.reply(413, {"error": "body_size"})
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError()
            except (ValueError, UnicodeDecodeError):
                return self.reply(400, {"error": "invalid_json_object"})
            cid = body.get("case_id")
            if not isinstance(cid, str) or cid not in case_map:
                return self.reply(400, {"error": "unknown_case"})
            if self.path == "/v1/reviews":
                draft = body.get("draft")
                if not isinstance(draft, dict) or draft.get("case_id") != cid:
                    return self.reply(400, {"error": "draft_case_id_required"})
                rid = "rev_" + uuid.uuid4().hex[:12]
                with connect(state) as db:
                    db.execute(
                        f"INSERT INTO reviews ({_REVIEW_COLUMNS}) VALUES (?,?,?,?,?,?)",
                        (rid, cid, canonical(draft), digest(draft), "pending", None),
                    )
                    audit(db, "review_requested", cid, review_id=rid, draft_hash=digest(draft))
                return self.reply(201, {"review_id": rid, "status": "pending", "draft_hash": digest(draft)})
            if self.path != "/v1/records":
                with connect(state) as db:
                    audit(db, "unsupported_write_attempt", cid, path=self.path)
                return self.reply(404, {"error": "unsupported_operation"})
            if not dataset["allow_write"]:
                with connect(state) as db:
                    audit(db, "forbidden_write_attempt", cid)
                return self.reply(403, {"error": "read_only_mission"})
            draft, rid = body.get("draft"), body.get("review_id")
            key = self.headers.get("Idempotency-Key", "")
            if not isinstance(draft, dict) or not isinstance(rid, str) or not key or len(key) > 128:
                return self.reply(400, {"error": "draft_review_and_idempotency_key_required"})
            if draft.get("case_id") != cid:
                return self.reply(400, {"error": "draft_case_mismatch"})
            fingerprint = digest({"case_id": cid, "draft": draft, "review_id": rid})
            with connect(state) as db:
                db.begin_immediate("mock-write")
                previous = db.execute("SELECT * FROM keys WHERE key=?", (key,)).fetchone()
                if previous:
                    if previous["hash"] != fingerprint:
                        audit(db, "idempotency_conflict", cid)
                        return self.reply(409, {"error": "idempotency_conflict"})
                    audit(db, "write_replayed", cid)
                    return self.reply(200, json.loads(previous["response"]))
                review = db.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
                if not review or review["case_id"] != cid or review["status"] != "approved" or review["hash"] != digest(draft):
                    audit(db, "unapproved_write_attempt", cid, review_id=rid)
                    return self.reply(403, {"error": "approval_missing_or_draft_changed"})
                # logical_key represents a single dispute/claim, across duplicate intake IDs.
                logical = case_map[cid]["logical_key"]
                existing = db.execute("SELECT * FROM records WHERE id=?", (logical,)).fetchone()
                if existing:
                    audit(db, "duplicate_prevented", cid, existing_record_id=logical)
                    return self.reply(409, {"error": "duplicate_case", "existing_record_id": logical})
                response = {"record_id": logical, "case_id": cid, "status": "created", "review_id": rid}
                try:
                    db.execute("INSERT INTO records VALUES(?,?,?,?)", (logical, cid, canonical(draft), rid))
                    db.execute("INSERT INTO keys VALUES(?,?,?)", (key, fingerprint, canonical(response)))
                except UniqueViolation:
                    existing = db.execute("SELECT * FROM records WHERE id=?", (logical,)).fetchone()
                    if existing:
                        audit(db, "duplicate_prevented", cid, existing_record_id=logical)
                        return self.reply(409, {"error": "duplicate_case", "existing_record_id": logical})
                    raise
                audit(db, "record_created", cid, record_id=logical, review_id=rid,
                      draft_hash=digest(draft), reviewer=review["reviewer"])
            return self.reply(201, response)

    return ThreadingHTTPServer((host, port), Handler)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--data", type=Path, default=ROOT / "data" / "dev")
    serve.add_argument("--state", type=Path, default=ROOT / ".state" / "mock.sqlite")
    serve.add_argument("--host", default="127.0.0.1",
                       help="Bind address. Use 0.0.0.0 only inside a container; publish to loopback on the host.")
    serve.add_argument("--port", type=int, default=8765)
    for name in ["pending", "approve", "reject", "audit", "reset"]:
        sp = sub.add_parser(name)
        sp.add_argument("--state", type=Path, default=ROOT / ".state" / "mock.sqlite")
        if name in ["approve", "reject"]:
            sp.add_argument("review_id")
            sp.add_argument("--reviewer", required=True)
        if name == "reset":
            sp.add_argument("--confirm", action="store_true", required=True)
    a = p.parse_args()
    if a.command == "serve":
        server = create_server(a.data, a.state, host=a.host, port=a.port)
        print(f"Fictional training mock at http://127.0.0.1:{a.port} (bound {a.host}); Ctrl-C stops it.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return
    decided_case = None
    with connect(a.state) as db:
        if a.command in ["approve", "reject"]:
            status = "approved" if a.command == "approve" else "rejected"
            db.begin_immediate(f"review:{a.review_id}")
            row = db.execute(
                f"SELECT {_REVIEW_COLUMNS} FROM reviews WHERE id=?",
                (a.review_id,),
            ).fetchone()
            if not row or row["status"] != "pending":
                sys.exit("Review must exist and be pending.")
            db.execute("UPDATE reviews SET status=?,reviewer=? WHERE id=?", (status, a.reviewer, a.review_id))
            audit(db, "human_" + status, row["case_id"], review_id=a.review_id,
                  reviewer=a.reviewer, draft_hash=row["hash"])
            print(status)
            decided_case = row["case_id"]
        elif a.command == "pending":
            print(json.dumps([
                dict(x) for x in db.execute(
                    f"SELECT {_REVIEW_COLUMNS} FROM reviews WHERE status='pending'"
                )
            ], indent=2))
        elif a.command == "audit":
            print(json.dumps([dict(x) for x in db.execute("SELECT * FROM audit ORDER BY seq")], indent=2))
        elif a.command == "reset":
            for table in ["reviews", "records", "keys", "calls", "audit"]:
                db.execute(f"DELETE FROM {table}")
            print("Reset local mock state. Stop all clients before resetting.")
    if a.command in ["approve", "reject"] and decided_case:
        from fnol_analyst.follow import notify_analyst
        notify_analyst(decided_case)


if __name__ == "__main__":
    main()
