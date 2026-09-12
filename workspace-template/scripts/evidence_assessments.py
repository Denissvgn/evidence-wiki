#!/usr/bin/env python3
"""Authenticated research assessment preparation, consumption and reevaluation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _assessment_contract import MAX_BYTES, detached
from _assessment_engine import check, prepare
from _assessment_refresh import plan
from _evidence_authority import EvidenceInvalid
from _evidence_usage import transact
from _record_artifacts import json_document
from _script_errors import ScriptRefusal, emit_refusal, is_refusal
from _snapshot_verifier import SnapshotInvalid
from _temporal_contract import require
from evidence_usage import configuration


class AssessmentRefusal(ScriptRefusal, SystemExit):
    """Content-free refusal shared by library and command-line callers."""


def refusal(reason):
    return AssessmentRefusal("EVIDENCE_ASSESSMENT_REFUSED", "Evidence assessment operation refused.", exit_code=2,
        recoverable=True, details={"reason": reason},
        remediation="Check the bounded request, current source revisions, assessment authority and host checkpoint.")


def run_operation(project_root, *, operation, request):
    root = Path(project_root).resolve()
    try:
        request = detached(request)
        config = configuration(root)
        if operation == "prepare":
            return prepare(root, config, request)
        if operation == "check":
            return check(root, config, request)
        if operation == "plan-refresh":
            return plan(root, config, request)
        require(operation in {"issue", "apply-refresh"}, "assessment_operation_unsupported")
        expected = "register-assessment" if operation == "issue" else "invalidate-assessments"
        require(request.get("payload", {}).get("action") == expected, "assessment_envelope_action_invalid")
        return transact(root, config, request)
    except (EvidenceInvalid, SnapshotInvalid) as exc:
        raise refusal(str(exc)) from exc
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise refusal("assessment_invalid_or_unreadable_input") from exc
    except (Exception, SystemExit) as exc:
        if is_refusal(exc):
            raise refusal("assessment_current_publication_unavailable") from exc
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "issue", "check", "plan-refresh", "apply-refresh"))
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--format", choices=("json",), default="json")
    args = parser.parse_args(argv)
    try:
        data = sys.stdin.buffer.read(MAX_BYTES + 1)
        require(len(data) <= MAX_BYTES, "assessment_transport_bound_exceeded")
        report = run_operation(Path(args.project_root), operation=args.operation, request=json_document(data))
        print(json.dumps(report, sort_keys=True, indent=2))
        return int(args.operation == "check" and not report["eligible"])
    except ScriptRefusal as exc:
        return emit_refusal(exc, json_mode=True)
    except (OSError, EvidenceInvalid) as exc:
        return emit_refusal(refusal(str(exc) if isinstance(exc, EvidenceInvalid) else "assessment_input_unreadable"), json_mode=True)


if __name__ == "__main__":
    raise SystemExit(main())
