#!/usr/bin/env python3
"""Run the Harborlight reviewer UI against FNOL Postgres on AWS.

Refuses local SQLite. Opens (or reuses) the SSM tunnel to RDS, signs in with
IAM as fnol_reviewer, then serves http://127.0.0.1:8090/

  AWS_PROFILE=atharv uv run --locked --group postgres python tools/reviewer_aws.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
RDS_HOST = os.environ.get(
    "FNOL_RDS_ENDPOINT",
    "fnol-postgres.cc92c6i402lj.us-east-1.rds.amazonaws.com",
)
TUNNEL_INSTANCE = os.environ.get("FNOL_DB_TUNNEL_INSTANCE", "i-0fe9143719292e677")
LOCAL_PORT = int(os.environ.get("FNOL_DB_TUNNEL_PORT", "15432"))
REGION = os.environ.get("AWS_REGION") or os.environ.get("FNOL_AWS_REGION") or "us-east-1"
PROFILE = os.environ.get("AWS_PROFILE", "atharv")
HTTP_HOST = os.environ.get("FNOL_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("FNOL_HTTP_PORT", "8090"))


def _die(message: str, code: int = 1) -> None:
    print(f"reviewer_aws: {message}", file=sys.stderr)
    raise SystemExit(code)


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("AWS_PROFILE", PROFILE)
    env.setdefault("AWS_REGION", REGION)
    return subprocess.run(argv, env=env, text=True, **kwargs)


def _port_open(port: int) -> bool:
    try:
        import socket

        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _ensure_sso() -> None:
    check = _run(
        ["aws", "sts", "get-caller-identity", "--profile", PROFILE],
        capture_output=True,
    )
    if check.returncode == 0:
        return
    print(f"reviewer_aws: SSO for profile {PROFILE!r} expired; logging in…", flush=True)
    login = _run(["aws", "sso", "login", "--profile", PROFILE])
    if login.returncode != 0:
        _die("aws sso login failed")
    check = _run(
        ["aws", "sts", "get-caller-identity", "--profile", PROFILE],
        capture_output=True,
    )
    if check.returncode != 0:
        _die(check.stderr.strip() or "sts get-caller-identity failed after login")


def _ensure_tunnel() -> None:
    if _port_open(LOCAL_PORT):
        print(f"reviewer_aws: tunnel already on 127.0.0.1:{LOCAL_PORT}", flush=True)
        return
    plugin = shutil.which("session-manager-plugin")
    if not plugin:
        plugin_path = Path("/usr/local/sessionmanagerplugin/bin/session-manager-plugin")
        if plugin_path.exists():
            os.environ["PATH"] = f"{plugin_path.parent}:{os.environ.get('PATH', '')}"
        else:
            _die("session-manager-plugin not found on PATH")
    print(f"reviewer_aws: starting SSM tunnel to {RDS_HOST}…", flush=True)
    params = (
        f'{{"host":["{RDS_HOST}"],"portNumber":["5432"],'
        f'"localPortNumber":["{LOCAL_PORT}"]}}'
    )
    proc = subprocess.Popen(
        [
            "aws",
            "ssm",
            "start-session",
            "--target",
            TUNNEL_INSTANCE,
            "--document-name",
            "AWS-StartPortForwardingSessionToRemoteHost",
            "--parameters",
            params,
            "--region",
            REGION,
            "--profile",
            PROFILE,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        if _port_open(LOCAL_PORT):
            print(f"reviewer_aws: tunnel ready (pid {proc.pid})", flush=True)
            return
        if proc.poll() is not None:
            _die("SSM tunnel process exited before the port opened")
        time.sleep(0.25)
    _die(f"timed out waiting for 127.0.0.1:{LOCAL_PORT}")


def _database_url() -> str:
    token = _run(
        [
            "aws",
            "rds",
            "generate-db-auth-token",
            "--hostname",
            RDS_HOST,
            "--port",
            "5432",
            "--region",
            REGION,
            "--username",
            "fnol_reviewer",
            "--profile",
            PROFILE,
        ],
        capture_output=True,
        check=False,
    )
    if token.returncode != 0:
        _die(token.stderr.strip() or "generate-db-auth-token failed")
    auth = token.stdout.strip()
    if not auth:
        _die("empty IAM DB auth token")
    return (
        f"postgresql://fnol_reviewer:{quote(auth, safe='')}"
        f"@127.0.0.1:{LOCAL_PORT}/fnol?sslmode=require"
    )


def _clear_local_overrides(env: dict[str, str]) -> None:
    for key in (
        "FNOL_MOCK_STATE",
        "FNOL_STATE_DIR",
        "FNOL_RDS_HOST",
        "FNOL_RDS_IAM_AUTH",
        "FNOL_RDS_USER",
        "FNOL_RDS_PASSWORD",
        "FNOL_DB_SECRET_ARN",
        "FNOL_ALLOW_LOCAL_STATE",
    ):
        env.pop(key, None)


def main() -> int:
    os.chdir(ROOT)
    _ensure_sso()
    _ensure_tunnel()
    url = _database_url()

    env = os.environ.copy()
    _clear_local_overrides(env)
    env["AWS_PROFILE"] = PROFILE
    env["AWS_REGION"] = REGION
    env["FNOL_DATABASE_URL"] = url
    env["FNOL_PG_NAMESPACE"] = "shared"
    env["FNOL_HTTP_HOST"] = HTTP_HOST
    env["FNOL_HTTP_PORT"] = str(HTTP_PORT)
    # Hard refuse SQLite if something clears the URL later in-process.
    env["FNOL_REVIEWER_AWS_ONLY"] = "1"

    print(
        f"reviewer_aws: serving AWS Postgres via tunnel on http://{HTTP_HOST}:{HTTP_PORT}/",
        flush=True,
    )
    return subprocess.call(
        [
            "uv",
            "run",
            "--locked",
            "--group",
            "postgres",
            "python",
            "server/reviewer.py",
            "--host",
            HTTP_HOST,
            "--port",
            str(HTTP_PORT),
        ],
        env=env,
        cwd=ROOT,
    )


if __name__ == "__main__":
    raise SystemExit(main())
