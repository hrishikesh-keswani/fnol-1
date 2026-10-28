"""Edge branches of the shadow package: provision, Mantle client, HTTP, packet, safety, pipeline."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from fnol_foundation.stores import Stores
from fnol_shadow import aws_auth, http as shadow_http, pipeline, provision, safety
from fnol_shadow.bedrock import (
    ChatReply,
    MantleClient,
    _content_text,
    _guardrail_action,
    _message_text,
    _tokens,
    mantle_chat_url,
)
from fnol_shadow.errors import ControlError, ShadowError
from fnol_shadow.packet import build_packet
from test_shadow_summary import seed


class ScriptedControl:
    """ControlClient stand-in: replies come from a list of (status, body)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.replies.pop(0)


# ---- provision ---------------------------------------------------------------------------


class ProvisionEdgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_read_saved_variants(self):
        self.assertEqual(provision.read_saved(self.dir), {})
        provision.config_path(self.dir).write_text("{not json")
        self.assertEqual(provision.read_saved(self.dir), {})
        provision.config_path(self.dir).write_text("[1]")
        self.assertEqual(provision.read_saved(self.dir), {})
        provision.write_saved(self.dir, {"prompt_id": "p"})
        self.assertEqual(provision.read_saved(self.dir), {"prompt_id": "p"})

    def test_control_client_requires_a_key_and_sends_the_request(self):
        with self.assertRaises(ControlError) as caught:
            provision.ControlClient(api_key=" ", region="us-east-1").request("GET", "/x")
        self.assertEqual(caught.exception.code, "api_key_missing")
        client = provision.ControlClient(api_key="k", region="us-east-1", base_url="http://h/")
        with mock.patch.object(
            provision, "request_json", return_value=(200, {}, {"a": 1})
        ) as send:
            self.assertEqual(client.request("GET", "/x"), (200, {"a": 1}))
        self.assertEqual(send.call_args.args[0], "http://h/x")

    def test_guardrail_failures(self):
        with self.assertRaises(ControlError) as caught:
            provision.create_guardrail(ScriptedControl([(200, {})]))
        self.assertEqual(caught.exception.code, "guardrail_http")
        with self.assertRaises(ControlError) as caught:
            provision.create_guardrail(ScriptedControl([(200, {"guardrailId": "g"}), (409, {})]))
        self.assertEqual(caught.exception.code, "guardrail_conflict")
        with self.assertRaises(ControlError) as caught:
            provision.create_guardrail(
                ScriptedControl([(200, {"guardrailId": "g"}), (200, {"version": True})])
            )
        self.assertEqual(caught.exception.code, "guardrail_http")

    def test_prompt_failures_and_prompt_identifier(self):
        with self.assertRaises(ControlError) as caught:
            provision.create_prompt(ScriptedControl([(500, {})]), "text")
        self.assertEqual(caught.exception.code, "prompt_http")
        with self.assertRaises(ControlError) as caught:
            provision.create_prompt(ScriptedControl([(200, {})]), "text")
        self.assertEqual(caught.exception.code, "prompt_http")
        with self.assertRaises(ControlError) as caught:
            provision.create_prompt(ScriptedControl([(200, {"id": "p"}), (403, {})]), "text")
        self.assertEqual(caught.exception.code, "prompt_forbidden")
        with self.assertRaises(ControlError) as caught:
            provision.create_prompt(ScriptedControl([(200, {"id": "p"}), (200, {})]), "text")
        self.assertEqual(caught.exception.code, "prompt_http")
        control = ScriptedControl([(200, {"promptIdentifier": "p/1"}), (200, {"version": 3})])
        self.assertEqual(provision.create_prompt(control, "text"), ("p/1", "3"))
        self.assertIn("/prompts/p%2F1/versions", control.calls[1][1])

    def test_validation_with_conflict_marker_is_not_retried(self):
        control = ScriptedControl([(400, {"code": "ConflictException"})])
        with self.assertRaises(ControlError) as caught:
            provision.create_prompt(control, "text")
        self.assertEqual(caught.exception.code, "prompt_conflict")
        self.assertEqual(len(control.calls), 1)

    def test_fetch_prompt_text(self):
        body = {"variants": [{"templateConfiguration": {"text": {"text": "hello"}}}]}
        control = ScriptedControl([(200, body)])
        self.assertEqual(provision.fetch_prompt_text(control, "p", None), "hello")
        self.assertEqual(control.calls[0][1], "/prompts/p")
        with self.assertRaises(ControlError) as caught:
            provision.fetch_prompt_text(ScriptedControl([(404, {})]), "p", "1")
        self.assertEqual(caught.exception.code, "prompt_http")
        with self.assertRaises(ControlError) as caught:
            provision.fetch_prompt_text(ScriptedControl([(200, {"variants": []})]), "p", "1")
        self.assertEqual(caught.exception.code, "prompt_empty")

    def test_prompt_text_shapes(self):
        pt = provision._prompt_text
        self.assertIsNone(pt({}))
        self.assertIsNone(pt({"variants": "x"}))
        self.assertIsNone(pt({"variants": ["x", {}, {"templateConfiguration": {"text": {"text": " "}}}]}))
        chat = lambda system: {"variants": [{"templateConfiguration": {"chat": {"system": system}}}]}
        self.assertEqual(pt(chat("be brief")), "be brief")
        self.assertEqual(pt(chat([{"text": "a"}, "junk", {"text": "b"}])), "a\nb")
        self.assertIsNone(pt(chat(["junk", {"text": " "}])))
        self.assertIsNone(pt(chat(" ")))
        self.assertIsNone(pt(chat(7)))
        self.assertIsNone(pt({"variants": [{"templateConfiguration": {"chat": "x"}}]}))

    def test_failure_classification(self):
        f = provision._failure
        self.assertEqual(f("g", 500, {"__type": "AccessDeniedException"}).code, "g_forbidden")
        self.assertEqual(f("g", 500, {"code": "Forbidden"}).code, "g_forbidden")
        self.assertEqual(f("g", 500, {"__type": "ConflictException"}).code, "g_conflict")
        self.assertEqual(f("g", 500, {"__type": "ValidationException"}).code, "g_rejected")
        self.assertEqual(f("g", 400, {}).code, "g_rejected")
        self.assertEqual(f("g", 500, []).code, "g_http")

    def test_version_helper(self):
        self.assertIsNone(provision._version(True))
        self.assertEqual(provision._version(2), "2")
        self.assertEqual(provision._version("DRAFT"), "DRAFT")
        self.assertIsNone(provision._version(None))

    def test_provision_reuses_saved_values_when_calls_fail(self):
        provision.write_saved(
            self.dir,
            {"guardrail_id": "g", "guardrail_version": "1", "prompt_id": "p", "prompt_version": "2"},
        )
        control = ScriptedControl([(403, {}), (403, {})])
        result = provision.provision(self.dir, control)
        self.assertEqual(result["guardrail_id"], "g")
        self.assertEqual(result["prompt_version"], "2")
        self.assertEqual(result["guardrail_status"], "guardrail_forbidden")


# ---- bedrock client ----------------------------------------------------------------------


class BedrockEdgeTests(unittest.TestCase):
    def test_url_and_config_validation(self):
        self.assertIn("us-west-2", mantle_chat_url("us-west-2"))
        with self.assertRaises(ShadowError):
            mantle_chat_url("not a region")
        with self.assertRaises(ShadowError):
            mantle_chat_url(None)
        client = MantleClient(api_key="k", region="eu-west-1")
        self.assertIn("eu-west-1", client.chat_url)
        with self.assertRaises(ShadowError) as caught:
            MantleClient(api_key="k", guardrail_id="g")
        self.assertEqual(caught.exception.error_code, "guardrail_config")

    def _chat(self, client, payload, status=200, headers=None):
        with mock.patch.dict(os.environ, {"FNOL_BEDROCK_AUTH": ""}), mock.patch(
            "fnol_shadow.bedrock.request_json", return_value=(status, headers or {}, payload)
        ):
            return client.chat("s", "u")

    def test_chat_non_200_and_blank_key(self):
        client = MantleClient(api_key="k")
        with self.assertRaises(ShadowError) as caught:
            self._chat(client, {}, status=503)
        self.assertEqual(caught.exception.error_code, "bedrock_http")
        client.api_key = "k"
        client._use_iam = lambda: False
        client.api_key = " "
        with self.assertRaises(ShadowError) as caught:
            client.chat("s", "u")
        self.assertEqual(caught.exception.error_code, "api_key_missing")

    def test_message_shapes(self):
        for payload in ({}, {"choices": []}, {"choices": ["x"]}, {"choices": [{"message": "x"}]}):
            with self.assertRaises(ShadowError):
                _message_text(payload)
        self.assertEqual(
            _message_text({"choices": [{"message": {"content": "hi"}, "finish_reason": 5}]}),
            ("hi", None),
        )

    def test_content_text_shapes(self):
        self.assertEqual(_content_text(None), "")
        self.assertEqual(
            _content_text(
                [
                    "a",
                    {"type": "reasoning", "text": "hidden"},
                    {"type": "text", "text": "b"},
                    {"type": "text", "text": 3},
                    5,
                ]
            ),
            "ab",
        )

    def test_tokens_and_guardrail_action(self):
        self.assertEqual(_tokens({}, "a"), 0)
        self.assertEqual(_tokens({"usage": {"a": True, "b": 4}}, "a", "b"), 4)
        self.assertEqual(_tokens({"usage": {"a": "x"}}, "a"), 0)
        self.assertEqual(_guardrail_action({}, "content_filter"), "INTERVENED")
        self.assertEqual(
            _guardrail_action({"X-Amzn-Bedrock-GuardrailAction": " intervened "}, "stop"),
            "INTERVENED",
        )
        self.assertIsNone(_guardrail_action({"x-amzn-bedrock-guardrailaction": " "}, None))
        self.assertIsNone(_guardrail_action({"other": "v"}, "stop"))

    def test_iam_env_selects_signing(self):
        client = MantleClient(api_key="k")
        with mock.patch.dict(os.environ, {"FNOL_BEDROCK_AUTH": " IAM "}):
            self.assertTrue(client._use_iam())
        with mock.patch.dict(os.environ, {"FNOL_BEDROCK_AUTH": ""}):
            self.assertFalse(client._use_iam())

    def test_iam_signing_path(self):
        client = MantleClient(api_key="", guardrail_id="g", guardrail_version="1")
        captured = {}

        def fake_request(url, **kwargs):
            captured["headers_no_data"] = kwargs["signer"](None)
            captured["headers"] = kwargs["signer"](b"{}")
            return 200, {}, {"choices": [{"message": {"content": "ok"}}]}

        def fake_sign(url, method, data, region, headers):
            return dict(headers, Authorization="signed")

        with mock.patch("fnol_shadow.bedrock.request_json", fake_request), mock.patch(
            "fnol_shadow.aws_auth.sign_headers", fake_sign
        ):
            reply = client.chat("s", "u")
        self.assertEqual(reply.text, "ok")
        self.assertNotIn("Content-Type", captured["headers_no_data"])
        self.assertEqual(captured["headers"]["Content-Type"], "application/json")
        self.assertEqual(captured["headers"]["X-Amzn-Bedrock-GuardrailIdentifier"], "g")


# ---- http --------------------------------------------------------------------------------


class _Resp:
    def __init__(self, status, raw, headers=None):
        self.status = status
        self._raw = raw
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ShadowHttpTests(unittest.TestCase):
    def test_default_sleeper_sleeps(self):
        with mock.patch("fnol_shadow.http.time.sleep") as sleep:
            shadow_http._default_sleeper(2)
        sleep.assert_called_once_with(0.5)

    def test_attempt_validation(self):
        with self.assertRaises(ValueError):
            shadow_http.request_json("http://x", method="GET", api_key="k", max_attempts=0)

    def test_signer_and_bearer_headers(self):
        seen = []

        def fake_urlopen(request, timeout):
            seen.append(request)
            return _Resp(200, b'{"a": 1}')

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            status, headers, payload = shadow_http.request_json(
                "http://x", method="POST", api_key="k", body={"b": 1}, signer=lambda d: {"X-S": "1"}
            )
            self.assertEqual((status, payload), (200, {"a": 1}))
            self.assertEqual(seen[0].get_header("X-s"), "1")
            shadow_http.request_json("http://x", method="GET", api_key="k", extra_headers={"X-E": "e"})
        self.assertEqual(seen[1].get_header("Authorization"), "Bearer k")
        self.assertEqual(seen[1].get_header("X-e"), "e")

    def test_transport_error_retries_then_returns_zero(self):
        pauses = []
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            result = shadow_http.request_json(
                "http://x", method="GET", api_key="k", sleeper=pauses.append
            )
        self.assertEqual(result, (0, {}, {}))
        self.assertEqual(pauses, [1, 2])

    def test_parsed_variants(self):
        self.assertEqual(shadow_http._parsed(200, {}, b""), (200, {}, {}))
        self.assertEqual(shadow_http._parsed(200, {"K": "v"}, b"\xff"), (200, {"k": "v"}, {}))
        self.assertEqual(shadow_http._parsed(200, {}, b"nope"), (200, {}, {}))
        self.assertEqual(shadow_http._parsed(200, {}, b"[1]"), (200, {}, {}))
        self.assertEqual(shadow_http._parsed(200, {}, b'{"a": 1}'), (200, {}, {"a": 1}))

    def test_http_error_is_parsed(self):
        error = urllib.error.HTTPError("http://x", 429, "slow", {}, io.BytesIO(b'{"e": 1}'))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            status, _headers, payload = shadow_http._once(
                "http://x", method="GET", api_key="k", body=None, extra_headers=None, timeout=1
            )
        self.assertEqual((status, payload), (429, {"e": 1}))


# ---- aws_auth ----------------------------------------------------------------------------


class AwsAuthTests(unittest.TestCase):
    def _fake_botocore(self, credentials):
        botocore = types.ModuleType("botocore")
        session = types.ModuleType("botocore.session")
        auth = types.ModuleType("botocore.auth")
        awsrequest = types.ModuleType("botocore.awsrequest")

        class Session:
            def get_credentials(self):
                return credentials

        class AWSRequest:
            def __init__(self, method, url, data, headers):
                self.headers = dict(headers)
                self.data = data

        class SigV4Auth:
            def __init__(self, creds, service, region):
                self.args = (creds, service, region)

            def add_auth(self, request):
                request.headers["Authorization"] = "AWS4 signed"
                request.headers["X-Bytes"] = b"skipped"

        session.Session = Session
        auth.SigV4Auth = SigV4Auth
        awsrequest.AWSRequest = AWSRequest
        botocore.session = session
        return mock.patch.dict(
            sys.modules,
            {
                "botocore": botocore,
                "botocore.session": session,
                "botocore.auth": auth,
                "botocore.awsrequest": awsrequest,
            },
        )

    def test_signs_with_credentials(self):
        creds = mock.MagicMock()
        with self._fake_botocore(creds):
            signed = aws_auth.sign_headers("http://x", "POST", None, "us-east-1", {"A": "b"})
        self.assertEqual(signed["Authorization"], "AWS4 signed")
        self.assertNotIn("X-Bytes", signed)

    def test_missing_credentials(self):
        with self._fake_botocore(None):
            with self.assertRaises(ShadowError) as caught:
                aws_auth.sign_headers("http://x", "POST", b"", "us-east-1", {})
        self.assertEqual(caught.exception.error_code, "aws_credentials_missing")

    def test_missing_dependency(self):
        with mock.patch.dict(sys.modules, {"botocore": None, "botocore.session": None}):
            with self.assertRaises(ShadowError) as caught:
                aws_auth.sign_headers("http://x", "POST", b"", "us-east-1", {})
        self.assertEqual(caught.exception.error_code, "aws_deps")


# ---- packet and safety -------------------------------------------------------------------


class PacketSafetyTests(unittest.TestCase):
    def test_packet_tolerates_malformed_decisions(self):
        packet = build_packet(
            case_id="C",
            input_revision=1,
            facts_row={},
            policy={"policy_ids": "nope", "selected_policy_version": "7"},
            route={},
            citations={
                "policy_ids": ["X"],
                "citations": ["junk", {"claim": 1, "source_id": "S", "quote": None}],
                "omissions": ["junk", {"tool": "t", "source_id": 2, "reason": "r"}],
            },
        )
        self.assertEqual(packet["policy"]["policy_ids"], ["X"])
        self.assertEqual(packet["policy"]["selected_policy_version"], "7")
        self.assertEqual(
            packet["citations"], [{"claim": None, "source_id": "S", "quote": None}]
        )
        self.assertEqual(
            packet["omissions"], [{"tool": "t", "source_id": None, "reason": "r"}]
        )
        empty = build_packet(
            case_id="C", input_revision=1, facts_row={}, policy={}, route={},
            citations={"citations": "x", "omissions": "y"},
        )
        self.assertEqual((empty["citations"], empty["omissions"]), ([], []))

    def test_assess_edges(self):
        self.assertEqual(safety.assess(5), "blocked")
        self.assertEqual(safety.assess("too short"), "blocked")
        self.assertEqual(safety.assess(safety.WITHHELD_NOTE + " "), "blocked")
        self.assertEqual(safety.assess(safety.blocked_output_message()), "blocked")
        self.assertEqual(safety.assess('{"coverage": "x", "padding": "to length"}'), "blocked")
        self.assertEqual(safety.assess("```json\n{broken json that is long enough\n```"), "passed")
        self.assertEqual(safety.assess("{this looks like json but is long enough, not valid}"), "passed")

    def test_blocked_message_fallback(self):
        with mock.patch.object(Path, "read_text", return_value=json.dumps({"blockedOutputsMessaging": " "})):
            self.assertEqual(safety.blocked_output_message(), safety.WITHHELD_NOTE)


# ---- pipeline ----------------------------------------------------------------------------


class PipelineEdgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stores = Stores(Path(self.tmp.name))

    def _client(self, text="x"):
        client = mock.MagicMock()
        client.model_id = "m"
        client.chat.return_value = ChatReply(text, 1, 1, None)
        return client

    def test_case_ids_sorted(self):
        seed(self.stores)
        self.stores.input.append_revision("AUTO-A00", {"policy_number": "P"}, trace_id="t")
        self.assertEqual(pipeline.case_ids(self.stores), ["AUTO-A00", "AUTO-D01"])

    def test_missing_decision_kind(self):
        self.stores.input.append_revision("AUTO-X", {"policy_number": "P"}, trace_id="t")
        with self.assertRaises(ShadowError) as caught:
            pipeline.summarize_case(self.stores, "AUTO-X", self._client())
        self.assertEqual(caught.exception.error_code, "policy_decision_missing")

    def test_restricted_packet_is_recorded_and_refused(self):
        seed(self.stores)
        client = self._client()
        with mock.patch.object(
            pipeline, "assert_no_restricted", side_effect=[pipeline.RestrictedValueError("x"), None]
        ):
            with self.assertRaises(ShadowError) as caught:
                pipeline.summarize_case(self.stores, "AUTO-D01", client, system_prompt="s")
        self.assertEqual(caught.exception.error_code, "restricted_value")
        client.chat.assert_not_called()
        with self.stores.trace.connect() as db:
            row = db.execute(
                "SELECT status, error_code FROM trace_events WHERE actor='shadow'"
            ).fetchone()
        self.assertEqual((row["status"], row["error_code"]), ("error", "restricted_value"))

    def test_summarize_cases_and_write(self):
        seed(self.stores)
        good = (
            "Draft for the human handler. Case AUTO-D01 is a collision on policy PA-D01. "
            "A person must review it."
        )
        rows = pipeline.summarize_cases(
            self.stores, ["AUTO-D01"], self._client(good), system_prompt="s"
        )
        self.assertEqual(rows[0]["shadow_safety_status"], "passed")
        out = pipeline.write_summaries(Path(self.tmp.name) / "sub" / "out.json", rows)
        self.assertEqual(json.loads(out.read_text())[0]["case_id"], "AUTO-D01")

    def test_generation_error_is_recorded_and_raised(self):
        seed(self.stores)
        client = self._client()
        client.chat.side_effect = ShadowError("bedrock_http", "down")
        with self.assertRaises(ShadowError):
            pipeline.summarize_case(self.stores, "AUTO-D01", client, system_prompt="s")

    def test_resolve_system_prompt(self):
        local = pipeline.local_prompt_text()
        settings = pipeline.ShadowSettings(
            region="r", model_id="m", api_key="k", guardrail_id=None, guardrail_version=None,
            prompt_id="p", prompt_version="1", mantle_base_url=None, control_base_url=None,
        )
        self.assertEqual(pipeline.resolve_system_prompt(None, None), local)
        with mock.patch.object(pipeline, "fetch_prompt_text", side_effect=ControlError("x", 1)):
            self.assertEqual(pipeline.resolve_system_prompt(settings, object()), local)
        with mock.patch.object(pipeline, "fetch_prompt_text", return_value="managed"):
            self.assertEqual(pipeline.resolve_system_prompt(settings, object()), "managed")

    def test_generate_withholds_after_two_bad_replies(self):
        client = self._client("We will pay the repair estimate in full today.")
        status, text, tokens_in, tokens_out = pipeline._generate(client, "s", "u")
        self.assertEqual((status, text, tokens_in, tokens_out), ("blocked", safety.WITHHELD_NOTE, 2, 2))
        self.assertIn(pipeline.REWRITE, client.chat.call_args.args[1])

    def test_settings_prefer_environment(self):
        env = {
            "BEDROCK_GUARDRAIL_ID": " env-g ",
            "AWS_REGION": "",
            "BEDROCK_MODEL_ID": "",
            "AWS_BEARER_TOKEN_BEDROCK": "",
        }
        with mock.patch.dict(os.environ, env):
            settings = pipeline.load_settings(self.tmp.name)
        self.assertEqual(settings.guardrail_id, "env-g")
        self.assertEqual(settings.region, "us-east-1")
        self.assertEqual(settings.api_key, "")


if __name__ == "__main__":
    unittest.main()
