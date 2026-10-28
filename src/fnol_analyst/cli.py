"""Command line for the analyst client.

  ingest         step 1 through the intake server
  policy         step 2 through the policy server (optional --case-id)
  route          step 3 through the rules server (optional --case-id)
  cite           step 4 through the citations server (optional --case-id)
  prepare        steps 1-4, then build and export every draft
  queue          policy through review for every ingested case that has none
  build          steps 5-8 for one case or all cases
  export         write the contract JSON array
  submit         step 9: POST /v1/reviews for a stored draft
  review-status  step 10: poll the human decision (never approves)
  edit           local edit: new draft_id; old approval is void
  file           steps 11-12: duplicate check then POST /v1/records
"""

from __future__ import annotations

import argparse
import json
import sys
from fnol_analyst.client import AnalystClient, AnalystError
from fnol_analyst.mcp import ServerError
from fnol_analyst.servers import PrepareError


def _print(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="FNOL analyst client.")
    parser.add_argument("--mock-url", default=None)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--documents-dir", default=None)
    parser.add_argument("--policies-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ingest")
    policy = sub.add_parser("policy")
    policy.add_argument("--case-id", default=None)
    route = sub.add_parser("route")
    route.add_argument("--case-id", default=None)
    cite = sub.add_parser("cite")
    cite.add_argument("--case-id", default=None)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--out", default="submissions/results.json")
    prepare.add_argument("--workers", type=int, default=3)
    sub.add_parser("queue")
    build = sub.add_parser("build")
    build.add_argument("--case-id", default=None)
    export = sub.add_parser("export")
    export.add_argument("--out", default="submissions/results.json")

    submit = sub.add_parser("submit")
    submit.add_argument("--case-id", required=True)
    review_status = sub.add_parser("review-status")
    review_status.add_argument("--case-id", required=True)
    edit = sub.add_parser("edit")
    edit.add_argument("--case-id", required=True)
    edit.add_argument("--narrative", default=None)
    edit.add_argument("--evidence-summary", default=None)
    edit.add_argument("--clear-evidence-summary", action="store_true")
    file_cmd = sub.add_parser("file")
    file_cmd.add_argument("--case-id", required=True)

    args = parser.parse_args(argv)
    client = AnalystClient(
        mock_url=args.mock_url,
        state_dir_path=args.state_dir,
        documents_dir=args.documents_dir,
        policies_dir=args.policies_dir,
    )
    try:
        with client:
            return _run(client, args)
    except (AnalystError, ServerError, PrepareError) as exc:
        code = getattr(exc, "error_code", "server_error")
        print(f"{code}: {exc}", file=sys.stderr)
        return 1


def _run(client: AnalystClient, args) -> int:
    if args.command == "ingest":
        _print(client.ingest())
        return 0
    if args.command == "policy":
        _print(client.select_policy(args.case_id))
        return 0
    if args.command == "route":
        _print(client.route(args.case_id))
        return 0
    if args.command == "cite":
        _print(client.cite(args.case_id))
        return 0
    if args.command == "prepare":
        prepared = client.prepare(workers=args.workers)
        drafts = client.build_all()
        path = client.export(args.out)
        _print({
            "cases": len(prepared["cases"]),
            "drafts": [
                {"draft_id": d.draft_id, "case_id": d.case_id, "draft_hash": d.draft_hash}
                for d in drafts
            ],
            "exported": str(path),
        })
        return 0
    if args.command == "queue":
        _print(client.enqueue_pending())
        return 0
    if args.command == "build":
        drafts = [client.build(args.case_id)] if args.case_id else client.build_all()
        _print([
            {
                "draft_id": d.draft_id,
                "case_id": d.case_id,
                "draft_hash": d.draft_hash,
                "status": d.status,
                "safety_status": d.draft.get("safety_status"),
            }
            for d in drafts
        ])
        return 0
    if args.command == "export":
        print(str(client.export(args.out)))
        return 0
    if args.command == "submit":
        record = client.submit(args.case_id)
        _print({
            "draft_id": record.draft_id,
            "case_id": record.case_id,
            "draft_hash": record.draft_hash,
            "status": record.status,
            "review_id": record.review_id,
            "outcome": record.outcome,
        })
        return 0
    if args.command == "review-status":
        _print(client.review_status(args.case_id))
        return 0
    if args.command == "edit":
        record = client.edit(
            args.case_id,
            narrative=args.narrative,
            evidence_summary=args.evidence_summary,
            clear_evidence_summary=args.clear_evidence_summary,
        )
        _print({
            "draft_id": record.draft_id,
            "case_id": record.case_id,
            "draft_hash": record.draft_hash,
            "status": record.status,
        })
        return 0
    if args.command == "file":
        _print(client.file(args.case_id))
        return 0
    return 2
