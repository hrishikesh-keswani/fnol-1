"""Input, output, and append-only trace stores.

SQLite is the default: three files in one directory (input.sqlite, output.sqlite,
trace.sqlite). FNOL_STATE_DIR overrides the default .state directory.

PostgreSQL is selected by FNOL_DATABASE_URL, FNOL_DB_SECRET_ARN, or FNOL_RDS_HOST.
The same tables live in the input, output, and trace schemas. Triggers reject
updates and deletes. See fnol_foundation.db and tools/migrate.py.
"""

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fnol_foundation.canonical import (
    EVIDENCE_TEXT_COLUMNS,
    assert_no_restricted,
    canonical,
    digest,
    redact_for_store,
)
from fnol_foundation.db import (
    UniqueViolation,
    drop_namespace,
    open_db,
    schema_for,
    using_postgres,
)

TRACE_COLUMNS = (
    "event_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "case_id",
    "input_revision",
    "draft_id",
    "step",
    "actor",
    "rule_version",
    "started_at",
    "duration_ms",
    "attempt",
    "status",
    "error_code",
    "mock_http_status",
    "input_sha256",
    "output_sha256",
    "model_id",
    "input_tokens",
    "output_tokens",
    "shadow_narrative",
    "shadow_safety_status",
    "injection_field_status",
    "injection_model_status",
    "event",
    "prev_hash",
    "event_hash",
)

_TRACE_NOT_NULL = {"trace_id", "span_id", "step", "actor", "status"}
_TRACE_INTEGER = {"input_revision", "duration_ms", "attempt", "mock_http_status", "input_tokens", "output_tokens"}

INPUT_COLUMNS = (
    "intake_source",
    "account_text",
    "incident_timestamp",
    "policy_number",
    "vehicle_id",
    "loss_type",
    "driver_name",
    "driver_name_supplied",
    "incident_location",
    "incident_location_supplied",
    "injury_reported_raw",
    "injury_reported",
    "vehicle_drivable_raw",
    "vehicle_drivable",
    "referral_indicator",
    "loss_reference",
    "intake_sha256",
    "raw_intake",
    "intake_notes",
    "estimate_source_id",
    "estimate_sha256",
    "estimate_reference",
    "estimate_vehicle",
    "repair_amount",
    "estimate_prepared_by",
    "estimate_notes",
    "raw_estimate",
    "report_source_id",
    "report_sha256",
    "report_vehicle_reference",
    "report_incident_timestamp",
    "reported_loss_type",
    "report_notes",
    "raw_report",
    "repeated_fields",
    "extra_fields",
    "injection_field_status",
    "injection_model_status",
)

JSON_LIST_COLUMNS = {"repeated_fields", "extra_fields"}

BOOL_COLUMNS = {
    "driver_name_supplied",
    "incident_location_supplied",
    "injury_reported",
    "vehicle_drivable",
}

INPUT_SCHEMA = """
CREATE TABLE IF NOT EXISTS input_revisions (
  case_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  ingested_at TEXT NOT NULL,
  document_set_sha256 TEXT NOT NULL,
  trace_id TEXT,
  intake_source TEXT,
  account_text TEXT,
  incident_timestamp TEXT,
  policy_number TEXT,
  vehicle_id TEXT,
  loss_type TEXT,
  driver_name TEXT,
  driver_name_supplied INTEGER,
  incident_location TEXT,
  incident_location_supplied INTEGER,
  injury_reported_raw TEXT,
  injury_reported INTEGER,
  vehicle_drivable_raw TEXT,
  vehicle_drivable INTEGER,
  referral_indicator TEXT,
  loss_reference TEXT,
  intake_sha256 TEXT,
  raw_intake TEXT,
  intake_notes TEXT,
  estimate_source_id TEXT,
  estimate_sha256 TEXT,
  estimate_reference TEXT,
  estimate_vehicle TEXT,
  repair_amount TEXT,
  estimate_prepared_by TEXT,
  estimate_notes TEXT,
  raw_estimate TEXT,
  report_source_id TEXT,
  report_sha256 TEXT,
  report_vehicle_reference TEXT,
  report_incident_timestamp TEXT,
  reported_loss_type TEXT,
  report_notes TEXT,
  raw_report TEXT,
  repeated_fields TEXT,
  extra_fields TEXT,
  injection_field_status TEXT,
  injection_model_status TEXT,
  UNIQUE (case_id, revision)
);
CREATE TABLE IF NOT EXISTS procedure_sources (
  source_id TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL,
  loaded_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS input_revisions_no_update
BEFORE UPDATE ON input_revisions
BEGIN
  SELECT RAISE(ABORT, 'input_revisions is append-only');
END;
CREATE TRIGGER IF NOT EXISTS input_revisions_no_delete
BEFORE DELETE ON input_revisions
BEGIN
  SELECT RAISE(ABORT, 'input_revisions is append-only');
END;
"""

TOOL_PAYLOAD_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_payloads (
  case_id TEXT NOT NULL,
  input_revision INTEGER NOT NULL,
  tool TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  stored_at TEXT NOT NULL,
  PRIMARY KEY (case_id, input_revision, tool)
);
CREATE TRIGGER IF NOT EXISTS tool_payloads_no_update
BEFORE UPDATE ON tool_payloads
BEGIN
  SELECT RAISE(ABORT, 'tool_payloads is append-only');
END;
CREATE TRIGGER IF NOT EXISTS tool_payloads_no_delete
BEFORE DELETE ON tool_payloads
BEGIN
  SELECT RAISE(ABORT, 'tool_payloads is append-only');
END;
"""

DECISION_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  case_id TEXT NOT NULL,
  input_revision INTEGER NOT NULL,
  kind TEXT NOT NULL,
  body_json TEXT NOT NULL,
  policy_sha256 TEXT,
  claims_sha256 TEXT,
  stored_at TEXT NOT NULL,
  PRIMARY KEY (case_id, input_revision, kind)
);
CREATE TRIGGER IF NOT EXISTS decisions_no_update
BEFORE UPDATE ON decisions
BEGIN
  SELECT RAISE(ABORT, 'decisions is append-only');
END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete
BEFORE DELETE ON decisions
BEGIN
  SELECT RAISE(ABORT, 'decisions is append-only');
END;
"""

_DECISION_KINDS = frozenset({"policy", "route", "citations"})

OUTPUT_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
  draft_id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL,
  input_revision INTEGER,
  draft_json TEXT NOT NULL,
  draft_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  review_id TEXT,
  outcome TEXT
);
CREATE TRIGGER IF NOT EXISTS drafts_body_immutable
BEFORE UPDATE ON drafts
WHEN OLD.draft_json IS NOT NEW.draft_json OR OLD.draft_hash IS NOT NEW.draft_hash
BEGIN
  SELECT RAISE(ABORT, 'draft_json and draft_hash are immutable');
END;
"""


def _trace_column_sql(name):
    if name == "event_id":
        return "event_id TEXT PRIMARY KEY"
    if name in _TRACE_NOT_NULL or name == "started_at":
        return f"{name} TEXT NOT NULL"
    if name in _TRACE_INTEGER:
        return f"{name} INTEGER"
    return f"{name} TEXT"


TRACE_SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_events (
  """ + ", ".join(_trace_column_sql(name) for name in TRACE_COLUMNS) + """
);
CREATE TRIGGER IF NOT EXISTS trace_events_no_update
BEFORE UPDATE ON trace_events
BEGIN
  SELECT RAISE(ABORT, 'trace_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trace_events_no_delete
BEFORE DELETE ON trace_events
BEGIN
  SELECT RAISE(ABORT, 'trace_events is append-only');
END;
"""


def state_dir():
    return Path(os.environ.get("FNOL_STATE_DIR", ".state"))


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _as_text(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class ImmutableRecordError(ValueError):
    """A second write tried to change an append-only payload or decision."""


class _Store:
    """One logical database (a SQLite file, or one Postgres schema)."""

    kind = ""

    def __init__(self, path):
        self.path = Path(path)
        self.backend = "postgres" if using_postgres() else "sqlite"
        self.schema = schema_for(self.path, self.kind) if self.kind else ""

    def _conn(self):
        return open_db(self.path, kind=self.kind)

    def connect(self):
        """Open a connection. Tests use this to assert triggers and read rows."""
        return self._conn()

    @property
    def locator(self) -> str:
        if self.backend == "postgres":
            return f"postgres:{self.schema}"
        return str(self.path.resolve())

    def stored_blob(self) -> bytes:
        """Bytes a caller can scan for restricted markers.

        SQLite returns the file. PostgreSQL returns the text of every row.
        """
        if self.backend == "sqlite":
            return self.path.read_bytes()
        parts: list[str] = []
        with self._conn() as db:
            tables = db.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'
                   ORDER BY table_name"""
            ).fetchall()
            for table in tables:
                name = table["table_name"]
                if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
                    continue
                rows = db.execute(f"SELECT * FROM {name}").fetchall()
                for row in rows:
                    parts.append(repr(dict(row)))
        return "\n".join(parts).encode()


@dataclass(frozen=True)
class Revision:
    case_id: str
    revision: int
    ingested_at: str
    document_set_sha256: str
    payload: dict
    trace_id: str | None = None


@dataclass(frozen=True)
class DraftRecord:
    draft_id: str
    case_id: str
    input_revision: int | None
    draft: dict
    draft_hash: str
    status: str
    created_at: str
    review_id: str | None
    outcome: str | None


def _bind_value(column, value):
    if column in JSON_LIST_COLUMNS:
        if not value:
            return None
        return canonical(list(value))
    if column in BOOL_COLUMNS:
        if value is None:
            return None
        return 1 if value else 0
    return value


def _read_value(column, value):
    if column in BOOL_COLUMNS and value is not None:
        return bool(value)
    if column in JSON_LIST_COLUMNS:
        if not value:
            return []
        return json.loads(value)
    return value


def _row_payload(row):
    return {column: _read_value(column, row[column]) for column in INPUT_COLUMNS}


def _table_names(db, table):
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(db, table, name, declaration):
    names = _table_names(db, table)
    if names and name not in names:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


class InputStore(_Store):
    kind = "input"

    def __init__(self, path):
        super().__init__(path)
        if self.backend == "postgres":
            return
        with self._conn() as db:
            db.executescript(INPUT_SCHEMA)
            _add_column(db, "input_revisions", "trace_id", "TEXT")
            _add_column(db, "input_revisions", "repeated_fields", "TEXT")
            _add_column(db, "input_revisions", "extra_fields", "TEXT")
            _add_column(db, "input_revisions", "injection_field_status", "TEXT")
            _add_column(db, "input_revisions", "injection_model_status", "TEXT")
            names = _table_names(db, "tool_payloads")
            if names and "input_revision" not in names:
                db.execute("DROP TABLE tool_payloads")
            db.executescript(TOOL_PAYLOAD_SCHEMA)
            db.executescript(DECISION_SCHEMA)

    def append_revision(self, case_id, payload, document_set_sha256=None, ingested_at=None, trace_id=None):
        """Insert the next revision for case_id. Re-ingesting always increments.

        Raw document columns may contain a restricted marker. Structured fields may not.
        Callers must not copy the raw columns into the trace.
        """
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("case_id is required")
        if not isinstance(payload, dict):
            raise TypeError("payload must be a JSON object")
        assert_no_restricted({
            key: value for key, value in payload.items() if key not in EVIDENCE_TEXT_COLUMNS
        })
        sha = document_set_sha256 if document_set_sha256 is not None else digest(payload)
        if not isinstance(sha, str) or not sha:
            raise ValueError("document_set_sha256 must be a non-empty string")
        assert_no_restricted(sha)
        if trace_id is not None:
            assert_no_restricted(trace_id)
        when = _as_text(ingested_at) or utc_now()
        columns = ["case_id", "revision", "ingested_at", "document_set_sha256", "trace_id", *INPUT_COLUMNS]
        placeholders = ", ".join("?" for _ in columns)
        revision = None
        for _attempt in range(2):
            try:
                with self._conn() as db:
                    db.begin_immediate(f"revision:{case_id}")
                    row = db.execute(
                        "SELECT COALESCE(MAX(revision), 0) FROM input_revisions WHERE case_id = ?",
                        (case_id,),
                    ).fetchone()
                    revision = int(row[0]) + 1
                    values = [case_id, revision, when, sha, trace_id]
                    values.extend(_bind_value(column, payload.get(column)) for column in INPUT_COLUMNS)
                    db.execute(
                        f"INSERT INTO input_revisions ({', '.join(columns)}) VALUES ({placeholders})",
                        values,
                    )
                break
            except UniqueViolation:
                if _attempt == 1:
                    raise
        return self.get_revision(case_id, revision)

    def get_revision(self, case_id, revision=None):
        with self._conn() as db:
            if revision is None:
                row = db.execute(
                    """SELECT * FROM input_revisions
                       WHERE case_id = ? ORDER BY revision DESC LIMIT 1""",
                    (case_id,),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM input_revisions WHERE case_id = ? AND revision = ?",
                    (case_id, revision),
                ).fetchone()
        if row is None:
            return None
        return Revision(
            case_id=row["case_id"],
            revision=row["revision"],
            ingested_at=row["ingested_at"],
            document_set_sha256=row["document_set_sha256"],
            payload=_row_payload(row),
            trace_id=row["trace_id"],
        )

    def latest_loss_keys(self):
        """Latest revision of each case: case_id and loss_reference, ordered by case_id."""
        with self._conn() as db:
            rows = db.execute(
                """SELECT case_id, loss_reference
                   FROM input_revisions AS current
                   WHERE revision = (
                       SELECT MAX(revision) FROM input_revisions
                       WHERE case_id = current.case_id
                   )
                   ORDER BY case_id"""
            ).fetchall()
        return [{"case_id": row["case_id"], "loss_reference": row["loss_reference"]} for row in rows]

    def store_procedures(self, sources):
        """Replace the procedure-source table with this run's id and sha256 rows."""
        when = utc_now()
        assert_no_restricted(sources)
        with self._conn() as db:
            db.execute("DELETE FROM procedure_sources")
            db.executemany(
                "INSERT INTO procedure_sources (source_id, sha256, loaded_at) VALUES (?, ?, ?)",
                [(item["source_id"], item["sha256"], when) for item in sources],
            )

    def list_procedures(self):
        with self._conn() as db:
            rows = db.execute(
                "SELECT source_id, sha256, loaded_at FROM procedure_sources ORDER BY source_id"
            ).fetchall()
        return [{"source_id": row["source_id"], "sha256": row["sha256"], "loaded_at": row["loaded_at"]} for row in rows]

    def put_tool_payload(self, case_id, tool, raw_json, input_revision):
        """Store one redacted tool snapshot for this input revision.

        private_token keys are dropped. A second write of different bytes is refused.
        The same bytes may be written again and return the existing sha256.
        """
        self._require_revision(case_id, input_revision)
        if tool not in {"policy", "claims"}:
            raise ValueError("tool is not available")
        if not isinstance(raw_json, dict):
            raise TypeError("raw_json must be a JSON object")
        cleaned = redact_for_store(raw_json)
        body = canonical(cleaned)
        sha = digest(cleaned)
        when = utc_now()
        with self._conn() as db:
            db.begin_immediate(f"payload:{case_id}:{input_revision}:{tool}")
            existing = db.execute(
                """SELECT sha256 FROM tool_payloads
                   WHERE case_id = ? AND input_revision = ? AND tool = ?""",
                (case_id, input_revision, tool),
            ).fetchone()
            if existing is not None:
                if existing["sha256"] == sha:
                    return sha
                raise ImmutableRecordError("tool payload already stored for this revision")
            try:
                db.execute(
                    """INSERT INTO tool_payloads
                       (case_id, input_revision, tool, raw_json, sha256, stored_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (case_id, input_revision, tool, body, sha, when),
                )
            except UniqueViolation:
                existing = db.execute(
                    """SELECT sha256 FROM tool_payloads
                       WHERE case_id = ? AND input_revision = ? AND tool = ?""",
                    (case_id, input_revision, tool),
                ).fetchone()
                if existing is not None and existing["sha256"] == sha:
                    return sha
                raise ImmutableRecordError("tool payload already stored for this revision")
        return sha

    def get_tool_payload(self, case_id, tool, input_revision):
        with self._conn() as db:
            row = db.execute(
                """SELECT raw_json, sha256, stored_at FROM tool_payloads
                   WHERE case_id = ? AND input_revision = ? AND tool = ?""",
                (case_id, input_revision, tool),
            ).fetchone()
        if row is None:
            return None
        return {
            "raw_json": json.loads(row["raw_json"]),
            "sha256": row["sha256"],
            "stored_at": row["stored_at"],
        }

    def put_decision(self, case_id, input_revision, kind, body, policy_sha256=None, claims_sha256=None):
        """Append one policy, route, or citations decision bound to an input revision."""
        self._require_revision(case_id, input_revision)
        if kind not in _DECISION_KINDS:
            raise ValueError("decision kind is not available")
        if not isinstance(body, dict):
            raise TypeError("decision body must be a JSON object")
        assert_no_restricted(body)
        if policy_sha256 is not None:
            assert_no_restricted(policy_sha256)
        if claims_sha256 is not None:
            assert_no_restricted(claims_sha256)
        stored = canonical(body)
        when = utc_now()

        def _same(existing):
            return (
                existing["body_json"] == stored
                and existing["policy_sha256"] == policy_sha256
                and existing["claims_sha256"] == claims_sha256
            )

        with self._conn() as db:
            db.begin_immediate(f"decision:{case_id}:{input_revision}:{kind}")
            existing = db.execute(
                """SELECT body_json, policy_sha256, claims_sha256 FROM decisions
                   WHERE case_id = ? AND input_revision = ? AND kind = ?""",
                (case_id, input_revision, kind),
            ).fetchone()
            if existing is not None:
                if not _same(existing):
                    raise ImmutableRecordError("decision already stored for this revision")
            else:
                try:
                    db.execute(
                        """INSERT INTO decisions
                           (case_id, input_revision, kind, body_json, policy_sha256, claims_sha256, stored_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (case_id, input_revision, kind, stored, policy_sha256, claims_sha256, when),
                    )
                except UniqueViolation:
                    existing = db.execute(
                        """SELECT body_json, policy_sha256, claims_sha256 FROM decisions
                           WHERE case_id = ? AND input_revision = ? AND kind = ?""",
                        (case_id, input_revision, kind),
                    ).fetchone()
                    if existing is None or not _same(existing):
                        raise ImmutableRecordError("decision already stored for this revision")
        return self.get_decision(case_id, input_revision, kind)

    def get_decision(self, case_id, input_revision, kind):
        with self._conn() as db:
            row = db.execute(
                """SELECT body_json, policy_sha256, claims_sha256, stored_at FROM decisions
                   WHERE case_id = ? AND input_revision = ? AND kind = ?""",
                (case_id, input_revision, kind),
            ).fetchone()
        if row is None:
            return None
        return {
            "body": json.loads(row["body_json"]),
            "policy_sha256": row["policy_sha256"],
            "claims_sha256": row["claims_sha256"],
            "stored_at": row["stored_at"],
        }

    def _require_revision(self, case_id, input_revision):
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("case_id is required")
        if not isinstance(input_revision, int) or isinstance(input_revision, bool) or input_revision < 1:
            raise ValueError("input_revision is required")
        if self.get_revision(case_id, input_revision) is None:
            raise ValueError("input revision does not exist")


class OutputStore(_Store):
    kind = "output"

    def __init__(self, path):
        super().__init__(path)
        if self.backend == "postgres":
            return
        with self._conn() as db:
            db.executescript(OUTPUT_SCHEMA)

    def record_draft(self, case_id, draft, input_revision=None, draft_id=None, status="recorded", created_at=None):
        """Store a draft and the mock's hash of that exact object."""
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("case_id is required")
        if not isinstance(draft, dict):
            raise TypeError("draft must be a JSON object")
        assert_no_restricted(draft)
        draft_json = canonical(draft)
        draft_hash = digest(draft)
        draft_id = draft_id or "draft_" + uuid.uuid4().hex[:12]
        when = _as_text(created_at) or utc_now()
        with self._conn() as db:
            db.execute(
                """INSERT INTO drafts
                   (draft_id, case_id, input_revision, draft_json, draft_hash, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (draft_id, case_id, input_revision, draft_json, draft_hash, status, when),
            )
        return self.get_draft(draft_id)

    def get_draft(self, draft_id):
        with self._conn() as db:
            row = db.execute("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
        return None if row is None else self._record(row)

    def in_review(self):
        """Drafts that have been sent to a human and not copied forward yet."""
        with self._conn() as db:
            rows = db.execute(
                """SELECT * FROM drafts WHERE status = 'in_review'
                   ORDER BY created_at"""
            ).fetchall()
        return [self._record(row) for row in rows]

    def approved_unfiled(self):
        """Approvals copied onto the draft, with the claim record not filed yet."""
        with self._conn() as db:
            rows = db.execute(
                """SELECT * FROM drafts
                   WHERE status = 'approved' AND outcome = 'approved'
                   ORDER BY created_at"""
            ).fetchall()
        return [self._record(row) for row in rows]

    def latest_draft(self, case_id):
        """Most recently recorded draft for this case, or None."""
        with self._conn() as db:
            row = db.execute(
                """SELECT * FROM drafts WHERE case_id = ?
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (case_id,),
            ).fetchone()
        return None if row is None else self._record(row)

    def _record(self, row):
        return DraftRecord(
            draft_id=row["draft_id"],
            case_id=row["case_id"],
            input_revision=row["input_revision"],
            draft=json.loads(row["draft_json"]),
            draft_hash=row["draft_hash"],
            status=row["status"],
            created_at=row["created_at"],
            review_id=row["review_id"],
            outcome=row["outcome"],
        )

    def update_status(self, draft_id, status, review_id=None, outcome=None, *, clear_review_id=False):
        """Record review progress. The draft body and hash stay as inserted."""
        assert_no_restricted({"status": status, "review_id": review_id, "outcome": outcome})
        with self._conn() as db:
            if clear_review_id:
                cur = db.execute(
                    """UPDATE drafts
                       SET status = ?, review_id = NULL, outcome = COALESCE(?, outcome)
                       WHERE draft_id = ?""",
                    (status, outcome, draft_id),
                )
            else:
                cur = db.execute(
                    """UPDATE drafts
                       SET status = ?, review_id = COALESCE(?, review_id), outcome = COALESCE(?, outcome)
                       WHERE draft_id = ?""",
                    (status, review_id, outcome, draft_id),
                )
            if cur.rowcount != 1:
                raise KeyError(draft_id)
        return self.get_draft(draft_id)


class TraceStore(_Store):
    kind = "trace"

    def __init__(self, path):
        super().__init__(path)
        if self.backend == "postgres":
            return
        with self._conn() as db:
            db.executescript(TRACE_SCHEMA)
            _add_column(db, "trace_events", "event", "TEXT")
            _add_column(db, "trace_events", "prev_hash", "TEXT")
            _add_column(db, "trace_events", "event_hash", "TEXT")
            _add_column(db, "trace_events", "injection_field_status", "TEXT")
            _add_column(db, "trace_events", "injection_model_status", "TEXT")

    def append_event(self, **fields):
        """Insert one hash-chained trace event. Updates and deletes are rejected by triggers."""
        event = {name: fields.get(name) for name in TRACE_COLUMNS}
        missing = [name for name in ("trace_id", "span_id", "step", "actor", "status") if not event[name]]
        if missing:
            raise ValueError("missing trace fields: " + ", ".join(missing))
        event["event_id"] = event["event_id"] or str(uuid.uuid4())
        event["started_at"] = _as_text(event["started_at"]) or utc_now()
        assert_no_restricted({key: value for key, value in event.items() if value is not None})
        columns = ", ".join(TRACE_COLUMNS)
        placeholders = ", ".join("?" for _ in TRACE_COLUMNS)
        with self._conn() as db:
            db.begin_immediate("trace-chain")
            previous = db.execute(
                "SELECT event_hash FROM trace_events ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            prev_hash = previous["event_hash"] if previous is not None else None
            body = {
                name: event[name]
                for name in TRACE_COLUMNS
                if name not in ("event_hash", "prev_hash")
            }
            event["prev_hash"] = prev_hash
            event["event_hash"] = digest({"prev_hash": prev_hash, "event": body})
            db.execute(
                f"INSERT INTO trace_events ({columns}) VALUES ({placeholders})",
                tuple(event[name] for name in TRACE_COLUMNS),
            )
        return self.get_event(event["event_id"])

    def get_event(self, event_id):
        with self._conn() as db:
            row = db.execute("SELECT * FROM trace_events WHERE event_id = ?", (event_id,)).fetchone()
        return None if row is None else {name: row[name] for name in TRACE_COLUMNS}

    def column_names(self):
        with self._conn() as db:
            if self.backend == "sqlite":
                rows = db.execute("PRAGMA table_info(trace_events)").fetchall()
                found = [row["name"] for row in rows]
            else:
                rows = db.execute(
                    """SELECT column_name FROM information_schema.columns
                       WHERE table_schema = current_schema() AND table_name = 'trace_events'
                       ORDER BY ordinal_position"""
                ).fetchall()
                found = [row["column_name"] for row in rows]
        return tuple(name for name in found if name != "seq")


class Stores:
    """Input, output, and trace for one state directory."""

    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else state_dir()
        if not using_postgres():
            self.directory.mkdir(parents=True, exist_ok=True)
        self.input = InputStore(self.directory / "input.sqlite")
        self.output = OutputStore(self.directory / "output.sqlite")
        self.trace = TraceStore(self.directory / "trace.sqlite")

    @property
    def backend(self):
        return self.input.backend

    def drop(self):
        if using_postgres():
            drop_namespace(self.directory, ("input", "output", "trace"))
            return
        for path in (self.input.path, self.output.path, self.trace.path):
            path.unlink(missing_ok=True)
