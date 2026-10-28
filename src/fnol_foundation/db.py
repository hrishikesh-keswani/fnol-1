"""SQLite and PostgreSQL connections for FNOL state.

Local dev and the default test run keep using SQLite files. Set
``FNOL_DATABASE_URL``, ``FNOL_DB_SECRET_ARN``, or ``FNOL_RDS_HOST`` with
``FNOL_RDS_IAM_AUTH=1`` to use PostgreSQL (RDS in AWS, Postgres in compose).

``FNOL_PG_NAMESPACE=shared`` (the default) uses the schemas ``input``,
``output``, ``trace``, and ``mock``. ``isolate`` gives each state
directory or mock file its own schemas so tests can share one server.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import quote

_READY: set[str] = set()
_READY_LOCK = threading.Lock()
_SECRET_CACHE: dict[str, tuple[float, str]] = {}
_IAM_CACHE: dict[str, tuple[float, str]] = {}

_KINDS = ("input", "output", "trace", "mock")
_SCHEMA_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


class IntegrityError(Exception):
    """The database rejected a write (constraint or append-only trigger)."""


class UniqueViolation(IntegrityError):
    """A unique key already holds a row."""


class AppendOnlyViolation(IntegrityError):
    """A trigger blocked an update or delete of an immutable row."""


class MappingRow:
    """Index and name access, matching sqlite3.Row."""

    def __init__(self, data: dict, columns: list[str]):
        self._data = data
        self._columns = list(columns)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._data[self._columns[key]]
        return self._data[key]

    def keys(self):
        return self._columns

    def __iter__(self):
        return (self._data[name] for name in self._columns)

    def __len__(self):
        return len(self._columns)


class Cursor:
    def __init__(self, raw, columns: list[str]):
        self._raw = raw
        self._columns = columns

    def _wrap(self, row):
        if row is None:
            return None
        if isinstance(row, sqlite3.Row):
            columns = self._columns or list(row.keys())
            return MappingRow({name: row[name] for name in columns}, columns)
        if isinstance(row, MappingRow):
            return row
        if isinstance(row, dict):
            columns = self._columns or list(row.keys())
            return MappingRow(row, columns)
        if self._columns:
            return MappingRow(dict(zip(self._columns, row)), self._columns)
        return row

    def fetchone(self):
        return self._wrap(self._raw.fetchone())

    def fetchall(self):
        return [self._wrap(row) for row in self._raw.fetchall()]

    @property
    def rowcount(self) -> int:
        return self._raw.rowcount

    def __iter__(self):
        for row in self._raw:
            yield self._wrap(row)


class Connection:
    """One database connection. The context manager commits, or rolls back, then closes."""

    def __init__(self, raw, backend: str, *, schema: str, path: Path):
        self._raw = raw
        self.backend = backend
        self.schema = schema
        self.path = path
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False

    def commit(self) -> None:
        if not self._closed:
            self._raw.commit()

    def rollback(self) -> None:
        if not self._closed:
            self._raw.rollback()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._raw.close()

    def begin_immediate(self, lock_key: str = "write") -> None:
        """Take the write lock for this transaction.

        SQLite locks the file. PostgreSQL takes a transaction advisory lock so
        two containers serialize the same critical section.
        """
        if self.backend == "sqlite":
            self._raw.execute("BEGIN IMMEDIATE")
            return
        self.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
            (f"{self.schema}:{lock_key}",),
        )

    def execute(self, sql: str, params=()):
        statement = self._prepare(sql)
        values = tuple(params or ())
        if self.backend == "postgres":
            self._raw.execute("SAVEPOINT fnol_stmt")
        try:
            cursor = self._raw.execute(statement, values)
        except Exception as exc:
            if self.backend == "postgres":
                self._raw.execute("ROLLBACK TO SAVEPOINT fnol_stmt")
                self._raw.execute("RELEASE SAVEPOINT fnol_stmt")
            raise _translate(exc) from exc
        if self.backend == "postgres":
            self._raw.execute("RELEASE SAVEPOINT fnol_stmt")
        columns = _columns(cursor)
        return Cursor(cursor, columns)

    def executemany(self, sql: str, seq) -> None:
        statement = self._prepare(sql)
        rows = list(seq)
        if self.backend == "postgres":
            self._raw.execute("SAVEPOINT fnol_stmt")
            try:
                with self._raw.cursor() as cursor:
                    cursor.executemany(statement, rows)
            except Exception as exc:
                self._raw.execute("ROLLBACK TO SAVEPOINT fnol_stmt")
                self._raw.execute("RELEASE SAVEPOINT fnol_stmt")
                raise _translate(exc) from exc
            self._raw.execute("RELEASE SAVEPOINT fnol_stmt")
            return
        try:
            self._raw.executemany(statement, rows)
        except Exception as exc:
            raise _translate(exc) from exc

    def executescript(self, script: str) -> None:
        if self.backend != "sqlite":
            raise RuntimeError("executescript is only used to bootstrap SQLite files")
        self._raw.executescript(script)

    def _prepare(self, sql: str) -> str:
        if self.backend != "postgres":
            return sql
        rewritten = re.sub(r"\browid\b", "seq", sql, flags=re.IGNORECASE)
        return _qmark_to_pyformat(rewritten)


def using_postgres() -> bool:
    return database_url() is not None


def database_url() -> str | None:
    """Resolve the PostgreSQL URL, or None when this process should use SQLite."""
    explicit = os.environ.get("FNOL_DATABASE_URL", "").strip()
    if explicit:
        return explicit
    secret = os.environ.get("FNOL_DB_SECRET_ARN", "").strip()
    if secret:
        return _url_from_secret(secret)
    host = os.environ.get("FNOL_RDS_HOST", "").strip()
    if not host:
        return None
    iam = os.environ.get("FNOL_RDS_IAM_AUTH", "").strip().lower()
    if iam in {"1", "true", "yes"}:
        return _url_from_iam(host)
    password = os.environ.get("FNOL_RDS_PASSWORD", "")
    user = os.environ.get("FNOL_RDS_USER", "").strip()
    if not user or not password:
        raise RuntimeError(
            "FNOL_RDS_HOST is set without FNOL_DATABASE_URL. "
            "Set FNOL_RDS_IAM_AUTH=1 or FNOL_RDS_USER and FNOL_RDS_PASSWORD."
        )
    return _password_url(host, user, password)


def namespace_suffix(anchor: Path | None = None) -> str:
    """Return '' for the shared schemas, or '_<token>' for an isolated set."""
    mode = os.environ.get("FNOL_PG_NAMESPACE", "shared").strip() or "shared"
    if mode == "shared":
        return ""
    if mode == "isolate":
        raw = str(Path(anchor or ".").resolve())
        return "_" + hashlib.sha256(raw.encode()).hexdigest()[:16]
    token = mode[1:] if mode.startswith("_") else mode
    if not re.fullmatch(r"[a-z0-9_]{1,24}", token):
        raise ValueError("FNOL_PG_NAMESPACE must be shared, isolate, or a short token")
    return "_" + token


def anchor_for(path: Path, kind: str) -> Path:
    path = Path(path)
    if kind in {"input", "output", "trace"}:
        return path.parent
    return path


def schema_name(kind: str, anchor: Path | None = None) -> str:
    if kind not in _KINDS:
        raise ValueError(f"unknown store kind {kind}")
    name = kind + namespace_suffix(anchor)
    if not _SCHEMA_TOKEN.fullmatch(name):
        raise ValueError(f"unsafe schema name {name}")
    return name


def schema_for(path: Path, kind: str) -> str:
    if not using_postgres():
        return ""
    return schema_name(kind, anchor_for(path, kind))


def open_db(path: Path, *, kind: str) -> Connection:
    path = Path(path)
    if not using_postgres():
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(path, timeout=15)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("PRAGMA busy_timeout=15000")
        raw.execute("PRAGMA foreign_keys=ON")
        raw.commit()
        return Connection(raw, "sqlite", schema="", path=path)
    anchor = anchor_for(path, kind)
    ensure_ready(anchor)
    schema = schema_name(kind, anchor)
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    raw = psycopg.connect(database_url(), connect_timeout=15, row_factory=dict_row)
    raw.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
    return Connection(raw, "postgres", schema=schema, path=path)


def ensure_ready(anchor: Path | None = None) -> str:
    """Confirm versioned DDL for this namespace once per process. Returns the suffix.

    Isolate mode, used by the Postgres CI job, applies the files for that
    namespace. A shared deployment only checks that migrate already ran.
    """
    suffix = namespace_suffix(anchor)
    with _READY_LOCK:
        if suffix in _READY:
            return suffix
        from fnol_foundation.migrate import apply_migrations, verify_migrations

        if os.environ.get("FNOL_PG_NAMESPACE", "").strip() == "isolate":
            apply_migrations(suffix)
        verify_migrations(suffix)
        _READY.add(suffix)
    return suffix


_TABLES = {
    "input": ("input_revisions", "procedure_sources", "tool_payloads", "decisions"),
    "output": ("drafts",),
    "trace": ("trace_events",),
    "mock": ("reviews", "records", "keys", "calls", "audit"),
}


def namespace_label(suffix: str) -> str:
    if not suffix:
        return "shared"
    return "ns" + suffix


def drop_namespace(anchor: Path, kinds: tuple[str, ...] = _KINDS) -> None:
    """Drop isolated schemas, or truncate the shared ones."""
    if not using_postgres():
        return
    suffix = namespace_suffix(anchor)
    import psycopg
    from psycopg import sql

    with psycopg.connect(database_url(), connect_timeout=15, autocommit=True) as raw:
        if suffix == "":
            for kind in kinds:
                for table in _TABLES[kind]:
                    raw.execute(
                        sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(
                            sql.Identifier(kind, table)
                        )
                    )
            return
        for kind in kinds:
            raw.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(kind + suffix))
            )
        raw.execute(
            "DELETE FROM public.fnol_schema_migrations WHERE namespace = %s",
            (namespace_label(suffix),),
        )
    _READY.discard(suffix)


def _qmark_to_pyformat(sql: str) -> str:
    out: list[str] = []
    i = 0
    in_single = False
    while i < len(sql):
        ch = sql[i]
        if in_single:
            out.append(ch)
            if ch == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                in_single = False
            i += 1
            continue
        if ch == "'":
            in_single = True
            out.append(ch)
            i += 1
            continue
        if ch == "?":
            out.append("%s")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _columns(cursor) -> list[str]:
    description = getattr(cursor, "description", None)
    if not description:
        return []
    return [item[0] for item in description]


def _translate(exc: Exception) -> Exception:
    if isinstance(exc, IntegrityError):
        return exc
    if isinstance(exc, sqlite3.IntegrityError):
        text = str(exc).lower()
        if "unique" in text:
            return UniqueViolation(str(exc))
        return AppendOnlyViolation(str(exc))
    try:
        import psycopg
    except ImportError:
        return exc
    if isinstance(exc, psycopg.errors.UniqueViolation):
        return UniqueViolation(str(exc))
    if isinstance(exc, (psycopg.errors.RaiseException, psycopg.errors.CheckViolation, psycopg.errors.IntegrityError)):
        return AppendOnlyViolation(str(exc))
    return exc


def _region() -> str:
    return (os.environ.get("AWS_REGION") or os.environ.get("FNOL_AWS_REGION") or "us-east-1").strip()


def _password_url(host: str, user: str, password: str) -> str:
    port = os.environ.get("FNOL_RDS_PORT", "5432").strip() or "5432"
    dbname = os.environ.get("FNOL_RDS_DB", "fnol").strip() or "fnol"
    sslmode = os.environ.get("FNOL_RDS_SSLMODE", "require").strip() or "require"
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{dbname}?sslmode={quote(sslmode, safe='')}"
    )


def _url_from_iam(host: str) -> str:
    user = os.environ.get("FNOL_RDS_USER", "").strip()
    if not user:
        raise RuntimeError("FNOL_RDS_USER is required when FNOL_RDS_IAM_AUTH=1")
    port = int(os.environ.get("FNOL_RDS_PORT", "5432") or "5432")
    cache_key = f"{host}:{port}:{user}"
    now = time.time()
    cached = _IAM_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]
    import boto3

    token = boto3.client("rds", region_name=_region()).generate_db_auth_token(
        DBHostname=host,
        Port=port,
        DBUsername=user,
        Region=_region(),
    )
    url = _password_url(host, user, token)
    _IAM_CACHE[cache_key] = (now + 600, url)
    return url


def secret_host(payload: dict) -> str:
    """Host from a secret, or FNOL_RDS_HOST when the secret only has a password."""
    host = str(payload.get("host") or "").strip()
    if host:
        return host
    fallback = os.environ.get("FNOL_RDS_HOST", "").strip()
    if not fallback:
        raise RuntimeError(
            "The database secret has no host. Set FNOL_RDS_HOST to the database endpoint."
        )
    return fallback


def _url_from_secret(arn: str) -> str:
    now = time.time()
    cached = _SECRET_CACHE.get(arn)
    if cached and cached[0] > now:
        return cached[1]
    import boto3

    client = boto3.client("secretsmanager", region_name=_region())
    payload = json.loads(client.get_secret_value(SecretId=arn)["SecretString"])
    user = payload["username"]
    password = payload["password"]
    host = secret_host(payload)
    port = str(payload.get("port") or 5432)
    dbname = payload.get("dbname") or payload.get("database") or os.environ.get("FNOL_RDS_DB", "fnol")
    previous_port = os.environ.get("FNOL_RDS_PORT")
    previous_db = os.environ.get("FNOL_RDS_DB")
    os.environ["FNOL_RDS_PORT"] = port
    os.environ["FNOL_RDS_DB"] = str(dbname)
    try:
        url = _password_url(host, user, password)
    finally:
        if previous_port is None:
            os.environ.pop("FNOL_RDS_PORT", None)
        else:
            os.environ["FNOL_RDS_PORT"] = previous_port
        if previous_db is None:
            os.environ.pop("FNOL_RDS_DB", None)
        else:
            os.environ["FNOL_RDS_DB"] = previous_db
    _SECRET_CACHE[arn] = (now + 300, url)
    return url
