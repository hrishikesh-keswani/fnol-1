"""Gemma 4 31B on the Bedrock Mantle OpenAI chat route.

Gemma 4 is not served by Converse. Sampling follows the AWS recommendation
for this model: temperature 1.0 and top_p 0.95, so the wording is not a template.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable

from fnol_shadow.errors import ShadowError
from fnol_shadow.http import Sleeper, request_json

DEFAULT_MODEL_ID = "google.gemma-4-31b"
DEFAULT_REGION = "us-east-1"
_REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d+")


@dataclass(frozen=True)
class ChatReply:
    text: str
    input_tokens: int
    output_tokens: int
    guardrail_action: str | None


def mantle_chat_url(region: str) -> str:
    if not isinstance(region, str) or _REGION.fullmatch(region) is None:
        raise ShadowError("bad_region", "AWS region is not usable")
    return f"https://bedrock-mantle.{region}.api.aws/openai/v1/chat/completions"


class MantleClient:
    def __init__(
        self,
        *,
        api_key: str,
        region: str = DEFAULT_REGION,
        model_id: str = DEFAULT_MODEL_ID,
        guardrail_id: str | None = None,
        guardrail_version: str | None = None,
        base_url: str | None = None,
        sleeper: Sleeper | None = None,
        timeout: float = 60,
    ):
        self.api_key = api_key
        self.region = region
        self.model_id = model_id
        self.guardrail_id = guardrail_id or None
        self.guardrail_version = guardrail_version or None
        self.sleeper = sleeper
        self.timeout = timeout
        if base_url:
            self.chat_url = base_url.rstrip("/") + "/chat/completions"
        else:
            self.chat_url = mantle_chat_url(region)
        if bool(self.guardrail_id) != bool(self.guardrail_version):
            raise ShadowError("guardrail_config", "guardrail id and version are both required")

    def chat(self, system: str, user: str) -> ChatReply:
        body = {
            "model": self.model_id,
            "temperature": 1.0,
            "top_p": 0.95,
            "max_tokens": 1200,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self._use_iam():
            status, headers, payload = request_json(
                self.chat_url,
                method="POST",
                api_key="",
                body=body,
                timeout=self.timeout,
                sleeper=self.sleeper,
                signer=self._iam_signer(),
            )
        else:
            self._require_key()
            status, headers, payload = request_json(
                self.chat_url,
                method="POST",
                api_key=self.api_key,
                body=body,
                extra_headers=self._guardrail_headers(),
                timeout=self.timeout,
                sleeper=self.sleeper,
            )
        if status != 200:
            raise ShadowError("bedrock_http", f"bedrock returned {status}")
        text, finish = _message_text(payload)
        return ChatReply(
            text=text,
            input_tokens=_tokens(payload, "prompt_tokens", "input_tokens"),
            output_tokens=_tokens(payload, "completion_tokens", "output_tokens"),
            guardrail_action=_guardrail_action(headers, finish),
        )

    def _use_iam(self) -> bool:
        if os.environ.get("FNOL_BEDROCK_AUTH", "").strip().lower() == "iam":
            return True
        return not isinstance(self.api_key, str) or not self.api_key.strip()

    def _iam_signer(self):
        from fnol_shadow.aws_auth import sign_headers

        extra = self._guardrail_headers() or {}

        def signer(data: bytes | None) -> dict[str, str]:
            headers = {"Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            headers.update(extra)
            return sign_headers(self.chat_url, "POST", data, self.region, headers)

        return signer

    def _require_key(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ShadowError("api_key_missing", "AWS_BEARER_TOKEN_BEDROCK is required")

    def _guardrail_headers(self) -> dict[str, str] | None:
        """Identifier and version only. Trace is left off so the filter text is not returned."""
        if not self.guardrail_id:
            return None
        return {
            "X-Amzn-Bedrock-GuardrailIdentifier": self.guardrail_id,
            "X-Amzn-Bedrock-GuardrailVersion": self.guardrail_version or "",
        }


def _message_text(payload: dict) -> tuple[str, str | None]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ShadowError("bedrock_response", "bedrock response had no message")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ShadowError("bedrock_response", "bedrock response had no message")
    finish = choice.get("finish_reason")
    finish_text = finish if isinstance(finish, str) else None
    return _content_text(message.get("content")), finish_text


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("type") != "reasoning":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _tokens(payload: dict, *names: str) -> int:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0
    for name in names:
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def _guardrail_action(headers: dict[str, str], finish: str | None) -> str | None:
    if isinstance(finish, str) and finish.lower() in {"content_filter", "guardrail_intervened"}:
        return "INTERVENED"
    for key, value in headers.items():
        if key.lower() in {"x-amzn-bedrock-guardrailaction", "x-amzn-bedrock-guardrail-action"}:
            if isinstance(value, str) and value.strip():
                return value.strip().upper()
    return None
