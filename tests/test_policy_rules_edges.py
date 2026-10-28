"""Edge branches for policy selection/pipeline and the rules screen, claims, and pipeline."""

from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from fake_model import FakeModel
from fnol_foundation.http import HttpAttempt
from fnol_foundation.stores import Stores
from fnol_intake.injection import CLEAN, ERROR, INJECTION
from fnol_policy.pipeline import PolicyError, start_policy
from fnol_policy.select import parse_utc, select_policy
from fnol_policy.service import run_policy
from fnol_rules import claims, screen
from fnol_rules.adapter import fields_from_payload
from fnol_rules.claims import ClaimsAttempt, ClaimsFetchResult
from fnol_rules.pipeline import RulesError, start_routing
from fnol_rules.service import policy_from_decision, run_rules
from fnol_rules.types import ClaimsSnapshot, PolicyResult

import importlib

route_mod = importlib.import_module("fnol_rules.route")

GOOD = {
    "version": "1",
    "effective_from": "2026-01-01T00:00:00Z",
    "effective_to": "2027-01-01T00:00:00Z",
    "wording_source": "AUTO-WORDING-V1",
    "endorsements": [],
}


# ---- policy select -----------------------------------------------------------------------


def test_parse_utc_variants():
    assert parse_utc(None) is None
    assert parse_utc("") is None
    assert parse_utc("garbage") is None
    assert parse_utc("2026-01-01T00:00:00") is None  # no timezone
    assert parse_utc("2026-01-01T02:00:00+02:00").hour == 0
    assert parse_utc("2026-01-01T00:00:00Z").hour == 0


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"source_id": "S"}, "malformed"),
        ({"policies": ["junk"]}, "malformed"),
        ({"policies": [{"effective_from": "bad", "effective_to": "bad"}]}, "malformed"),
        ({"policies": [dict(GOOD, effective_to=None)]}, "malformed"),
        ({"policies": [dict(GOOD, version="")]}, "malformed"),
        ({"policies": [dict(GOOD, wording_source=5)]}, "malformed"),
        ({"policies": [dict(GOOD, endorsements="x")]}, "malformed"),
        ({"policies": [dict(GOOD, endorsements=[1])]}, "malformed"),
    ],
)
def test_malformed_payloads(payload, reason):
    body = select_policy("2026-06-01T00:00:00Z", payload)
    assert body["reason"] == reason
    assert body["policy_available"] is False
    assert body["selected_policy_version"] is None
    assert body["policy_ids"] == ["AUTO-TRIAGE-01"]


def test_unparsed_timestamp_is_available_but_unselected():
    body = select_policy("not a time", {"policies": [GOOD]})
    assert (body["reason"], body["policy_available"]) == ("unparsed", True)


def test_unknown_wording_is_listed_as_missing_and_not_cited():
    body = select_policy("2026-06-01T00:00:00Z", {"policies": [dict(GOOD, endorsements=["E1"])]}, {"E1"})
    assert body["reason"] == "wording_missing"
    assert body["missing_sources"] == ["AUTO-WORDING-V1"]
    assert body["policy_ids"] == ["AUTO-TRIAGE-01"]
    ok = select_policy(
        "2026-06-01T00:00:00Z",
        {"source_id": "S", "policy_number": "", "policies": [dict(GOOD, wording_source=None)]},
        set(),
    )
    assert ok["reason"] == "matched"
    assert ok["policy_number"] is None
    assert ok["policy_ids"] == ["AUTO-TRIAGE-01"]


# ---- policy pipeline / service ----------------------------------------------------------


def test_policy_pipeline_errors():
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(PolicyError) as caught:
            start_policy(mock_url="http://mock", state_dir_path=tmp)
        assert caught.value.error_code == "no_cases"
        with pytest.raises(PolicyError) as caught:
            start_policy("AUTO-NONE", mock_url="http://mock", state_dir_path=tmp)
        assert caught.value.error_code == "revision_missing"


def test_policy_pipeline_default_directory_and_url(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("FNOL_STATE_DIR", tmp)
        monkeypatch.delenv("FNOL_MOCK_URL", raising=False)
        with pytest.raises(PolicyError):
            start_policy()


def test_restricted_policy_payload_is_refused():
    body = {"source_id": "S", "policies": [], "note": "SYNTHETIC-PRIVATE-9"}

    def fetch(case_id, base_url):
        return (HttpAttempt(1, 200, body, 1),), body

    with tempfile.TemporaryDirectory() as tmp:
        stores = Stores(tmp)
        revision = stores.input.append_revision("AUTO-D01", {"loss_type": "collision"}, "sha", trace_id="t")
        decision = run_policy(
            "AUTO-D01", "2026-06-01T00:00:00Z", base_url="http://mock", stores=stores,
            input_revision=revision.revision, trace_id="t", fetch=fetch,
        )
        assert decision["reason"] == "restricted"
        assert decision["policy_available"] is False
        assert stores.input.get_tool_payload("AUTO-D01", "policy", revision.revision) is None
        with stores.trace.connect() as db:
            codes = [r["error_code"] for r in db.execute("SELECT error_code FROM trace_events")]
        assert "restricted_value" in codes


def test_unreachable_policy_with_no_attempts():
    with tempfile.TemporaryDirectory() as tmp:
        stores = Stores(tmp)
        revision = stores.input.append_revision("AUTO-D01", {"loss_type": "collision"}, "sha", trace_id="t")
        decision = run_policy(
            "AUTO-D01", None, base_url="http://mock", stores=stores,
            input_revision=revision.revision, trace_id="t", fetch=lambda c, b: ((), None),
        )
        assert decision["reason"] == "unavailable"


# ---- rules pipeline / claims / route / service ------------------------------------------


def test_rules_pipeline_errors():
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(RulesError) as caught:
            start_routing(mock_url="http://mock", state_dir_path=tmp)
        assert caught.value.error_code == "no_cases"
        with pytest.raises(RulesError) as caught:
            start_routing("AUTO-NONE", mock_url="http://mock", state_dir_path=tmp)
        assert caught.value.error_code == "revision_missing"


def test_rules_pipeline_defaults(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("FNOL_STATE_DIR", tmp)
        monkeypatch.delenv("FNOL_MOCK_URL", raising=False)
        with pytest.raises(RulesError):
            start_routing()


def test_claims_snapshot_validation():
    assert claims._snapshot({"existing_claims": "x", "intake_deliveries": 1}) is None
    assert claims._snapshot({"existing_claims": [], "intake_deliveries": True}) is None
    assert claims._snapshot({"existing_claims": [], "intake_deliveries": "1"}) is None
    snap = claims._snapshot({"existing_claims": [], "intake_deliveries": 2, "source_id": 5, "loss_reference": "L"})
    assert (snap.intake_deliveries, snap.source_id, snap.loss_reference) == (2, None, "L")


def test_fetch_claims_malformed_200_is_unavailable():
    def get(url, **kwargs):
        return 200, {"existing_claims": "bad", "intake_deliveries": 1}

    result = claims.fetch_claims("AUTO-D01", "http://mock", get=get)
    assert result.snapshot.available is False
    assert result.raw_json is None


def test_missing_values_in_route_helper():
    assert route_mod._is_missing(None) is True
    assert route_mod._is_missing("  ") is True
    assert route_mod._is_missing("Not Supplied") is True
    assert route_mod._is_missing("Ada") is False


def test_restricted_claims_payload_is_refused_and_screen_runs():
    raw = {"source_id": "C", "existing_claims": [], "intake_deliveries": 1, "note": "SYNTHETIC-PRIVATE-1"}

    def fetch(case_id, base_url, **_kw):
        return ClaimsFetchResult(
            snapshot=ClaimsSnapshot(available=True, existing_claims=[], intake_deliveries=1),
            attempts=(ClaimsAttempt(1, 200, raw, 1),),
            raw_json=raw,
        )

    payload = {"loss_type": "collision", "vehicle_id": "V", "incident_timestamp": "2026-06-01T00:00:00Z"}
    with tempfile.TemporaryDirectory() as tmp:
        stores = Stores(tmp)
        revision = stores.input.append_revision("AUTO-D01", payload, "sha", trace_id="t")
        result = run_rules(
            "AUTO-D01",
            fields_from_payload(payload),
            PolicyResult(selected_policy_version="1"),
            base_url="http://mock",
            stores=stores,
            input_revision=revision.revision,
            trace_id="t",
            fetch=fetch,
            payload=payload,
            screen_client=None,
        )
        assert stores.input.get_tool_payload("AUTO-D01", "claims", revision.revision) is None
        with stores.trace.connect() as db:
            events = [r["event"] for r in db.execute("SELECT event FROM trace_events ORDER BY rowid")]
    assert "claims_refused" in events
    assert result.route == "manual_review"


def test_policy_from_decision_defaults():
    policy = policy_from_decision({"body": {"selected_policy_version": "2", "policy_available": 1}})
    assert policy.endorsements == () and policy.policy_available is True


# ---- rules screen ------------------------------------------------------------------------


class _Reply:
    def __init__(self, text, action=None, tokens=(1, 2)):
        self.text, self.guardrail_action = text, action
        self.input_tokens, self.output_tokens = tokens


class _Client:
    model_id = "stub"

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def chat(self, system, user):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _CodedError(Exception):
    error_code = "coded"


ITEMS = [{"source_id": "S", "label": "extra"}]


def test_leftover_lines_use_notes_key_when_source_is_missing():
    rows = screen.leftover_lines({"intake_notes": "a\n\nb", "estimate_notes": "  ", "report_notes": "c", "report_source_id": " "})
    assert rows == [
        {"source_id": "intake_notes", "line": "a"},
        {"source_id": "intake_notes", "line": "b"},
        {"source_id": "report_notes", "line": "c"},
    ]


def test_resolve_client_and_screen_fields_fall_back_to_payload_extra_fields():
    assert screen.resolve_client(None) is None
    sentinel = object()
    assert screen.resolve_client(sentinel) is sentinel
    with mock.patch.object(screen, "default_client", return_value="dflt"):
        assert screen.resolve_client(screen.USE_DEFAULT) == "dflt"
    fields = fields_from_payload({"loss_type": "collision"})
    updated, verdict, _ms = screen.screen_fields(
        fields, {"extra_fields": [{"source_id": "S", "label": "x"}, "junk"]}, FakeModel("injection")
    )
    assert verdict.status == INJECTION
    assert updated.injection_model_status == INJECTION


def test_model_check_paths():
    assert screen.model_check(None, [], []).status == CLEAN
    assert screen.model_check(None, ITEMS, []).error_code == "model_unavailable"
    client = _Client(_Reply("nonsense"), _Reply("  Clean. "))
    verdict = screen.model_check(client, ITEMS, [])
    assert (verdict.status, verdict.attempt, client.calls) == (CLEAN, 2, 2)
    client = _Client(_Reply("x"), _Reply("x"), _Reply("x"))
    assert screen.model_check(client, ITEMS, []).error_code == "model_non_answer"
    assert client.calls == 3


def test_one_call_error_shapes():
    timeout = screen._one_call(_Client(TimeoutError()), ITEMS, [], 1)
    assert timeout.error_code == "model_timeout"
    sock = screen._one_call(_Client(socket.timeout()), ITEMS, [], 1)
    assert sock.error_code == "model_timeout"
    coded = screen._one_call(_Client(_CodedError()), ITEMS, [], 1)
    assert (coded.status, coded.error_code) == (ERROR, "coded")
    generic = screen._one_call(_Client(RuntimeError("x")), ITEMS, [], 2)
    assert (generic.error_code, generic.attempt) == ("model_error", 2)
    filtered = screen._one_call(_Client(_Reply("clean", "INTERVENED")), ITEMS, [], 1)
    assert filtered.error_code == "model_filtered"
    assert (filtered.input_tokens, filtered.output_tokens) == (1, 2)
    injected = screen._one_call(_Client(_Reply("INJECTION!")), ITEMS, [], 1)
    assert (injected.status, injected.error_code) == (INJECTION, "model_injection")
    assert screen._word(None) is None
    assert screen._word(' "Clean." ') == "clean"


def test_build_prompt_masks_markers():
    system, user = screen.build_prompt(
        [{"source_id": "S", "label": "private_token"}], [{"source_id": "S", "line": "SYNTHETIC-PRIVATE-1 hi"}]
    )
    assert "private_token" not in user and "SYNTHETIC-PRIVATE-" not in user
    assert "[restricted]" in user
    _, empty = screen.build_prompt([], [])
    assert empty.count("(none)") == 2


class DefaultClientTests(unittest.TestCase):
    def test_provider_switch(self):
        with mock.patch.dict(os.environ, {"FNOL_MODEL_PROVIDER": ""}):
            self.assertIsNone(screen.default_client())
        with mock.patch.dict(os.environ, {"FNOL_MODEL_PROVIDER": " Bedrock "}), mock.patch.object(
            screen, "_bedrock_client", return_value="client"
        ):
            self.assertEqual(screen.default_client(), "client")

    def test_bedrock_client_variants(self):
        base = {"FNOL_BEDROCK_AUTH": "", "AWS_BEARER_TOKEN_BEDROCK": ""}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, dict(base, FNOL_STATE_DIR=tmp)):
            self.assertIsNone(screen._bedrock_client())
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, dict(base, FNOL_STATE_DIR=tmp, AWS_BEARER_TOKEN_BEDROCK="k")
        ):
            client = screen._bedrock_client()
            self.assertEqual(client.api_key, "k")
            self.assertIsNone(client.guardrail_id)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, dict(base, FNOL_STATE_DIR=tmp, FNOL_BEDROCK_AUTH="iam")
        ):
            self.assertEqual(screen._bedrock_client().api_key, "")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, dict(base, FNOL_STATE_DIR=tmp, AWS_BEARER_TOKEN_BEDROCK="k", AWS_REGION="not a region")
        ):
            self.assertIsNone(screen._bedrock_client())
