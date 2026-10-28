"""AnalystClient edge branches: lifecycle, error mapping, shadow failure modes."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fnol_analyst import client as client_mod
from fnol_analyst.client import AnalystClient, AnalystError
from fnol_foundation.canonical import RestrictedValueError
from fnol_foundation.stores import Stores
from fnol_shadow.errors import ShadowError
from test_analyst_pipeline import GOOD_SUMMARY, FakeTransport, _seed_case


class ClientEdgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.transport = FakeTransport()

    def _client(self, **kwargs):
        return AnalystClient(
            state_dir_path=self.state, mock_url="http://mock.test", transport=self.transport, **kwargs
        )

    def _events(self, stores):
        with stores.trace.connect() as db:
            return [
                tuple(row[k] for k in ("step", "event", "status", "error_code"))
                for row in db.execute(
                    "SELECT step, event, status, error_code FROM trace_events "
                    "WHERE actor='analyst' ORDER BY rowid"
                )
            ]

    def test_context_manager_closes_servers(self):
        with self._client() as client:
            servers = client.servers
            self.assertIs(client.servers, servers)
            with mock.patch.object(servers, "close") as close:
                pass
        # close() on the exit path ran on the real server set and reset it.
        self.assertIsNone(client._servers)

    def test_close_without_servers_is_a_noop(self):
        client = self._client()
        client.close()
        self.assertIsNone(client._servers)

    def test_build_without_revision(self):
        with self.assertRaises(AnalystError) as ctx:
            self._client().build("NOPE")
        self.assertEqual(ctx.exception.error_code, "revision_missing")

    def test_build_missing_decision(self):
        stores = Stores(self.state)
        stores.input.append_revision("AUTO-D01", {"policy_number": "P"}, "sha", trace_id="t")
        stores.input.put_decision("AUTO-D01", 1, "policy", {"selected_policy_version": "1"})
        with self.assertRaises(AnalystError) as ctx:
            self._client().build("AUTO-D01")
        self.assertEqual(ctx.exception.error_code, "route_decision_missing")

    def test_build_restricted_value(self):
        stores = Stores(self.state)
        _seed_case(stores)
        with mock.patch.object(client_mod, "build_draft", side_effect=RestrictedValueError("x")):
            with self.assertRaises(AnalystError) as ctx:
                self._client().build("AUTO-D01")
        self.assertEqual(ctx.exception.error_code, "restricted_value")
        self.assertIn(("5", "draft_built", "error", "restricted_value"), self._events(stores))

    def test_submit_family_maps_submit_errors(self):
        stores = Stores(self.state)
        _seed_case(stores)
        client = self._client()
        for call in (
            lambda: client.submit("AUTO-D01"),
            lambda: client.review_status("AUTO-D01"),
            lambda: client.edit("AUTO-D01", narrative="Some neutral narrative text."),
            lambda: client.file("AUTO-D01"),
        ):
            with self.assertRaises(AnalystError) as ctx:
                call()
            self.assertEqual(ctx.exception.error_code, "draft_missing")

    def test_call_transport_strips_mock_url(self):
        client = self._client()
        self.transport.script = [(200, {})]
        client._call_transport("GET", "http://mock.test", None, None)
        self.assertEqual(self.transport.calls[0][1], "/")

    def test_call_transport_defaults_to_http(self):
        client = AnalystClient(state_dir_path=self.state, mock_url="http://mock.test")
        with mock.patch.object(client_mod, "default_transport", return_value=(200, {})) as dt:
            self.assertEqual(client._call_transport("GET", "http://x/y", None, None), (200, {}))
        dt.assert_called_once()


class ShadowFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.stores = Stores(self.state)
        _seed_case(self.stores)

    def _build(self, **kwargs):
        env = {"FNOL_AI_ENABLED": "1"}
        with mock.patch.dict(os.environ, env):
            return AnalystClient(
                state_dir_path=self.state, mock_url="http://mock.test", **kwargs
            ).build("AUTO-D01")

    def _shadow_events(self):
        with self.stores.trace.connect() as db:
            return [
                tuple(row[k] for k in ("step", "status", "error_code"))
                for row in db.execute(
                    "SELECT step, status, error_code FROM trace_events "
                    "WHERE actor='analyst' AND step IN ('6','7') ORDER BY rowid"
                )
            ]

    def test_restricted_packet_falls_back(self):
        with mock.patch.object(
            client_mod, "assert_no_restricted", side_effect=RestrictedValueError("x")
        ):
            self._build(shadow_chat=lambda s, u: None)
        self.assertEqual(
            self._shadow_events(), [("6", "error", "restricted_value"), ("7", "error", "fallback")]
        )

    def test_restricted_narrative_falls_back(self):
        from fnol_shadow.bedrock import ChatReply

        chat = lambda s, u: ChatReply(
            text=GOOD_SUMMARY, input_tokens=1, output_tokens=1, guardrail_action=None
        )
        with mock.patch.object(
            client_mod,
            "assert_no_restricted",
            side_effect=[None, RestrictedValueError("x")],
        ):
            self._build(shadow_chat=chat)
        self.assertEqual(self._shadow_events()[0], ("6", "error", "restricted_value"))

    def test_missing_api_key_is_a_shadow_error(self):
        env = {"AWS_BEARER_TOKEN_BEDROCK": "", "FNOL_BEDROCK_AUTH": ""}
        with mock.patch.dict(os.environ, env):
            self._build()
        self.assertEqual(
            self._shadow_events(), [("6", "error", "api_key_missing"), ("7", "error", "fallback")]
        )

    def test_saved_prompt_uses_control_client(self):
        from fnol_shadow.bedrock import ChatReply

        chat = lambda s, u: ChatReply(
            text=GOOD_SUMMARY, input_tokens=1, output_tokens=1, guardrail_action=None
        )
        env = {
            "AWS_BEARER_TOKEN_BEDROCK": "k",
            "BEDROCK_PROMPT_ID": "prompt-1",
            "FNOL_BEDROCK_AUTH": "",
        }
        with mock.patch.dict(os.environ, env), mock.patch.object(
            client_mod, "ControlClient"
        ) as control, mock.patch.object(
            client_mod, "resolve_system_prompt", return_value="system"
        ):
            self._build(shadow_chat=chat)
        control.assert_called_once()
        self.assertEqual(self._shadow_events()[1], ("7", "ok", None))

    def test_mantle_client_iam_mode(self):
        client = AnalystClient(state_dir_path=self.state, mock_url="http://mock.test")
        with mock.patch.dict(
            os.environ, {"FNOL_BEDROCK_AUTH": "IAM", "AWS_BEARER_TOKEN_BEDROCK": ""}
        ), mock.patch("fnol_shadow.bedrock.MantleClient") as mantle:
            result = client._mantle_client()
        self.assertIs(result, mantle.return_value)
        self.assertEqual(mantle.call_args.kwargs["api_key"], "")

    def test_mantle_client_with_api_key(self):
        client = AnalystClient(state_dir_path=self.state, mock_url="http://mock.test")
        with mock.patch.dict(
            os.environ, {"FNOL_BEDROCK_AUTH": "", "AWS_BEARER_TOKEN_BEDROCK": "k"}
        ), mock.patch("fnol_shadow.bedrock.MantleClient") as mantle:
            client._mantle_client()
        self.assertEqual(mantle.call_args.kwargs["api_key"], "k")


if __name__ == "__main__":
    unittest.main()
