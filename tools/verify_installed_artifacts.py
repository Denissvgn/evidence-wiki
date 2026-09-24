"""Require both independently validated installations of this run's exact archives."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evidence_wiki import __version__
from tools._journey_cases import load_cases, reference_cases
from tools.validate_installed_artifacts import (
    REPO_ROOT,
    check_archive_membership,
    find_artifacts,
    sha256_of,
    validation_identity,
)


def verify(dist_dir, evidence, *, commit, run_id):
    if not commit or not run_id:
        raise ValueError("commit and run ID are required")
    wheel, sdist = find_artifacts(Path(dist_dir))
    archives = {kind: {"name": path.name, "sha256": sha256_of(path)} for kind, path in (("wheel", wheel), ("sdist", sdist))}
    membership = check_archive_membership(wheel, sdist)
    inputs = validation_identity()
    cases = load_cases(REPO_ROOT / "tests/fixtures/onboarding-journeys/cases.json")["cases"]
    cases += reference_cases(REPO_ROOT / "tests/fixtures/strict-evidence/review-cases.json")[0]
    expected = [{"case_id": case["id"], "trial": 1} for case in cases]
    reports = {}
    for path in sorted(Path(evidence).rglob("summary.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        kind = report["artifact"]
        if kind not in archives or kind in reports:
            raise ValueError("unexpected or duplicate installation report")
        if report["workflow"]["GITHUB_SHA"] != commit or report["workflow"]["GITHUB_RUN_ID"] != run_id:
            raise ValueError("installation report belongs to a different commit or run")
        if (report["status"] != "partial" or report["selection_status"] != "passed"
                or report["validation_inputs"] != inputs or any(report[key] != value for key, value in archives.items())):
            raise ValueError("installation is incomplete or its archive/validation bytes differ")
        checks = report["checks"]
        if set(checks) != {"membership", "installed_" + kind} or checks["membership"] != membership:
            raise ValueError("installation check inventory differs")
        installed = checks["installed_" + kind]
        if installed["label"] != kind or installed["checkout_imports"] != "disabled" or installed["version"] != __version__:
            raise ValueError("installed package identity differs")
        journeys = installed["journeys"]
        observed = [{"case_id": row["case_id"], "trial": row["trial"]} for row in journeys["trials"]]
        if (journeys["status"] != "passed" or journeys["candidate_unchanged"] is not True
                or journeys["package_version"] != __version__ or journeys["planned_trials"] != expected or observed != expected):
            raise ValueError("research scenario execution is incomplete, duplicated or changed")
        for case, row in zip(cases, journeys["trials"], strict=True):
            if case["id"] == "unsupported-domain":
                if row["outcome"] != "blocked_scope":
                    raise ValueError("unsupported scenario did not preserve its scope refusal")
            else:
                outcomes = {"q" + str(index): value for index, value in enumerate(case["release_expected"], 1)}
                if (row["outcome"] != "passed" or row["expected_outcomes"] != outcomes or row["actual_outcomes"] != outcomes):
                    raise ValueError("research scenario outcome differs from its frozen reference")
        if not report["commands"] or any(row["status"] != "passed" for row in report["commands"]):
            raise ValueError("installation commands did not all complete")
        reports[kind] = {"version": installed["version"], "scenarios": len(observed)}
    if set(reports) != {"wheel", "sdist"}:
        raise ValueError("both wheel and source-archive installation reports are required")
    return {"status": "passed", "archives": archives, "installations": reports}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        report = verify(args.dist_dir, args.evidence, commit=args.commit, run_id=args.run_id)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Installed qualification verification failed: {error}")
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
