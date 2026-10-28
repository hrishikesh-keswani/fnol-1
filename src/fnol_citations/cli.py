"""Command line for the citations pipeline."""

import argparse
import json
import sys

from fnol_citations.pipeline import start_citations
from fnol_citations.service import CitationError


def main(argv=None):
    parser = argparse.ArgumentParser(description="Slice exact evidence quotes for ingested FNOL cases.")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--policies-dir", default=None)
    parser.add_argument("--case-id", default=None)
    args = parser.parse_args(argv)
    try:
        summary = start_citations(
            args.case_id,
            state_dir_path=args.state_dir,
            policies_dir=args.policies_dir,
        )
    except CitationError as exc:
        print(f"{exc.error_code}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0
