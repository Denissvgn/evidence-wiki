#!/usr/bin/env python3
"""Freeze, observe and independently replay bounded qualification jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools._acceptance_protocol import digest  # noqa: E402
from tools._integration_costs import freeze_cost_plan  # noqa: E402
from tools._qualification_process import interruptible  # noqa: E402
from tools._terminal_qualification import (  # noqa: E402
    ROLES,
    freeze_plan,
    ready_for_verification,
    reconstruct,
    require,
    validate_plan,
)
from tools.measure_integration_costs import _authenticated, _signed, observe_command  # noqa: E402
from tools.run_comparative_trials import checked_directory, publish_new, read_private  # noqa: E402


def _json(path):
    return json.loads(read_private(Path(path), 4_194_304))


def _new_directory(path):
    path = Path(path).absolute()
    require(path.resolve() == path and not path.is_relative_to(ROOT) and not ROOT.is_relative_to(path), "qualification_external_evidence_required")
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    return checked_directory(path)


def prepare(config, *, directory):
    plan = freeze_plan(**config)
    directory = _new_directory(directory)
    publish_new(directory / "plan.json", plan)
    return plan


def start(directory, *, observer_key):
    directory = checked_directory(Path(directory))
    plan = validate_plan(_json(directory / "plan.json"))
    commands = directory / "commands"
    workers = directory / "workers"
    commands.mkdir(mode=0o700)
    workers.mkdir(mode=0o700)
    instant = datetime.now(timezone.utc)
    deadline = instant + timedelta(seconds=plan["timeout_seconds"])
    cost_plan = freeze_cost_plan(scope_id="unattended-qualification", binding={"plan_sha256": plan["plan_sha256"]},
        sources=[{"source_id": "host", "provenance": "host_observed", "kind": "automated",
            "origin": "original qualification host job intervals; provider work remains unknown",
            "collection_started_at": instant.isoformat(), "collection_ended_at": deadline.isoformat(),
            "coverage": "complete", "authentication_ref": None}],
        attempts=[{"attempt_id": role.lower(), "phase": "qualification", "allocation": "repeated",
            "source_id": "host", "required_metrics": ["elapsed_seconds"]} for role in ROLES])
    value = {"schema_version": "unattended-qualification-execution/v1", "plan_sha256": plan["plan_sha256"],
             "started_utc": instant.isoformat(), "deadline_utc": deadline.isoformat(),
             "directory": str(commands), "work_root": str(workers), "cost_plan": cost_plan}
    publish_new(directory / "execution.json", _signed(value, observer_key))
    return value


def _execution(directory, plan, key):
    value = _authenticated(directory / "execution.json", key)
    require(value["schema_version"] == "unattended-qualification-execution/v1"
            and value["plan_sha256"] == plan["plan_sha256"]
            and value["directory"] == str(directory / "commands") and value["work_root"] == str(directory / "workers"), "qualification_execution_changed")
    start_at, deadline = (datetime.fromisoformat(value[name]) for name in ("started_utc", "deadline_utc"))
    require((deadline-start_at).total_seconds() == plan["timeout_seconds"], "qualification_deadline_changed")
    return value


def observe(directory, role, *, observer_key):
    """One actual agent-assigned job; reuse the host receipt lifecycle."""
    directory = checked_directory(Path(directory))
    plan = validate_plan(_json(directory / "plan.json"))
    execution = _execution(directory, plan, observer_key)
    job = next(row for row in plan["jobs"] if row["role"] == role)
    destination = directory / (role.lower() + ".observation.json")
    require(not destination.exists(), "qualification_job_already_observed")
    remaining = (datetime.fromisoformat(execution["deadline_utc"]) - datetime.now(timezone.utc)).total_seconds() - 12
    value, interruption = None, None
    try:
        if remaining > 0:
            value = observe_command(execution["cost_plan"], role.lower(), job["argv"], observer_key=observer_key,
                directory=execution["directory"], work_root=execution["work_root"],
                timeout_seconds=min(job["timeout_seconds"], remaining), input_bytes=b"")
    except BaseException as error:
        value = getattr(error, "cost_receipt", None)
        interruption = error
    refs = []
    for ref in job["reports"]:
        retained = {**ref, "sha256": None}
        try:
            raw = read_private(Path(ref["path"]), 4_194_304)
            retained["sha256"] = hashlib.sha256(raw).hexdigest()
            if ref["kind"] in {"attack", "automatic"}:
                retained["report_sha256"] = digest(json.loads(raw))
        except (ValueError, OSError, UnicodeError):
            pass
        refs.append(retained)
    observation = {"schema_version": "unattended-qualification-job/v1", "plan_sha256": plan["plan_sha256"],
        "role": role, "argv": job["argv"], "receipt_sha256": None if value is None else value["receipt_sha256"],
        "reports": refs, "stop_reason": "original_budget_exhausted" if remaining <= 0 else None}
    publish_new(destination, _signed(observation, observer_key))
    if interruption is not None:
        raise interruption
    return observation


def _observations(directory, key):
    return [_authenticated(directory / (role.lower() + ".observation.json"), key) for role in ROLES
            if (directory / (role.lower() + ".observation.json")).exists()]


def bind_provenance(directory, role, path, expected_sha256, *, observer_key):
    """Retain the root host's original actual agent/tool transcript commitment."""
    directory = checked_directory(Path(directory))
    raw = read_private(Path(path), 4_194_304)
    require(hashlib.sha256(raw).hexdigest() == expected_sha256, "qualification_tool_transcript_changed")
    publish_new(directory / (role.lower() + ".provenance.json"),
        _signed({"path": str(Path(path).resolve()), "sha256": expected_sha256}, observer_key))


def seal(directory, *, observer_key, preliminary=False):
    directory = checked_directory(Path(directory))
    plan = _json(directory / "plan.json")
    execution = _execution(directory, plan, observer_key)
    previous = None if preliminary else _authenticated(directory / "preliminary.json", observer_key)
    observations = _observations(directory, observer_key)
    if preliminary:
        require(not any(row["role"] == "Verification" for row in observations), "qualification_verification_already_started")
    result = reconstruct(plan, execution, observations, observer_key=observer_key, preliminary=previous)
    final = result.get("final_independent_obligations")
    if (not preliminary and final is not None and final["complete"] is False
            and result["failures"] == [] and result["gaps"] == ["qualification_final_manifest_not_published"]):
        manifest_path = directory / "evidence-manifest.json"
        require(final["path"] == str(manifest_path), "qualification_final_manifest_path")
        publish_new(manifest_path, _signed(final["manifest"], observer_key))
        manifest_path.chmod(0o400)
        result = reconstruct(plan, execution, observations, observer_key=observer_key, preliminary=previous)
    result["original_observations_sha256"] = digest(observations)
    result["execution_sha256"] = digest(execution)
    result["preliminary"] = preliminary
    result["finished_utc"] = datetime.now(timezone.utc).isoformat()
    path = directory / ("preliminary.json" if preliminary else "report.json")
    signed = _signed(result, observer_key)
    if result["decision"] == "qualified" and datetime.now(timezone.utc) > datetime.fromisoformat(execution["deadline_utc"]):
        result["decision"] = "not_qualified"
        result["gaps"].append("qualification_final_publication_deadline")
        final = result.get("final_independent_obligations")
        if final is not None:
            final["complete"] = False
            for duty in final["obligations"]:
                duty["status"] = "not_qualified"
        result["finished_utc"] = datetime.now(timezone.utc).isoformat()
        signed = _signed(result, observer_key)
    publish_new(path, signed)
    path.chmod(0o400)
    return {"report_path": str(path), "report_sha256": digest(signed), "decision": result["decision"]}


def replay(path, *, observer_key, expected_report_sha256):
    """External latest hash and original producer replays determine the result."""
    path = Path(path)
    original = _authenticated(path, observer_key)
    require(digest(original) == expected_report_sha256, "qualification_latest_report_changed")
    directory = path.parent
    plan = _json(directory / "plan.json")
    execution = _execution(directory, plan, observer_key)
    observations = _observations(directory, observer_key)
    if original["preliminary"]:
        observations = [row for row in observations if row["role"] != "Verification"]
    previous = None if original["preliminary"] else _authenticated(directory / "preliminary.json", observer_key)
    result = reconstruct(plan, execution, observations, observer_key=observer_key, preliminary=previous,
        as_of_utc=original["finished_utc"] if original["preliminary"] else None)
    require(all(original.get(key) == item for key, item in result.items())
            and original["original_observations_sha256"] == digest(observations)
            and original["execution_sha256"] == digest(execution), "qualification_original_replay_conflict")
    return original


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "start", "observe", "bind", "seal", "replay", "verify"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--observer-key", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--role", choices=ROLES)
    parser.add_argument("--preliminary", action="store_true")
    parser.add_argument("--expected-report")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.action == "freeze":
        require(args.config is not None, "qualification_config_required")
        value = prepare(_json(args.config), directory=args.directory)
        output = {"plan_sha256": value["plan_sha256"], "candidate_sha256": digest(value["candidate"])}
    else:
        require(args.observer_key is not None and not args.observer_key.absolute().is_relative_to(args.directory.absolute()), "qualification_external_key_required")
        key = read_private(args.observer_key, 128)
        require(32 <= len(key) <= 128, "qualification_observer_key")
        if args.action == "start":
            value = start(args.directory, observer_key=key)
            output = {"deadline_utc": value["deadline_utc"], "execution_sha256": digest(value)}
        elif args.action == "observe":
            require(args.role is not None, "qualification_role_required")
            value = observe(args.directory, args.role, observer_key=key)
            output = {"role": args.role, "receipt_sha256": value["receipt_sha256"]}
        elif args.action == "bind":
            require(args.role is not None and args.config is not None and args.expected_report is not None, "qualification_tool_provenance_required")
            bind_provenance(args.directory, args.role, args.config, args.expected_report, observer_key=key)
            output = {"role": args.role, "tool_transcript_sha256": args.expected_report}
        elif args.action == "seal":
            output = seal(args.directory, observer_key=key, preliminary=args.preliminary)
        elif args.action == "verify":
            require(args.config is not None and args.output is not None, "qualification_independent_anchor_required")
            anchor = _json(args.config)
            previous = replay(args.directory/"preliminary.json",observer_key=key,expected_report_sha256=anchor["report_sha256"])
            ready_for_verification(previous)
            output = {"schema_version":"qualification-verification/v1","plan_sha256":previous["plan_sha256"],
                "preliminary_sha256":digest(previous),"decision":previous["decision"]}
            require(anchor["plan_sha256"] == previous["plan_sha256"], "qualification_verifier_anchor_plan")
            publish_new(args.output,output)
        else:
            require(args.expected_report is not None, "qualification_external_latest_hash_required")
            path = args.directory / ("preliminary.json" if args.preliminary else "report.json")
            value = replay(path, observer_key=key, expected_report_sha256=args.expected_report)
            output = {"decision": value["decision"], "report_sha256": args.expected_report}
    print(json.dumps(output, sort_keys=True))
    return 0 if args.action == "verify" or output.get("decision", "qualified") == "qualified" else 2


if __name__ == "__main__":
    with interruptible():
        raise SystemExit(main())
