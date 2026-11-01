#!/usr/bin/env python3
"""Qualify complete selective native review outside the grouped test suite.

A complete selective baseline runs for hours and retains more records than one legacy collection
holds, so it cannot be a test group. This route needs a measured extended recipe and bounded journal
collections. It grants no authority of its own: the native child enforces the recipe's budgets.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from unittest import SkipTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._review_dependency_fixture import execute, require_storage  # noqa: E402
from tools._execution_budget import execution_policy  # noqa: E402
from tools._review_dependency_cases import CASE_IDS, INITIAL_CASES  # noqa: E402

PROFILES = ("eight-hour", "sixteen-hour", "twenty-four-hour", "forty-eight-hour")
WAIT_MARGIN_SECONDS = 660


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def check_initial_cases(report):
    """Every initial case passes with claim-local custody and reusable historical evidence."""
    rows = {row["case_id"]: row for row in report["cases"]}
    require(len(rows) == len(CASE_IDS) == report["case_count"] == 99, "case_inventory_changed")
    require(set(rows) == set(CASE_IDS), "case_inventory_changed")
    require(report["status"] == "completed", "run_not_completed")
    require(report["mode"] == "selective_reuse", "mode_changed")
    for case_id in INITIAL_CASES:
        row = rows[case_id]
        require(row["status"] == "passed", "initial_case_not_passed")
        require("read_only_closing_custody" in row["assertions"], "closing_custody_missing")
        if case_id != "unchanged-baseline":
            require("exact_before_refresh_core_acceptance" in row["assertions"], "core_acceptance_missing")
            require(row["evidence"], "reuse_evidence_missing")
            require(all(item["state"] == "eligible" for item in row["evidence"]), "reuse_evidence_ineligible")
            require("signed_historical_workspace_provenance_preserved" in row["assertions"], "historical_provenance_missing")
    require(all(row["status"] == "not_executed" for key, row in rows.items() if key not in INITIAL_CASES),
            "unselected_case_executed")


def check_changed_claim(report):
    """A changed claim needs an actual authenticated refresh before final publication."""
    row = next(row for row in report["cases"] if row["case_id"] == "independent-claim-text")
    require(row["accepted_before_refresh"] == sorted(
        {"observed-capacity", "effective-capacity", "observed-demand", "adequacy", "margin"}), "accepted_set_changed")
    require(row["material_invalidated"] == ["independent-note"], "invalidated_set_changed")
    require(row["refreshed"], "refresh_missing")
    require("actual_authenticated_refresh_cohort" in row["assertions"], "refresh_cohort_missing")
    require("final_native_acceptance_and_publication" in row["assertions"], "final_publication_missing")


def check_newest_negative(report):
    """The newest negative review blocks an older pass without changing its basis."""
    case_id = report["selected_case_ids"][0]
    row = next(row for row in report["cases"] if row["case_id"] == case_id)
    require(row["status"] == "passed", "negative_case_not_passed")
    require("same_native_claim_and_workspace_bases" in row["assertions"], "basis_changed")
    require("exact_newest_negative_acceptance_set" in row["assertions"], "acceptance_set_changed")
    require("native_no_older_pass_resurrection" in row["assertions"], "older_pass_resurrected")


WORKLOADS = (
    ("initial-cases", None, False, check_initial_cases),
    ("changed-claim", ("independent-claim-text",), True, check_changed_claim),
    ("newest-fail", ("newest-fail-blocks-older-pass",), False, check_newest_negative),
    ("newest-unknown", ("newest-unknown-blocks-older-pass",), False, check_newest_negative),
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--extended-recipe", required=True, type=Path)
    parser.add_argument("--execution-profile", required=True, choices=PROFILES)
    parser.add_argument("--temporal-margin-seconds", type=int)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.exists():
        parser.error("--output must not exist")
    try:
        require_storage()
    except SkipTest as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
        return 2
    output.mkdir(mode=0o700, parents=True)
    route = ["--extended-recipe", str(args.extended_recipe.resolve()), "--execution-profile", args.execution_profile,
             "--journal-collection"]
    if args.temporal_margin_seconds is not None:
        route += ["--temporal-margin-seconds", str(args.temporal_margin_seconds)]
    authority = execution_policy(args.execution_profile)["authority_seconds"]
    rows = []
    for label, cases, refresh, check in WORKLOADS:
        # The child owns its budget; this outer wait only outlasts every fixture it may reserve.
        wait_seconds = (len(cases or INITIAL_CASES) + 1) * authority + WAIT_MARGIN_SECONDS
        try:
            check(execute(output / label, mode="selective_reuse", cases=cases, refresh=refresh, route=route,
                          wait_seconds=wait_seconds))
        except (AssertionError, ValueError, subprocess.TimeoutExpired) as error:
            rows.append({"workload": label, "status": "failed", "reason": str(error)[:1000]})
        else:
            rows.append({"workload": label, "status": "passed"})
    summary = {"schema_version": "selective-review-qualification/v1",
               "status": "passed" if all(row["status"] == "passed" for row in rows) else "failed",
               "execution_profile": args.execution_profile, "extended_recipe": str(args.extended_recipe.resolve()),
               "workloads": rows}
    (output / "qualification.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": summary["status"], "report": str(output / "qualification.json")}))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
