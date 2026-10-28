"""Close the last uncovered lines in policy / rules / shadow HTTP."""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from fnol_foundation.canonical import RestrictedValueError
from fnol_foundation.stores import Stores
from fnol_policy import pipeline as policy_pipeline
from fnol_policy.select import parse_utc, select_policy
from fnol_policy.service import run_policy
from fnol_rules import pipeline as rules_pipeline
from fnol_rules.claims import _snapshot, fetch_claims
from fnol_rules.route import _is_missing
from fnol_rules.screen import (
    _bedrock_client,
    _one_call,
    _word,
    default_client,
    leftover_lines,
    model_check,
)
from fnol_rules.service import run_rules
from fnol_rules.adapter import fields_from_payload
from fnol_rules.types import ClaimsSnapshot, PolicyResult
from fnol_rules.claims import ClaimsAttempt, ClaimsFetchResult


BASE_POLICY = {
    "version": "2",
    "effective_from": "2026-09-01T00:00:00Z",
    "effective_to": "2027-01-01T00:00:00Z",
    "wording_source": "AUTO-WORDING-V2",
    "endorsements": [],
}


def _claims_ok(case_id, base_url, **_kw):
    raw = {
        "source_id": f"{case_id}-CLAIMS",
        "existing_claims": [],
        "intake_deliveries": 1,
        "loss_reference": f"loss-{case_id}",
    }
    return ClaimsFetchResult(
        snapshot=ClaimsSnapshot(available=True, intake_deliveries=1),
        attempts=(ClaimsAttempt(1, 200, raw),),
        raw_json=raw,
    )


class ParseUtcEdges(unittest.TestCase):
    def test_rejects_non_string_empty_and_bad_iso(self):
        self.assertIsNone(parse_utc(None))
        self.assertIsNone(parse_utc(""))
        self.assertIsNone(parse_utc("not-a-timestamp"))
        self.assertIsNone(parse_utc("2026-10-01T12:00:00"))  # naive


class SelectPolicyEdges(unittest.TestCase):
    def test_malformed_and_unparsed_paths(self):
        self.assertEqual(
            select_policy("2026-10-01T12:00:00Z", {"policies": "bad"})["reason"],
            "malformed",
        )
        self.assertEqual(
            select_policy(None, {"policies": [BASE_POLICY]})["reason"],
            "unparsed",
        )
        self.assertEqual(
            select_policy("2026-10-01T12:00:00Z", {"policies": ["nope"]})["reason"],
            "malformed",
        )
        bad_dates = {**BASE_POLICY, "effective_from": "nope", "effective_to": "nope"}
        self.assertEqual(
            select_policy("2026-10-01T12:00:00Z", {"policies": [bad_dates]})["reason"],
            "malformed",
        )
        no_version = {**BASE_POLICY, "version": ""}
        self.assertEqual(
            select_policy("2026-10-01T12:00:00Z", {"policies": [no_version]})["reason"],
            "malformed",
        )
        bad_wording = {**BASE_POLICY, "wording_source": 12}
        self.assertEqual(
            select_policy("2026-10-01T12:00:00Z", {"policies": [bad_wording]})["reason"],
            "malformed",
        )
        bad_ends = {**BASE_POLICY, "endorsements": [1]}
        self.assertEqual(
            select_policy("2026-10-01T12:00:00Z", {"policies": [bad_ends]})["reason"],
            "malformed",
        )


class PolicyPipelineEdges(unittest.TestCase):
    def test_missing_revision_and_empty_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(policy_pipeline.PolicyError) as missing:
                policy_pipeline.start_policy(case_id="AUTO-X", state_dir_path=tmp)
            self.assertEqual(missing.exception.error_code, "revision_missing")
            with self.assertRaises(policy_pipeline.PolicyError) as empty:
                policy_pipeline.start_policy(state_dir_path=tmp)
            self.assertEqual(empty.exception.error_code, "no_cases")


class PolicyServiceRestricted(unittest.TestCase):
    def test_restricted_payload_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            stores = Stores(tmp)
            revision = stores.input.append_revision(
                "AUTO-D01",
                {"loss_type": "collision", "incident_timestamp": "2026-10-01T12:00:00Z"},
                "sha",
                trace_id="t",
            )

            def fetch(case_id, base_url):
                from fnol_foundation.http import HttpAttempt

                body = {"source_id": "P", "policies": [BASE_POLICY]}
                return (HttpAttempt(1, 200, body, 1),), body

            with mock.patch.object(
                stores.input, "put_tool_payload", side_effect=RestrictedValueError("x")
            ):
                decision = run_policy(
                    "AUTO-D01",
                    "2026-10-01T12:00:00Z",
                    base_url="http://mock",
                    stores=stores,
                    input_revision=revision.revision,
                    trace_id="t",
                    fetch=fetch,
                )
            self.assertEqual(decision["reason"], "restricted")
            self.assertFalse(decision["policy_available"])


class ClaimsSnapshotEdges(unittest.TestCase):
    def test_bool_and_non_int_deliveries_rejected(self):
        self.assertIsNone(
            _snapshot({"existing_claims": [], "intake_deliveries": True})
        )
        self.assertIsNone(
            _snapshot({"existing_claims": [], "intake_deliveries": "1"})
        )
        result = fetch_claims(
            "AUTO-D01",
            "http://mock",
            get=lambda url: (200, {"existing_claims": [], "intake_deliveries": True}),
        )
        self.assertFalse(result.snapshot.available)


class RulesPipelineEdges(unittest.TestCase):
    def test_missing_revision_and_empty_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(rules_pipeline.RulesError) as missing:
                rules_pipeline.start_routing(case_id="AUTO-X", state_dir_path=tmp)
            self.assertEqual(missing.exception.error_code, "revision_missing")
            with self.assertRaises(rules_pipeline.RulesError) as empty:
                rules_pipeline.start_routing(state_dir_path=tmp)
            self.assertEqual(empty.exception.error_code, "no_cases")


class RouteMissingEdges(unittest.TestCase):
    def test_none_is_missing(self):
        self.assertTrue(_is_missing(None))


class ScreenEdges(unittest.TestCase):
    def test_leftover_falls_back_to_notes_key(self):
        rows = leftover_lines({"estimate_notes": "line one\n"})
        self.assertEqual(rows, [{"source_id": "estimate_notes", "line": "line one"}])

    def test_word_rejects_non_string(self):
        self.assertIsNone(_word(None))
        self.assertIsNone(_word(12))

    def test_one_call_generic_error_and_guardrail(self):
        class Boom(Exception):
            error_code = "model_boom"

        err_client = SimpleNamespace(model_id="m", chat=mock.Mock(side_effect=Boom("x")))
        verdict = _one_call(err_client, [{"source_id": "a", "label": "x"}], [], 1)
        self.assertEqual(verdict.error_code, "model_boom")

        filtered = SimpleNamespace(
            model_id="m",
            chat=mock.Mock(
                return_value=SimpleNamespace(
                    text="clean",
                    input_tokens=1,
                    output_tokens=1,
                    guardrail_action="INTERVENED",
                )
            ),
        )
        verdict = _one_call(filtered, [{"source_id": "a", "label": "x"}], [], 1)
        self.assertEqual(verdict.error_code, "model_filtered")

    def test_default_client_bedrock_paths(self):
        with mock.patch.dict(os.environ, {"FNOL_MODEL_PROVIDER": "openai"}, clear=False):
            self.assertIsNone(default_client())

        fake = object()
        with mock.patch.dict(os.environ, {"FNOL_MODEL_PROVIDER": "bedrock"}, clear=False):
            with mock.patch("fnol_rules.screen._bedrock_client", return_value=fake):
                self.assertIs(default_client(), fake)

    def test_bedrock_client_branches(self):
        settings = SimpleNamespace(
            api_key="",
            region="us-east-1",
            model_id="model",
            mantle_base_url="http://mantle",
        )
        with mock.patch("fnol_shadow.pipeline.load_settings", return_value=settings):
            with mock.patch.dict(os.environ, {"FNOL_BEDROCK_AUTH": ""}, clear=False):
                self.assertIsNone(_bedrock_client())

        settings.api_key = "key"
        with mock.patch("fnol_shadow.pipeline.load_settings", return_value=settings):
            with mock.patch("fnol_shadow.bedrock.MantleClient", return_value=object()) as ctor:
                client = _bedrock_client()
                self.assertIsNotNone(client)
                ctor.assert_called_once()

        from fnol_shadow.errors import ShadowError

        with mock.patch("fnol_shadow.pipeline.load_settings", return_value=settings):
            with mock.patch(
                "fnol_shadow.bedrock.MantleClient",
                side_effect=ShadowError("shadow_error", "x"),
            ):
                self.assertIsNone(_bedrock_client())

        settings.api_key = ""
        with mock.patch("fnol_shadow.pipeline.load_settings", return_value=settings):
            with mock.patch.dict(os.environ, {"FNOL_BEDROCK_AUTH": "iam"}, clear=False):
                with mock.patch(
                    "fnol_shadow.bedrock.MantleClient", return_value=object()
                ) as ctor:
                    self.assertIsNotNone(_bedrock_client())
                    self.assertEqual(ctor.call_args.kwargs.get("api_key"), "")


class RulesServiceRestricted(unittest.TestCase):
    def test_restricted_claims_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            stores = Stores(tmp)
            payload = {
                "loss_type": "collision",
                "vehicle_id": "VEH-D01",
                "incident_timestamp": "2026-10-01T12:00:00Z",
                "driver_name": "Alex",
                "driver_name_supplied": True,
                "incident_location": "Cedar",
                "incident_location_supplied": True,
                "injury_reported": False,
                "vehicle_drivable": True,
                "referral_indicator": "none",
                "loss_reference": "loss",
                "injection_field_status": "clean",
                "injection_model_status": "clean",
                "extra_fields": [],
            }
            revision = stores.input.append_revision(
                "AUTO-D01", payload, "sha", trace_id="t"
            )
            raw = {
                "source_id": "AUTO-D01-CLAIMS",
                "existing_claims": [],
                "intake_deliveries": 1,
            }

            def fetch(case_id, base_url, **_kw):
                return ClaimsFetchResult(
                    snapshot=ClaimsSnapshot(available=True, intake_deliveries=1),
                    attempts=(ClaimsAttempt(1, 200, raw),),
                    raw_json=raw,
                )

            with mock.patch.object(
                stores.input, "put_tool_payload", side_effect=RestrictedValueError("x")
            ):
                result = run_rules(
                    "AUTO-D01",
                    fields_from_payload(payload),
                    PolicyResult("2"),
                    base_url="http://mock",
                    stores=stores,
                    input_revision=revision.revision,
                    trace_id="t",
                    fetch=fetch,
                    payload=payload,
                    screen_client=None,
                )
            self.assertFalse(result.claims_available)


if __name__ == "__main__":
    unittest.main()
