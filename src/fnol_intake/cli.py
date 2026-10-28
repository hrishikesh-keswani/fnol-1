"""Command line for the intake server."""

import argparse
import sys

from fnol_foundation.stores import Stores, state_dir

from fnol_intake.run import DocumentMissing, IntakeError, run_intake


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ingest FNOL cases from the mock API into SQLite.")
    parser.add_argument("--mock-url", required=True)
    parser.add_argument("--documents-dir", required=True)
    parser.add_argument("--policies-dir", required=True)
    parser.add_argument("--state-dir", default=None, help="Directory for the SQLite files. Defaults to FNOL_STATE_DIR or .state.")
    args = parser.parse_args(argv)
    stores = Stores(args.state_dir or state_dir())
    try:
        result = run_intake(stores, args.mock_url, args.documents_dir, args.policies_dir)
    except DocumentMissing as exc:
        print(exc)
        return 1
    except IntakeError as exc:
        print(f"{exc.error_code}: {exc}")
        return 1
    for revision in result["revisions"]:
        print(f"{revision.case_id} revision {revision.revision}")
    refused = result["refused"]
    if refused:
        print("restricted_value: " + ", ".join(refused), file=sys.stderr)
        return 1
    return 0
