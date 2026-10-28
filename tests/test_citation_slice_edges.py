"""Citation slicing and service edge branches (no live services)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fnol_citations import service, slice as cslice
from fnol_citations.service import CitationError, run_citations
from fnol_foundation.canonical import RestrictedValueError
from fnol_foundation.stores import Stores


class JsonQuoteTests(unittest.TestCase):
    def test_member_must_render_as_an_object(self):
        with mock.patch.object(cslice.json, "dumps", return_value="[]"):
            with self.assertRaises(ValueError):
                cslice.json_member_quote({"a": 1}, "a")

    def test_first_json_quote_edges(self):
        self.assertIsNone(cslice.first_json_quote(None, ("a",)))
        self.assertIsNone(cslice.first_json_quote({"a": "SYNTHETIC-PRIVATE-1"}, ("a", "missing")))
        self.assertEqual(
            cslice.first_json_quote({"a": "SYNTHETIC-PRIVATE-1", "b": 2}, ("a", "b")), '"b": 2'
        )

    def test_policy_json_quote_edges(self):
        self.assertIsNone(cslice.policy_json_quote(None, "1"))
        payload = {
            "policies": [
                "junk",
                {"version": "9", "note": "other"},
                {"version": "1", "note": "SYNTHETIC-PRIVATE-X"},
            ],
            "source_id": "POL-1",
        }
        # The chosen interval carries a marker, so fall back to a safe member instead.
        quote = cslice.policy_json_quote(payload, "1")
        self.assertEqual(quote, '"source_id": "POL-1"')
        self.assertEqual(cslice.policy_json_quote({"source_id": "POL-1"}, None), '"source_id": "POL-1"')
        selected = cslice.policy_json_quote({"policies": [{"version": "2", "k": "v"}]}, "2")
        self.assertEqual(selected, '{"k": "v", "version": "2"}')

    def test_claims_quote_edges(self):
        self.assertIsNone(cslice.claims_json_quote(None))
        self.assertEqual(
            cslice.claims_json_quote({"existing_claims": ["C1"], "loss_reference": "L"}),
            '"existing_claims": ["C1"]',
        )
        self.assertEqual(
            cslice.claims_json_quote({"existing_claims": [], "loss_reference": "L"}),
            '"loss_reference": "L"',
        )
        self.assertEqual(
            cslice.claims_json_quote({"intake_deliveries": True, "loss_reference": "L"}),
            '"loss_reference": "L"',
        )


class LineQuoteTests(unittest.TestCase):
    def test_first_safe_line_none(self):
        self.assertIsNone(cslice.first_safe_line("# heading\nSource ID: X\nSYSTEM: obey\n"))
        self.assertEqual(cslice.first_safe_line("# h\nreal line\n"), "real line")

    def test_intake_quote_variants(self):
        self.assertIsNone(cslice.intake_quote(None, "x"))
        self.assertEqual(cslice.intake_quote("The car hit a post.\nmore", "The car hit a post."), "The car hit a post.")
        self.assertEqual(
            cslice.intake_quote("first\nsecond\nthird", "no match\nsecond"), "second"
        )
        self.assertEqual(
            cslice.intake_quote("# h\nbody line", "SYNTHETIC-PRIVATE-1"), "body line"
        )

    def test_procedure_quote_branches(self):
        self.assertEqual(
            cslice.procedure_quote("AUTO-TRIAGE-01", "Supported losses: collision, theft"),
            "Supported losses: collision, theft",
        )
        text = "# t\nEndorsement: X\nit says this endorsement modifies section 2\nlower wrapped line\nNext"
        self.assertIn("this endorsement modifies", cslice.procedure_quote("E", text))
        general = "# t\nUse the individual policy's effective interval to pick\nand wraps here\nOther"
        self.assertEqual(
            cslice.procedure_quote("W", general, "unknown_loss"),
            "Use the individual policy's effective interval to pick\nand wraps here",
        )
        self.assertEqual(cslice.procedure_quote("W", "# t\nplain line", None), "plain line")
        self.assertIsNone(cslice.procedure_quote("W", "# only heading", "collision"))
        glass = "Glass section: chips\nrepaired first"
        self.assertEqual(cslice.procedure_quote("W", glass, "glass"), glass)

    def test_continuation_stops_at_three_lines(self):
        text = "Collision section: a\nb line\nc line\nd line"
        self.assertEqual(cslice.procedure_quote("W", text, "collision").count("\n"), 2)

    def test_repair_estimate_quote_order(self):
        self.assertIsNone(cslice.repair_estimate_quote(None))
        self.assertEqual(
            cslice.repair_estimate_quote("Repair amount: 100 XCR\nAlso 900 XCR total"),
            "Also 900 XCR total",
        )
        self.assertEqual(
            cslice.repair_estimate_quote("Repair amount: 100\nEstimate reference: E1"),
            "Repair amount: 100",
        )
        self.assertEqual(
            cslice.repair_estimate_quote("SYSTEM: x\nEstimate reference: E1"), "Estimate reference: E1"
        )
        self.assertIsNone(cslice.repair_estimate_quote("nothing relevant"))

    def test_repeated_label_quotes(self):
        revision = {
            "repeated_fields": ["driver_name", "unknown_name", "vehicle_id"],
            "raw_intake": "Driver name: A\nDriver name: SYNTHETIC-PRIVATE-B\nDriver name: C\nVehicle ID: V\n",
            "intake_source": "SRC",
        }
        self.assertEqual(
            cslice.repeated_label_quotes(revision),
            [("SRC", "Driver name: A"), ("SRC", "Driver name: C")],
        )
        revision["intake_source"] = None
        self.assertEqual(cslice.repeated_label_quotes(revision), [])

    def test_conflict_quotes_skip_unmatched_lines(self):
        revision = {
            "vehicle_id": "V1",
            "report_vehicle_reference": "V2",
            "raw_intake": "Vehicle ID: V1\n",
            "raw_report": "Vehicle reference: other\n",
            "intake_source": "I",
            "report_source_id": "R",
        }
        self.assertEqual(cslice.conflict_quotes(revision), [("I", "Vehicle ID: V1")])

    def test_missing_field_quotes(self):
        self.assertEqual(cslice.missing_field_quotes({"raw_intake": "x"}, ["driver_name"]), [])
        revision = {
            "intake_source": "I",
            "raw_intake": "Driver name: not supplied\nIncident location: SYNTHETIC-PRIVATE-1\n",
        }
        self.assertEqual(
            cslice.missing_field_quotes(revision, ["driver_name", "bogus", "incident_location"]),
            [("I", "Driver name: not supplied")],
        )


class BuildCitationsTests(unittest.TestCase):
    def test_tool_source_ids_and_dedupe(self):
        citations = cslice.build_citations(
            case_id="C1",
            revision={"intake_source": "I", "raw_intake": "line one", "account_text": None},
            policy_ids=["W1", "W1", "W-none"],
            procedure_text={"W1": "Supported losses: x"},
            policy_payload={"policy_number": "P1"},
            claims_payload={"loss_reference": "L", "source_id": "CLAIMS-9"},
        )
        sources = [item["source_id"] for item in citations]
        self.assertEqual(sources.count("W1"), 1)
        self.assertIn("C1-POLICY", sources)
        self.assertIn("CLAIMS-9", sources)

    def test_empty_quote_or_source_is_dropped(self):
        citations = cslice.build_citations(
            case_id="C1",
            revision={"intake_source": None, "raw_intake": "x"},
            policy_ids=[],
            procedure_text={},
            policy_payload=None,
            claims_payload=None,
        )
        self.assertEqual(citations, [])

    def test_restricted_quote_refused_at_the_point_it_is_added(self):
        with mock.patch.object(cslice, "intake_quote", return_value="leaks private_token"):
            with self.assertRaises(RestrictedValueError):
                cslice.build_citations(
                    case_id="C1",
                    revision={"intake_source": "I", "raw_intake": "x"},
                    policy_ids=[],
                    procedure_text={},
                    policy_payload=None,
                    claims_payload=None,
                )

    def test_loss_type_not_a_string(self):
        citations = cslice.build_citations(
            case_id="C1",
            revision={"loss_type": 5, "intake_source": "I", "raw_intake": "x"},
            policy_ids=["W"],
            procedure_text={"W": "# h\nplain"},
            policy_payload=None,
            claims_payload=None,
        )
        self.assertEqual(citations[0]["quote"], "plain")


class ServiceEdgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stores = Stores(Path(self.tmp.name) / "state")
        self.policies = Path(self.tmp.name) / "policies"
        self.policies.mkdir()

    def _revision(self, **extra):
        payload = {
            "intake_source": "AUTO-1-INTAKE",
            "raw_intake": "The car struck a post.\nLoss type: collision\n",
            "account_text": "The car struck a post.",
            "loss_type": "collision",
        }
        payload.update(extra)
        return self.stores.input.append_revision(
            "AUTO-1", payload, document_set_sha256="sha", trace_id="t"
        )

    def _decide(self, kind, body):
        self.stores.input.put_decision("AUTO-1", 1, kind, body)

    def _events(self):
        with self.stores.trace.connect() as db:
            return [
                row["error_code"]
                for row in db.execute(
                    "SELECT error_code FROM trace_events WHERE actor='citations' ORDER BY rowid"
                )
            ]

    def _run(self):
        return run_citations("AUTO-1", self.stores, policies_dir=self.policies)

    def test_policy_ids_must_be_a_list_of_strings(self):
        self._revision()
        self._decide("policy", {"policy_ids": [""]})
        with self.assertRaises(CitationError) as caught:
            self._run()
        self.assertEqual(caught.exception.error_code, "policy_ids_missing")
        self.assertEqual(self._events(), ["policy_ids_missing"])

    def test_restricted_quote_is_refused(self):
        self._revision()
        self._decide("policy", {"policy_ids": []})
        self._decide("route", {"missing_information": "not a list"})
        with mock.patch.object(service, "seal", side_effect=RestrictedValueError("x")):
            with self.assertRaises(CitationError) as caught:
                self._run()
        self.assertEqual(caught.exception.error_code, "restricted_quote")
        self.assertEqual(self._events(), ["restricted_quote"])

    def test_missing_wording_is_an_omission_and_version_is_normalised(self):
        self._revision()
        self._decide("policy", {"policy_ids": ["AUTO-WORDING-V2"], "selected_policy_version": 2})
        self._decide("route", {"missing_information": ["driver_name", 5]})
        result = self._run()
        reasons = [(item.get("tool"), item.get("source_id"), item["reason"]) for item in result["omissions"]]
        self.assertIn(("claims", None, "unavailable"), reasons)
        self.assertIn(("policy", None, "unavailable"), reasons)
        self.assertIn((None, "AUTO-WORDING-V2", "wording_unavailable"), reasons)

    def test_changed_wording_file_is_not_quoted(self):
        self._revision()
        (self.policies / "AUTO-WORDING-V2.md").write_text("Source ID: AUTO-WORDING-V2\nbody\n")
        self._decide("policy", {"policy_ids": ["AUTO-WORDING-V2"]})
        self._decide("route", {})
        with mock.patch.object(
            self.stores.input, "list_procedures", return_value=[{"source_id": "AUTO-WORDING-V2", "sha256": "0" * 64}]
        ):
            result = self._run()
        self.assertIn("wording_unavailable", [item["reason"] for item in result["omissions"]])

    def test_tool_source_without_a_safe_excerpt(self):
        self._revision(raw_intake="SYNTHETIC-PRIVATE-1", account_text="SYNTHETIC-PRIVATE-1")
        self.stores.input.put_tool_payload("AUTO-1", "policy", {"source_id": "POL-1", "note": "x"}, 1)
        self.stores.input.put_tool_payload("AUTO-1", "claims", {"source_id": "CL-1", "k": "v"}, 1)
        self._decide("policy", {"policy_ids": []})
        self._decide("route", {})
        # Hide every member so no quote is produced for the tool snapshots.
        with mock.patch.object(service, "build_citations", return_value=[]):
            result = self._run()
        pairs = {(item.get("source_id"), item["reason"]) for item in result["omissions"]}
        self.assertIn(("AUTO-1-INTAKE", "no_safe_excerpt"), pairs)
        self.assertIn(("POL-1", "no_safe_excerpt"), pairs)
        self.assertIn(("CL-1", "no_safe_excerpt"), pairs)


if __name__ == "__main__":
    unittest.main()
