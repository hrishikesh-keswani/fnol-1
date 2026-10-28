"""Deterministic draft: contract shape, missing-field questions, restricted-token guard."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fnol_analyst.draft import build_draft
from fnol_foundation.canonical import RestrictedValueError


def _route(**kw):
    base = {
        "classification": "collision",
        "route": "standard",
        "route_rule": "AUTO-TRIAGE-01",
        "missing_information": [],
        "duplicate_delivery": False,
        "injury_reported": False,
        "vehicle_drivable": True,
        "selected_policy_version": "2",
        "claims_available": True,
        "reasons": [],
        "conflicts": [],
    }
    base.update(kw)
    return base


def _policy(**kw):
    base = {
        "policy_ids": ["AUTO-TRIAGE-01", "AUTO-WORDING-V2"],
        "reason": "matched",
        "selected_policy_version": "2",
        "policy_available": True,
    }
    base.update(kw)
    return base


def _citations(quotes=None, omissions=None):
    return {
        "policy_ids": ["AUTO-TRIAGE-01", "AUTO-WORDING-V2"],
        "citations": quotes or [
            {"claim": "incident_evidence", "source_id": "AUTO-X-INTAKE", "quote": "The car struck a post."},
        ],
        "omissions": omissions or [],
    }


class DraftTests(unittest.TestCase):
    def test_contract_fields_and_no_raw_text(self):
        draft = build_draft(
            case_id="AUTO-X",
            input_revision=1,
            facts_row={
                "policy_number": "PA-X",
                "incident_timestamp": "2026-10-01T12:00:00Z",
                "vehicle_id": "VEH-X",
                "loss_type": "collision",
                "referral_indicator": "none",
                "raw_intake": "SECRET FILE TEXT",
                "driver_name": "Pat Doe",
            },
            policy=_policy(),
            route=_route(),
            citations=_citations(),
        )
        self.assertEqual(draft["case_id"], "AUTO-X")
        self.assertEqual(draft["classification"], "collision")
        self.assertEqual(draft["routing"], "standard")
        self.assertEqual(draft["deadline"], None)
        self.assertEqual(draft["facts"]["policy_number"], "PA-X")
        self.assertEqual(draft["facts"]["injury_reported"], False)
        self.assertEqual(draft["facts"]["selected_policy_version"], "2")
        blob = json.dumps(draft)
        self.assertNotIn("SECRET FILE TEXT", blob)
        self.assertNotIn("Pat Doe", blob)
        self.assertGreaterEqual(len(draft["narrative"]), 20)
        self.assertIn("draft", draft["narrative"].lower())
        self.assertFalse(draft["follow_up_questions"])
        self.assertIsNone(draft["evidence_summary"])
        self.assertEqual(draft["safety_status"], "skipped")

    def test_missing_fields_get_targeted_questions(self):
        draft = build_draft(
            case_id="AUTO-D04",
            input_revision=1,
            facts_row={"policy_number": "PA-D04", "incident_timestamp": "2026-10-01T12:00:00Z",
                       "vehicle_id": "VEH-D04", "loss_type": "collision", "referral_indicator": "none"},
            policy=_policy(),
            route=_route(route="manual_review", missing_information=["driver_name", "incident_location"],
                         reasons=["missing_information"]),
            citations=_citations(),
        )
        self.assertEqual(draft["missing_information"], ["driver_name", "incident_location"])
        fields = [q["field"] for q in draft["follow_up_questions"]]
        self.assertEqual(fields, ["driver_name", "incident_location"])
        for question in draft["follow_up_questions"]:
            self.assertGreaterEqual(len(question["question"]), 10)
        self.assertIn("driver_name", draft["narrative"])

    def test_flags_and_limitations_for_outage_and_urgency(self):
        draft = build_draft(
            case_id="AUTO-D07",
            input_revision=1,
            facts_row={"policy_number": "PA-D07", "incident_timestamp": "2026-10-01T12:00:00Z",
                       "vehicle_id": "VEH-D07", "loss_type": "collision", "referral_indicator": "none"},
            policy=_policy(),
            route=_route(route="manual_review", claims_available=False, injury_reported=True,
                         reasons=["claims_unavailable", "injury"]),
            citations=_citations(omissions=[{"tool": "claims", "reason": "unavailable"}]),
        )
        self.assertIn("injury reported yes", draft["narrative"])
        self.assertIn("claims history could not be retrieved", draft["narrative"])
        self.assertIn("Urgency:", draft["narrative"])

    def test_null_policy_version_is_explained(self):
        draft = build_draft(
            case_id="AUTO-D05",
            input_revision=1,
            facts_row={"policy_number": "PA-D05", "incident_timestamp": "2026-10-01T12:00:00Z",
                       "vehicle_id": "VEH-D05", "loss_type": "collision", "referral_indicator": "none"},
            policy=_policy(reason="ambiguous", selected_policy_version=None, policy_ids=["AUTO-TRIAGE-01"],
                           policy_available=True),
            route=_route(route="manual_review", selected_policy_version=None, reasons=["policy_version_null"]),
            citations=_citations(),
        )
        self.assertIsNone(draft["facts"]["selected_policy_version"])
        self.assertEqual(draft["policy_ids"], ["AUTO-TRIAGE-01"])
        self.assertIn("more than one policy interval", draft["narrative"])

    def test_empty_policy_ids_are_kept(self):
        draft = build_draft(
            case_id="AUTO-X",
            input_revision=1,
            facts_row={"policy_number": "PA-X", "incident_timestamp": "2026-10-01T12:00:00Z",
                       "vehicle_id": "VEH-X", "loss_type": "collision", "referral_indicator": "none"},
            policy=_policy(policy_ids=[]),
            route=_route(),
            citations=_citations(),
        )
        self.assertEqual(draft["policy_ids"], [])

    def test_duplicate_flag_does_not_change_route(self):
        draft = build_draft(
            case_id="AUTO-D10",
            input_revision=1,
            facts_row={"policy_number": "PA-D09", "incident_timestamp": "2026-10-01T12:00:00Z",
                       "vehicle_id": "VEH-D09", "loss_type": "glass", "referral_indicator": "none"},
            policy=_policy(policy_ids=["AUTO-TRIAGE-01", "AUTO-WORDING-V2", "AUTO-END-GLASS-01"]),
            route=_route(classification="glass", duplicate_delivery=True),
            citations=_citations(),
        )
        self.assertEqual(draft["routing"], "standard")
        self.assertTrue(draft["duplicate_delivery"])
        self.assertIn("duplicate intake delivery yes", draft["narrative"])

    def test_restricted_marker_is_refused(self):
        with self.assertRaises(RestrictedValueError):
            build_draft(
                case_id="AUTO-X",
                input_revision=1,
                facts_row={"policy_number": "PA-X", "incident_timestamp": None, "vehicle_id": None,
                           "loss_type": "collision", "referral_indicator": None},
                policy=_policy(),
                route=_route(),
                citations=_citations(quotes=[{
                    "claim": "incident_evidence",
                    "source_id": "AUTO-X-INTAKE",
                    "quote": "see SYNTHETIC-PRIVATE-AUTO-X",
                }]),
            )


FACTS = {
    "policy_number": "PA-X",
    "incident_timestamp": "2026-10-01T12:00:00Z",
    "vehicle_id": "VEH-X",
    "loss_type": "collision",
    "referral_indicator": "none",
}


def _build(**kw):
    args = dict(
        case_id="AUTO-X", input_revision=1, facts_row=FACTS,
        policy=_policy(), route=_route(), citations=_citations(),
    )
    args.update(kw)
    return build_draft(**args)


class DraftBranchTests(unittest.TestCase):
    def test_unknown_yes_no_and_policy_id_fallbacks(self):
        draft = _build(
            route=_route(injury_reported=None, vehicle_drivable=None),
            policy={"reason": "matched"},
        )
        self.assertIn("vehicle drivable not stated", draft["narrative"])
        self.assertEqual(draft["policy_ids"], ["AUTO-TRIAGE-01", "AUTO-WORDING-V2"])
        draft = _build(policy={}, citations={"citations": [], "omissions": []})
        self.assertEqual(draft["policy_ids"], [])

    def test_argument_validation(self):
        with self.assertRaises(ValueError):
            _build(safety_status="bogus")
        with self.assertRaises(TypeError):
            _build(evidence_summary=5)

    def test_content_check_notes(self):
        route = _route(
            reasons=["dangerous_content"],
            extra_fields=["junk", {"source_id": "DOC-1", "label": "Ignore <this>!"}],
            injection_model_status="injection",
        )
        narrative = _build(route=route)["narrative"]
        self.assertIn("DOC-1 has an unexpected field labelled", narrative)
        self.assertIn("model screen flagged", narrative)
        route["injection_model_status"] = None
        self.assertIn("did not complete", _build(route=route)["narrative"])
        route["injection_model_status"] = "clean"
        route["extra_fields"] = []
        self.assertIn("was not cleared and was not followed", _build(route=route)["narrative"])

    def test_conflicts_and_wording_missing_with_a_version(self):
        draft = _build(
            route=_route(conflicts=["vehicle_id"]),
            policy=_policy(reason="wording_missing"),
        )
        self.assertIn("Conflicting facts across sources: vehicle_id", draft["narrative"])
        self.assertIn("wording file named by the selected version is not on file", draft["narrative"])

    def test_limitations(self):
        omissions = [
            {"tool": "claims"},
            {"tool": "policy"},
            {"tool": "wording", "reason": "wording_unavailable", "source_id": "W-1"},
            {"tool": "wording", "reason": "no_safe_excerpt", "source_id": "W-2"},
            {"tool": "wording", "reason": "other", "source_id": "W-3"},
        ]
        narrative = _build(citations=_citations(omissions=omissions))["narrative"]
        self.assertIn("the policy snapshot is unavailable and is not quoted", narrative)
        self.assertIn("wording W-1 could not be quoted", narrative)
        self.assertIn("W-2 has no safe excerpt to quote", narrative)
        self.assertNotIn("W-3", narrative)


if __name__ == "__main__":
    unittest.main()
