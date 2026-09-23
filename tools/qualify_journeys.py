#!/usr/bin/env python3
"""Run frozen synthetic research journeys through installed public command owners."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

# The capsule contains only these explicit harness/fixture inputs, never src/.
INPUT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INPUT_ROOT))
if "tests" not in sys.modules:
    fixture_package = types.ModuleType("tests")
    fixture_package.__path__ = [str(INPUT_ROOT / "tests")]
    sys.modules["tests"] = fixture_package

from tools._journey_cases import candidate_identity, canonical, digest, load_cases, reference_cases  # noqa: E402
from tools._journey_driver import Journey  # noqa: E402


def qualify(output, suite, selected=None, trials=1, *, protected_host=False):
    import evidence_wiki

    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    identity = candidate_identity()
    frozen = {"cases": suite, "selected": selected, "trials": trials, "protected_host": protected_host, "candidate": identity,
              "package_version": evidence_wiki.__version__, "started_at": datetime.now(timezone.utc).isoformat()}
    (output / "frozen.json").write_bytes(canonical(frozen))
    report = {"schema_version": "evidence-journey-observation/v1", "frozen_sha256": digest(frozen),
        "package_version": evidence_wiki.__version__, "package_location": str(Path(evidence_wiki.__file__).resolve()),
        "platform": platform.platform(), "python": platform.python_version(), "trials": [], "status": "incomplete",
        "grading_basis": suite["grading_basis"], "live_model": False, "candidate": identity,
        "semantic_metrics": None, "operator_interventions": None,
        "limits": ["Synthetic reference conformance does not certify arbitrary semantic truth.",
                   "Reference evaluator uses disclosed fixture credentials, not production or expert approval.",
                   "Ordinary command execution is artifact_checked; host protection is reported separately."]}
    def save():
        (output / "observations.json").write_bytes(canonical(report))
    save()
    if os.name != "posix":
        report.update(status="unsupported", reason="Setup and authenticated host storage require POSIX primitives")
        save()
        return report
    for case in suite["cases"]:
        if selected and case["id"] not in selected:
            continue
        for trial in range(1, trials + 1):
            journey = Journey(output / (case["id"] + "-" + str(trial)), case)
            try:
                result = journey.run(protected_host=protected_host)
            except Exception as error:
                result = {"case_id": case["id"], "outcome": "failed", "reason": str(error)[:4096], "commands": journey.commands}
            path = output / (case["id"] + "-" + str(trial) + ".json")
            path.write_bytes(canonical(result))
            report["trials"].append({"case_id": case["id"], "trial": trial, "outcome": result["outcome"],
                "evidence": path.name, "sha256": digest(result), "reason": result.get("reason"),
                "expected_outcomes": result.get("expected"), "actual_outcomes": result.get("actual"),
                "first_usable_evidence_seconds": result.get("first_usable_evidence_seconds"),
                "host": result.get("host", {"status": "not_run"})})
            save()
            print(json.dumps(report["trials"][-1]), flush=True)
    report["status"] = "passed" if report["trials"] and all(r["outcome"] in {"passed", "blocked_scope"} for r in report["trials"]) else "failed"
    report["candidate_unchanged"] = candidate_identity() == identity
    if not report["candidate_unchanged"]:
        report.update(status="failed", reason="candidate_changed_during_qualification")
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    save()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=INPUT_ROOT / "tests/fixtures/onboarding-journeys/cases.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append")
    parser.add_argument("--trials", type=int, default=1, choices=range(1, 11))
    parser.add_argument("--protected-host", action="store_true")
    args = parser.parse_args(argv)
    suite = load_cases(args.cases)
    references, suite["strict_reference"] = reference_cases(INPUT_ROOT / "tests/fixtures/strict-evidence/review-cases.json")
    suite["cases"].extend(references)
    if args.case and not set(args.case) <= {row["id"] for row in suite["cases"]}:
        parser.error("unknown case selection")
    result = qualify(args.output, suite, args.case, args.trials, protected_host=args.protected_host)
    return 0 if result["status"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
