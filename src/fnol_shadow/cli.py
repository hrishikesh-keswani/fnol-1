"""Command line for the Bedrock shadow summary.

  provision   create the guardrail and managed prompt when the API key allows it
  summarize   write one handler summary per case from stored decisions

The API key is AWS_BEARER_TOKEN_BEDROCK. It is not written to the repo or the trace.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fnol_foundation.stores import Stores, state_dir
from fnol_shadow.bedrock import MantleClient
from fnol_shadow.errors import ShadowError
from fnol_shadow.pipeline import case_ids, load_settings, summarize_cases, write_summaries
from fnol_shadow.provision import ControlClient, provision


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fnol_shadow",
        description="FNOL Bedrock shadow summary.",
    )
    parser.add_argument("--state-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("provision")
    summarize = sub.add_parser("summarize")
    summarize.add_argument("--case-id", default=None)
    summarize.add_argument("--out", default="submissions/shadow_summaries.json")
    args = parser.parse_args(argv)
    directory = Path(args.state_dir) if args.state_dir else state_dir()
    try:
        if args.command == "provision":
            return _provision(directory)
        if args.command == "summarize":
            return _summarize(directory, args.case_id, args.out)
    except ShadowError as exc:
        print(f"{exc.error_code}: {exc}", file=sys.stderr)
        return 1
    return 2


def _provision(directory: Path) -> int:
    settings = load_settings(directory)
    if not settings.api_key:
        raise ShadowError("api_key_missing", "AWS_BEARER_TOKEN_BEDROCK is required")
    client = ControlClient(
        api_key=settings.api_key,
        region=settings.region,
        base_url=settings.control_base_url,
    )
    result = provision(directory, client)
    print(json.dumps(result, indent=2))
    return 0 if _provision_ok(result) else 1


def _summarize(directory: Path, case_id: str | None, out: str) -> int:
    settings = load_settings(directory)
    if not settings.api_key:
        raise ShadowError("api_key_missing", "AWS_BEARER_TOKEN_BEDROCK is required")
    stores = Stores(directory)
    ids = [case_id] if case_id else case_ids(stores)
    client = MantleClient(
        api_key=settings.api_key,
        region=settings.region,
        model_id=settings.model_id,
        guardrail_id=settings.guardrail_id,
        guardrail_version=settings.guardrail_version,
        base_url=settings.mantle_base_url,
    )
    prompt_client = ControlClient(
        api_key=settings.api_key,
        region=settings.region,
        base_url=settings.control_base_url,
    )
    rows = summarize_cases(stores, ids, client, prompt_client=prompt_client, settings=settings)
    path = write_summaries(out, rows)
    print(json.dumps({"exported": str(path), "summaries": rows}, indent=2, ensure_ascii=False))
    return 0


def _provision_ok(result: dict) -> bool:
    for key in ("guardrail_status", "prompt_status"):
        status = result.get(key) or ""
        if status == "created" or status.endswith("_forbidden") or status.endswith("_conflict"):
            continue
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
