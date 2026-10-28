"""Read case documents and policy wording from disk or S3.

Disk is the default. Set FNOL_S3_BUCKET to read from S3.
Prefixes default to case-documents and policy-wording; override with
FNOL_DOCUMENTS_CONTAINER and FNOL_POLICIES_CONTAINER.
"""

from __future__ import annotations

import os
from pathlib import Path


class BlobError(RuntimeError):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def s3_enabled() -> bool:
    return object_store() == "s3"


def s3_bucket() -> str:
    return os.environ.get("FNOL_S3_BUCKET", "").strip()


def object_store() -> str:
    """Which file store is active: s3 or disk."""
    if os.environ.get("FNOL_S3_BUCKET", "").strip():
        return "s3"
    return "disk"


def documents_container() -> str:
    return os.environ.get("FNOL_DOCUMENTS_CONTAINER", "case-documents").strip() or "case-documents"


def policies_container() -> str:
    return os.environ.get("FNOL_POLICIES_CONTAINER", "policy-wording").strip() or "policy-wording"


def fixtures_container() -> str:
    return os.environ.get("FNOL_FIXTURES_CONTAINER", "fnol-fixtures").strip() or "fnol-fixtures"


def container_for(directory: str | Path) -> str:
    """Map a local directory path to the S3 prefix."""
    name = Path(directory).name.lower()
    if name in {"documents", "case-documents"}:
        return documents_container()
    if name in {"policies", "policy-wording"}:
        return policies_container()
    if name in {"dev", "fixtures", "fnol-fixtures"}:
        return fixtures_container()
    return name


def read_source(directory: str | Path, source_id: str):
    """Return (path_or_uri, bytes, text) for ``{source_id}.md``, or None.

    Same contract as ``fnol_intake.parse.read_source``. Refuses path traversal.
    """
    if not isinstance(source_id, str) or not source_id:
        return None
    if any(part in source_id for part in ("/", "\\", "..")):
        return None
    blob_name = f"{source_id}.md"
    if object_store() == "s3":
        return _read_s3(container_for(directory), blob_name)
    root = Path(directory).resolve()
    path = (root / blob_name).resolve()
    if path.parent != root or not path.is_file():
        return None
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return path, data, text


def list_markdown_sources(directory: str | Path) -> list[dict[str, str]]:
    """Return ``[{source_id, sha256}, ...]`` for every ``*.md`` in the store."""
    from fnol_intake.parse import file_sha256

    if object_store() == "s3":
        return _list_s3_markdown(container_for(directory))
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"policies directory does not exist: {directory}")
    sources = []
    for path in sorted(root.glob("*.md")):
        sources.append({
            "source_id": path.stem,
            "sha256": file_sha256(path.read_bytes()),
        })
    return sources


def _read_s3(prefix: str, name: str):
    from fnol_foundation.s3store import get_object_bytes

    bucket = s3_bucket()
    key = f"{prefix}/{name}"
    try:
        data = get_object_bytes(bucket, key)
    except BlobError:
        raise
    if data is None:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return f"s3://{bucket}/{key}", data, text


def _list_s3_markdown(prefix: str) -> list[dict[str, str]]:
    from fnol_foundation.s3store import get_object_bytes, list_keys
    from fnol_intake.parse import file_sha256

    bucket = s3_bucket()
    items: list[dict[str, str]] = []
    for key in list_keys(bucket, prefix):
        name = key.rsplit("/", 1)[-1]
        if not name.endswith(".md") or "/" in name:
            continue
        data = get_object_bytes(bucket, key)
        if data is None:
            continue
        items.append({"source_id": Path(name).stem, "sha256": file_sha256(data)})
    return items
