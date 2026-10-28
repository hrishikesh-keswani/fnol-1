"""Upload the local dev corpus into the foundation bucket.

Uses the caller's AWS credentials. Task roles are not allowed to write.
Keys match the readers: {prefix}/{source_id}.md and fnol-fixtures/dataset.json.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _upload(client, bucket: str, documents: Path, policies: Path, fixture: Path) -> None:
    extra = {"ServerSideEncryption": "aws:kms"}
    key_id = os.environ.get("FNOL_S3_KMS_KEY_ID", "").strip()
    if key_id:
        extra["SSEKMSKeyId"] = key_id
    for path in sorted(documents.glob("*.md")):
        key = f"case-documents/{path.name}"
        client.upload_file(str(path), bucket, key, ExtraArgs=extra)
        print(key)
    for path in sorted(policies.glob("*.md")):
        key = f"policy-wording/{path.name}"
        client.upload_file(str(path), bucket, key, ExtraArgs=extra)
        print(key)
    key = "fnol-fixtures/dataset.json"
    client.upload_file(str(fixture), bucket, key, ExtraArgs=extra)
    print(key)


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload FNOL dev files to S3.")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--documents", type=Path, default=ROOT / "data" / "dev" / "documents")
    parser.add_argument("--policies", type=Path, default=ROOT / "data" / "dev" / "policies")
    parser.add_argument("--fixture", type=Path, default=ROOT / "data" / "dev" / "dataset.json")
    args = parser.parse_args()
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        print("Error: boto3 is required", file=sys.stderr)
        return 1
    region_name = os.environ.get("AWS_REGION") or os.environ.get("FNOL_AWS_REGION") or "us-east-1"
    try:
        client = boto3.client("s3", region_name=region_name)
        _upload(client, args.bucket, args.documents, args.policies, args.fixture)
    except ClientError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
