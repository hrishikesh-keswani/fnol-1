"""Sign a Bedrock Mantle request with the task role.

Gemma 4 is called on the Mantle chat route. SigV4 uses service name
``bedrock``. A bearer token is not added. Credentials come from the default
boto3 chain (ECS task role, or a local profile). Nothing here is logged.
"""

from __future__ import annotations

from fnol_shadow.errors import ShadowError


def sign_headers(
    url: str,
    method: str,
    body: bytes | None,
    region: str,
    headers: dict[str, str],
) -> dict[str, str]:
    """Return headers for ``url``, including the SigV4 Authorization header."""
    try:
        import botocore.session
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
    except ImportError as exc:
        raise ShadowError("aws_deps", "boto3 is required for IAM Bedrock auth") from exc
    credentials = botocore.session.Session().get_credentials()
    if credentials is None:
        raise ShadowError(
            "aws_credentials_missing",
            "no AWS credentials for Bedrock; set a task role or AWS_BEARER_TOKEN_BEDROCK",
        )
    request = AWSRequest(
        method=method,
        url=url,
        data=body if body is not None else b"",
        headers=dict(headers),
    )
    SigV4Auth(credentials.get_frozen_credentials(), "bedrock", region).add_auth(request)
    signed = {}
    for key, value in request.headers.items():
        if isinstance(value, str):
            signed[str(key)] = value
    return signed
