"""Require both independently validated installations of this run's exact archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from evidence_wiki import __version__
from tools._installation_contract import check_review_producer_inventory
from tools._installed_receipts import evidence_inventory, replay_installation
from tools._journey_cases import load_cases, reference_cases
from tools.probe_installed_cli import OUTCOME_CASES, checked_outcomes
from tools.probe_installed_public import checked_public_outcomes, digest
from tools.validate_installed_artifacts import (
    REPO_ROOT,
    check_archive_membership,
    check_archive_source_bytes,
    checked_html_outcomes,
    find_artifacts,
    qualification_expectations,
    sha256_of,
    source_identity,
    source_layout,
    validation_identity,
)


def verify(dist_dir, evidence, *, commit, run_id, expected_version=None, required_runtimes=None, report_sha256=None,
           provenance="github"):
    """Require exact artifact bindings, complete installed CLI/action groups and every frozen journey."""
    if (not commit or provenance not in {"github", "local"}
            or provenance == "github" and not run_id or provenance == "local" and run_id is not None):
        raise ValueError("commit and run ID are required")
    if (not isinstance(report_sha256, list) or not report_sha256 or len(set(report_sha256)) != len(report_sha256)
            or any(not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None for value in report_sha256)):
        raise ValueError("independently retained original producer report SHA256 values are required")
    if expected_version is not None and expected_version != __version__:
        raise ValueError("requested release version differs from the checked-out package")
    wheel, sdist = find_artifacts(Path(dist_dir))
    archives = {kind: {"name": path.name, "sha256": sha256_of(path)} for kind, path in (("wheel", wheel), ("sdist", sdist))}
    membership = check_archive_membership(wheel, sdist)
    inputs = validation_identity()
    layout, source_files = source_layout(REPO_ROOT)
    check_review_producer_inventory(REPO_ROOT, layout)
    source = source_identity(source_files)
    expectations = qualification_expectations(layout)
    # Synthetic report-shape fixtures cannot prove bytes. Actual archives are
    # always checked against the independently observed candidate source.
    check_archive_source_bytes(wheel, sdist, source, layout)
    cases = load_cases(REPO_ROOT / "tests/fixtures/onboarding-journeys/cases.json")["cases"]
    cases += reference_cases(REPO_ROOT / "tests/fixtures/strict-evidence/review-cases.json")[0]
    expected = [{"case_id": case["id"], "trial": 1} for case in cases]
    reports = {}
    runtime_reports = {}
    observed_hashes = []
    for path in sorted(Path(evidence).rglob("summary.json")):
        if path.is_symlink() or path.stat().st_size > 33_554_432:
            raise ValueError("producer summary is linked or exceeds byte bound")
        raw = path.read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        if checksum not in report_sha256 or checksum in observed_hashes:
            raise ValueError("producer summary differs from external original custody or is duplicated")
        observed_hashes.append(checksum)
        report = json.loads(raw)
        if report.get("retained_files") != evidence_inventory(path.parent):
            raise ValueError("retained command/input/output bytes differ from original producer inventory")
        kind = report["artifact"]
        runtime = report.get("qualification_runtime")
        if not isinstance(runtime, dict) or set(runtime) != {"python_version", "implementation", "system", "machine"}:
            raise ValueError("installation runtime identity is incomplete")
        runtime_key = digest(runtime)
        reports = runtime_reports.setdefault(runtime_key, {"runtime": runtime, "installations": {}})["installations"]
        if kind not in archives or kind in reports:
            raise ValueError("unexpected or duplicate installation report")
        if (provenance == "github" and (report["workflow"]["GITHUB_SHA"] != commit or report["workflow"]["GITHUB_RUN_ID"] != run_id)
                or provenance == "local" and report["workflow"] != dict.fromkeys(("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"))):
            raise ValueError("installation report belongs to a different commit or run")
        if expected_version is not None and report.get("expected_version") != expected_version:
            raise ValueError("installation report was not qualified for the requested release version")
        if (report["status"] != "partial" or report["selection_status"] != "passed"
                or report["validation_inputs"] != inputs or any(report[key] != value for key, value in archives.items())):
            raise ValueError("installation is incomplete or its archive/validation bytes differ")
        checks = report["checks"]
        if set(checks) != {"membership", "installed_" + kind} or checks["membership"] != membership:
            raise ValueError("installation check inventory differs")
        installed = checks["installed_" + kind]
        if installed["label"] != kind or installed["checkout_imports"] != "disabled" or installed["version"] != __version__:
            raise ValueError("installed package identity differs")
        for group in OUTCOME_CASES:
            checked_outcomes(group, installed.get(group))
        checked_html_outcomes(installed.get("html_usability"))
        if installed.get("native_initialization") != {
            "direct_profile": "passed", "nested_profile": "passed",
            "no_write_refusals": "passed", "controller_submission": "passed",
        }:
            raise ValueError("native initialization scenarios did not all complete")
        if installed.get("planned_delegation") != {
            "schema_discovery": "passed", "plan_check": "passed", "apply_replay": "passed",
            "strict_bindings": "passed", "controller_submission": "passed",
        }:
            raise ValueError("planned delegation scenarios did not all complete")
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
        if (report.get("candidate_unchanged") is not True or report.get("candidate_source") != source
                or report.get("candidate_source_sha256") != digest(source)
                or report.get("public_expectations") != expectations
                or report.get("public_expectations_sha256") != digest(expectations)):
            raise ValueError("candidate source/configuration or frozen expectations differ")
        if report.get("archive_expectations") != layout:
            raise ValueError("archive layout differs from frozen source configuration")
        replay_installation(report, path.parent, expectations)
        public = checked_public_outcomes(installed.get("public_installed"), expectations)
        if public.get("runtime") != runtime:
            raise ValueError("public installed runtime differs from frozen selection")
        if installed.get("native_regression_scope") != "copied_owner_fixtures":
            raise ValueError("native owner observations are not identified separately")
        reports[kind] = {"version": installed["version"], "scenarios": len(observed)}
    if not runtime_reports or any(set(row["installations"]) != {"wheel", "sdist"} for row in runtime_reports.values()):
        raise ValueError("both wheel and source-archive installation reports are required")
    if set(observed_hashes) != set(report_sha256):
        raise ValueError("externally retained producer report inventory is incomplete")
    if required_runtimes is not None:
        if (not isinstance(required_runtimes, list) or not required_runtimes
                or any(not isinstance(row, dict) or set(row) != {"python_version", "implementation", "system", "machine"} for row in required_runtimes)
                or len({digest(row) for row in required_runtimes}) != len(required_runtimes)):
            raise ValueError("required runtime matrix is invalid or duplicated")
        if set(runtime_reports) != {digest(row) for row in required_runtimes}:
            raise ValueError("required installed runtime rows are missing or unexpected")
    elif len(runtime_reports) != 1:
        raise ValueError("multiple runtimes require an explicit frozen runtime matrix")
    return {"status": "passed", "artifact": "both", "selection_status": "passed",
            "expected_version": expected_version or __version__, **archives,
            "archives": archives, "installations": reports, "runtime_matrix": list(runtime_reports.values()),
            "scope": "exact_observed_installed_runtimes", "candidate_source_sha256": digest(source),
            "public_expectations_sha256": digest(expectations),
            "producer_custody_sha256": sorted(observed_hashes), "custody": "external_original_report_hashes_and_retained_command_replay",
            "workflow": {"GITHUB_SHA": commit, "GITHUB_RUN_ID": run_id} if provenance == "github" else None,
            "provenance": provenance, "candidate_commit": commit, "validation_inputs": inputs}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-version", help="Require both reports to have qualified this release version explicitly.")
    parser.add_argument("--require-runtime", type=Path, help="Frozen JSON list of exact required interpreter/system/machine rows; missing rows refuse qualification.")
    parser.add_argument("--report-sha256", action="append", required=True, help="Original finalized report digest retained independently from the downloaded evidence; repeat for each report.")
    args = parser.parse_args(argv)
    try:
        required = json.loads(args.require_runtime.read_text(encoding="utf-8")) if args.require_runtime else None
        report = verify(args.dist_dir, args.evidence, commit=args.commit, run_id=args.run_id, expected_version=args.expected_version, required_runtimes=required, report_sha256=args.report_sha256)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Installed qualification verification failed: {error}")
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
