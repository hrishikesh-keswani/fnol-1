"""Read case documents and wording from one private S3 bucket.

Keys are ``{prefix}/{source_id}.md``. The prefix is the logical container
name (case-documents, policy-wording, fnol-fixtures). No write methods live
here: task roles that import this module are not granted PutObject.
"""

from __future__ import annotations

import os

from fnol_foundation.blobs import BlobError

_CLIENT = None


def region() -> str:
    value = os.environ.get("AWS_REGION") or os.environ.get("FNOL_AWS_REGION") or "us-east-1"
    return value.strip() or "us-east-1"


def _client():
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise BlobError("s3_deps", "boto3 is required when FNOL_S3_BUCKET is set") from exc
    _CLIENT = boto3.client(
        "s3",
        region_name=region(),
        config=Config(
            retries={"total_max_attempts": 3, "mode": "adaptive"},
            connect_timeout=5,
            read_timeout=20,
        ),
    )
    return _CLIENT


def get_object_bytes(bucket: str, key: str) -> bytes | None:
    """Return the object body, or None when the key does not exist."""
    from botocore.exceptions import ClientError

    client = _client()
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except client.exceptions.NoSuchKey:
        return None
    except ClientError as exc:
        raise BlobError("s3_read", f"failed to read s3://{bucket}/{key}") from exc
    body = response["Body"]
    try:
        data = body.read()
    finally:
        body.close()
    return data if isinstance(data, bytes) else None


def list_keys(bucket: str, prefix: str) -> list[str]:
    """List object keys under ``prefix/``. The prefix itself is not returned."""
    from botocore.exceptions import ClientError

    folder = prefix.strip("/") + "/"
    client = _client()
    keys: list[str] = []
    try:
        pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=folder)
        for page in pages:
            for item in page.get("Contents") or []:
                name = item.get("Key")
                if isinstance(name, str) and name.startswith(folder) and name != folder:
                    keys.append(name)
    except ClientError as exc:
        raise BlobError("s3_list", f"failed to list s3://{bucket}/{folder}") from exc
    return keys
