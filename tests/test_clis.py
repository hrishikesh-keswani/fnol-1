"""Command-line entry points: argv parsing, success output, and error exit codes.

Pipeline functions and clients are mocked; these tests only exercise the CLI glue
and the ``__main__`` modules.
"""

from __future__ import annotations

import contextlib
import io
import json
import runpy
import sys
import unittest
import warnings
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fnol_analyst import cli as analyst_cli  # noqa: E402
from fnol_analyst.client import AnalystError  # noqa: E402
from fnol_analyst.mcp import ServerError  # noqa: E402
from fnol_analyst.servers import PrepareError  # noqa: E402
from fnol_citations import cli as citations_cli  # noqa: E402
from fnol_citations.service import CitationError  # noqa: E402
from fnol_intake import cli as intake_cli  # noqa: E402
from fnol_intake.run import DocumentMissing, IntakeError  # noqa: E402
from fnol_policy import cli as policy_cli  # noqa: E402
from fnol_policy.pipeline import PolicyError  # noqa: E402
from fnol_rules import cli as rules_cli  # noqa: E402
from fnol_rules.pipeline import RulesError  # noqa: E402
from fnol_shadow import cli as shadow_cli  # noqa: E402
from fnol_shadow.errors import ShadowError  # noqa: E402


def run(main, argv):
    """Call ``main(argv)`` and return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class PipelineCliTests(unittest.TestCase):
    """policy, rules, and citations CLIs share one shape."""

    def test_policy_success_and_error(self):
        with mock.patch.object(policy_cli, "start_policy", return_value={"n": 1}) as start:
            code, out, _ = run(policy_cli.main, [
                "--mock-url", "http://m", "--state-dir", "/tmp/s", "--case-id", "C1",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"n": 1})
        start.assert_called_once_with(case_id="C1", mock_url="http://m", state_dir_path="/tmp/s")

        with mock.patch.object(policy_cli, "start_policy", return_value={}) as start:
            run(policy_cli.main, [])
        start.assert_called_once_with(case_id=None, mock_url=None, state_dir_path=None)

        with mock.patch.object(policy_cli, "start_policy", side_effect=PolicyError("bad_policy", "boom")):
            code, out, err = run(policy_cli.main, [])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err.strip(), "bad_policy: boom")

    def test_rules_success_and_error(self):
        with mock.patch.object(rules_cli, "start_routing", return_value={"n": 1}) as start:
            code, out, _ = run(rules_cli.main, [
                "--mock-url", "http://m", "--state-dir", "/tmp/s", "--case-id", "C1",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"n": 1})
        start.assert_called_once_with(case_id="C1", mock_url="http://m", state_dir_path="/tmp/s")

        with mock.patch.object(rules_cli, "start_routing", side_effect=RulesError("bad_route", "boom")):
            code, out, err = run(rules_cli.main, [])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err.strip(), "bad_route: boom")

    def test_citations_success_and_error(self):
        with mock.patch.object(citations_cli, "start_citations", return_value={"n": 1}) as start:
            code, out, _ = run(citations_cli.main, [
                "--state-dir", "/tmp/s", "--policies-dir", "/tmp/p", "--case-id", "C1",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"n": 1})
        start.assert_called_once_with("C1", state_dir_path="/tmp/s", policies_dir="/tmp/p")

        with mock.patch.object(citations_cli, "start_citations", side_effect=CitationError("bad_quote", "boom")):
            code, out, err = run(citations_cli.main, [])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err.strip(), "bad_quote: boom")


class IntakeCliTests(unittest.TestCase):
    ARGV = [
        "--mock-url", "http://m",
        "--documents-dir", "/tmp/docs",
        "--policies-dir", "/tmp/pol",
        "--state-dir", "/tmp/state",
    ]

    def setUp(self):
        stores = mock.patch.object(intake_cli, "Stores")
        self.stores = stores.start()
        self.addCleanup(stores.stop)

    def _result(self, refused=()):
        revisions = [SimpleNamespace(case_id="C1", revision=1), SimpleNamespace(case_id="C2", revision=3)]
        return {"revisions": revisions, "refused": list(refused)}

    def test_success_prints_revisions(self):
        with mock.patch.object(intake_cli, "run_intake", return_value=self._result()) as intake:
            code, out, err = run(intake_cli.main, self.ARGV)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["C1 revision 1", "C2 revision 3"])
        self.assertEqual(err, "")
        self.stores.assert_called_once_with("/tmp/state")
        intake.assert_called_once_with(self.stores.return_value, "http://m", "/tmp/docs", "/tmp/pol")

    def test_default_state_dir_comes_from_foundation(self):
        argv = [a for a in self.ARGV[:6]]
        with mock.patch.object(intake_cli, "state_dir", return_value="/tmp/default"), \
                mock.patch.object(intake_cli, "run_intake", return_value=self._result()):
            code, _, _ = run(intake_cli.main, argv)
        self.assertEqual(code, 0)
        self.stores.assert_called_once_with("/tmp/default")

    def test_refused_values_fail_after_printing_revisions(self):
        with mock.patch.object(intake_cli, "run_intake", return_value=self._result(["C1", "C2"])):
            code, out, err = run(intake_cli.main, self.ARGV)
        self.assertEqual(code, 1)
        self.assertIn("C1 revision 1", out)
        self.assertEqual(err.strip(), "restricted_value: C1, C2")

    def test_document_missing(self):
        error = DocumentMissing([{"case_id": "C1", "source_id": "S1"}])
        with mock.patch.object(intake_cli, "run_intake", side_effect=error):
            code, out, err = run(intake_cli.main, self.ARGV)
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "document_missing: C1:S1")
        self.assertEqual(err, "")

    def test_intake_error(self):
        error = IntakeError("health_failed", "mock down")
        with mock.patch.object(intake_cli, "run_intake", side_effect=error):
            code, out, _ = run(intake_cli.main, self.ARGV)
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "health_failed: mock down")


class FakeClient:
    """Stand-in for AnalystClient recording calls; behaviour set per test."""

    instances: list["FakeClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.closed = False
        self.error = None
        FakeClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed = True

    def _call(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.error is not None:
            raise self.error

    def ingest(self):
        self._call("ingest")
        return {"ingested": 1}

    def select_policy(self, case_id=None):
        self._call("select_policy", case_id)
        return {"policy": case_id}

    def route(self, case_id=None):
        self._call("route", case_id)
        return {"route": case_id}

    def cite(self, case_id=None):
        self._call("cite", case_id)
        return {"cite": case_id}

    def prepare(self, workers):
        self._call("prepare", workers=workers)
        return {"cases": ["C1", "C2"]}

    def build_all(self):
        self._call("build_all")
        return [self._draft("C1"), self._draft("C2")]

    def build(self, case_id):
        self._call("build", case_id)
        return self._draft(case_id)

    def export(self, out):
        self._call("export", out)
        return Path(out)

    def submit(self, case_id):
        self._call("submit", case_id)
        return SimpleNamespace(
            draft_id="D-" + case_id, case_id=case_id, draft_hash="h", status="submitted",
            review_id="R1", outcome="pending",
        )

    def review_status(self, case_id):
        self._call("review_status", case_id)
        return {"status": "pending"}

    def edit(self, case_id, narrative=None, evidence_summary=None, clear_evidence_summary=False):
        self._call(
            "edit", case_id, narrative=narrative, evidence_summary=evidence_summary,
            clear_evidence_summary=clear_evidence_summary,
        )
        return SimpleNamespace(draft_id="D2", case_id=case_id, draft_hash="h2", status="draft")

    def file(self, case_id):
        self._call("file", case_id)
        return {"filed": case_id}

    def enqueue_pending(self):
        self._call("enqueue_pending")
        return [{"case_id": "C1", "action": "queued"}]

    @staticmethod
    def _draft(case_id):
        return SimpleNamespace(
            draft_id="D-" + case_id, case_id=case_id, draft_hash="h-" + case_id,
            status="draft", draft={"safety_status": "passed"},
        )


class AnalystCliTests(unittest.TestCase):
    def setUp(self):
        FakeClient.instances = []
        patcher = mock.patch.object(analyst_cli, "AnalystClient", FakeClient)
        patcher.start()
        self.addCleanup(patcher.stop)

    def invoke(self, argv):
        code, out, err = run(analyst_cli.main, argv)
        client = FakeClient.instances[-1] if FakeClient.instances else None
        return code, out, err, client

    def test_global_options_reach_the_client(self):
        code, out, _, client = self.invoke([
            "--mock-url", "http://m", "--state-dir", "/s", "--documents-dir", "/d",
            "--policies-dir", "/p", "ingest",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"ingested": 1})
        self.assertEqual(client.kwargs, {
            "mock_url": "http://m", "state_dir_path": "/s",
            "documents_dir": "/d", "policies_dir": "/p",
        })
        self.assertTrue(client.closed)
        self.assertEqual(client.calls, [("ingest", (), {})])

    def test_policy_route_cite(self):
        for command, key in (("policy", "policy"), ("route", "route"), ("cite", "cite")):
            with self.subTest(command=command):
                code, out, _, client = self.invoke([command, "--case-id", "C1"])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out), {key: "C1"})
                code, out, _, client = self.invoke([command])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out), {key: None})

    def test_prepare(self):
        code, out, _, client = self.invoke(["prepare", "--out", "/tmp/o.json", "--workers", "5"])
        self.assertEqual(code, 0)
        body = json.loads(out)
        self.assertEqual(body["cases"], 2)
        self.assertEqual(body["exported"], "/tmp/o.json")
        self.assertEqual(
            body["drafts"][0], {"draft_id": "D-C1", "case_id": "C1", "draft_hash": "h-C1"}
        )
        self.assertEqual(client.calls[0], ("prepare", (), {"workers": 5}))

        code, out, _, client = self.invoke(["prepare"])
        self.assertEqual(json.loads(out)["exported"], "submissions/results.json")
        self.assertEqual(client.calls[0], ("prepare", (), {"workers": 3}))

    def test_queue_prints_the_pass(self):
        code, out, _, client = self.invoke(["queue"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [{"case_id": "C1", "action": "queued"}])
        self.assertEqual(client.calls, [("enqueue_pending", (), {})])

    def test_build_one_and_all(self):
        code, out, _, client = self.invoke(["build", "--case-id", "C1"])
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(rows, [{
            "draft_id": "D-C1", "case_id": "C1", "draft_hash": "h-C1",
            "status": "draft", "safety_status": "passed",
        }])
        self.assertEqual(client.calls, [("build", ("C1",), {})])

        code, out, _, client = self.invoke(["build"])
        self.assertEqual([row["case_id"] for row in json.loads(out)], ["C1", "C2"])
        self.assertEqual(client.calls, [("build_all", (), {})])

    def test_export_prints_the_path_only(self):
        code, out, _, client = self.invoke(["export", "--out", "/tmp/x.json"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "/tmp/x.json")
        code, out, _, _ = self.invoke(["export"])
        self.assertEqual(out.strip(), "submissions/results.json")

    def test_submit(self):
        code, out, _, _ = self.invoke(["submit", "--case-id", "C1"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {
            "draft_id": "D-C1", "case_id": "C1", "draft_hash": "h",
            "status": "submitted", "review_id": "R1", "outcome": "pending",
        })

    def test_review_status(self):
        code, out, _, client = self.invoke(["review-status", "--case-id", "C1"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"status": "pending"})
        self.assertEqual(client.calls, [("review_status", ("C1",), {})])

    def test_edit(self):
        code, out, _, client = self.invoke([
            "edit", "--case-id", "C1", "--narrative", "n", "--evidence-summary", "e",
            "--clear-evidence-summary",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {
            "draft_id": "D2", "case_id": "C1", "draft_hash": "h2", "status": "draft",
        })
        self.assertEqual(client.calls, [("edit", ("C1",), {
            "narrative": "n", "evidence_summary": "e", "clear_evidence_summary": True,
        })])
        code, _, _, client = self.invoke(["edit", "--case-id", "C1"])
        self.assertEqual(client.calls[0][2], {
            "narrative": None, "evidence_summary": None, "clear_evidence_summary": False,
        })

    def test_file(self):
        code, out, _, _ = self.invoke(["file", "--case-id", "C1"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"filed": "C1"})

    def test_unknown_command_returns_two(self):
        client = FakeClient()
        self.assertEqual(analyst_cli._run(client, Namespace(command="nope")), 2)

    def test_error_types_return_one(self):
        cases = (
            (AnalystError("case_missing", "no case"), "case_missing: no case"),
            (ServerError("srv", "tool", "down"), "server_error: srv.tool: down"),
            (PrepareError({"C1": RuntimeError("x")}), "prepare_failed: prepare failed for C1: x"),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                original = FakeClient._call

                def failing(self, name, *args, _error=error, **kwargs):
                    raise _error

                with mock.patch.object(FakeClient, "_call", failing):
                    code, out, err, client = self.invoke(["ingest"])
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertEqual(err.strip(), expected)
                self.assertTrue(client.closed)
                self.assertIs(FakeClient._call, original)

    def test_error_without_error_code_falls_back_to_server_error(self):
        class Plain(AnalystError):
            def __init__(self):
                Exception.__init__(self, "plain")

        with mock.patch.object(FakeClient, "_call", side_effect=Plain()):
            code, _, err, _ = self.invoke(["ingest"])
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "server_error: plain")


class ShadowCliTests(unittest.TestCase):
    SETTINGS = SimpleNamespace(
        api_key="key", region="us-east-1", model_id="m", guardrail_id="g", guardrail_version="1",
        mantle_base_url="http://mantle", control_base_url="http://control",
    )

    def settings(self, **overrides):
        return SimpleNamespace(**{**vars(self.SETTINGS), **overrides})

    def test_provision_success_default_state_dir(self):
        result = {"guardrail_status": "created", "prompt_status": "created"}
        with mock.patch.object(shadow_cli, "state_dir", return_value=Path("/tmp/default")), \
                mock.patch.object(shadow_cli, "load_settings", return_value=self.settings()) as load, \
                mock.patch.object(shadow_cli, "ControlClient") as control, \
                mock.patch.object(shadow_cli, "provision", return_value=result) as provision:
            code, out, _ = run(shadow_cli.main, ["provision"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), result)
        load.assert_called_once_with(Path("/tmp/default"))
        control.assert_called_once_with(api_key="key", region="us-east-1", base_url="http://control")
        provision.assert_called_once_with(Path("/tmp/default"), control.return_value)

    def test_provision_failure_status_returns_one(self):
        result = {"guardrail_status": "created", "prompt_status": "prompt_error"}
        with mock.patch.object(shadow_cli, "load_settings", return_value=self.settings()), \
                mock.patch.object(shadow_cli, "ControlClient"), \
                mock.patch.object(shadow_cli, "provision", return_value=result):
            code, out, _ = run(shadow_cli.main, ["--state-dir", "/tmp/x", "provision"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), result)

    def test_provision_ok_statuses(self):
        ok = shadow_cli._provision_ok
        self.assertTrue(ok({"guardrail_status": "created", "prompt_status": "created"}))
        self.assertTrue(ok({"guardrail_status": "guardrail_forbidden", "prompt_status": "prompt_conflict"}))
        self.assertFalse(ok({"guardrail_status": "created"}))
        self.assertFalse(ok({"guardrail_status": None, "prompt_status": "created"}))
        self.assertFalse(ok({"guardrail_status": "created", "prompt_status": "prompt_failed"}))

    def test_missing_api_key(self):
        for command in (["provision"], ["summarize"]):
            with self.subTest(command=command):
                with mock.patch.object(shadow_cli, "load_settings", return_value=self.settings(api_key=None)):
                    code, out, err = run(shadow_cli.main, ["--state-dir", "/tmp/x", *command])
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertTrue(err.startswith("api_key_missing: "))

    def test_summarize_one_case(self):
        rows = [{"case_id": "C1", "summary": "caf\u00e9"}]
        with mock.patch.object(shadow_cli, "load_settings", return_value=self.settings()), \
                mock.patch.object(shadow_cli, "Stores") as stores, \
                mock.patch.object(shadow_cli, "MantleClient") as mantle, \
                mock.patch.object(shadow_cli, "ControlClient") as control, \
                mock.patch.object(shadow_cli, "summarize_cases", return_value=rows) as summarize, \
                mock.patch.object(shadow_cli, "write_summaries", return_value=Path("/tmp/o.json")) as write:
            code, out, _ = run(shadow_cli.main, [
                "--state-dir", "/tmp/x", "summarize", "--case-id", "C1", "--out", "/tmp/o.json",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"exported": "/tmp/o.json", "summaries": rows})
        self.assertIn("caf\u00e9", out)
        mantle.assert_called_once_with(
            api_key="key", region="us-east-1", model_id="m", guardrail_id="g",
            guardrail_version="1", base_url="http://mantle",
        )
        control.assert_called_once_with(api_key="key", region="us-east-1", base_url="http://control")
        args, kwargs = summarize.call_args
        self.assertEqual(args, (stores.return_value, ["C1"], mantle.return_value))
        self.assertIs(kwargs["prompt_client"], control.return_value)
        write.assert_called_once_with("/tmp/o.json", rows)

    def test_summarize_all_cases_uses_store_ids(self):
        with mock.patch.object(shadow_cli, "load_settings", return_value=self.settings()), \
                mock.patch.object(shadow_cli, "Stores"), \
                mock.patch.object(shadow_cli, "MantleClient"), \
                mock.patch.object(shadow_cli, "ControlClient"), \
                mock.patch.object(shadow_cli, "case_ids", return_value=["C1", "C2"]), \
                mock.patch.object(shadow_cli, "summarize_cases", return_value=[]) as summarize, \
                mock.patch.object(shadow_cli, "write_summaries", return_value=Path("out")) as write:
            code, _, _ = run(shadow_cli.main, ["--state-dir", "/tmp/x", "summarize"])
        self.assertEqual(code, 0)
        self.assertEqual(summarize.call_args.args[1], ["C1", "C2"])
        write.assert_called_once_with("submissions/shadow_summaries.json", [])

    def test_shadow_error_from_pipeline_returns_one(self):
        with mock.patch.object(shadow_cli, "load_settings", return_value=self.settings()), \
                mock.patch.object(shadow_cli, "Stores"), \
                mock.patch.object(shadow_cli, "MantleClient"), \
                mock.patch.object(shadow_cli, "ControlClient"), \
                mock.patch.object(shadow_cli, "case_ids", return_value=["C1"]), \
                mock.patch.object(shadow_cli, "summarize_cases", side_effect=ShadowError("bad", "boom")):
            code, _, err = run(shadow_cli.main, ["--state-dir", "/tmp/x", "summarize"])
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "bad: boom")

    def test_unknown_command_returns_two(self):
        parsed = Namespace(state_dir="/tmp/x", command="other")
        with mock.patch("argparse.ArgumentParser.parse_args", return_value=parsed):
            code, _, _ = run(shadow_cli.main, [])
        self.assertEqual(code, 2)


class MainModuleTests(unittest.TestCase):
    PACKAGES = ("analyst", "citations", "intake", "policy", "rules", "shadow")

    def run_main_module(self, package, code):
        cli = sys.modules[f"fnol_{package}.cli"]
        with mock.patch.object(cli, "main", return_value=code) as main, \
                mock.patch.object(sys, "argv", [f"fnol_{package}"]), \
                warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module(f"fnol_{package}.__main__", run_name="__main__")
        main.assert_called_once_with()
        return caught.exception.code

    def test_main_modules_exit_with_the_cli_return_code(self):
        for package in self.PACKAGES:
            with self.subTest(package=package):
                self.assertEqual(self.run_main_module(package, 0), 0)
                self.assertEqual(self.run_main_module(package, 1), 1)

    def test_importing_main_modules_does_not_run_the_cli(self):
        for package in self.PACKAGES:
            with self.subTest(package=package):
                module = runpy.run_module(f"fnol_{package}.__main__", run_name="not_main")
                self.assertTrue(callable(module["main"]))

    def test_shadow_cli_module_guard(self):
        argv = ["fnol_shadow", "--state-dir", "/tmp/none", "provision"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch("fnol_shadow.pipeline.load_settings",
                           return_value=ShadowCliTests().settings(api_key=None)), \
                warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("fnol_shadow.cli", run_name="__main__")
        self.assertEqual(caught.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
