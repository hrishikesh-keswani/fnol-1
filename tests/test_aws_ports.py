"""Disk and S3 file stores, and IAM Bedrock signing. No live AWS account required."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from fnol_foundation.blobs import object_store, read_source
from fnol_intake.parse import load_procedures
from fnol_shadow.bedrock import MantleClient

ROOT = Path(__file__).resolve().parents[1]
DEV_DOCS = ROOT / "data" / "dev" / "documents"
DEV_POLICIES = ROOT / "data" / "dev" / "policies"


class DiskStoreTests(unittest.TestCase):
    def test_disk_read_source_unchanged(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FNOL_S3_BUCKET", None)
            found = read_source(DEV_DOCS, "AUTO-D01-INTAKE")
        self.assertIsNotNone(found)
        path, data, text = found
        self.assertTrue(str(path).endswith("AUTO-D01-INTAKE.md"))
        self.assertIn("Policy number", text)
        self.assertEqual(data.decode("utf-8"), text)

    def test_disk_load_procedures(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FNOL_S3_BUCKET", None)
            sources = load_procedures(DEV_POLICIES)
        ids = {row["source_id"] for row in sources}
        self.assertIn("AUTO-WORDING-V1", ids)
        self.assertTrue(all(row["sha256"] for row in sources))


class StoreSelectionTests(unittest.TestCase):
    def test_disk_when_neither_cloud_store_is_set(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FNOL_S3_BUCKET", None)
            self.assertEqual(object_store(), "disk")

    def test_s3_read_uses_the_container_prefix(self):
        with mock.patch.dict(os.environ, {"FNOL_S3_BUCKET": "fnol-evidence"}, clear=False):
            with mock.patch(
                "fnol_foundation.s3store.get_object_bytes",
                return_value=b"Policy number: PA-1\n",
            ) as reader:
                found = read_source("/tmp/documents", "AUTO-D01-INTAKE")
        self.assertIsNotNone(found)
        self.assertEqual(found[0], "s3://fnol-evidence/case-documents/AUTO-D01-INTAKE.md")
        self.assertIn("Policy number", found[2])
        reader.assert_called_once_with(
            "fnol-evidence", "case-documents/AUTO-D01-INTAKE.md"
        )


class IamBedrockTests(unittest.TestCase):
    def test_empty_key_signs_instead_of_sending_a_bearer_token(self):
        payload = {
            "choices": [{"message": {"content": "handler summary text here ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }
        client = MantleClient(api_key="", region="us-east-1", base_url="https://bedrock.example/openai/v1")
        with mock.patch(
            "fnol_shadow.aws_auth.sign_headers",
            return_value={"Authorization": "AWS4-HMAC-SHA256 signed", "Accept": "application/json"},
        ) as signed:
            with mock.patch("fnol_shadow.bedrock.request_json", return_value=(200, {}, payload)) as request:
                reply = client.chat("system", "user")
        self.assertEqual(reply.text, "handler summary text here ok")
        self.assertEqual(request.call_args.kwargs["api_key"], "")
        headers = request.call_args.kwargs["signer"](b"{}")
        self.assertEqual(headers["Authorization"], "AWS4-HMAC-SHA256 signed")
        signed.assert_called_once()
        url, method, body, region, _headers = signed.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(region, "us-east-1")
        self.assertIn("chat/completions", url)
        self.assertIsInstance(body, bytes)

    def test_iam_flag_signs_even_when_a_bearer_token_is_set(self):
        payload = {
            "choices": [{"message": {"content": "handler summary text here ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        client = MantleClient(api_key="local-bearer", region="us-east-1", base_url="https://bedrock.example/openai/v1")
        with mock.patch.dict(os.environ, {"FNOL_BEDROCK_AUTH": "iam"}, clear=False):
            with mock.patch("fnol_shadow.aws_auth.sign_headers", return_value={"Authorization": "AWS4-HMAC-SHA256 signed"}):
                with mock.patch("fnol_shadow.bedrock.request_json", return_value=(200, {}, payload)) as request:
                    client.chat("system", "user")
        self.assertEqual(request.call_args.kwargs["api_key"], "")
        self.assertIsNotNone(request.call_args.kwargs["signer"])
