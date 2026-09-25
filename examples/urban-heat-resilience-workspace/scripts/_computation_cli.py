#!/usr/bin/env python3
"""One CLI boundary for shared computation and explicitly requested effects."""

from __future__ import annotations

import argparse
import json

from _computation_contract import MAX_BYTES
from _computation_service import refusal, run

OPERATIONS = ("schemas", "check", "aggregate", "evaluate", "verify", "schedule", "write", "apply-warnings", "dispatch")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise refusal("computation_arguments_invalid")


def main(argv=None, *, operation=None):
    parser = _Parser(description=__doc__)
    if operation is None:
        parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("--target", "--project-root", dest="target", default=".")
    parser.add_argument("--as-of")
    parser.add_argument("--schema-id")
    parser.add_argument("--expected-result-id")
    parser.add_argument("--request-id")
    parser.add_argument("--cadence-id")
    parser.add_argument("--dry-run", action="store_true")
    try:
        args = parser.parse_args(argv)
        selected = operation or args.operation
        if args.schema_id is not None and selected != "schemas":
            raise refusal("computation_schema_option_requires_schemas")
        result = run(args.target, selected, as_of=args.as_of, expected_result_id=args.expected_result_id,
                     request_id=args.request_id, cadence_id=args.cadence_id, dry_run=args.dry_run)
        if args.schema_id is not None:
            if args.schema_id not in result:
                raise refusal("computation_schema_unknown")
            result = result[args.schema_id]
        rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(rendered.encode("utf-8")) + 1 > MAX_BYTES:
            raise refusal("computation_output_bound")
        print(rendered)
        return 3 if result.get("status") == "failed" else 0
    except (Exception, SystemExit) as error:
        if isinstance(error, SystemExit):
            raise
        if not getattr(error, "error_code", "").startswith("COMPUTATION_"):
            error = refusal("computation_input_invalid_or_unavailable")
        print(json.dumps(error.to_envelope(), sort_keys=True))
        return error.exit_code
