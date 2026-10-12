#!/usr/bin/env python3
"""Freeze repeated reference integrations and replay their improvement decision."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools._integration_improvement import (  # noqa: E402
    CYCLES,
    INTEGRITY_ERRORS,
    ROUTES,
    decide,
    freeze_plan,
    replay_decision,
    validate_plan,
)
from tools._integration_improvement_owners import evidence_pins, host_readers  # noqa: E402
from tools._release_decision import digest  # noqa: E402
from tools._repeated_integration_host import host_identity, input_identity, run_scenario, scenario  # noqa: E402
from tools.decide_release import _deadline, _HostDeadline, _publish, _read  # noqa: E402
from tools.verify_acceptance_paths import candidate_manifest  # noqa: E402


def reference_plan(proposals=()):
    now = datetime.now(timezone.utc)
    hosts = [{"host_id": route, "route": route, "host_sha256": digest(host_identity(route)),
              "scenario_sha256": digest(scenario(route))} for route in ROUTES]
    slots = [{"slot_id": route + "-" + cycle, "host_id": route, "cycle": cycle,
              "input_sha256": input_identity(route, cycle), "owner_ref": "original-" + route,
              "elapsed_cap_seconds": 900 if cycle == "baseline" else 600} for route in ROUTES for cycle in CYCLES]
    return freeze_plan(candidate_sha256=digest(candidate_manifest(ROOT)), hosts=hosts, slots=slots,
        scope={"kind": "synthetic_finite_identity_revisions", "host_execution": "local_three_routes",
               "observation": "same_workspace_baseline_and_two_updates", "whole_host_cost": "unknown",
               "provider_cost": "unknown", "business_value": "unobserved"},
        frozen_at=now.isoformat(), expires_at=(now + timedelta(hours=2)).isoformat(), proposals=list(proposals))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["freeze", "run", "decide", "replay"])
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--expected-plan")
    parser.add_argument("--proposals", type=Path)
    parser.add_argument("--host", choices=ROUTES)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--originals", type=Path)
    parser.add_argument("--expected-evidence")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "freeze":
            if args.output is None:
                raise ValueError("integration_output_required")
            plan = reference_plan(_read(args.proposals) if args.proposals else ())
            _publish(args.output, plan)
            result = {"plan_path": str(args.output), "plan_sha256": digest(plan)}
        else:
            if args.plan is None or args.expected_plan is None:
                raise ValueError("integration_plan_required")
            plan = validate_plan(_read(args.plan), args.expected_plan)
            if digest(candidate_manifest(ROOT)) != plan["candidate_sha256"]:
                raise ValueError("integration_current_candidate_changed")
            remaining = (datetime.fromisoformat(plan["expires_at"]) - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                raise ValueError("integration_plan_expired")
            if args.action == "run":
                if args.host is None or args.directory is None:
                    raise ValueError("integration_run_arguments")
                result = run_scenario(plan, args.host, args.directory, remaining_seconds=remaining)
                _publish(args.directory / "report.json", result)
                result = {"report_path": str(args.directory / "report.json"), "source_head_sha256": digest(result),
                          "completed": result["failure"] is None, "release_published": False}
            else:
                if args.originals is None or args.expected_evidence is None:
                    raise ValueError("integration_originals_required")
                originals = _read(args.originals)
                pins = evidence_pins(plan, originals)
                with _deadline(min(remaining, 900)):
                    readers = host_readers(plan, originals)
                    def closing_check():
                        if digest(candidate_manifest(ROOT)) != plan["candidate_sha256"] or any(
                                digest(host_identity(h["route"])) != h["host_sha256"] for h in plan["hosts"]):
                            raise ValueError("integration_closing_candidate_or_host_changed")
                    if args.action == "replay":
                        if args.report is None:
                            raise ValueError("integration_report_required")
                        result = replay_decision(_read(args.report), args.expected_plan, args.expected_evidence, readers,
                                                 closing_check=closing_check)
                    else:
                        if args.output is None:
                            raise ValueError("integration_output_required")
                        result = decide(plan, args.expected_plan, pins, args.expected_evidence, readers,
                                        closing_check=closing_check)
                    if args.action == "decide":
                        _publish(args.output, result)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("outcome", "continue") == "continue" and result.get("completed", True) else 2
    except (ValueError, OSError, TypeError, KeyError, _HostDeadline) as error:
        print(json.dumps({"outcome": "stop" if isinstance(error, ValueError) and str(error) in INTEGRITY_ERRORS
                          else "revise", "reason": type(error).__name__, "release_published": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
