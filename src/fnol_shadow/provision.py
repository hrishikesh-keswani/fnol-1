"""Create a Bedrock guardrail and a managed prompt with the same bearer token.

Prompt Management cannot be the Gemma 4 model id. Converse is not available for
that model. GetPrompt loads the stored instruction text into the chat system
message. A 403 leaves the local prompt and the local safety check in place.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fnol_foundation.canonical import assert_no_restricted
from fnol_shadow.errors import ControlError
from fnol_shadow.http import Sleeper, request_json
from fnol_shadow.prompt_text import local_prompt_text

_CREATED = {200, 201, 202}
_SPEC_PATH = Path(__file__).with_name("guardrail_spec.json")
STATE_NAME = "bedrock_shadow.json"


def control_base_url(region: str) -> str:
    return f"https://bedrock.{region}.amazonaws.com"


def config_path(state_dir: Path) -> Path:
    return Path(state_dir) / STATE_NAME


def read_saved(state_dir: Path) -> dict[str, Any]:
    path = config_path(state_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def write_saved(state_dir: Path, data: dict[str, Any]) -> None:
    assert_no_restricted(data)
    path = config_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


class ControlClient:
    def __init__(
        self,
        *,
        api_key: str,
        region: str,
        base_url: str | None = None,
        sleeper: Sleeper | None = None,
        timeout: float = 30,
    ):
        self.api_key = api_key
        self.base_url = (base_url or control_base_url(region)).rstrip("/")
        self.sleeper = sleeper
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ControlError("api_key_missing", 0)
        status, _headers, payload = request_json(
            self.base_url + path,
            method=method,
            api_key=self.api_key,
            body=body,
            timeout=self.timeout,
            sleeper=self.sleeper,
        )
        return status, payload


def provision(state_dir: Path, client: ControlClient) -> dict[str, Any]:
    """Create the guardrail and the managed prompt. Permission failures are reported, not raised."""
    saved = read_saved(state_dir)
    result = {
        "guardrail_status": None,
        "prompt_status": None,
        "guardrail_id": _text(saved.get("guardrail_id")),
        "guardrail_version": _text(saved.get("guardrail_version")),
        "prompt_id": _text(saved.get("prompt_id")),
        "prompt_version": _text(saved.get("prompt_version")),
    }
    try:
        guardrail_id, guardrail_version = create_guardrail(client)
    except ControlError as exc:
        result["guardrail_status"] = exc.code
    else:
        result["guardrail_status"] = "created"
        result["guardrail_id"] = guardrail_id
        result["guardrail_version"] = guardrail_version
    try:
        prompt_id, prompt_version = create_prompt(client, local_prompt_text())
    except ControlError as exc:
        result["prompt_status"] = exc.code
    else:
        result["prompt_status"] = "created"
        result["prompt_id"] = prompt_id
        result["prompt_version"] = prompt_version
    write_saved(state_dir, {
        "guardrail_id": result["guardrail_id"],
        "guardrail_version": result["guardrail_version"],
        "prompt_id": result["prompt_id"],
        "prompt_version": result["prompt_version"],
    })
    return result


def create_guardrail(client: ControlClient) -> tuple[str, str]:
    spec = json.loads(_SPEC_PATH.read_text(encoding="utf-8"))
    spec["clientRequestToken"] = uuid.uuid4().hex
    status, body = client.request("POST", "/guardrails", spec)
    if status not in _CREATED:
        raise _failure("guardrail", status, body)
    guardrail_id = _text(body.get("guardrailId"))
    if guardrail_id is None:
        raise ControlError("guardrail_http", status)
    status, published = client.request(
        "POST",
        f"/guardrails/{quote(guardrail_id, safe='')}/version",
        {"description": "FNOL shadow summary", "clientRequestToken": uuid.uuid4().hex},
    )
    if status not in _CREATED:
        raise _failure("guardrail", status, published)
    version = _version(published.get("version"))
    if version is None:
        raise ControlError("guardrail_http", status)
    return guardrail_id, version


def create_prompt(client: ControlClient, text: str) -> tuple[str, str]:
    status, body = client.request("POST", "/prompts", _prompt_request(text, with_model=True))
    if _is_validation(status, body):
        status, body = client.request("POST", "/prompts", _prompt_request(text, with_model=False))
    if status not in _CREATED:
        raise _failure("prompt", status, body)
    prompt_id = _text(body.get("id")) or _text(body.get("promptIdentifier"))
    if prompt_id is None:
        raise ControlError("prompt_http", status)
    status, published = client.request(
        "POST",
        f"/prompts/{quote(prompt_id, safe='')}/versions",
        {"description": "FNOL shadow summary", "clientRequestToken": uuid.uuid4().hex},
    )
    if status not in _CREATED:
        raise _failure("prompt", status, published)
    version = _version(published.get("version"))
    if version is None:
        raise ControlError("prompt_http", status)
    return prompt_id, version


def fetch_prompt_text(client: ControlClient, prompt_id: str, prompt_version: str | None) -> str:
    path = "/prompts/" + quote(prompt_id, safe="")
    if prompt_version:
        path += "?" + urlencode({"promptVersion": prompt_version})
    status, body = client.request("GET", path)
    if status not in _CREATED:
        raise _failure("prompt", status, body)
    text = _prompt_text(body)
    if text is None:
        raise ControlError("prompt_empty", status)
    return text


def _prompt_request(text: str, *, with_model: bool) -> dict[str, Any]:
    variant: dict[str, Any] = {
        "name": "default",
        "templateType": "TEXT",
        "templateConfiguration": {"text": {"text": text}},
    }
    if with_model:
        variant["modelId"] = "google.gemma-4-31b"
        variant["inferenceConfiguration"] = {
            "text": {"temperature": 1, "topP": 0.95, "maxTokens": 1200}
        }
    return {
        "name": "fnol-shadow-summary",
        "description": "Handler-facing FNOL case summary. No claim decisions.",
        "defaultVariant": "default",
        "variants": [variant],
        "clientRequestToken": uuid.uuid4().hex,
    }


def _prompt_text(body: dict) -> str | None:
    variants = body.get("variants")
    if not isinstance(variants, list):
        return None
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        config = variant.get("templateConfiguration")
        if not isinstance(config, dict):
            continue
        text_block = config.get("text")
        if isinstance(text_block, dict) and isinstance(text_block.get("text"), str):
            text = text_block["text"].strip()
            if text:
                return text_block["text"]
        chat = config.get("chat")
        if isinstance(chat, dict):
            system = chat.get("system")
            if isinstance(system, str) and system.strip():
                return system
            if isinstance(system, list):
                parts = [
                    item.get("text")
                    for item in system
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                ]
                joined = "\n".join(parts).strip()
                if joined:
                    return "\n".join(parts)
    return None


def _failure(kind: str, status: int, body: dict) -> ControlError:
    marker = ""
    if isinstance(body, dict):
        marker = str(body.get("__type") or "") + str(body.get("code") or "")
    if status == 403 or "AccessDenied" in marker or "Forbidden" in marker:
        return ControlError(f"{kind}_forbidden", status)
    if status == 409 or "Conflict" in marker:
        return ControlError(f"{kind}_conflict", status)
    if status == 400 or "Validation" in marker:
        return ControlError(f"{kind}_rejected", status)
    return ControlError(f"{kind}_http", status)


def _is_validation(status: int, body: dict) -> bool:
    if status != 400:
        return False
    marker = str(body.get("__type") or "") + str(body.get("code") or "")
    return "Conflict" not in marker


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _version(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    return _text(value)
