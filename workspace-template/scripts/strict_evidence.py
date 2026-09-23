#!/usr/bin/env python3
"""Inspect strict evidence, register authenticated review, and export checked claims."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import nullcontext
from pathlib import Path

from _evidence_authority import EvidenceInvalid
from _strict_contract import MAX_BYTES, schema_documents
from _strict_evidence import (
    configuration,
    evaluate,
    prepare_review,
    publication,
    read_json,
    refusal,
    render_markdown,
    resolve_policy,
    sibling,
)


def run_operation(root, operation, *, claim_id=None, envelope=None):
    try:
        root = Path(root).resolve()
        config = configuration(root)
        policy = resolve_policy(root, config)
        if policy is None:
            raise refusal("strict_policy_missing")
        if operation == "prepare-review":
            return prepare_review(root, claim_id)
        if operation == "review":
            if not isinstance(envelope, dict) or envelope.get("payload", {}).get("action") not in {
                    "register-strict-review", "register-strict-human-review"}:
                raise refusal("strict_review_command_invalid")
            return sibling("_evidence_usage").transact(root, config, envelope)
        if operation == "export":
            return publication(root)
        if operation != "check":
            raise refusal("strict_operation_unknown")
        usage = sibling("_evidence_usage")
        with usage.current_view(root, config) if usage.configured(config) else nullcontext(None) as view:
            return evaluate(root, config, policy, view=view)
    except EvidenceInvalid as exc:
        raise refusal(str(exc)) from None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise refusal("strict_input_invalid_or_unreadable") from None


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise refusal("strict_arguments_invalid")


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument("operation", choices=("schemas", "check", "prepare-review", "review", "export"))
    parser.add_argument("--target", "--project-root", dest="target", default=".")
    parser.add_argument("--claim-id")
    parser.add_argument("--schema-id")
    parser.add_argument("--from-file")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    try:
        args = parser.parse_args(argv)
        if args.operation == "schemas":
            schemas = schema_documents()
            if args.schema_id is not None and args.schema_id not in schemas:
                raise refusal("strict_schema_unknown")
            print(json.dumps(schemas if args.schema_id is None else schemas[args.schema_id], sort_keys=True))
            return 0
        envelope = read_json(args.from_file) if args.from_file else None
        result = run_operation(args.target, args.operation, claim_id=args.claim_id, envelope=envelope)
        if args.format == "markdown":
            if args.operation != "export":
                raise refusal("strict_markdown_requires_export")
            print(render_markdown(result), end="")
        else:
            rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if len(rendered.encode()) > MAX_BYTES:
                raise refusal("strict_output_bound_exceeded")
            print(rendered)
        if args.operation == "export" and result["verdict"] != "ship":
            return 3
        if args.operation == "check" and (not result["claims"] or not all(row["accepted"] for row in result["claims"])):
            return 3
        return 0
    except (Exception, SystemExit) as exc:
        if isinstance(exc, SystemExit):
            raise
        error = exc if sibling("_script_errors").is_refusal(exc) else refusal("strict_input_invalid_or_unreadable")
        # This new namespace always has one JSON stdout document on refusal.
        print(json.dumps(error.to_envelope(), sort_keys=True))
        return error.exit_code


if __name__ == "__main__":
    sys.exit(main())
