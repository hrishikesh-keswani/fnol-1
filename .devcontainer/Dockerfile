# Reproducible teammate environment: Python 3.12 + uv.
# The default path is the standard library. Compose installs the postgres group.
FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

# Pin uv for lockfile-compatible installs across machines.
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /workspaces/auto-fnol-participant
