"""Command line for the rules pipeline."""

import argparse
import json
import sys

from fnol_rules.pipeline import RulesError, start_routing


def main(argv=None):
    parser = argparse.ArgumentParser(description="Route FNOL cases from the stored policy decision.")
    parser.add_argument("--mock-url", default=None)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--case-id", default=None)
    args = parser.parse_args(argv)
    try:
        summary = start_routing(
            case_id=args.case_id,
            mock_url=args.mock_url,
            state_dir_path=args.state_dir,
        )
    except RulesError as exc:
        print(f"{exc.error_code}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0
