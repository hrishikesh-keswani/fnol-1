"""Coverage for the foundation modules: db, migrate, s3store, blobs, stores, http, events, aws_auth.

No live AWS account or PostgreSQL server is needed. boto3, botocore and psycopg
connections are mocked. SQLite files live in temporary directories.
"""

from __future__ import annotations

import contextlib
import io
import os
import runpy
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import warnings
from pathlib import Path
from unittest import mock

import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from fnol_foundation import blobs, db, events, http, migrate, s3store, stores
from fnol_foundation.blobs import BlobError
from fnol_foundation.db import (
    AppendOnlyViolation,
    Connection,
    Cursor,
    IntegrityError,
    MappingRow,
    UniqueViolation,
)
from fnol_foundation.stores import ImmutableRecordError
from fnol_shadow import aws_auth
from fnol_shadow.errors import ShadowError

# ---------------------------------------------------------------------------
# Fake third-party modules. CI installs neither boto3 nor psycopg for the default
# job, and no test may reach a real AWS account or database server.
# ---------------------------------------------------------------------------


def _module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _build_psycopg():
    class Error(Exception):
        sqlstate = None

    class IntegrityError(Error):
        pass

    def error(name, base=Error, sqlstate=None):
        return type(name, (base,), {"sqlstate": sqlstate})

    errors = _module(
        "psycopg.errors",
        Error=Error,
        IntegrityError=IntegrityError,
        UniqueViolation=error("UniqueViolation", IntegrityError, "23505"),
        CheckViolation=error("CheckViolation", IntegrityError, "23514"),
        NotNullViolation=error("NotNullViolation", IntegrityError, "23502"),
        RaiseException=error("RaiseException", Error, "P0001"),
        UndefinedTable=error("UndefinedTable", Error, "42P01"),
        InsufficientPrivilege=error("InsufficientPrivilege", Error, "42501"),
        OperationalError=error("OperationalError"),
        SyntaxError=error("SyntaxError", Error, "42601"),
    )

    class SQL:
        def __init__(self, text):
            self.text = text

        def format(self, *args):
            return self

        def __str__(self):
            return self.text

    class Identifier:
        def __init__(self, *parts):
            self.parts = parts

    sql = _module("psycopg.sql", SQL=SQL, Identifier=Identifier)
    rows = _module("psycopg.rows", dict_row=object())
    root = _module("psycopg", Error=Error, errors=errors, sql=sql, rows=rows, connect=mock.MagicMock())
    return {"psycopg": root, "psycopg.errors": errors, "psycopg.sql": sql, "psycopg.rows": rows}


def _build_botocore():
    class ClientError(Exception):
        def __init__(self, response, operation_name):
            super().__init__(str(response))
            self.response = response
            self.operation_name = operation_name

    class Config:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class AWSRequest:
        def __init__(self, method, url, data, headers):
            self.method = method
            self.url = url
            self.data = data
            self.headers = dict(headers)

    class SigV4Auth:
        calls: list = []

        def __init__(self, credentials, service, region):
            self.credentials = credentials
            self.service = service
            self.region = region
            SigV4Auth.calls.append((credentials, service, region))

        def add_auth(self, request):
            request.headers["Authorization"] = (
                f"AWS4-HMAC-SHA256 Credential={self.credentials.access_key}/20260101/"
                f"{self.region}/{self.service}/aws4_request"
            )
            if self.credentials.token:
                request.headers["X-Amz-Security-Token"] = self.credentials.token
            request.headers[b"ignored"] = b"bytes"
            request.headers["X-Number"] = 5

    exceptions = _module("botocore.exceptions", ClientError=ClientError)
    config = _module("botocore.config", Config=Config)
    awsrequest = _module("botocore.awsrequest", AWSRequest=AWSRequest)
    auth = _module("botocore.auth", SigV4Auth=SigV4Auth)
    session = _module("botocore.session", Session=mock.MagicMock())
    root = _module(
        "botocore", exceptions=exceptions, config=config, awsrequest=awsrequest, auth=auth, session=session
    )
    boto3 = _module("boto3", client=mock.MagicMock())
    return {
        "botocore": root,
        "botocore.exceptions": exceptions,
        "botocore.config": config,
        "botocore.awsrequest": awsrequest,
        "botocore.auth": auth,
        "botocore.session": session,
        "boto3": boto3,
    }


_FAKE_MODULES = {**_build_psycopg(), **_build_botocore()}
pg_errors = _FAKE_MODULES["psycopg.errors"]
ClientError = _FAKE_MODULES["botocore.exceptions"].ClientError
SigV4Auth = _FAKE_MODULES["botocore.auth"].SigV4Auth


class _Credentials:
    def __init__(self, access_key, secret_key, token=None):
        self.access_key = access_key
        self.secret_key = secret_key
        self.token = token

    def get_frozen_credentials(self):
        return self


_DB_ENV = (
    "FNOL_DATABASE_URL",
    "FNOL_DB_SECRET_ARN",
    "FNOL_RDS_HOST",
    "FNOL_RDS_IAM_AUTH",
    "FNOL_RDS_USER",
    "FNOL_RDS_PASSWORD",
    "FNOL_RDS_PORT",
    "FNOL_RDS_DB",
    "FNOL_RDS_SSLMODE",
    "FNOL_PG_NAMESPACE",
    "AWS_REGION",
    "FNOL_AWS_REGION",
)


class EnvTestCase(unittest.TestCase):
    """Start every test from a clean database environment."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        modules = mock.patch.dict(sys.modules, _FAKE_MODULES)
        modules.start()
        self.addCleanup(modules.stop)
        for name in _DB_ENV:
            os.environ.pop(name, None)
        os.environ.pop("FNOL_S3_BUCKET", None)

        for cache in (db._SECRET_CACHE, db._IAM_CACHE):
            snapshot = dict(cache)
            cache.clear()
            self.addCleanup(lambda c=cache, s=snapshot: (c.clear(), c.update(s)))
        ready = set(db._READY)
        self.addCleanup(lambda: (db._READY.clear(), db._READY.update(ready)))


# ---------------------------------------------------------------------------
# db.py
# ---------------------------------------------------------------------------


class _FakeCursor:
    description = [("a",), ("b",)]
    rowcount = 3

    def fetchone(self):
        return {"a": 1, "b": 2}

    def fetchall(self):
        return [{"a": 1, "b": 2}]

    def __iter__(self):
        return iter([{"a": 1, "b": 2}])


class _FakeRaw:
    """A stand-in for a psycopg connection."""

    def __init__(self, fail_on=None, error=None):
        self.calls: list[tuple] = []
        self.fail_on = fail_on
        self.error = error
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.cursor_obj = mock.MagicMock()

    def execute(self, sql, values=None):
        self.calls.append((sql, values))
        if self.fail_on and self.fail_on in sql:
            raise self.error
        return _FakeCursor()

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


def _pg_connection(raw, schema="input"):
    return Connection(raw, "postgres", schema=schema, path=Path("x"))


class MappingRowAndCursorTests(unittest.TestCase):
    def test_mapping_row_access(self):
        row = MappingRow({"a": 1, "b": 2}, ["a", "b"])
        self.assertEqual(row[0], 1)
        self.assertEqual(row["b"], 2)
        self.assertEqual(row.keys(), ["a", "b"])
        self.assertEqual(list(row), [1, 2])
        self.assertEqual(len(row), 2)

    def test_cursor_wraps_every_row_shape(self):
        existing = MappingRow({"a": 1}, ["a"])
        cases = [
            (None, None),
            (existing, existing),
        ]
        for raw_row, expected in cases:
            raw = mock.MagicMock()
            raw.fetchone.return_value = raw_row
            self.assertIs(Cursor(raw, ["a"]).fetchone(), expected)

        raw = mock.MagicMock()
        raw.fetchone.return_value = {"a": 1, "b": 2}
        wrapped = Cursor(raw, []).fetchone()
        self.assertEqual(wrapped.keys(), ["a", "b"])
        wrapped = Cursor(raw, ["b"]).fetchone()
        self.assertEqual(wrapped.keys(), ["b"])

        raw.fetchone.return_value = (1, 2)
        wrapped = Cursor(raw, ["a", "b"]).fetchone()
        self.assertEqual(wrapped["b"], 2)

        raw.fetchone.return_value = (1, 2)
        self.assertEqual(Cursor(raw, []).fetchone(), (1, 2))

    def test_cursor_wraps_sqlite_rows_and_iterates(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (a INTEGER, b INTEGER)")
        conn.execute("INSERT INTO t VALUES (1, 2)")
        raw = conn.execute("SELECT a, b FROM t")
        cursor = Cursor(raw, [])
        rows = list(cursor)
        self.assertEqual(rows[0]["b"], 2)
        self.assertEqual(rows[0].keys(), ["a", "b"])
        self.assertEqual(cursor.rowcount, -1)
        self.assertEqual(Cursor(conn.execute("SELECT a FROM t"), ["a"]).fetchall()[0][0], 1)
        conn.close()


class ConnectionTests(EnvTestCase):
    def test_sqlite_connection_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "one.sqlite"
            conn = db.open_db(path, kind="input")
            self.assertEqual(conn.backend, "sqlite")
            self.assertEqual(conn.schema, "")
            with conn:
                conn.executescript("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT);")
                conn.begin_immediate()
                conn.executemany("INSERT INTO t VALUES (?, ?)", [("a", "1"), ("b", "2")])
                cursor = conn.execute("SELECT k, v FROM t ORDER BY k")
                self.assertEqual([tuple(row) for row in cursor.fetchall()], [("a", "1"), ("b", "2")])
            # closed: commit and rollback are no-ops, close is idempotent
            conn.commit()
            conn.rollback()
            conn.close()

    def test_sqlite_errors_are_translated(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.open_db(Path(tmp) / "x.sqlite", kind="trace")
            conn.executescript("CREATE TABLE t (k TEXT PRIMARY KEY);")
            conn.execute("INSERT INTO t VALUES ('a')")
            with self.assertRaises(UniqueViolation):
                conn.execute("INSERT INTO t VALUES ('a')")
            with self.assertRaises(UniqueViolation):
                conn.executemany("INSERT INTO t VALUES (?)", [("a",)])
            conn.close()

    def test_context_manager_rolls_back_on_error(self):
        raw = _FakeRaw()
        with self.assertRaises(RuntimeError):
            with _pg_connection(raw):
                raise RuntimeError("boom")
        self.assertEqual((raw.rollbacks, raw.commits, raw.closes), (1, 0, 1))

    def test_postgres_execute_rewrites_sql_and_wraps_rows(self):
        raw = _FakeRaw()
        conn = _pg_connection(raw)
        cursor = conn.execute("SELECT a, b FROM t WHERE x = ? AND rowid > ?", (1, 2))
        sql, values = raw.calls[1]
        self.assertEqual(sql, "SELECT a, b FROM t WHERE x = %s AND seq > %s")
        self.assertEqual(values, (1, 2))
        self.assertEqual(raw.calls[0][0], "SAVEPOINT fnol_stmt")
        self.assertEqual(raw.calls[-1][0], "RELEASE SAVEPOINT fnol_stmt")
        self.assertEqual(cursor.fetchone()["b"], 2)

    def test_postgres_execute_failure_rolls_back_to_savepoint(self):
        raw = _FakeRaw(fail_on="INSERT", error=pg_errors.UniqueViolation("dup"))
        conn = _pg_connection(raw)
        with self.assertRaises(UniqueViolation):
            conn.execute("INSERT INTO t VALUES (?)", (1,))
        statements = [call[0] for call in raw.calls]
        self.assertIn("ROLLBACK TO SAVEPOINT fnol_stmt", statements)
        self.assertEqual(statements[-1], "RELEASE SAVEPOINT fnol_stmt")

    def test_postgres_executemany(self):
        raw = _FakeRaw()
        conn = _pg_connection(raw)
        conn.executemany("INSERT INTO t VALUES (?, ?)", iter([(1, 2), (3, 4)]))
        inner = raw.cursor_obj.__enter__.return_value
        inner.executemany.assert_called_once_with("INSERT INTO t VALUES (%s, %s)", [(1, 2), (3, 4)])
        self.assertEqual(raw.calls[-1][0], "RELEASE SAVEPOINT fnol_stmt")

    def test_postgres_executemany_failure(self):
        raw = _FakeRaw()
        raw.cursor_obj.__enter__.return_value.executemany.side_effect = pg_errors.CheckViolation("bad")
        conn = _pg_connection(raw)
        with self.assertRaises(AppendOnlyViolation):
            conn.executemany("INSERT INTO t VALUES (?)", [(1,)])
        statements = [call[0] for call in raw.calls]
        self.assertIn("ROLLBACK TO SAVEPOINT fnol_stmt", statements)

    def test_postgres_begin_immediate_takes_advisory_lock(self):
        raw = _FakeRaw()
        conn = _pg_connection(raw, schema="input_ab")
        conn.begin_immediate("revision:C1")
        sql, values = raw.calls[1]
        self.assertIn("pg_advisory_xact_lock", sql)
        self.assertEqual(values, ("input_ab:revision:C1",))

    def test_executescript_is_sqlite_only(self):
        with self.assertRaises(RuntimeError):
            _pg_connection(_FakeRaw()).executescript("SELECT 1;")

    def test_prepare_leaves_sqlite_sql_alone(self):
        conn = Connection(mock.MagicMock(), "sqlite", schema="", path=Path("x"))
        self.assertEqual(conn._prepare("SELECT ? FROM t WHERE rowid = 1"), "SELECT ? FROM t WHERE rowid = 1")


class ConfigTests(EnvTestCase):
    def test_using_postgres_follows_url(self):
        self.assertFalse(db.using_postgres())
        os.environ["FNOL_DATABASE_URL"] = "postgresql://u:p@h/d"
        self.assertTrue(db.using_postgres())

    def test_database_url_from_secret(self):
        os.environ["FNOL_DB_SECRET_ARN"] = "arn:secret"
        with mock.patch.object(db, "_url_from_secret", return_value="postgresql://s") as loader:
            self.assertEqual(db.database_url(), "postgresql://s")
        loader.assert_called_once_with("arn:secret")

    def test_database_url_from_rds_password(self):
        os.environ.update({
            "FNOL_RDS_HOST": "db.example",
            "FNOL_RDS_USER": "us er",
            "FNOL_RDS_PASSWORD": "p@ss/word",
            "FNOL_RDS_PORT": "6543",
            "FNOL_RDS_DB": "claims",
            "FNOL_RDS_SSLMODE": "verify-full",
        })
        self.assertEqual(
            db.database_url(),
            "postgresql://us%20er:p%40ss%2Fword@db.example:6543/claims?sslmode=verify-full",
        )

    def test_password_url_defaults(self):
        self.assertEqual(
            db._password_url("h", "u", "p"),
            "postgresql://u:p@h:5432/fnol?sslmode=require",
        )
        os.environ.update({"FNOL_RDS_PORT": " ", "FNOL_RDS_DB": " ", "FNOL_RDS_SSLMODE": " "})
        self.assertEqual(
            db._password_url("h", "u", "p"),
            "postgresql://u:p@h:5432/fnol?sslmode=require",
        )

    def test_rds_host_without_credentials_fails(self):
        os.environ["FNOL_RDS_HOST"] = "db.example"
        with self.assertRaises(RuntimeError):
            db.database_url()
        os.environ["FNOL_RDS_USER"] = "fnol"
        with self.assertRaises(RuntimeError):
            db.database_url()

    def test_rds_iam_flag_uses_iam_url(self):
        os.environ.update({"FNOL_RDS_HOST": "db.example", "FNOL_RDS_IAM_AUTH": "Yes"})
        with mock.patch.object(db, "_url_from_iam", return_value="postgresql://iam") as loader:
            self.assertEqual(db.database_url(), "postgresql://iam")
        loader.assert_called_once_with("db.example")

    def test_region_order(self):
        self.assertEqual(db._region(), "us-east-1")
        os.environ["FNOL_AWS_REGION"] = "eu-west-1"
        self.assertEqual(db._region(), "eu-west-1")
        os.environ["AWS_REGION"] = " ap-south-1 "
        self.assertEqual(db._region(), "ap-south-1")

    def test_iam_url_requires_a_user(self):
        with self.assertRaises(RuntimeError):
            db._url_from_iam("db.example")

    def test_iam_url_signs_a_token_and_caches_it(self):
        os.environ.update({"FNOL_RDS_USER": "fnol", "FNOL_RDS_PORT": "5433", "AWS_REGION": "us-west-2"})
        client = mock.MagicMock()
        client.generate_db_auth_token.return_value = "tok/en"
        with mock.patch("boto3.client", return_value=client) as factory:
            first = db._url_from_iam("db.example")
            second = db._url_from_iam("db.example")
        self.assertEqual(first, second)
        self.assertIn("fnol:tok%2Fen@db.example:5433", first)
        factory.assert_called_once_with("rds", region_name="us-west-2")
        client.generate_db_auth_token.assert_called_once_with(
            DBHostname="db.example", Port=5433, DBUsername="fnol", Region="us-west-2"
        )

    def test_secret_url_reads_secrets_manager_and_caches(self):
        os.environ["FNOL_RDS_PORT"] = "1111"
        os.environ["FNOL_RDS_DB"] = "keepme"
        client = mock.MagicMock()
        client.get_secret_value.return_value = {
            "SecretString": '{"username": "u", "password": "p", "host": "h", "port": 6000, "dbname": "secretdb"}'
        }
        with mock.patch("boto3.client", return_value=client) as factory:
            first = db._url_from_secret("arn:one")
            second = db._url_from_secret("arn:one")
        self.assertEqual(first, "postgresql://u:p@h:6000/secretdb?sslmode=require")
        self.assertEqual(first, second)
        factory.assert_called_once()
        # Environment values are restored.
        self.assertEqual(os.environ["FNOL_RDS_PORT"], "1111")
        self.assertEqual(os.environ["FNOL_RDS_DB"], "keepme")

    def test_secret_url_removes_temporary_env_and_uses_fallbacks(self):
        os.environ["FNOL_RDS_HOST"] = "fallback.example"
        client = mock.MagicMock()
        client.get_secret_value.return_value = {
            "SecretString": '{"username": "u", "password": "p", "database": "other"}'
        }
        with mock.patch("boto3.client", return_value=client):
            url = db._url_from_secret("arn:two")
        self.assertEqual(url, "postgresql://u:p@fallback.example:5432/other?sslmode=require")
        self.assertNotIn("FNOL_RDS_PORT", os.environ)
        self.assertNotIn("FNOL_RDS_DB", os.environ)

    def test_secret_url_default_database_name(self):
        client = mock.MagicMock()
        client.get_secret_value.return_value = {
            "SecretString": '{"username": "u", "password": "p", "host": "h"}'
        }
        with mock.patch("boto3.client", return_value=client):
            url = db._url_from_secret("arn:three")
        self.assertEqual(url, "postgresql://u:p@h:5432/fnol?sslmode=require")


class NamespaceAndSchemaTests(EnvTestCase):
    def test_custom_namespace_tokens(self):
        os.environ["FNOL_PG_NAMESPACE"] = "ci1"
        self.assertEqual(db.namespace_suffix(), "_ci1")
        os.environ["FNOL_PG_NAMESPACE"] = "_ci2"
        self.assertEqual(db.namespace_suffix(), "_ci2")

    def test_invalid_namespace_token(self):
        os.environ["FNOL_PG_NAMESPACE"] = "Not Valid!"
        with self.assertRaises(ValueError):
            db.namespace_suffix()

    def test_isolate_without_anchor_uses_current_directory(self):
        os.environ["FNOL_PG_NAMESPACE"] = "isolate"
        self.assertEqual(db.namespace_suffix(), db.namespace_suffix(Path(".")))

    def test_namespace_label(self):
        self.assertEqual(db.namespace_label(""), "shared")
        self.assertEqual(db.namespace_label("_abc"), "ns_abc")

    def test_anchor_for_kinds(self):
        path = Path("/tmp/state/input.sqlite")
        self.assertEqual(db.anchor_for(path, "input"), path.parent)
        self.assertEqual(db.anchor_for(path, "trace"), path.parent)
        self.assertEqual(db.anchor_for(path, "mock"), path)

    def test_schema_name_validation(self):
        with self.assertRaises(ValueError):
            db.schema_name("bogus")
        with mock.patch.object(db, "namespace_suffix", return_value="-bad"):
            with self.assertRaises(ValueError):
                db.schema_name("input")

    def test_schema_for_depends_on_backend(self):
        self.assertEqual(db.schema_for(Path("/tmp/s/input.sqlite"), "input"), "")
        os.environ["FNOL_DATABASE_URL"] = "postgresql://u:p@h/d"
        self.assertEqual(db.schema_for(Path("/tmp/s/input.sqlite"), "input"), "input")

    def test_open_db_postgres(self):
        os.environ["FNOL_DATABASE_URL"] = "postgresql://u:p@h/d"
        raw = mock.MagicMock()
        with mock.patch.object(db, "ensure_ready", return_value="") as ready:
            with mock.patch("psycopg.connect", return_value=raw) as connect:
                conn = db.open_db(Path("/tmp/s/trace.sqlite"), kind="trace")
        ready.assert_called_once_with(Path("/tmp/s"))
        self.assertEqual(connect.call_args.args[0], "postgresql://u:p@h/d")
        self.assertEqual(conn.backend, "postgres")
        self.assertEqual(conn.schema, "trace")
        raw.execute.assert_called_once()

    def test_ensure_ready_shared_only_verifies(self):
        db._READY.clear()
        with mock.patch("fnol_foundation.migrate.apply_migrations") as apply:
            with mock.patch("fnol_foundation.migrate.verify_migrations") as verify:
                self.assertEqual(db.ensure_ready(), "")
                self.assertEqual(db.ensure_ready(), "")
        apply.assert_not_called()
        verify.assert_called_once_with("")

    def test_ensure_ready_isolate_applies_then_verifies(self):
        os.environ["FNOL_PG_NAMESPACE"] = "isolate"
        db._READY.clear()
        with mock.patch("fnol_foundation.migrate.apply_migrations") as apply:
            with mock.patch("fnol_foundation.migrate.verify_migrations") as verify:
                suffix = db.ensure_ready(Path("/tmp/iso"))
        self.assertTrue(suffix.startswith("_"))
        apply.assert_called_once_with(suffix)
        verify.assert_called_once_with(suffix)

    def test_drop_namespace_is_a_noop_on_sqlite(self):
        with mock.patch("psycopg.connect") as connect:
            db.drop_namespace(Path("/tmp/x"))
        connect.assert_not_called()

    def test_drop_namespace_truncates_shared_schemas(self):
        os.environ["FNOL_DATABASE_URL"] = "postgresql://u:p@h/d"
        raw = mock.MagicMock()
        with mock.patch("psycopg.connect") as connect:
            connect.return_value.__enter__.return_value = raw
            db.drop_namespace(Path("/tmp/x"), ("output", "trace"))
        self.assertEqual(raw.execute.call_count, 2)

    def test_drop_namespace_drops_isolated_schemas(self):
        os.environ["FNOL_DATABASE_URL"] = "postgresql://u:p@h/d"
        os.environ["FNOL_PG_NAMESPACE"] = "isolate"
        suffix = db.namespace_suffix(Path("/tmp/x"))
        db._READY.add(suffix)
        raw = mock.MagicMock()
        with mock.patch("psycopg.connect") as connect:
            connect.return_value.__enter__.return_value = raw
            db.drop_namespace(Path("/tmp/x"), ("input", "output"))
        self.assertEqual(raw.execute.call_count, 3)
        delete_call = raw.execute.call_args_list[-1]
        self.assertIn("DELETE FROM public.fnol_schema_migrations", delete_call.args[0])
        self.assertEqual(delete_call.args[1], (db.namespace_label(suffix),))
        self.assertNotIn(suffix, db._READY)


class SqlHelperTests(EnvTestCase):
    def test_qmark_inside_quotes_is_kept(self):
        self.assertEqual(
            db._qmark_to_pyformat("SELECT 'what?' , ? , 'it''s ?' , ?"),
            "SELECT 'what?' , %s , 'it''s ?' , %s",
        )

    def test_columns_helper(self):
        self.assertEqual(db._columns(mock.Mock(description=None)), [])
        self.assertEqual(db._columns(mock.Mock(description=[("a",), ("b",)])), ["a", "b"])

    def test_translate(self):
        already = UniqueViolation("x")
        self.assertIs(db._translate(already), already)
        self.assertIsInstance(db._translate(sqlite3.IntegrityError("UNIQUE constraint failed: t.k")), UniqueViolation)
        self.assertIsInstance(db._translate(sqlite3.IntegrityError("t is append-only")), AppendOnlyViolation)
        self.assertIsInstance(db._translate(pg_errors.UniqueViolation("u")), UniqueViolation)
        self.assertIsInstance(db._translate(pg_errors.RaiseException("r")), AppendOnlyViolation)
        self.assertIsInstance(db._translate(pg_errors.CheckViolation("c")), AppendOnlyViolation)
        self.assertIsInstance(db._translate(pg_errors.NotNullViolation("n")), AppendOnlyViolation)
        other = ValueError("other")
        self.assertIs(db._translate(other), other)

    def test_translate_without_psycopg(self):
        other = ValueError("other")
        with mock.patch.dict(sys.modules, {"psycopg": None}):
            self.assertIs(db._translate(other), other)

    def test_error_hierarchy(self):
        self.assertTrue(issubclass(UniqueViolation, IntegrityError))
        self.assertTrue(issubclass(AppendOnlyViolation, IntegrityError))


# ---------------------------------------------------------------------------
# migrate.py
# ---------------------------------------------------------------------------


class SplitSqlTests(unittest.TestCase):
    def test_splits_on_semicolons_and_skips_empty(self):
        self.assertEqual(migrate.split_sql("SELECT 1;; SELECT 2 ;"), ["SELECT 1", "SELECT 2"])

    def test_keeps_tail_without_semicolon(self):
        self.assertEqual(migrate.split_sql("SELECT 1; SELECT 2"), ["SELECT 1", "SELECT 2"])

    def test_semicolons_in_single_quotes(self):
        self.assertEqual(
            migrate.split_sql("INSERT INTO t VALUES ('a;b', 'it''s;ok'); SELECT 2;"),
            ["INSERT INTO t VALUES ('a;b', 'it''s;ok')", "SELECT 2"],
        )

    def test_dollar_quoted_bodies(self):
        script = (
            "CREATE FUNCTION f() RETURNS void AS $$ BEGIN PERFORM 1; END; $$ LANGUAGE plpgsql;"
            "CREATE FUNCTION g() RETURNS void AS $body$ SELECT $$x;$$; $body$ LANGUAGE sql;"
        )
        statements = migrate.split_sql(script)
        self.assertEqual(len(statements), 2)
        self.assertIn("PERFORM 1; END;", statements[0])
        self.assertIn("$body$ SELECT $$x;$$; $body$", statements[1])

    def test_bare_dollar_is_plain_text(self):
        self.assertEqual(migrate.split_sql("SELECT $1; SELECT 2"), ["SELECT $1", "SELECT 2"])

    def test_comments_are_dropped(self):
        self.assertEqual(
            migrate.split_sql("-- first; not a split\nSELECT 1; -- trailing\nSELECT 2;"),
            ["SELECT 1", "SELECT 2"],
        )
        self.assertEqual(migrate.split_sql("SELECT 1; -- ends without newline; x"), ["SELECT 1"])


class MigrateFileTests(EnvTestCase):
    def test_render_replaces_schema_placeholders(self):
        script = "CREATE SCHEMA {{input}}; CREATE SCHEMA {{output}}; {{trace}} {{mock}}"
        self.assertEqual(
            migrate.render(script, "_x"),
            "CREATE SCHEMA input_x; CREATE SCHEMA output_x; trace_x mock_x",
        )

    def test_migration_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(migrate, "SQL_DIR", root / "missing"):
                with self.assertRaises(FileNotFoundError):
                    migrate.migration_files()
            with mock.patch.object(migrate, "SQL_DIR", root):
                with self.assertRaises(FileNotFoundError):
                    migrate.migration_files()
                (root / "002_b.sql").write_text("SELECT 2;")
                (root / "001_a.sql").write_text("SELECT 1;")
                (root / "dir.sql").mkdir()
                (root / "notes.txt").write_text("x")
                self.assertEqual([p.name for p in migrate.migration_files()], ["001_a.sql", "002_b.sql"])

    def test_real_migration_files_exist(self):
        files = migrate.migration_files()
        self.assertTrue(files[0].name.startswith("001"))


def _connect_returning(conn):
    patcher = mock.patch("psycopg.connect")
    connect = patcher.start()
    connect.return_value.__enter__.return_value = conn
    return patcher, connect


class VerifyMigrationsTests(EnvTestCase):
    def setUp(self):
        super().setUp()
        os.environ["FNOL_DATABASE_URL"] = "postgresql://u:p@h/d"
        patcher = mock.patch.object(migrate, "migration_files", return_value=[Path("001_a.sql"), Path("002_b.sql")])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _conn(self, rows=None, error=None):
        conn = mock.MagicMock()
        if error is not None:
            conn.execute.side_effect = error
        else:
            conn.execute.return_value.fetchall.return_value = rows or []
        patcher, self.connect = _connect_returning(conn)
        self.addCleanup(patcher.stop)
        return conn

    def test_requires_a_database_url(self):
        os.environ.pop("FNOL_DATABASE_URL")
        with self.assertRaises(RuntimeError):
            migrate.verify_migrations("")

    def test_passes_when_all_files_are_recorded(self):
        conn = self._conn(rows=[("001_a.sql",), ("002_b.sql",)])
        migrate.verify_migrations()
        self.assertEqual(conn.execute.call_args.args[1], ("shared",))

    def test_reports_missing_versions(self):
        self._conn(rows=[("001_a.sql",)])
        with self.assertRaisesRegex(RuntimeError, "002_b.sql"):
            migrate.verify_migrations("_ns")

    def test_missing_table(self):
        self._conn(error=pg_errors.UndefinedTable("no table"))
        with self.assertRaisesRegex(RuntimeError, "does not exist"):
            migrate.verify_migrations("")

    def test_missing_privilege(self):
        self._conn(error=pg_errors.InsufficientPrivilege("denied"))
        with self.assertRaisesRegex(RuntimeError, "cannot read"):
            migrate.verify_migrations("")

    def test_other_database_errors_propagate(self):
        self._conn(error=pg_errors.OperationalError("down"))
        with self.assertRaises(pg_errors.OperationalError):
            migrate.verify_migrations("")


class ApplyMigrationsTests(EnvTestCase):
    def setUp(self):
        super().setUp()
        os.environ["FNOL_DATABASE_URL"] = "postgresql://u:p@h/d"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "001_a.sql").write_text("CREATE SCHEMA {{input}}; -- c\nCREATE TABLE {{input}}.t (x int);")
        (root / "002_b.sql").write_text("SELECT 2;")
        patcher = mock.patch.object(
            migrate, "migration_files", return_value=[root / "001_a.sql", root / "002_b.sql"]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_requires_a_database_url(self):
        os.environ.pop("FNOL_DATABASE_URL")
        with self.assertRaises(RuntimeError):
            migrate.apply_migrations("")

    def test_applies_only_pending_files_and_unlocks(self):
        conn = mock.MagicMock()

        def execute(sql, params=None):
            result = mock.MagicMock()
            if "SELECT 1 FROM public.fnol_schema_migrations" in str(sql):
                result.fetchone.return_value = (1,) if params[0] == "001_a.sql" else None
            return result

        conn.execute.side_effect = execute
        patcher, _connect = _connect_returning(conn)
        self.addCleanup(patcher.stop)
        applied = migrate.apply_migrations()
        self.assertEqual(applied, ["002_b.sql"])
        statements = [str(call.args[0]) for call in conn.execute.call_args_list]
        self.assertIn("SELECT pg_advisory_unlock(%s)", statements[-1])
        inserts = [call for call in conn.execute.call_args_list if "INSERT INTO public.fnol_schema_migrations" in str(call.args[0])]
        self.assertEqual(inserts[0].args[1], ("002_b.sql", "shared"))

    def test_applies_every_file_and_unlocks_after_failure(self):
        conn = mock.MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        patcher, _connect = _connect_returning(conn)
        self.addCleanup(patcher.stop)
        self.assertEqual(migrate.apply_migrations("_x"), ["001_a.sql", "002_b.sql"])

        failing = mock.MagicMock()

        def execute(sql, params=None):
            if "SELECT 1 FROM public" in str(sql):
                return mock.MagicMock(**{"fetchone.return_value": None})
            if "CREATE TABLE IF NOT EXISTS" in str(sql) or "advisory" in str(sql):
                return mock.MagicMock()
            raise pg_errors.SyntaxError("bad sql")

        failing.execute.side_effect = execute
        with mock.patch("psycopg.connect") as connect:
            connect.return_value.__enter__.return_value = failing
            with self.assertRaises(pg_errors.SyntaxError):
                migrate.apply_migrations("")
        self.assertIn("pg_advisory_unlock", str(failing.execute.call_args_list[-1].args[0]))


class MigrateMainTests(EnvTestCase):
    def test_main_reports_applied_and_current(self):
        out = io.StringIO()
        with mock.patch.object(migrate, "apply_migrations", return_value=["001_a.sql", "002_b.sql"]):
            with contextlib.redirect_stdout(out):
                self.assertEqual(migrate.main([]), 0)
        self.assertIn("applied 2 migration(s) to namespace shared: 001_a.sql, 002_b.sql", out.getvalue())

        out = io.StringIO()
        with mock.patch.object(migrate, "apply_migrations", return_value=[]):
            with contextlib.redirect_stdout(out):
                self.assertEqual(migrate.main(["--namespace", "tok1"]), 0)
        self.assertIn("namespace ns_tok1 is up to date", out.getvalue())
        self.assertEqual(os.environ["FNOL_PG_NAMESPACE"], "tok1")

    def test_module_entry_point(self):
        os.environ["FNOL_DATABASE_URL"] = "postgresql://u:p@h/d"
        conn = mock.MagicMock()
        patcher, _connect = _connect_returning(conn)
        self.addCleanup(patcher.stop)
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["migrate"]):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with contextlib.redirect_stdout(out):
                    with self.assertRaises(SystemExit) as raised:
                        runpy.run_module("fnol_foundation.migrate", run_name="__main__")
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("up to date", out.getvalue())


# ---------------------------------------------------------------------------
# s3store.py and blobs.py (boto3 mocked)
# ---------------------------------------------------------------------------


class _NoSuchKey(Exception):
    pass


def _s3_client():
    client = mock.MagicMock()
    client.exceptions.NoSuchKey = _NoSuchKey
    return client


def _client_error(code="AccessDenied"):
    return ClientError({"Error": {"Code": code, "Message": "no"}}, "GetObject")


class S3StoreTests(EnvTestCase):
    def setUp(self):
        super().setUp()
        s3store._CLIENT = None
        self.addCleanup(setattr, s3store, "_CLIENT", None)

    def test_region_order(self):
        self.assertEqual(s3store.region(), "us-east-1")
        os.environ["FNOL_AWS_REGION"] = "eu-west-2"
        self.assertEqual(s3store.region(), "eu-west-2")
        os.environ["AWS_REGION"] = "ap-southeast-1"
        self.assertEqual(s3store.region(), "ap-southeast-1")
        os.environ["AWS_REGION"] = "  "
        self.assertEqual(s3store.region(), "us-east-1")

    def test_client_is_built_once_with_retry_config(self):
        os.environ["AWS_REGION"] = "us-west-1"
        with mock.patch("boto3.client") as factory:
            first = s3store._client()
            second = s3store._client()
        self.assertIs(first, second)
        factory.assert_called_once()
        self.assertEqual(factory.call_args.args, ("s3",))
        self.assertEqual(factory.call_args.kwargs["region_name"], "us-west-1")
        self.assertEqual(factory.call_args.kwargs["config"].connect_timeout, 5)

    def test_client_requires_boto3(self):
        with mock.patch.dict(sys.modules, {"boto3": None}):
            with self.assertRaises(BlobError) as raised:
                s3store._client()
        self.assertEqual(raised.exception.error_code, "s3_deps")

    def test_get_object_bytes(self):
        client = _s3_client()
        body = mock.MagicMock()
        body.read.return_value = b"hello"
        client.get_object.return_value = {"Body": body}
        s3store._CLIENT = client
        self.assertEqual(s3store.get_object_bytes("bucket", "a/b.md"), b"hello")
        client.get_object.assert_called_once_with(Bucket="bucket", Key="a/b.md")
        body.close.assert_called_once()

    def test_get_object_bytes_non_bytes_body(self):
        client = _s3_client()
        body = mock.MagicMock()
        body.read.return_value = "text"
        client.get_object.return_value = {"Body": body}
        s3store._CLIENT = client
        self.assertIsNone(s3store.get_object_bytes("bucket", "k"))
        body.close.assert_called_once()

    def test_get_object_bytes_missing_key(self):
        client = _s3_client()
        client.get_object.side_effect = _NoSuchKey()
        s3store._CLIENT = client
        self.assertIsNone(s3store.get_object_bytes("bucket", "missing"))

    def test_get_object_bytes_client_error(self):
        client = _s3_client()
        client.get_object.side_effect = _client_error()
        s3store._CLIENT = client
        with self.assertRaises(BlobError) as raised:
            s3store.get_object_bytes("bucket", "k")
        self.assertEqual(raised.exception.error_code, "s3_read")

    def test_list_keys_filters_and_paginates(self):
        client = _s3_client()
        paginator = mock.MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "docs/"}, {"Key": "docs/a.md"}, {"Key": 7}, {"Key": "other/x.md"}]},
            {},
            {"Contents": [{"Key": "docs/b.md"}]},
        ]
        client.get_paginator.return_value = paginator
        s3store._CLIENT = client
        self.assertEqual(s3store.list_keys("bucket", "/docs/"), ["docs/a.md", "docs/b.md"])
        client.get_paginator.assert_called_once_with("list_objects_v2")
        paginator.paginate.assert_called_once_with(Bucket="bucket", Prefix="docs/")

    def test_list_keys_client_error(self):
        client = _s3_client()
        client.get_paginator.return_value.paginate.side_effect = _client_error()
        s3store._CLIENT = client
        with self.assertRaises(BlobError) as raised:
            s3store.list_keys("bucket", "docs")
        self.assertEqual(raised.exception.error_code, "s3_list")


class BlobsTests(EnvTestCase):
    def test_blob_error_has_a_code(self):
        error = BlobError("code_x", "message")
        self.assertEqual(error.error_code, "code_x")
        self.assertEqual(str(error), "message")

    def test_store_selection_and_containers(self):
        self.assertFalse(blobs.s3_enabled())
        self.assertEqual(blobs.s3_bucket(), "")
        os.environ["FNOL_S3_BUCKET"] = " bucket "
        self.assertTrue(blobs.s3_enabled())
        self.assertEqual(blobs.s3_bucket(), "bucket")
        self.assertEqual(blobs.documents_container(), "case-documents")
        self.assertEqual(blobs.policies_container(), "policy-wording")
        self.assertEqual(blobs.fixtures_container(), "fnol-fixtures")
        os.environ.update({
            "FNOL_DOCUMENTS_CONTAINER": "docs",
            "FNOL_POLICIES_CONTAINER": "pols",
            "FNOL_FIXTURES_CONTAINER": "fix",
        })
        self.assertEqual(blobs.container_for("/x/Documents"), "docs")
        self.assertEqual(blobs.container_for("/x/case-documents"), "docs")
        self.assertEqual(blobs.container_for("/x/policies"), "pols")
        self.assertEqual(blobs.container_for("/x/policy-wording"), "pols")
        self.assertEqual(blobs.container_for("/x/dev"), "fix")
        self.assertEqual(blobs.container_for("/x/fixtures"), "fix")
        self.assertEqual(blobs.container_for("/x/fnol-fixtures"), "fix")
        self.assertEqual(blobs.container_for("/x/Other"), "other")

    def test_empty_container_overrides_use_defaults(self):
        os.environ.update({
            "FNOL_DOCUMENTS_CONTAINER": " ",
            "FNOL_POLICIES_CONTAINER": " ",
            "FNOL_FIXTURES_CONTAINER": " ",
        })
        self.assertEqual(blobs.documents_container(), "case-documents")
        self.assertEqual(blobs.policies_container(), "policy-wording")
        self.assertEqual(blobs.fixtures_container(), "fnol-fixtures")

    def test_disk_read_source_rejects_bad_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "good.md").write_text("hello", encoding="utf-8")
            Path(tmp, "binary.md").write_bytes(b"\xff\xfe\x00")
            found = blobs.read_source(tmp, "good")
            self.assertEqual(found[2], "hello")
            self.assertIsNone(blobs.read_source(tmp, "missing"))
            self.assertIsNone(blobs.read_source(tmp, "binary"))
            for bad in ("", None, "../x", "a/b", "a\\b", 5):
                self.assertIsNone(blobs.read_source(tmp, bad))

    def test_disk_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "b.md").write_text("two", encoding="utf-8")
            Path(tmp, "a.md").write_text("one", encoding="utf-8")
            Path(tmp, "skip.txt").write_text("x", encoding="utf-8")
            listed = blobs.list_markdown_sources(tmp)
            self.assertEqual([item["source_id"] for item in listed], ["a", "b"])
            self.assertTrue(all(len(item["sha256"]) == 64 for item in listed))
            with self.assertRaises(FileNotFoundError):
                blobs.list_markdown_sources(Path(tmp) / "nope")

    def test_s3_read_source_outcomes(self):
        os.environ["FNOL_S3_BUCKET"] = "bkt"
        with mock.patch("fnol_foundation.s3store.get_object_bytes", return_value=b"hi") as reader:
            found = blobs.read_source("/x/policies", "P1")
        self.assertEqual(found, ("s3://bkt/policy-wording/P1.md", b"hi", "hi"))
        reader.assert_called_once_with("bkt", "policy-wording/P1.md")
        with mock.patch("fnol_foundation.s3store.get_object_bytes", return_value=None):
            self.assertIsNone(blobs.read_source("/x/policies", "P1"))
        with mock.patch("fnol_foundation.s3store.get_object_bytes", return_value=b"\xff\xfe"):
            self.assertIsNone(blobs.read_source("/x/policies", "P1"))
        with mock.patch("fnol_foundation.s3store.get_object_bytes", side_effect=BlobError("s3_read", "x")):
            with self.assertRaises(BlobError):
                blobs.read_source("/x/policies", "P1")

    def test_s3_listing_skips_non_markdown_and_missing_objects(self):
        os.environ["FNOL_S3_BUCKET"] = "bkt"
        keys = [
            "policy-wording/a.md",
            "policy-wording/b.txt",
            "policy-wording/sub/c.md",
            "policy-wording/gone.md",
        ]

        def read(bucket, key):
            return None if key.endswith("gone.md") else b"body " + key.encode()

        with mock.patch("fnol_foundation.s3store.list_keys", return_value=keys) as lister:
            with mock.patch("fnol_foundation.s3store.get_object_bytes", side_effect=read):
                listed = blobs.list_markdown_sources("/x/policies")
        lister.assert_called_once_with("bkt", "policy-wording")
        # The listing uses the final path segment, so a nested .md is still listed.
        self.assertEqual([item["source_id"] for item in listed], ["a", "c"])


# ---------------------------------------------------------------------------
# aws_auth.py (botocore credentials mocked)
# ---------------------------------------------------------------------------


class AwsAuthTests(EnvTestCase):
    URL = "https://bedrock-mantle.us-east-1.api.aws/v1/chat/completions"

    def _session(self, credentials):
        session = mock.MagicMock()
        session.get_credentials.return_value = credentials
        return mock.patch("botocore.session.Session", return_value=session)

    def test_signs_with_sigv4(self):
        credentials = _Credentials("AKIDEXAMPLE", "secret", "session-token")
        with self._session(credentials):
            signed = aws_auth.sign_headers(
                self.URL, "POST", b'{"a":1}', "us-east-1", {"Content-Type": "application/json"}
            )
        self.assertTrue(signed["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/"))
        self.assertIn("/us-east-1/bedrock/aws4_request", signed["Authorization"])
        self.assertEqual(signed["X-Amz-Security-Token"], "session-token")
        self.assertEqual(signed["Content-Type"], "application/json")
        self.assertNotIn("X-Number", signed)
        self.assertEqual(SigV4Auth.calls[-1][1:], ("bedrock", "us-east-1"))

    def test_signs_without_a_body(self):
        with self._session(_Credentials("AKIDEXAMPLE", "secret")):
            signed = aws_auth.sign_headers(self.URL, "GET", None, "us-east-1", {})
        self.assertIn("Authorization", signed)

    def test_missing_credentials(self):
        with self._session(None):
            with self.assertRaises(ShadowError) as raised:
                aws_auth.sign_headers(self.URL, "POST", b"", "us-east-1", {})
        self.assertEqual(raised.exception.error_code, "aws_credentials_missing")

    def test_missing_botocore(self):
        with mock.patch.dict(sys.modules, {"botocore.session": None}):
            with self.assertRaises(ShadowError) as raised:
                aws_auth.sign_headers(self.URL, "POST", b"", "us-east-1", {})
        self.assertEqual(raised.exception.error_code, "aws_deps")


# ---------------------------------------------------------------------------
# http.py and events.py
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class HttpTests(unittest.TestCase):
    def test_tool_url_validation(self):
        self.assertEqual(http.tool_url("http://h/", "C-1", "policy"), "http://h/v1/cases/C-1/policy")
        with self.assertRaises(ValueError):
            http.tool_url("http://h", "C-1", "admin")
        with self.assertRaises(ValueError):
            http.tool_url("http://h", "../x", "policy")
        with self.assertRaises(ValueError):
            http.tool_url("http://h", 5, "claims")
        with self.assertRaises(ValueError):
            http.tool_url("", "C-1", "claims")
        with self.assertRaises(ValueError):
            http.tool_url(None, "C-1", "claims")

    def test_public_error(self):
        self.assertEqual(http.public_error(None), "http_error")
        self.assertEqual(http.public_error({"error": 5}), "http_error")
        self.assertEqual(http.public_error({"error": "has space"}), "http_error")
        self.assertEqual(http.public_error({"error": "private_token"}), "restricted_value")
        self.assertEqual(http.public_error({"error": "not_found"}), "not_found")

    def test_default_get_success(self):
        with mock.patch("urllib.request.urlopen", return_value=_Response(200, b'{"ok": true}')):
            self.assertEqual(http.default_get("http://h/x"), (200, {"ok": True}))

    def test_default_get_http_error(self):
        error = urllib.error.HTTPError("http://h/x", 404, "nf", {}, io.BytesIO(b'{"error": "nope"}'))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertEqual(http.default_get("http://h/x"), (404, {"error": "nope"}))

    def test_default_get_transport_errors(self):
        for failure in (urllib.error.URLError("down"), TimeoutError(), OSError("reset")):
            with mock.patch("urllib.request.urlopen", side_effect=failure):
                self.assertEqual(http.default_get("http://h/x"), (0, {"error": "transport_error"}))

    def test_object_helper(self):
        self.assertIsNone(http._object(b"\xff\xfe"))
        self.assertIsNone(http._object(b"not json"))
        self.assertIsNone(http._object(b"[1, 2]"))
        self.assertEqual(http._object(b'{"a": 1}'), {"a": 1})

    def test_fetch_requires_an_attempt(self):
        with self.assertRaises(ValueError):
            http.fetch_with_retries("http://h/x", max_attempts=0)

    def test_fetch_retries_then_succeeds(self):
        responses = iter([(0, None), (503, {"error": "busy"}), (200, {"ok": 1})])
        attempts, body = http.fetch_with_retries("http://h/x", get=lambda url: next(responses))
        self.assertEqual(body, {"ok": 1})
        self.assertEqual([attempt.status for attempt in attempts], [0, 503, 200])

    def test_fetch_stops_on_client_error_and_exhaustion(self):
        attempts, body = http.fetch_with_retries("http://h/x", get=lambda url: (404, {"error": "nf"}))
        self.assertIsNone(body)
        self.assertEqual(len(attempts), 1)
        attempts, body = http.fetch_with_retries("http://h/x", get=lambda url: (500, None))
        self.assertIsNone(body)
        self.assertEqual(len(attempts), http.MAX_ATTEMPTS)

    def test_fetch_ignores_non_object_bodies(self):
        attempts, body = http.fetch_with_retries("http://h/x", get=lambda url: (200, [1, 2]))
        self.assertIsNone(body)
        self.assertIsNone(attempts[0].body)


class EventsTests(unittest.TestCase):
    def test_record_event_requires_a_name_and_adds_span_id(self):
        trace = mock.MagicMock()
        fake_stores = mock.MagicMock(trace=trace)
        with self.assertRaises(ValueError):
            events.record_event(fake_stores, step="x")
        span = events.record_event(fake_stores, event="e", step="x")
        self.assertEqual(len(span), 32)
        self.assertEqual(trace.append_event.call_args.kwargs["span_id"], span)
        self.assertEqual(events.record_event(fake_stores, event="e", span_id="fixed"), "fixed")


# ---------------------------------------------------------------------------
# stores.py
# ---------------------------------------------------------------------------


class _InsertHook:
    """Wrap a connection so an INSERT into one table raises UniqueViolation."""

    def __init__(self, real, table, *, params=None, times=1, state=None):
        self.real = real
        self.table = table
        self.params = params
        self.times = times
        self.state = state if state is not None else {"count": 0}

    def __enter__(self):
        self.real.__enter__()
        return self

    def __exit__(self, *exc):
        return self.real.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self.real, name)

    def execute(self, sql, params=()):
        if sql.lstrip().startswith(f"INSERT INTO {self.table}") and self.state["count"] < self.times:
            self.state["count"] += 1
            if self.params is not None:
                self.real.execute(sql, self.params(tuple(params)))
            raise UniqueViolation("duplicate")
        return self.real.execute(sql, params)


class FakeDb:
    def __init__(self, *results):
        self.results = list(results)
        self.sql: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.sql.append(sql)
        return mock.MagicMock(**{"fetchall.return_value": self.results.pop(0)})


class StoresTestCase(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stores = stores.Stores(self.tmp.name)
        self.payload = {"policy_number": "PA-1", "loss_reference": "L-1"}

    def revision(self, case_id="C-1"):
        return self.stores.input.append_revision(case_id, self.payload)


class InputStoreCoverageTests(StoresTestCase):
    def test_locator_and_stored_blob_for_sqlite(self):
        self.assertTrue(self.stores.input.locator.endswith("input.sqlite"))
        self.assertTrue(self.stores.input.stored_blob().startswith(b"SQLite format"))

    def test_append_revision_validation(self):
        with self.assertRaises(ValueError):
            self.stores.input.append_revision("", self.payload)
        with self.assertRaises(TypeError):
            self.stores.input.append_revision("C-1", ["not", "a", "dict"])
        with self.assertRaises(ValueError):
            self.stores.input.append_revision("C-1", self.payload, document_set_sha256="")

    def test_append_revision_retries_once_after_a_unique_violation(self):
        store = self.stores.input
        real = store._conn
        state = {"count": 0}
        with mock.patch.object(store, "_conn", side_effect=lambda: _InsertHook(real(), "input_revisions", state=state)):
            revision = store.append_revision("C-1", self.payload)
        self.assertEqual(revision.revision, 1)
        self.assertEqual(state["count"], 1)

    def test_append_revision_gives_up_after_two_violations(self):
        store = self.stores.input
        real = store._conn
        with mock.patch.object(store, "_conn", side_effect=lambda: _InsertHook(real(), "input_revisions", times=5)):
            with self.assertRaises(UniqueViolation):
                store.append_revision("C-1", self.payload)

    def test_require_revision_validation(self):
        store = self.stores.input
        with self.assertRaises(ValueError):
            store.put_tool_payload("", "policy", {}, 1)
        for revision in (None, 0, True, "1"):
            with self.assertRaises(ValueError):
                store.put_tool_payload("C-1", "policy", {}, revision)
        with self.assertRaises(ValueError):
            store.put_tool_payload("C-1", "policy", {}, 1)

    def test_tool_payload_argument_validation(self):
        self.revision()
        with self.assertRaises(ValueError):
            self.stores.input.put_tool_payload("C-1", "admin", {}, 1)
        with self.assertRaises(TypeError):
            self.stores.input.put_tool_payload("C-1", "policy", [], 1)

    def test_tool_payload_conflicts(self):
        store = self.stores.input
        self.revision()
        sha = store.put_tool_payload("C-1", "policy", {"a": 1}, 1)
        self.assertEqual(store.put_tool_payload("C-1", "policy", {"a": 1}, 1), sha)
        with self.assertRaises(ImmutableRecordError):
            store.put_tool_payload("C-1", "policy", {"a": 2}, 1)
        self.assertIsNone(store.get_tool_payload("C-1", "claims", 1))

    def test_tool_payload_race_with_identical_bytes_returns_the_sha(self):
        store = self.stores.input
        self.revision()
        real = store._conn
        with mock.patch.object(
            store, "_conn",
            side_effect=lambda: _InsertHook(real(), "tool_payloads", params=lambda values: values),
        ):
            sha = store.put_tool_payload("C-1", "policy", {"a": 1}, 1)
        self.assertEqual(store.get_tool_payload("C-1", "policy", 1)["sha256"], sha)

    def test_tool_payload_race_with_different_bytes_is_refused(self):
        store = self.stores.input
        self.revision()
        real = store._conn

        def other_sha(values):
            return values[:4] + ("0" * 64,) + values[5:]

        with mock.patch.object(
            store, "_conn", side_effect=lambda: _InsertHook(real(), "tool_payloads", params=other_sha)
        ):
            with self.assertRaises(ImmutableRecordError):
                store.put_tool_payload("C-1", "policy", {"a": 1}, 1)

    def test_tool_payload_race_without_a_row_is_refused(self):
        store = self.stores.input
        self.revision()
        real = store._conn
        with mock.patch.object(store, "_conn", side_effect=lambda: _InsertHook(real(), "tool_payloads")):
            with self.assertRaises(ImmutableRecordError):
                store.put_tool_payload("C-1", "policy", {"a": 1}, 1)

    def test_decision_validation_and_conflicts(self):
        store = self.stores.input
        self.revision()
        with self.assertRaises(ValueError):
            store.put_decision("C-1", 1, "bogus", {})
        with self.assertRaises(TypeError):
            store.put_decision("C-1", 1, "policy", [])
        first = store.put_decision("C-1", 1, "policy", {"a": 1}, policy_sha256="p", claims_sha256="c")
        self.assertEqual(first["body"], {"a": 1})
        again = store.put_decision("C-1", 1, "policy", {"a": 1}, policy_sha256="p", claims_sha256="c")
        self.assertEqual(again["policy_sha256"], "p")
        with self.assertRaises(ImmutableRecordError):
            store.put_decision("C-1", 1, "policy", {"a": 2}, policy_sha256="p", claims_sha256="c")
        self.assertIsNone(store.get_decision("C-1", 1, "route"))

    def test_decision_race_outcomes(self):
        store = self.stores.input
        self.revision()
        real = store._conn

        def run(params):
            with mock.patch.object(
                store, "_conn", side_effect=lambda: _InsertHook(real(), "decisions", params=params)
            ):
                return store.put_decision("C-1", 1, "route", {"a": 1})

        self.assertEqual(run(lambda values: values)["body"], {"a": 1})

        store.put_decision("C-1", 1, "citations", {"b": 1})
        self.assertIsNone(store.get_decision("C-1", 1, "policy"))

        def run_other(kind, params):
            with mock.patch.object(
                store, "_conn", side_effect=lambda: _InsertHook(real(), "decisions", params=params)
            ):
                return store.put_decision("C-1", 1, kind, {"a": 1})

        def other_body(values):
            return values[:3] + ('{"z":9}',) + values[4:]

        with self.assertRaises(ImmutableRecordError):
            run_other("policy", other_body)
        with self.assertRaises(ImmutableRecordError):
            with mock.patch.object(store, "_conn", side_effect=lambda: _InsertHook(real(), "decisions")):
                store.put_decision("C-1", 1, "route", {"a": 5})

    def test_procedures_roundtrip(self):
        store = self.stores.input
        store.store_procedures([{"source_id": "B", "sha256": "2"}, {"source_id": "A", "sha256": "1"}])
        self.assertEqual([row["source_id"] for row in store.list_procedures()], ["A", "B"])
        self.assertEqual(store.latest_loss_keys(), [])
        self.revision("C-2")
        self.assertEqual(store.latest_loss_keys(), [{"case_id": "C-2", "loss_reference": "L-1"}])
        self.assertIsNone(store.get_revision("C-9"))

    def test_legacy_sqlite_files_are_upgraded(self):
        path = Path(self.tmp.name) / "legacy" / "input.sqlite"
        path.parent.mkdir()
        raw = sqlite3.connect(path)
        raw.executescript(
            """
            CREATE TABLE input_revisions (
              case_id TEXT NOT NULL, revision INTEGER NOT NULL,
              ingested_at TEXT NOT NULL, document_set_sha256 TEXT NOT NULL,
              UNIQUE (case_id, revision)
            );
            CREATE TABLE tool_payloads (case_id TEXT, tool TEXT, raw_json TEXT);
            """
        )
        raw.close()
        store = stores.InputStore(path)
        with store.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(input_revisions)").fetchall()}
            payload_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tool_payloads)").fetchall()}
        self.assertTrue({"trace_id", "repeated_fields", "extra_fields", "injection_field_status"} <= columns)
        self.assertIn("input_revision", payload_columns)
        self.assertEqual(stores._table_names(sqlite3_conn(path), "missing_table"), set())

    def test_value_helpers(self):
        self.assertIsNone(stores._bind_value("repeated_fields", []))
        self.assertEqual(stores._bind_value("repeated_fields", ["a"]), '["a"]')
        self.assertIsNone(stores._bind_value("injury_reported", None))
        self.assertEqual(stores._bind_value("injury_reported", True), 1)
        self.assertEqual(stores._bind_value("loss_type", "x"), "x")
        self.assertTrue(stores._read_value("injury_reported", 1))
        self.assertIsNone(stores._read_value("injury_reported", None))
        self.assertEqual(stores._read_value("extra_fields", None), [])
        self.assertEqual(stores._read_value("extra_fields", '["a"]'), ["a"])
        self.assertEqual(stores._read_value("loss_type", "x"), "x")
        self.assertEqual(stores._as_text(__import__("datetime").datetime(2026, 1, 2)), "2026-01-02T00:00:00")


def sqlite3_conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


class OutputStoreCoverageTests(StoresTestCase):
    def test_record_validation(self):
        with self.assertRaises(ValueError):
            self.stores.output.record_draft("", {})
        with self.assertRaises(TypeError):
            self.stores.output.record_draft("C-1", [])

    def test_review_queries(self):
        output = self.stores.output
        a = output.record_draft("C-1", {"n": 1}, draft_id="a", created_at="2026-01-01T00:00:00")
        b = output.record_draft("C-1", {"n": 2}, draft_id="b", created_at="2026-01-02T00:00:00")
        c = output.record_draft("C-2", {"n": 3}, draft_id="c", created_at="2026-01-03T00:00:00")
        self.assertEqual(a.status, "recorded")
        self.assertEqual(output.latest_draft("C-1").draft_id, "b")
        self.assertIsNone(output.latest_draft("nope"))
        self.assertIsNone(output.get_draft("missing"))

        self.assertEqual(output.in_review(), [])
        output.update_status("a", "in_review", review_id="R1")
        output.update_status("b", "in_review", review_id="R2")
        self.assertEqual([d.draft_id for d in output.in_review()], ["a", "b"])

        cleared = output.update_status("a", "recorded", clear_review_id=True)
        self.assertIsNone(cleared.review_id)
        self.assertEqual([d.draft_id for d in output.in_review()], ["b"])

        self.assertEqual(output.approved_unfiled(), [])
        output.update_status("c", "approved", review_id="R3", outcome="approved")
        output.update_status("b", "approved", outcome="rejected")
        self.assertEqual([d.draft_id for d in output.approved_unfiled()], ["c"])
        self.assertEqual(c.draft_hash, output.get_draft("c").draft_hash)

        with self.assertRaises(KeyError):
            output.update_status("missing", "approved")
        with self.assertRaises(KeyError):
            output.update_status("missing", "approved", clear_review_id=True)


class TraceStoreCoverageTests(StoresTestCase):
    def test_missing_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing trace fields"):
            self.stores.trace.append_event(trace_id="t", step="s")

    def test_append_and_columns(self):
        trace = self.stores.trace
        first = trace.append_event(trace_id="t", span_id="1", step="s", actor="a", status="ok", started_at=__import__("datetime").datetime(2026, 1, 1))
        second = trace.append_event(trace_id="t", span_id="2", step="s", actor="a", status="ok")
        self.assertEqual(second["prev_hash"], first["event_hash"])
        self.assertIsNone(trace.get_event("missing"))
        self.assertEqual(trace.column_names(), tuple(stores.TRACE_COLUMNS))

    def test_legacy_trace_table_gains_columns(self):
        path = Path(self.tmp.name) / "legacy" / "trace.sqlite"
        path.parent.mkdir()
        raw = sqlite3.connect(path)
        raw.executescript(
            """
            CREATE TABLE trace_events (
              event_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, span_id TEXT NOT NULL,
              step TEXT NOT NULL, actor TEXT NOT NULL, status TEXT NOT NULL,
              started_at TEXT NOT NULL
            );
            """
        )
        raw.close()
        store = stores.TraceStore(path)
        self.assertIn("event_hash", store.column_names())

    def test_event_helper_writes_through_the_trace_store(self):
        span = events.record_event(
            self.stores, event="done", trace_id="t", step="s", actor="a", status="ok"
        )
        rows = self.stores.trace.connect().execute("SELECT span_id, event FROM trace_events").fetchall()
        self.assertEqual((rows[0]["span_id"], rows[0]["event"]), (span, "done"))


class PostgresModeStoreTests(StoresTestCase):
    """The Postgres branches, with the connection faked."""

    def setUp(self):
        super().setUp()
        for target, value in (
            ("fnol_foundation.stores.using_postgres", True),
        ):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("fnol_foundation.stores.schema_for", side_effect=lambda path, kind: kind)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stores_skip_sqlite_bootstrap(self):
        directory = Path(self.tmp.name) / "never-created"
        pg = stores.Stores(directory)
        self.assertFalse(directory.exists())
        self.assertEqual(pg.backend, "postgres")
        self.assertEqual(pg.input.locator, "postgres:input")
        self.assertEqual(pg.output.schema, "output")
        self.assertEqual(pg.trace.schema, "trace")

    def test_stored_blob_dumps_every_table(self):
        pg = stores.Stores(Path(self.tmp.name) / "pg")
        fake = FakeDb(
            [{"table_name": "alpha"}, {"table_name": "Bad-Name"}, {"table_name": "beta"}],
            [{"id": 1}],
            [{"id": 2}, {"id": 3}],
        )
        with mock.patch.object(pg.input, "_conn", return_value=fake):
            blob = pg.input.stored_blob()
        self.assertEqual(blob, b"{'id': 1}\n{'id': 2}\n{'id': 3}")
        self.assertEqual(len(fake.sql), 3)

    def test_trace_column_names_exclude_seq(self):
        pg = stores.Stores(Path(self.tmp.name) / "pg")
        fake = FakeDb([{"column_name": "seq"}, {"column_name": "event_id"}, {"column_name": "step"}])
        with mock.patch.object(pg.trace, "_conn", return_value=fake):
            self.assertEqual(pg.trace.column_names(), ("event_id", "step"))

    def test_drop_uses_the_namespace_helper(self):
        pg = stores.Stores(Path(self.tmp.name) / "pg")
        with mock.patch("fnol_foundation.stores.drop_namespace") as drop:
            pg.drop()
        drop.assert_called_once_with(pg.directory, ("input", "output", "trace"))


class StoresDirectoryTests(EnvTestCase):
    def test_state_dir_and_drop_on_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FNOL_STATE_DIR"] = str(Path(tmp) / "state")
            self.assertEqual(stores.state_dir(), Path(tmp) / "state")
            state = stores.Stores()
            self.assertTrue(state.input.path.exists())
            self.assertEqual(state.backend, "sqlite")
            state.drop()
            self.assertFalse(state.input.path.exists())
            self.assertFalse(state.trace.path.exists())


if __name__ == "__main__":
    unittest.main()
