#!/usr/bin/env python3
"""Evaluate captured source revisions at one bounded current or historical cutoff."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _evidence_authority import EvidenceInvalid
from _script_errors import ScriptRefusal, emit_refusal
from _snapshot_verifier import SnapshotInvalid, document
from _temporal_contract import BOUNDS, require
from _temporal_replay import evaluate
from evidence_usage import configuration


class TemporalRefusal(ScriptRefusal, SystemExit):
    """A content-free refusal shared across isolated script module families."""


def refusal(reason: str) -> TemporalRefusal:
    return TemporalRefusal(
        "EVIDENCE_TEMPORAL_REFUSED", "Temporal evidence evaluation refused.",
        exit_code=2, recoverable=True, details={"reason": reason},
        remediation="Check the bounded request, immutable source clocks, host checkpoint and independent authority.",
    )


def run_evaluate(project_root: Path, *, request: dict[str, Any]) -> dict[str, Any]:
    root = Path(project_root).resolve()
    try:
        return evaluate(root, configuration(root), request)
    except (EvidenceInvalid, SnapshotInvalid) as exc:
        raise refusal(str(exc)) from exc
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise refusal("temporal_invalid_or_unreadable_input") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("evaluate",))
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--format", choices=("json",), default="json")
    args = parser.parse_args(argv)
    try:
        data = sys.stdin.buffer.read(BOUNDS["request_bytes"] + 1)
        require(len(data) <= BOUNDS["request_bytes"], "temporal_transport_bound_exceeded")
        report = run_evaluate(Path(args.project_root), request=document(data, BOUNDS["request_bytes"]))
        print(json.dumps(report, sort_keys=True, indent=2))
        return int(not report["result"]["complete"])
    except ScriptRefusal as exc:
        return emit_refusal(exc, json_mode=True)
    except (OSError, EvidenceInvalid, SnapshotInvalid) as exc:
        return emit_refusal(refusal(str(exc) if isinstance(exc, (EvidenceInvalid, SnapshotInvalid)) else "temporal_input_unreadable"),
                            json_mode=True)


if __name__ == "__main__":
    raise SystemExit(main())
