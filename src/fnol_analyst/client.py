"""AnalystClient: drive the step servers, build the draft, and submit for review.

Steps 1-4 are the platform servers. Step 5 copies their answers into the
deterministic narrative. Step 8 hashes that package. When FNOL_AI_ENABLED=1,
steps 6-7 call Gemma and append the shadow narrative and its safety verdict
to the trace, keyed by draft_id. That text is not copied into the draft.
Steps 9-12 live in submit.py and never call approve.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from fnol_analyst.draft import FACT_COLUMNS, build_draft
from fnol_analyst.http import default_transport
from fnol_analyst.queue import queue_action
from fnol_analyst.servers import ServerSet
from fnol_analyst.submit import (
    SubmitError,
    edit_draft,
    file_record,
    poll_review,
    submit_review,
)
from fnol_foundation.canonical import RestrictedValueError, assert_no_restricted, digest
from fnol_foundation.events import record_event
from fnol_foundation.stores import DraftRecord, Stores, state_dir
from fnol_shadow.errors import ShadowError
from fnol_shadow.packet import STRUCTURED_FACTS, build_packet, packet_text
from fnol_shadow.pipeline import _generate, load_settings, resolve_system_prompt
from fnol_shadow.provision import ControlClient

ACTOR = "analyst"

Transport = Callable[[str, str, dict | None, dict[str, str] | None], tuple[int, dict[str, Any]]]
ShadowChat = Callable[[str, str], Any]


class AnalystError(Exception):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def ai_enabled() -> bool:
    return os.environ.get("FNOL_AI_ENABLED", "").strip() == "1"


class AnalystClient:
    def __init__(
        self,
        *,
        mock_url: str | None = None,
        state_dir_path: str | Path | None = None,
        documents_dir: str | Path | None = None,
        policies_dir: str | Path | None = None,
        transport: Transport | None = None,
        shadow_chat: ShadowChat | None = None,
        shadow_model_id: str | None = None,
    ):
        self.mock_url = (mock_url or os.environ.get("FNOL_MOCK_URL", "http://127.0.0.1:8767")).rstrip("/")
        self.state_dir = Path(state_dir_path) if state_dir_path is not None else state_dir()
        self.documents_dir = Path(documents_dir) if documents_dir else None
        self.policies_dir = Path(policies_dir) if policies_dir else None
        self.stores = Stores(self.state_dir)
        self._servers: ServerSet | None = None
        self._transport = transport
        self._shadow_chat = shadow_chat
        self._shadow_model_id = shadow_model_id or "test-model"

    @property
    def servers(self) -> ServerSet:
        if self._servers is None:
            self._servers = ServerSet(
                state_dir=self.state_dir,
                mock_url=self.mock_url,
                documents_dir=self.documents_dir,
                policies_dir=self.policies_dir,
            )
        return self._servers

    def close(self) -> None:
        if self._servers is not None:
            self._servers.close()
            self._servers = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def case_ids(self) -> list[str]:
        return [row["case_id"] for row in self.stores.input.latest_loss_keys() if row.get("case_id")]

    def ingest(self) -> dict[str, Any]:
        return self.servers.ingest()

    def select_policy(self, case_id: str | None = None) -> dict[str, Any]:
        return self.servers.select_policy(case_id)

    def route(self, case_id: str | None = None) -> dict[str, Any]:
        return self.servers.route(case_id)

    def cite(self, case_id: str | None = None) -> dict[str, Any]:
        return self.servers.cite(case_id)

    def prepare(self, case_ids: list[str] | None = None, *, workers: int = 3) -> dict[str, Any]:
        """Steps 1-4. Intake once, then policy -> rules -> citations per case, pipelined.

        If any case fails after intake, PrepareError lists every failure. Decisions
        already written for successful cases remain on disk.
        """
        ingest = self.ingest()
        ids = case_ids or [row["case_id"] for row in ingest.get("revisions", [])]
        cases = self.servers.prepare_all(ids, workers=workers)
        return {"ingest": ingest, "cases": cases}

    def build(self, case_id: str) -> DraftRecord:
        """Steps 5-8. Reuses a recorded draft for the same input revision."""
        revision = self.stores.input.get_revision(case_id)
        if revision is None:
            raise AnalystError("revision_missing", f"no input revision for {case_id}")
        existing = self.stores.output.latest_draft(case_id)
        if existing is not None and existing.input_revision == revision.revision:
            return existing
        decisions = {}
        for kind in ("policy", "route", "citations"):
            row = self.stores.input.get_decision(case_id, revision.revision, kind)
            if row is None:
                raise AnalystError(
                    f"{kind}_decision_missing",
                    f"no {kind} decision for {case_id}",
                )
            decisions[kind] = row["body"]
        trace_id = revision.trace_id or uuid.uuid4().hex
        started = time.perf_counter()
        try:
            draft = build_draft(
                case_id=case_id,
                input_revision=revision.revision,
                facts_row={key: revision.payload.get(key) for key in FACT_COLUMNS},
                policy=decisions["policy"],
                route=decisions["route"],
                citations=decisions["citations"],
                evidence_summary=None,
                safety_status="skipped",
            )
        except RestrictedValueError:
            self._event(
                trace_id, "5", "draft_built", case_id, revision.revision, None,
                status="error", error_code="restricted_value", started=started,
            )
            raise AnalystError("restricted_value", "draft refused: restricted value") from None
        self._event(
            trace_id, "5", "draft_built", case_id, revision.revision, None,
            status="ok", output_sha256=digest(draft), started=started,
        )
        started = time.perf_counter()
        record = self.stores.output.record_draft(case_id, draft, input_revision=revision.revision)
        self._event(
            trace_id, "8", "draft_recorded", case_id, revision.revision, record.draft_id,
            status="ok", output_sha256=record.draft_hash, started=started,
        )
        if ai_enabled():
            self._record_shadow(
                case_id=case_id,
                revision=revision,
                decisions=decisions,
                trace_id=trace_id,
                draft_id=record.draft_id,
            )
        return record

    def build_all(self, case_ids: list[str] | None = None) -> list[DraftRecord]:
        return [self.build(case_id) for case_id in (case_ids or self.case_ids())]

    def enqueue_case(self, case_id: str) -> dict[str, Any]:
        """Run policy, rules, citations, the draft, Gemma, and POST /v1/reviews.

        Intake has already stored the case. A revision whose documents were
        already sent for review is left alone. This does not approve or file.
        """
        revision = self.stores.input.get_revision(case_id)
        draft = self.stores.output.latest_draft(case_id) if revision is not None else None
        prior = None
        if (
            revision is not None
            and draft is not None
            and draft.input_revision not in (None, revision.revision)
        ):
            prior = self.stores.input.get_revision(case_id, draft.input_revision)
        action = queue_action(revision, draft, prior)
        if action == "skip":
            return {
                "case_id": case_id,
                "action": "skipped",
                "revision": None if revision is None else revision.revision,
            }
        if action == "process":
            self.servers.prepare_case(case_id)
            self.build(case_id)
        record = self.submit(case_id)
        return {
            "case_id": case_id,
            "action": "submitted" if action == "submit" else "queued",
            "revision": None if revision is None else revision.revision,
            "draft_id": record.draft_id,
            "review_id": record.review_id,
            "status": record.status,
        }

    def enqueue_pending(self) -> list[dict[str, Any]]:
        """Queue every ingested case that does not already have a review.

        One failure is recorded and the remaining cases still run.
        """
        results = []
        for case_id in self.case_ids():
            try:
                results.append(self.enqueue_case(case_id))
            except Exception as exc:
                code = getattr(exc, "error_code", None) or exc.__class__.__name__
                results.append({"case_id": case_id, "action": "error", "error": code})
        return results

    def export(self, path: str | Path, case_ids: list[str] | None = None) -> Path:
        """Write the contract JSON array from drafts already on disk.

        Does not build. Run ``build`` or ``prepare`` first.
        """
        rows = []
        for case_id in case_ids or self.case_ids():
            record = self.stores.output.latest_draft(case_id)
            if record is None:
                raise AnalystError("draft_missing", f"no draft for {case_id}; run build first")
            rows.append(record.draft)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
        return target

    def submit(self, case_id: str) -> DraftRecord:
        """Step 9. POST /v1/reviews for the stored draft."""
        try:
            return submit_review(
                self.stores,
                case_id=case_id,
                base_url=self.mock_url,
                transport=self._call_transport,
                trace_id=self._trace_id(case_id),
            )
        except SubmitError as exc:
            raise AnalystError(exc.error_code, str(exc)) from None

    def review_status(self, case_id: str) -> dict[str, Any]:
        """Step 10. Poll the human decision. Never calls approve."""
        try:
            return poll_review(
                self.stores,
                case_id=case_id,
                base_url=self.mock_url,
                transport=self._call_transport,
                trace_id=self._trace_id(case_id),
            )
        except SubmitError as exc:
            raise AnalystError(exc.error_code, str(exc)) from None

    def edit(
        self,
        case_id: str,
        *,
        narrative: str | None = None,
        evidence_summary: str | None = None,
        clear_evidence_summary: bool = False,
    ) -> DraftRecord:
        """Local edit: new draft_id and hash. Old approval is void."""
        try:
            return edit_draft(
                self.stores,
                case_id=case_id,
                narrative=narrative,
                evidence_summary=evidence_summary,
                clear_evidence_summary=clear_evidence_summary,
                trace_id=self._trace_id(case_id),
            )
        except SubmitError as exc:
            raise AnalystError(exc.error_code, str(exc)) from None

    def file(self, case_id: str) -> dict[str, Any]:
        """Steps 11-12. Duplicate check then POST /v1/records."""
        try:
            return file_record(
                self.stores,
                case_id=case_id,
                base_url=self.mock_url,
                transport=self._call_transport,
                trace_id=self._trace_id(case_id),
            )
        except SubmitError as exc:
            raise AnalystError(exc.error_code, str(exc)) from None

    def _record_shadow(
        self,
        *,
        case_id: str,
        revision,
        decisions: dict[str, Any],
        trace_id: str,
        draft_id: str,
    ) -> None:
        """Steps 6-7. The narrative is stored on the trace and not on the draft."""
        started = time.perf_counter()
        model_id = None
        input_tokens = 0
        output_tokens = 0
        candidate_hash = None
        safety = "fallback"
        stored_narrative = None
        step6_status = "error"
        step6_error = None
        try:
            packet = build_packet(
                case_id=case_id,
                input_revision=revision.revision,
                facts_row={key: revision.payload.get(key) for key in STRUCTURED_FACTS},
                policy=decisions["policy"],
                route=decisions["route"],
                citations=decisions["citations"],
            )
            assert_no_restricted(packet)
            client = self._mantle_client()
            model_id = getattr(client, "model_id", self._shadow_model_id)
            settings = load_settings(self.state_dir)
            prompt_client = None
            if settings.api_key and settings.prompt_id and not _iam_bedrock():
                prompt_client = ControlClient(
                    api_key=settings.api_key,
                    region=settings.region,
                    base_url=settings.control_base_url,
                )
            system = resolve_system_prompt(settings, prompt_client)
            verdict, narrative, input_tokens, output_tokens = _generate(
                client, system, packet_text(packet)
            )
            if verdict == "passed":
                assert_no_restricted(narrative)
                stored_narrative = narrative
                candidate_hash = digest(narrative)
                safety = "passed"
                step6_status = "ok"
            else:
                step6_error = verdict
                safety = "fallback"
                step6_status = "error"
        except RestrictedValueError:
            step6_error = "restricted_value"
            safety = "fallback"
            stored_narrative = None
            candidate_hash = None
        except ShadowError as exc:
            step6_error = exc.error_code
            safety = "fallback"
        except Exception:
            step6_error = "model_error"
            safety = "fallback"

        self._event(
            trace_id,
            "6",
            "shadow_summary",
            case_id,
            revision.revision,
            draft_id,
            status=step6_status,
            error_code=step6_error,
            started=started,
            output_sha256=candidate_hash,
            model_id=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        self._event(
            trace_id,
            "7",
            "safety_scan",
            case_id,
            revision.revision,
            draft_id,
            status="ok" if safety == "passed" else "error",
            error_code=None if safety == "passed" else safety,
            started=started,
            model_id=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            shadow_safety_status=safety,
            shadow_narrative=stored_narrative,
        )

    def _mantle_client(self):
        if self._shadow_chat is not None:
            return _StubMantle(self._shadow_chat, self._shadow_model_id)
        settings = load_settings(self.state_dir)
        use_iam = _iam_bedrock()
        if not settings.api_key and not use_iam:
            raise ShadowError("api_key_missing", "AWS_BEARER_TOKEN_BEDROCK is required")
        from fnol_shadow.bedrock import MantleClient

        return MantleClient(
            api_key="" if use_iam else settings.api_key,
            region=settings.region,
            model_id=settings.model_id,
            guardrail_id=settings.guardrail_id,
            guardrail_version=settings.guardrail_version,
            base_url=settings.mantle_base_url,
        )

    def _call_transport(
        self,
        method: str,
        url: str,
        body: dict | None,
        headers: dict[str, str] | None,
    ) -> tuple[int, dict[str, Any]]:
        if self._transport is not None:
            path = url
            if url.startswith(self.mock_url):
                path = url[len(self.mock_url) :] or "/"
            return self._transport(method, path, body, headers)
        return default_transport(method, url, body, headers)

    def _trace_id(self, case_id: str) -> str | None:
        revision = self.stores.input.get_revision(case_id)
        return revision.trace_id if revision is not None else None

    def _event(
        self,
        trace_id,
        step,
        event,
        case_id,
        input_revision,
        draft_id,
        *,
        status,
        error_code=None,
        duration_ms=None,
        output_sha256=None,
        started=None,
        model_id=None,
        input_tokens=None,
        output_tokens=None,
        shadow_safety_status=None,
        shadow_narrative=None,
        mock_http_status=None,
    ):
        if duration_ms is None and started is not None:
            duration_ms = int((time.perf_counter() - started) * 1000)
        record_event(
            self.stores,
            trace_id=trace_id,
            step=step,
            event=event,
            actor=ACTOR,
            status=status,
            case_id=case_id,
            input_revision=input_revision,
            draft_id=draft_id,
            error_code=error_code,
            duration_ms=duration_ms,
            output_sha256=output_sha256,
            model_id=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            shadow_safety_status=shadow_safety_status,
            shadow_narrative=shadow_narrative,
            mock_http_status=mock_http_status,
        )


def _iam_bedrock() -> bool:
    return os.environ.get("FNOL_BEDROCK_AUTH", "").strip().lower() == "iam"


class _StubMantle:
    def __init__(self, chat: ShadowChat, model_id: str):
        self._chat = chat
        self.model_id = model_id

    def chat(self, system: str, user: str):
        return self._chat(system, user)
