"""Shadow summary pipeline.

Reads stored decisions, asks Gemma 4 31B for a handler summary, and appends one
trace row. It does not build or replace the deterministic draft.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fnol_foundation.canonical import RestrictedValueError, assert_no_restricted, digest
from fnol_foundation.events import record_event
from fnol_foundation.stores import Stores, state_dir
from fnol_shadow.bedrock import DEFAULT_MODEL_ID, DEFAULT_REGION, ChatReply, MantleClient
from fnol_shadow.errors import ControlError, ShadowError
from fnol_shadow.packet import build_packet, packet_text
from fnol_shadow.prompt_text import local_prompt_text
from fnol_shadow.provision import ControlClient, fetch_prompt_text, read_saved
from fnol_shadow.safety import WITHHELD_NOTE, assess, blocked_output_message

ACTOR = "shadow"
STEP = "6"
EVENT = "shadow_summary"
RULE_VERSION = "AUTO-TRIAGE-01"

REWRITE = (
    "\n\nRewrite the summary as prose for the human handler. "
    "Do not decide coverage, liability, fraud, reserves, settlement, denial, or payment. "
    "Do not include restricted tokens. Do not return JSON."
)


@dataclass(frozen=True)
class ShadowSettings:
    region: str
    model_id: str
    api_key: str
    guardrail_id: str | None
    guardrail_version: str | None
    prompt_id: str | None
    prompt_version: str | None
    mantle_base_url: str | None
    control_base_url: str | None


def load_settings(directory: str | Path | None = None) -> ShadowSettings:
    saved = read_saved(Path(directory) if directory is not None else state_dir())
    return ShadowSettings(
        region=os.environ.get("AWS_REGION") or DEFAULT_REGION,
        model_id=os.environ.get("BEDROCK_MODEL_ID") or DEFAULT_MODEL_ID,
        api_key=os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or "",
        guardrail_id=_pick("BEDROCK_GUARDRAIL_ID", saved.get("guardrail_id")),
        guardrail_version=_pick("BEDROCK_GUARDRAIL_VERSION", saved.get("guardrail_version")),
        prompt_id=_pick("BEDROCK_PROMPT_ID", saved.get("prompt_id")),
        prompt_version=_pick("BEDROCK_PROMPT_VERSION", saved.get("prompt_version")),
        mantle_base_url=_env_text("BEDROCK_MANTLE_BASE_URL"),
        control_base_url=_env_text("BEDROCK_CONTROL_BASE_URL"),
    )


def summarize_case(
    stores: Stores,
    case_id: str,
    client: MantleClient,
    *,
    system_prompt: str | None = None,
    prompt_client: ControlClient | None = None,
    settings: ShadowSettings | None = None,
) -> dict[str, Any]:
    """One case. Requires the stored policy, route, and citations decisions."""
    revision, decisions = _load_case(stores, case_id)
    packet = build_packet(
        case_id=case_id,
        input_revision=revision.revision,
        facts_row=revision.payload,
        policy=decisions["policy"],
        route=decisions["route"],
        citations=decisions["citations"],
    )
    trace_id = revision.trace_id or uuid.uuid4().hex
    try:
        assert_no_restricted(packet)
    except RestrictedValueError:
        _record(
            stores,
            trace_id=trace_id,
            case_id=case_id,
            input_revision=revision.revision,
            model_id=client.model_id,
            status="error",
            error_code="restricted_value",
            safety="blocked",
            narrative=WITHHELD_NOTE,
            input_tokens=0,
            output_tokens=0,
            started=time.perf_counter(),
        )
        raise ShadowError("restricted_value", "case packet refused: restricted value") from None

    started = time.perf_counter()
    try:
        system = system_prompt or resolve_system_prompt(settings, prompt_client)
        safety, narrative, input_tokens, output_tokens = _generate(client, system, packet_text(packet))
    except ShadowError as exc:
        _record(
            stores,
            trace_id=trace_id,
            case_id=case_id,
            input_revision=revision.revision,
            model_id=client.model_id,
            status="error",
            error_code=exc.error_code,
            safety="error",
            narrative=None,
            input_tokens=0,
            output_tokens=0,
            started=started,
        )
        raise
    _record(
        stores,
        trace_id=trace_id,
        case_id=case_id,
        input_revision=revision.revision,
        model_id=client.model_id,
        status="ok" if safety == "passed" else "error",
        error_code=None if safety == "passed" else safety,
        safety=safety,
        narrative=narrative,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        started=started,
    )
    return {
        "case_id": case_id,
        "input_revision": revision.revision,
        "model_id": client.model_id,
        "shadow_safety_status": safety,
        "summary": narrative,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def summarize_cases(
    stores: Stores,
    case_ids: list[str],
    client: MantleClient,
    *,
    system_prompt: str | None = None,
    prompt_client: ControlClient | None = None,
    settings: ShadowSettings | None = None,
) -> list[dict[str, Any]]:
    return [
        summarize_case(
            stores,
            case_id,
            client,
            system_prompt=system_prompt,
            prompt_client=prompt_client,
            settings=settings,
        )
        for case_id in case_ids
    ]


def write_summaries(path: str | Path, rows: list[dict[str, Any]]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    assert_no_restricted(rows)
    target.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def resolve_system_prompt(
    settings: ShadowSettings | None,
    prompt_client: ControlClient | None,
) -> str:
    local = local_prompt_text()
    if settings is None or not settings.prompt_id or prompt_client is None:
        return local
    try:
        text = fetch_prompt_text(prompt_client, settings.prompt_id, settings.prompt_version)
    except ControlError:
        return local
    return text or local


def case_ids(stores: Stores) -> list[str]:
    return sorted(row["case_id"] for row in stores.input.latest_loss_keys() if row.get("case_id"))


def _generate(client: MantleClient, system: str, user: str) -> tuple[str, str, int, int]:
    warning = ""
    total_in = 0
    total_out = 0
    status = "blocked"
    canned = blocked_output_message()
    for _attempt in range(2):
        reply = client.chat(system, user + warning)
        total_in += reply.input_tokens
        total_out += reply.output_tokens
        verdict = _verdict(reply, canned)
        if verdict == "passed":
            return "passed", reply.text.strip(), total_in, total_out
        status = verdict
        warning = REWRITE
    return status, WITHHELD_NOTE, total_in, total_out


def _verdict(reply: ChatReply, canned: str) -> str:
    text = reply.text.strip()
    if reply.guardrail_action == "INTERVENED" or text == canned:
        return "guardrail_intervened"
    return assess(reply.text)


def _load_case(stores: Stores, case_id: str):
    revision = stores.input.get_revision(case_id)
    if revision is None:
        raise ShadowError("revision_missing", f"no input revision for {case_id}")
    decisions = {}
    for kind in ("policy", "route", "citations"):
        row = stores.input.get_decision(case_id, revision.revision, kind)
        if row is None:
            raise ShadowError(f"{kind}_decision_missing", f"no {kind} decision for {case_id}")
        decisions[kind] = row["body"]
    return revision, decisions


def _record(
    stores,
    *,
    trace_id,
    case_id,
    input_revision,
    model_id,
    status,
    error_code,
    safety,
    narrative,
    input_tokens,
    output_tokens,
    started,
):
    if narrative is not None:
        assert_no_restricted(narrative)
    duration_ms = int((time.perf_counter() - started) * 1000)
    record_event(
        stores,
        trace_id=trace_id,
        step=STEP,
        event=EVENT,
        actor=ACTOR,
        rule_version=RULE_VERSION,
        status=status,
        case_id=case_id,
        input_revision=input_revision,
        error_code=error_code,
        duration_ms=duration_ms,
        output_sha256=None if narrative is None else digest(narrative),
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        shadow_narrative=narrative,
        shadow_safety_status=safety,
    )


def _pick(env_name: str, saved: Any) -> str | None:
    chosen = _env_text(env_name)
    if chosen:
        return chosen
    return saved if isinstance(saved, str) and saved.strip() else None


def _env_text(name: str) -> str | None:
    value = os.environ.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
