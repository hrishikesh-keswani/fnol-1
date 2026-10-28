"""Apply versioned PostgreSQL DDL for FNOL state.

SQL files live in ``deploy/aws/sql/postgres``. Each file is applied once per
namespace and recorded in ``public.fnol_schema_migrations``. Files already
applied are not edited in place; ship a new version instead.

``python tools/migrate.py`` is the operator entry point.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from fnol_foundation.db import database_url, namespace_label, namespace_suffix

ROOT = Path(__file__).resolve().parents[2]
SQL_DIR = ROOT / "deploy" / "aws" / "sql" / "postgres"
_LOCK_KEY = 842150451


def split_sql(script: str) -> list[str]:
    """Split a script on semicolons, keeping dollar-quoted bodies intact."""
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(script)
    dollar: str | None = None
    in_single = False
    while i < n:
        if dollar is not None:
            if script.startswith(dollar, i):
                buf.append(dollar)
                i += len(dollar)
                dollar = None
                continue
            buf.append(script[i])
            i += 1
            continue
        if in_single:
            buf.append(script[i])
            if script[i] == "'":
                if i + 1 < n and script[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_single = False
            i += 1
            continue
        if script[i] == "'":
            in_single = True
            buf.append(script[i])
            i += 1
            continue
        if script[i] == "$":
            match = re.match(r"\$[A-Za-z0-9_]*\$", script[i:])
            if match:
                dollar = match.group(0)
                buf.append(dollar)
                i += len(dollar)
                continue
        if script[i] == "-" and i + 1 < n and script[i + 1] == "-":
            end = script.find("\n", i)
            if end < 0:
                break
            i = end
            continue
        if script[i] == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue
        buf.append(script[i])
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def render(script: str, suffix: str) -> str:
    names = {
        "input": "input" + suffix,
        "output": "output" + suffix,
        "trace": "trace" + suffix,
        "mock": "mock" + suffix,
    }
    rendered = script
    for kind, name in names.items():
        rendered = rendered.replace("{{" + kind + "}}", name)
    return rendered


def migration_files() -> list[Path]:
    if not SQL_DIR.is_dir():
        raise FileNotFoundError(f"migration directory missing: {SQL_DIR}")
    files = sorted(path for path in SQL_DIR.glob("*.sql") if path.is_file())
    if not files:
        raise FileNotFoundError(f"no SQL files in {SQL_DIR}")
    return files


def pending_versions(applied: set[str], files: list[Path]) -> list[str]:
    """SQL files that are not yet recorded for this namespace, in apply order."""
    return [path.name for path in files if path.name not in applied]


def verify_migrations(suffix: str | None = None) -> None:
    """Confirm this namespace's files are already recorded.

    Service roles call this at startup. They are not allowed to create
    ``public.fnol_schema_migrations`` or apply DDL. ``tools/migrate.py`` does that.
    """
    if suffix is None:
        suffix = namespace_suffix(None)
    url = database_url()
    if not url:
        raise RuntimeError(
            "PostgreSQL is not configured. Set FNOL_DATABASE_URL or FNOL_RDS_HOST "
            "with IAM auth or a password."
        )
    import psycopg

    label = namespace_label(suffix)
    files = migration_files()
    with psycopg.connect(url, connect_timeout=15) as conn:
        try:
            rows = conn.execute(
                """SELECT version FROM public.fnol_schema_migrations
                   WHERE namespace = %s""",
                (label,),
            ).fetchall()
        except psycopg.Error as exc:
            if getattr(exc, "sqlstate", None) == "42P01":
                raise RuntimeError(
                    "public.fnol_schema_migrations does not exist. "
                    "Run tools/migrate.py before starting services."
                ) from exc
            if getattr(exc, "sqlstate", None) == "42501":
                raise RuntimeError(
                    "this database user cannot read public.fnol_schema_migrations. "
                    "Grant SELECT, or run tools/migrate.py as fnol_migrate."
                ) from exc
            raise
    missing = pending_versions({row[0] for row in rows}, files)
    if missing:
        raise RuntimeError(
            "PostgreSQL migrations are not applied for namespace "
            f"{label}: {', '.join(missing)}. Run tools/migrate.py."
        )


def apply_migrations(suffix: str | None = None) -> list[str]:
    """Apply any pending files for this namespace. Returns the versions applied.

    This creates ``public.fnol_schema_migrations`` when it is missing. Only the
    migrate role (or a local database owner) may call it. Services use
    ``verify_migrations``.
    """
    if suffix is None:
        suffix = namespace_suffix(None)
    url = database_url()
    if not url:
        raise RuntimeError(
            "PostgreSQL is not configured. Set FNOL_DATABASE_URL, FNOL_DB_SECRET_ARN, "
            "or FNOL_RDS_HOST with IAM auth or a password."
        )
    import psycopg

    label = namespace_label(suffix)
    applied: list[str] = []
    with psycopg.connect(url, connect_timeout=15, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS public.fnol_schema_migrations (
                  version text NOT NULL,
                  namespace text NOT NULL,
                  applied_at timestamptz NOT NULL DEFAULT now(),
                  PRIMARY KEY (version, namespace)
                )
                """
            )
            for path in migration_files():
                version = path.name
                found = conn.execute(
                    """SELECT 1 FROM public.fnol_schema_migrations
                       WHERE version = %s AND namespace = %s""",
                    (version, label),
                ).fetchone()
                if found:
                    continue
                script = render(path.read_text(encoding="utf-8"), suffix)
                for statement in split_sql(script):
                    # sql.SQL leaves '%' alone. plpgsql RAISE and format() use it.
                    conn.execute(psycopg.sql.SQL(statement))
                conn.execute(
                    """INSERT INTO public.fnol_schema_migrations (version, namespace)
                       VALUES (%s, %s)""",
                    (version, label),
                )
                applied.append(version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply FNOL PostgreSQL migrations")
    parser.add_argument(
        "--namespace",
        default=None,
        help="Override FNOL_PG_NAMESPACE for this run (shared, isolate, or a token).",
    )
    args = parser.parse_args(argv)
    if args.namespace:
        import os

        os.environ["FNOL_PG_NAMESPACE"] = args.namespace
    suffix = namespace_suffix(Path.cwd())
    applied = apply_migrations(suffix)
    label = namespace_label(suffix)
    if applied:
        print(f"applied {len(applied)} migration(s) to namespace {label}: {', '.join(applied)}")
    else:
        print(f"namespace {label} is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
