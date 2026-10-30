#!/usr/bin/env python3
"""Freeze and observe genuine repeated native owners in private custody.

Freeze after source review, retain the printed study/config pins externally,
then observe primary direct and current_caller in that order. Limited mode
retains all nine obligations; the three scoped_mcp slots remain missing.
Full mode adds primary scoped_mcp, decide, hidden replay, then a fresh scoped_mcp
audit. Every action is single use. No retry, authority extension, witness
renewal, provider meter, grant, installation or release is performed here.
The reporter argument pins an archival reference only. Actual current proof
uses the production replay_qualification_native source in the controller pin.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools._integration_costs import freeze_cost_plan, validate_cost_plan  # noqa: E402
from tools._integration_improvement import validate_plan  # noqa: E402
from tools._integration_improvement_owners import evidence_pins  # noqa: E402
from tools._integration_owner_pass import _defer_signals, _group_alive, source_manifest  # noqa: E402
from tools._qualification_process import interruptible  # noqa: E402
from tools._release_decision import _original, canonical, digest  # noqa: E402
from tools.measure_integration_costs import _authenticated, observe_command, read_command_receipt  # noqa: E402
from tools.run_comparative_trials import checked_directory, command_manifest, publish_new, read_private  # noqa: E402
from tools.verify_acceptance_paths import candidate_manifest  # noqa: E402

ROUTES = ("direct", "current_caller", "scoped_mcp")
LIMIT = 1_048_576
REGISTRY_LIMIT = 8_388_608
MAX_CHILDREN = 4096
NATIVE_RUN_SECONDS = 14400
ROUTE_SECONDS = 2700
DECISION_SECONDS = 900
RECIPE = {"native_run_seconds": NATIVE_RUN_SECONDS, "slot_seconds": 900,
          "route_observer_seconds": ROUTE_SECONDS, "decision_seconds": DECISION_SECONDS,
          "owner_pass_seconds": 120, "authority_seconds": 7200, "witness_seconds": 30,
          "job_and_mcp_seconds": 600, "owner_raw_bytes": LIMIT, "owner_frame_bytes": 2 * LIMIT,
          "native_artifact_bytes": 4 * LIMIT, "cleanup_seconds": 60,
          "denominator": 9, "retry_limit": 0}


def _policy_recipe(selected):
    if selected is None:
        return dict(RECIPE)
    return dict(RECIPE, **{name: selected[name] for name in (
        "native_run_seconds", "slot_seconds", "route_observer_seconds", "decision_seconds",
        "authority_seconds", "witness_seconds", "owner_pass_seconds", "cleanup_seconds")},
        study_seconds=selected["study_seconds"], replay_seconds=selected["replay_seconds"])


def _scope_options(plan, stage):
    if plan["scope"].get("native_cleanup_recipe") == "owned-popen-groups/v2":
        return {"native_child_scope": {"plan_sha256": digest(plan), "stage": stage}}
    return {}


def _cleanup_options(verified, pid, plan, stage):
    if _scope_options(plan, stage):
        from tools.measure_integration_costs import producer_cleanup_proof
        return {"producer_cleanup": producer_cleanup_proof(verified, worker_pid=pid,
            plan_sha256=digest(plan), stage=stage)}
    return {}


def require(value, reason):
    if not value:
        raise ValueError(reason)


def raw_pin(raw):
    return hashlib.sha256(raw).hexdigest()


def exception_token(error):
    """Never export a factory-selected subclass name or diagnostic string."""
    tokens = {ValueError: "ValueError", OSError: "OSError", FileNotFoundError: "FileNotFoundError",
              PermissionError: "PermissionError", TypeError: "TypeError", KeyError: "KeyError",
              RuntimeError: "RuntimeError", TimeoutError: "TimeoutError"}
    return tokens.get(type(error), "unknown")


def document(path, maximum=LIMIT):
    path = Path(path)
    require(path.is_absolute() and not any(p.is_symlink() for p in (path, *path.parents)),
            "native_custody_symlink")
    return _original(read_private(path, maximum))


def _external_root(path):
    path = Path(path)
    checked_directory(path)
    require(path.is_absolute() and path.resolve(strict=True) == path
            and path.is_relative_to(Path("/private/tmp")) and not path.is_relative_to(ROOT),
            "native_external_private_root")
    return path


def _adapter_pin():
    path = ROOT / "tools/run_native_owner_feasibility.py"
    require(path.is_file() and not path.is_symlink(), "native_adapter_source")
    return raw_pin(path.read_bytes())


def _package_manifest():
    import evidence_wiki
    package = Path(evidence_wiki.__file__).resolve().parent
    require(package == ROOT / "src/evidence_wiki", "native_package_origin")
    return {name: row for name, row in candidate_manifest(ROOT).items()
            if name.startswith("src/evidence_wiki/")}


def _source_identity(reporter):
    reporter = Path(reporter)
    require(reporter.is_absolute() and reporter.is_file()
            and not any(p.is_symlink() for p in (reporter, *reporter.parents)), "native_reporter_source")
    return {"candidate_sha256": digest(candidate_manifest(ROOT)),
            "controller_sha256": digest(source_manifest()), "adapter_raw_sha256": _adapter_pin(),
            "package_sha256": digest(_package_manifest()),
            "reference_reporter_path": str(reporter), "reference_reporter_raw_sha256": raw_pin(reporter.read_bytes()),
            "native_reporter_raw_sha256": raw_pin((ROOT / "tools/replay_qualification_native.py").read_bytes()),
            "interpreter_manifest": command_manifest([str(ROOT / ".venv/bin/python")])}


def _cleanup_binding(plan, action, host, directory):
    scope = plan["scope"]
    root = _external_root(scope["native_child_control_directory"])
    require(scope["native_child_adapter_raw_sha256"] == _adapter_pin()
            and scope["owner_read_controller_sha256"] == digest(source_manifest())
            and plan["candidate_sha256"] == digest(candidate_manifest(ROOT)), "native_cleanup_source_binding")
    require(scope["native_cleanup_recipe"] in {"owned-popen-groups/v1", "owned-popen-groups/v2"},
            "native_cleanup_recipe")
    if action == "run":
        selected = Path(directory)
        roles = [role for role in ("primary", "audit")
                 if selected == root / role / "work" / host / "scenario"]
        require(host in ROUTES and len(roles) == 1, "native_cleanup_runtime_root_binding")
        stage = "run-" + roles[0] + "-" + host
        expected = [str(ROOT / ".venv/bin/python"), str(ROOT / "tools/run_repeated_integrations.py"),
                    "run", "--plan", str(root / "control/study-plan.json"), "--expected-plan", digest(plan),
                    "--host", host, "--directory", str(selected)]
        require([str(Path(sys.executable).absolute()), *sys.argv] == expected,
                "native_cleanup_actual_producer_argv")
    else:
        require(action in {"decide", "replay"} and host is None and directory is None,
                "native_cleanup_action")
        stage = action
        originals = document(root / "control/originals.json")
        expected = [str(ROOT / ".venv/bin/python"), str(ROOT / "tools/run_repeated_integrations.py"), action,
                    "--plan", str(root / "control/study-plan.json"), "--expected-plan", digest(plan),
                    "--originals", str(root / "control/originals.json"),
                    "--expected-evidence", digest(evidence_pins(plan, originals))]
        actual = [str(Path(sys.executable).absolute()), *sys.argv]
        if action == "decide":
            matches = actual in (expected, expected + ["--output", str(root / "decisions/decision.json")])
        else:
            matches = actual == expected + ["--report", str(root / "decisions/decision.json")]
        require(matches,
                "native_cleanup_actual_owner_argv")
    checked_directory(root / "children")
    return root, stage


def _signal_owned_group(process, selected_signal):
    """Prevent a native watcher reaping/recycling the leader during signalling."""
    acquired = process._waitpid_lock.acquire(timeout=1)
    require(acquired, "native_child_wait_lock_unavailable")
    try:
        if process.returncode is not None:
            return
        try:
            require(os.getpgid(process.pid) == process.pid, "native_child_group_identity")
            os.killpg(process.pid, selected_signal)
        except ProcessLookupError:
            # The actual owned leader may have exited without being reaped.
            # Its next wait and read-only group check still must confirm close.
            pass
    finally:
        process._waitpid_lock.release()


def _stop_owned_group(process):
    """Bound leader reaping and never signal an already-reaped residual group."""
    _signal_owned_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _signal_owned_group(process, signal.SIGKILL)
        process.wait(timeout=5)
    require(process.returncode is not None and not _group_alive(process), "native_child_cleanup_unconfirmed")


@contextlib.contextmanager
def native_cleanup_context(plan, action, host=None, directory=None):
    """Own actual native Popen groups without changing native argv or results.

    The production CLI opts in only through this prospective source-bound scope.
    Registry PIDs are read-only evidence. Only actual live Popen objects owned
    by this process can be signalled; a reaped group is never signalled again.
    """
    if "native_child_control_directory" not in plan["scope"]:
        yield
        return
    root, stage = _cleanup_binding(plan, action, host, directory)
    path = root / "children" / (stage + ".jsonl")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    if plan["scope"].get("native_cleanup_recipe") == "owned-popen-groups/v2":
        from tools._native_child_registry import ChildTracker
        binding = {"schema_version": "native-owned-child/v2", "plan_sha256": digest(plan),
                   "control_directory": str(root), "stage": stage, "worker_pid": os.getpid(),
                   "adapter_raw_sha256": _adapter_pin(), "controller_sha256": digest(source_manifest()),
                   "candidate_sha256": plan["candidate_sha256"]}
        try:
            tracker = ChildTracker(descriptor, binding)
        except BaseException:
            os.close(descriptor)
            raise
        subprocess.Popen = tracker.popen
        try:
            with interruptible():
                yield tracker
        finally:
            # Keep the closed wrapper installed through producer termination.
            tracker.close(RECIPE["cleanup_seconds"])
        return
    real_popen, owned, lock = subprocess.Popen, [], threading.RLock()
    state = {"sequence": 0, "head": "0" * 64, "bytes": 0, "closed": False, "failed": False}
    binding = {"schema_version": "native-owned-child/v1", "plan_sha256": digest(plan),
               "control_directory": str(root), "stage": stage, "worker_pid": os.getpid(),
               "adapter_raw_sha256": _adapter_pin(), "controller_sha256": digest(source_manifest()),
               "candidate_sha256": plan["candidate_sha256"]}

    def append(kind, data):
        raw = canonical(binding | {"sequence": state["sequence"], "previous_sha256": state["head"],
                                   "kind": kind, "data": data})
        require(state["bytes"] + len(raw) <= REGISTRY_LIMIT, "native_child_registry_bound")
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            require(written > 0, "native_child_registry_write")
            offset += written
        os.fsync(descriptor)
        state.update(sequence=state["sequence"] + 1, head=raw_pin(raw), bytes=state["bytes"] + len(raw))

    def popen(*args, **kwargs):
        with lock, _defer_signals():
            require(not state["closed"] and len(owned) < MAX_CHILDREN, "native_child_launch_bound")
            require(kwargs.get("start_new_session") is True, "native_child_owned_session")
            process = real_popen(*args, **kwargs)
            owned.append(process)
            try:
                require(os.getpgid(process.pid) == process.pid, "native_child_group_identity")
                append("spawn", {"pid": process.pid, "pgid": process.pid})
            except BaseException:
                state["failed"] = True
                raise
            return process

    try:
        append("open", {"maximum_children": MAX_CHILDREN})
        subprocess.Popen = popen
        with interruptible():
            yield
    finally:
        # Close launch custody atomically, including late trusted callbacks.
        with lock, _defer_signals():
            state["closed"] = True
            cleanup_deadline = time.monotonic() + RECIPE["cleanup_seconds"]
            for process in owned:
                try:
                    if process.poll() is None:
                        # Reserve both bounded lock/wait attempts and closing
                        # group inventory checks before starting the stopper.
                        require(time.monotonic() + 17 <= cleanup_deadline, "native_child_cleanup_budget")
                        _stop_owned_group(process)
                    require(process.poll() is not None and not _group_alive(process),
                            "native_child_cleanup_unconfirmed")
                    append("cleanup", {"pid": process.pid, "pgid": process.pid, "returncode": process.returncode})
                except BaseException:
                    state["failed"] = True
            try:
                append("seal", {"spawn_count": len(owned), "cleanup_confirmed": not state["failed"]})
            finally:
                os.close(descriptor)
            # This process exits immediately after the CLI context. Keep the
            # closed wrapper installed so background callbacks cannot bypass it.
        if state["failed"]:
            raise RuntimeError("native_child_cleanup_unconfirmed")


def verify_child_registry(path, *, plan, stage, worker_pid, producer_cleanup=None):
    """Verify complete source-bound custody; never signal a registry PID."""
    root = Path(plan["scope"]["native_child_control_directory"])
    require(Path(path) == root / "children" / (stage + ".jsonl"), "native_child_registry_location")
    require(plan["scope"].get("native_cleanup_recipe") in {None, "owned-popen-groups/v1", "owned-popen-groups/v2"},
            "native_cleanup_recipe")
    raw = read_private(path, REGISTRY_LIMIT)
    if plan["scope"].get("native_cleanup_recipe") == "owned-popen-groups/v2":
        from tools._native_child_registry import verify_registry
        require(type(worker_pid) is int and worker_pid > 1, "native_child_worker_identity")
        # The observer authenticates this separate, exact-Popen producer proof.
        # A child-owned registry cannot make claims about its own parent custody.
        require(type(producer_cleanup) is dict and set(producer_cleanup) == {
            "worker_pid", "plan_sha256", "stage", "group_absent", "returncode", "command_receipt_sha256"}
            and type(producer_cleanup["worker_pid"]) is int
            and producer_cleanup["worker_pid"] == worker_pid
            and producer_cleanup["plan_sha256"] == digest(plan) and producer_cleanup["stage"] == stage
            and producer_cleanup["group_absent"] is True and type(producer_cleanup["returncode"]) is int
            and type(producer_cleanup["command_receipt_sha256"]) is str
            and len(producer_cleanup["command_receipt_sha256"]) == 64
            and all(c in "0123456789abcdef" for c in producer_cleanup["command_receipt_sha256"]),
            "native_child_producer_completion")
        binding = {"schema_version": "native-owned-child/v2", "plan_sha256": digest(plan),
                   "control_directory": str(root), "stage": stage, "worker_pid": worker_pid,
                   "adapter_raw_sha256": plan["scope"]["native_child_adapter_raw_sha256"],
                   "controller_sha256": plan["scope"]["owner_read_controller_sha256"],
                   "candidate_sha256": plan["candidate_sha256"]}
        return {"path": str(path), **verify_registry(raw, binding)}
    rows = raw.splitlines(keepends=True)
    require(2 <= len(rows) <= 2 * MAX_CHILDREN + 2, "native_child_registry_rows")
    binding = {"schema_version": "native-owned-child/v1", "plan_sha256": digest(plan),
               "control_directory": str(root), "stage": stage, "worker_pid": worker_pid,
               "adapter_raw_sha256": plan["scope"]["native_child_adapter_raw_sha256"],
               "controller_sha256": plan["scope"]["owner_read_controller_sha256"],
               "candidate_sha256": plan["candidate_sha256"]}
    require(type(worker_pid) is int and worker_pid > 1, "native_child_worker_identity")
    head, spawned, cleaned = "0" * 64, set(), set()
    for sequence, original in enumerate(rows):
        row = _original(original)
        require(canonical(row) == original and set(row) == set(binding) | {"sequence", "previous_sha256", "kind", "data"}
                and all(row[key] == value for key, value in binding.items())
                and row["sequence"] == sequence and row["previous_sha256"] == head,
                "native_child_registry_binding")
        data = row["data"]
        if sequence == 0:
            require(row["kind"] == "open" and data == {"maximum_children": MAX_CHILDREN}, "native_child_registry_open")
        elif sequence == len(rows) - 1:
            require(row["kind"] == "seal" and data == {"spawn_count": len(spawned), "cleanup_confirmed": True}
                    and spawned == cleaned, "native_child_registry_seal")
        elif row["kind"] == "spawn":
            require(set(data) == {"pid", "pgid"} and type(data["pid"]) is int and data["pid"] > 1
                    and data["pgid"] == data["pid"] and data["pid"] != worker_pid and data["pid"] not in spawned,
                    "native_child_registry_spawn")
            spawned.add(data["pid"])
        elif row["kind"] == "cleanup":
            require(set(data) == {"pid", "pgid", "returncode"} and type(data["returncode"]) is int
                    and data["pgid"] == data["pid"] and data["pid"] in spawned and data["pid"] not in cleaned,
                    "native_child_registry_cleanup")
            cleaned.add(data["pid"])
        else:
            raise ValueError("native_child_registry_kind")
        head = raw_pin(original)
    require(not _group_alive(SimpleNamespace(pid=worker_pid)) and all(
        not _group_alive(SimpleNamespace(pid=pid)) for pid in spawned), "native_child_group_still_live")
    return {"path": str(path), "raw_sha256": raw_pin(raw), "head_sha256": head,
            "spawn_count": len(spawned), "cleanup_confirmed": True}


def producer_argv(root, role, host, plan_sha256):
    return [str(ROOT / ".venv/bin/python"), str(ROOT / "tools/run_repeated_integrations.py"), "run",
            "--plan", str(root / "control/study-plan.json"), "--expected-plan", plan_sha256,
            "--host", host, "--directory", str(root / role / "work" / host / "scenario")]


def freeze_study(args):
    from tools._native_owner_execution import execution_policy
    from tools.run_repeated_integrations import reference_plan

    require(args.control is None, "native_freeze_new_root")
    identity = _source_identity(args.reporter)
    require(identity["candidate_sha256"] == args.expected_candidate
            and identity["controller_sha256"] == args.expected_controller
            and identity["adapter_raw_sha256"] == args.expected_adapter
            and identity["reference_reporter_raw_sha256"] == args.expected_reporter, "native_reviewed_source_pins")
    proposal_raw = read_private(args.proposals, LIMIT)
    require(raw_pin(proposal_raw) == args.expected_proposals_raw, "native_proposal_raw_pin")
    proposals = _original(proposal_raw)
    if type(proposals) is dict:
        proposals = proposals["native_proposals"]
    require(type(proposals) is list and all(p.get("capability_authorized") is False for p in proposals),
            "native_inert_proposals")
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="evidence-native-owner-", dir="/private/tmp")).resolve(strict=True)
    for name in ("control", "children", "invocations", "decisions"):
        (root / name).mkdir(mode=0o700)
    selected = (execution_policy("extended") if getattr(args, "execution_profile", "legacy") == "extended" else None)
    recipe = _policy_recipe(selected)
    plan = reference_plan(proposals, owner_read_workers=3)
    for slot in plan["slots"]:
        slot["elapsed_cap_seconds"] = recipe["slot_seconds"]
    plan["expires_at"] = (datetime.fromisoformat(plan["frozen_at"]) + timedelta(
        seconds=NATIVE_RUN_SECONDS if selected is None else selected["study_seconds"])).isoformat()
    plan["scope"].update(native_run_seconds=NATIVE_RUN_SECONDS,
        native_child_control_directory=str(root), native_child_adapter_raw_sha256=args.expected_adapter,
        native_cleanup_recipe="owned-popen-groups/v1" if selected is None else "owned-popen-groups/v2", native_study_mode=args.mode,
        reference_reporter_raw_sha256=args.expected_reporter,
        native_reporter_raw_sha256=identity["native_reporter_raw_sha256"])
    if selected is not None:
        plan["scope"].update(execution_policy=selected, execution_policy_sha256=digest(selected))
    validate_plan(plan, digest(plan))
    require(plan["candidate_sha256"] == args.expected_candidate
            and plan["scope"]["owner_read_controller_sha256"] == args.expected_controller,
            "native_source_changed_during_freeze")
    publish_new(root / "control/study-plan.json", plan)
    publish_new(root / "control/candidate-manifest.json", candidate_manifest(ROOT))
    publish_new(root / "control/controller-manifest.json", source_manifest())
    publish_new(root / "control/package-manifest.json", _package_manifest())
    config_pins = {}
    for role in ("primary", "audit"):
        base = root / role
        base.mkdir(mode=0o700)
        for name in ("control", "receipts", "work", "proof-work", "anchors"):
            (base / name).mkdir(mode=0o700)
        routes = ROUTES if role == "primary" and args.mode == "full" else (
            ("direct", "current_caller") if role == "primary" else ("scoped_mcp",))
        attempts = [{"attempt_id": h, "phase": "qualification", "allocation": "repeated",
                     "source_id": "native-command", "required_metrics": ["elapsed_seconds"]} for h in routes]
        if role == "primary":
            attempts += [{"attempt_id": h, "phase": "qualification", "allocation": "repeated",
                          "source_id": "native-command", "required_metrics": ["elapsed_seconds"]}
                         for h in ("decide", "replay")]
        source = {"source_id": "native-command", "provenance": "host_observed", "kind": "automated",
                  "origin": "local bounded native command observer", "collection_started_at": plan["frozen_at"],
                  "collection_ended_at": plan["expires_at"], "coverage": "partial",
                  "authentication_ref": "private-observer-custody"}
        cost_plan = freeze_cost_plan(scope_id="native-owner-" + role,
            binding={"integration_plan_sha256": digest(plan)}, sources=[source], attempts=attempts)
        key_path = base / "control/observer.key"
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(secrets.token_bytes(32))
        config = {"cost_plan": cost_plan, "jobs": {host: producer_argv(root, role, host, digest(plan)) for host in routes},
                  "key_path": str(key_path), "receipt_directory": str(base / "receipts"),
                  "work_root": str(base / "work"), "proof_work_root": str(base / "proof-work"),
                  "anchor_directory": str(base / "anchors")}
        publish_new(base / "control/runtime.json", config)
        config_pins[role] = digest(config)
    study = {"schema_version": "native-owner-study/v1" if selected is None else "native-owner-study/v2", "control_directory": str(root),
             "project": str(ROOT), "source_identity": identity, "plan_sha256": digest(plan),
             "config_pins": config_pins, "recipe": recipe, "mode": args.mode,
             "proposal_raw_sha256": args.expected_proposals_raw, "independent_audit_route": "scoped_mcp",
             "audit_fresh_custody": True, "native_execution_started": False}
    if selected is not None:
        study.update(execution_policy=selected, execution_policy_sha256=digest(selected))
    require(_source_identity(args.reporter) == identity, "native_source_changed_during_freeze")
    publish_new(root / "control/study.json", study)
    return {"control": str(root), "study_sha256": digest(study), "plan_sha256": digest(plan),
            "config_pins": config_pins, "native_execution_started": False, "denominator": 9}


def load_study(args):
    root = _external_root(args.control)
    study = document(root / "control/study.json")
    require(digest(study) == args.expected_study and study["control_directory"] == str(root)
            and study["project"] == str(ROOT)
            and study["mode"] in {"limited", "full"}, "native_study_external_pin")
    identity = _source_identity(Path(study["source_identity"]["reference_reporter_path"]))
    require(identity == study["source_identity"]
            and identity["candidate_sha256"] == args.expected_candidate
            and identity["controller_sha256"] == args.expected_controller
            and identity["adapter_raw_sha256"] == args.expected_adapter
            and identity["reference_reporter_raw_sha256"] == args.expected_reporter, "native_current_source_changed")
    require(digest(document(root / "control/candidate-manifest.json")) == args.expected_candidate
            and digest(document(root / "control/controller-manifest.json")) == args.expected_controller
            and digest(document(root / "control/package-manifest.json")) == identity["package_sha256"],
            "native_retained_source_changed")
    plan = validate_plan(document(root / "control/study-plan.json"), args.expected_plan)
    selected = plan["scope"].get("execution_policy")
    recipe = _policy_recipe(selected)
    require(study["recipe"] == recipe, "native_study_recipe")
    if selected is None:
        require(study["schema_version"] == "native-owner-study/v1"
                and "execution_policy" not in study and "execution_policy_sha256" not in study,
                "native_study_execution_policy")
    else:
        require(study["schema_version"] == "native-owner-study/v2" and study.get("execution_policy") == selected
                and study.get("execution_policy_sha256") == digest(selected)
                and plan["scope"]["native_cleanup_recipe"] == "owned-popen-groups/v2",
                "native_study_execution_policy")
        require(set(study) == {"schema_version", "control_directory", "project", "source_identity",
            "plan_sha256", "config_pins", "recipe", "mode", "proposal_raw_sha256",
            "independent_audit_route", "audit_fresh_custody", "native_execution_started",
            "execution_policy", "execution_policy_sha256"}, "native_study_execution_policy")
    require(digest(plan) == study["plan_sha256"] and plan["candidate_sha256"] == args.expected_candidate
            and len(plan["slots"]) == 9 and plan["scope"]["native_run_seconds"] == NATIVE_RUN_SECONDS
            and all(slot["elapsed_cap_seconds"] == recipe["slot_seconds"] for slot in plan["slots"])
            and plan["scope"]["owner_read_workers"] == 3
            and plan["scope"]["native_study_mode"] == study["mode"]
            and plan["scope"]["native_child_control_directory"] == str(root)
            and plan["scope"]["native_child_adapter_raw_sha256"] == args.expected_adapter
            and plan["scope"]["owner_read_controller_sha256"] == args.expected_controller,
            "native_study_envelope")
    require(datetime.now(timezone.utc) < datetime.fromisoformat(plan["expires_at"]), "native_study_expired")
    configs = {}
    for role in ("primary", "audit"):
        base = root / role
        config = document(base / "control/runtime.json")
        require(digest(config) == study["config_pins"][role], "native_runtime_external_pin")
        validate_cost_plan(config["cost_plan"])
        require(config["cost_plan"]["binding"] == {"integration_plan_sha256": args.expected_plan}, "native_cost_plan_binding")
        for field, suffix in (("key_path", "control/observer.key"), ("receipt_directory", "receipts"),
                              ("work_root", "work"), ("proof_work_root", "proof-work"), ("anchor_directory", "anchors")):
            require(config[field] == str(base / suffix), "native_runtime_path_binding")
        for host, argv in config["jobs"].items():
            require(argv == producer_argv(root, role, host, args.expected_plan), "native_exact_producer_argv")
        require(len(read_private(Path(config["key_path"]), 128)) == 32, "native_observer_key_size")
        configs[role] = config
    require(read_private(Path(configs["primary"]["key_path"]), 128)
            != read_private(Path(configs["audit"]["key_path"]), 128), "native_independent_audit_key")
    return root, study, plan, configs


def _invocation(root, name, argv, study, plan):
    value = {"schema_version": "native-owner-invocation/v1", "argv": argv,
             "command_manifest": command_manifest(argv), "study_sha256": digest(study),
             "plan_sha256": digest(plan), "source_identity": study["source_identity"],
             "started_at": datetime.now(timezone.utc).isoformat()}
    publish_new(root / "invocations" / (name + ".json"), value)
    return digest(value)


def _verify_observation(config, attempt, argv, receipt_pin, work_root, *, native_child_scope=None):
    key = read_private(Path(config["key_path"]), 128)
    options = {} if native_child_scope is None else {"native_child_scope": native_child_scope}
    verified = read_command_receipt(config["cost_plan"], attempt, argv, observer_key=key,
        directory=config["receipt_directory"], work_root=work_root, expected_receipt_sha256=receipt_pin, **options)
    require(verified["current_command_manifest_matches"], "native_observed_command_changed")
    start = _authenticated(Path(config["receipt_directory"]) / (attempt + ".start.json"), key)
    return verified, start["pid"]


def _authenticated_stdout(config, attempt, verified):
    """Parse one private read only after matching the authenticated stream pin."""
    receipt = verified["receipt"]
    require(type(receipt["exit_code"]) is int and receipt["exit_code"] in (0, 2)
            and receipt["status"] == ("succeeded" if receipt["exit_code"] == 0 else "failed")
            and receipt["failure"] is None and verified["coverage_gap"] is None
            and receipt["output_overflow"] is False
            and receipt["output_reader_work_unresolved"] is False
            and receipt["meter_work_unresolved"] is False and receipt["meter_id"] is None,
            "native_decision_observation_incomplete")
    path = Path(config["receipt_directory"]) / (attempt + ".stdout")
    require(path.is_absolute() and not any(p.is_symlink() for p in (path, *path.parents)),
            "native_custody_symlink")
    raw = read_private(path, LIMIT)
    stream = verified["receipt"]["streams"]["stdout"]
    require(len(raw) == stream["retained_bytes"] and raw_pin(raw) == stream["sha256"],
            "native_authenticated_stdout_changed")
    return _original(raw)


def full_report(value, plan, expected_evidence):
    """Accept only an entire native decision grammar with all nine slots."""
    fields = {"schema_version", "plan", "plan_sha256", "evidence_pins", "evidence_sha256",
              "outcome", "reasons", "slots", "ranked_improvements", "excluded_improvements",
              "metrics", "next_improvement", "limitations", "implementation_authorized",
              "release_published", "report_sha256"}
    try:
        return (type(value) is dict and set(value) == fields
        and value["schema_version"] == "integration-improvement-decision/v1"
        and value["plan_sha256"] == digest(plan) and canonical(value["plan"]) == canonical(plan)
        and value["evidence_sha256"] == expected_evidence and digest(value["evidence_pins"]) == expected_evidence
        and value["report_sha256"] == digest({k: v for k, v in value.items() if k != "report_sha256"})
        and value["outcome"] in {"continue", "revise", "stop", "narrow"}
        and value["implementation_authorized"] is False and value["release_published"] is False
        and type(value["slots"]) is list and len(value["slots"]) == 9
            and {row["slot"]["slot_id"] for row in value["slots"]} == {slot["slot_id"] for slot in plan["slots"]})
    except (ValueError, TypeError, KeyError):
        return False


def _originals(root, plan, configs):
    config, selected, inventory = configs["primary"], {}, []
    for host in ROUTES:
        path = root / "primary/anchors" / (host + ".json")
        producer_available, cleanup_confirmed = False, False
        if path.exists():
            anchor = document(path)
            pin = document(root / "control" / ("primary-" + host + "-pin.json"))
            require(digest(anchor) == pin["anchor_sha256"] and pin["plan_sha256"] == digest(plan)
                    and pin["runtime_control_sha256"] == digest(config), "native_anchor_external_pin")
            argv = config["jobs"][host]
            stage = "run-primary-" + host
            verified, pid = _verify_observation(config, host, argv, pin["receipt_sha256"], config["work_root"],
                **_scope_options(plan, stage))
            require(anchor["observation"] == verified, "native_anchor_receipt_pin")
            try:
                cleanup = verify_child_registry(root / "children" / ("run-primary-" + host + ".jsonl"),
                    plan=plan, stage=stage, worker_pid=pid, **_cleanup_options(verified, pid, plan, stage))
                require(anchor["cleanup"] == cleanup, "native_anchor_cleanup_pin")
                cleanup_confirmed = True
            except (ValueError, OSError, TypeError, KeyError, RuntimeError):
                # Retain the producer and nine obligations, but unconfirmed
                # process ownership cannot supply positive owner admission.
                cleanup_confirmed = False
            if anchor["original"] is not None:
                original = anchor["original"]
                raw = read_private(Path(original["path"]), LIMIT)
                require(raw_pin(raw) == original["raw_sha256"] and digest(_original(raw)) == original["source_head_sha256"],
                        "native_original_head_changed")
                producer_available = True
                if cleanup_confirmed:
                    selected[host] = original
        for slot in (s for s in plan["slots"] if s["host_id"] == host):
            inventory.append({"slot_id": slot["slot_id"], "host_id": host, "cycle": slot["cycle"],
                              "original_selected": host in selected, "producer_available": producer_available,
                              "cleanup_confirmed": cleanup_confirmed, "review_count": None, "acceptance": None})
    pins = evidence_pins(plan, selected)
    publish_new(root / "control/originals.json", selected)
    publish_new(root / "control/evidence-pins.json", pins)
    value = {"originals_sha256": digest(selected), "evidence_sha256": digest(pins), "plan_sha256": digest(plan),
             "inventory": inventory, "denominator": 9}
    publish_new(root / "control/originals-pin.json", value)
    return value


def verify_hidden_replay(root, study, plan, config, args):
    """Reauthenticate the exact replay before admitting a fresh audit launch."""
    retention = document(root / "decisions/replay-retention.json")
    require(digest(retention) == args.expected_replay, "native_external_replay_pin")
    raw = read_private(root / "decisions/decision.json", LIMIT)
    decision = _original(raw)
    originals = document(root / "control/originals.json")
    require(raw_pin(raw) == args.expected_decision_raw and digest(originals) == args.expected_originals
            and digest(evidence_pins(plan, originals)) == args.expected_evidence,
            "native_audit_current_external_pins")
    invocation = document(root / "invocations/replay.json")
    argv = [str(ROOT / ".venv/bin/python"), str(ROOT / "tools/run_repeated_integrations.py"), "replay",
            "--plan", str(root / "control/study-plan.json"), "--expected-plan", digest(plan),
            "--originals", str(root / "control/originals.json"), "--expected-evidence", args.expected_evidence,
            "--report", str(root / "decisions/decision.json")]
    require(invocation["argv"] == argv and invocation["command_manifest"] == command_manifest(argv)
            and invocation["study_sha256"] == digest(study) and invocation["source_identity"] == study["source_identity"]
            and retention["invocation_sha256"] == digest(invocation), "native_audit_replay_invocation")
    verified, pid = _verify_observation(config, "replay", argv, retention["receipt_sha256"], config["proof_work_root"],
        **_scope_options(plan, "replay"))
    cleanup = verify_child_registry(root / "children/replay.jsonl", plan=plan, stage="replay", worker_pid=pid,
        **_cleanup_options(verified, pid, plan, "replay"))
    output = _authenticated_stdout(config, "replay", verified)
    require(retention["verified"] is True and retention["output_hidden"] is True
            and retention["decision_raw_sha256"] == args.expected_decision_raw
            and retention["originals_sha256"] == args.expected_originals
            and retention["evidence_sha256"] == args.expected_evidence
            and retention["observation"] == verified and retention["cleanup"] == cleanup
            and retention["failure_classes"] == [] and verified["receipt"]["exit_code"] in (0, 2)
            and full_report(output, plan, args.expected_evidence) and full_report(decision, plan, args.expected_evidence)
            and canonical(output) == canonical(decision), "native_hidden_exact_replay_before_audit")


def execute(args):
    root, study, plan, configs = load_study(args)
    if args.action == "originals":
        _invocation(root, "originals", [str(ROOT / ".venv/bin/python"), str(Path(__file__).absolute()),
                                      *sys.argv[1:]], study, plan)
        return _originals(root, plan, configs)
    if args.action == "observe":
        require(args.role in configs and args.host in configs[args.role]["jobs"], "native_observer_selection")
        config = configs[args.role]
        require(args.expected_config == digest(config), "native_config_external_pin")
        if args.role == "primary":
            for previous in ROUTES[:ROUTES.index(args.host)]:
                require((root / "control" / ("primary-" + previous + "-pin.json")).exists(), "native_frozen_route_order")
        else:
            require(study["mode"] == "full" and args.host == "scoped_mcp", "native_preselected_audit_scope")
            verify_hidden_replay(root, study, plan, configs["primary"], args)
        argv, stage = config["jobs"][args.host], "run-" + args.role + "-" + args.host
        require(not Path(argv[-1]).exists(), "native_fresh_fixture_required")
        invocation = _invocation(root, "observe-" + args.role + "-" + args.host, argv, study, plan)
        observed = observe_command(config["cost_plan"], args.host, argv,
            observer_key=read_private(Path(config["key_path"]), 128), directory=config["receipt_directory"],
            work_root=config["work_root"], timeout_seconds=study["recipe"]["route_observer_seconds"],
            input_bytes=b"", meter=None, **_scope_options(plan, stage))
        cleanup, cleanup_error = None, None
        try:
            verified, pid = _verify_observation(config, args.host, argv, observed["receipt_sha256"], config["work_root"],
                **_scope_options(plan, stage))
            cleanup = verify_child_registry(root / "children" / (stage + ".jsonl"), plan=plan, stage=stage, worker_pid=pid,
                **_cleanup_options(verified, pid, plan, stage))
        except (ValueError, OSError, TypeError, KeyError, RuntimeError) as error:
            cleanup_error = exception_token(error)
        path = Path(argv[-1]) / "report.json"
        original = None
        if path.exists():
            raw = read_private(path, LIMIT)
            original = {"path": str(path), "raw_sha256": raw_pin(raw), "source_head_sha256": digest(_original(raw)),
                "observation": {"plan": config["cost_plan"], "attempt_id": args.host, "argv": argv,
                    "directory": config["receipt_directory"], "work_root": config["work_root"],
                    "expected_receipt_sha256": observed["receipt_sha256"], "observer_key_path": config["key_path"]}}
        anchor = {"host": args.host, "role": args.role, "observation": observed, "original": original,
                  "cleanup": cleanup, "cleanup_error_class": cleanup_error, "invocation_sha256": invocation}
        publish_new(Path(config["anchor_directory"]) / (args.host + ".json"), anchor)
        pin = {"anchor_sha256": digest(anchor), "receipt_sha256": observed["receipt_sha256"],
               "runtime_control_sha256": digest(config), "plan_sha256": digest(plan)}
        publish_new(root / "control" / (args.role + "-" + args.host + "-pin.json"), pin)
        return pin | {"status": observed["receipt"]["status"], "exit_code": observed["receipt"]["exit_code"],
                      "original_available": original is not None, "cleanup_confirmed": cleanup is not None}
    require(args.action in {"decide", "replay"}, "native_action")
    originals = document(root / "control/originals.json")
    pins = evidence_pins(plan, originals)
    require(digest(originals) == args.expected_originals and digest(pins) == args.expected_evidence
            and digest(document(root / "control/evidence-pins.json")) == args.expected_evidence,
            "native_external_evidence_pins")
    config = configs["primary"]
    argv = [str(ROOT / ".venv/bin/python"), str(ROOT / "tools/run_repeated_integrations.py"), args.action,
            "--plan", str(root / "control/study-plan.json"), "--expected-plan", digest(plan),
            "--originals", str(root / "control/originals.json"), "--expected-evidence", args.expected_evidence]
    decision_path = root / "decisions/decision.json"
    if args.action == "decide":
        require(not decision_path.exists(), "native_new_decision_required")
    else:
        require(raw_pin(read_private(decision_path, LIMIT)) == args.expected_decision_raw,
                "native_external_decision_pin")
        argv += ["--report", str(decision_path)]
    invocation = _invocation(root, args.action, argv, study, plan)
    observed = observe_command(config["cost_plan"], args.action, argv,
        observer_key=read_private(Path(config["key_path"]), 128), directory=config["receipt_directory"],
        work_root=config["proof_work_root"],
        timeout_seconds=study["recipe"].get("replay_seconds", DECISION_SECONDS) if args.action == "replay" else study["recipe"]["decision_seconds"],
        input_bytes=b"", meter=None, **_scope_options(plan, args.action))
    failures, cleanup, output, decision = [], None, None, None
    verified_observation, decision_raw_pin = None, None
    try:
        verified_observation, pid = _verify_observation(config, args.action, argv, observed["receipt_sha256"], config["proof_work_root"],
            **_scope_options(plan, args.action))
        cleanup = verify_child_registry(root / "children" / (args.action + ".jsonl"),
            plan=plan, stage=args.action, worker_pid=pid, **_cleanup_options(verified_observation, pid, plan, args.action))
        if args.action == "decide":
            output = _authenticated_stdout(config, args.action, verified_observation)
            require(full_report(output, plan, args.expected_evidence), "native_full_decision_required")
            publish_new(decision_path, output)
            decision_raw = read_private(decision_path, LIMIT)
            decision = _original(decision_raw)
            require(full_report(decision, plan, args.expected_evidence) and canonical(output) == canonical(decision)
                    and decision_raw == canonical(output), "native_decision_publication_changed")
            decision_raw_pin = raw_pin(decision_raw)
        else:
            output = _authenticated_stdout(config, args.action, verified_observation)
            decision = document(decision_path)
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as error:
        failures.append(exception_token(error))
    exact = (cleanup is not None and full_report(output, plan, args.expected_evidence)
             and full_report(decision, plan, args.expected_evidence) and canonical(output) == canonical(decision))
    if args.action == "replay":
        decision_raw_pin = raw_pin(read_private(decision_path, LIMIT)) if decision_path.exists() else None
    require(args.action != "replay" or decision_raw_pin == args.expected_decision_raw, "native_decision_changed_during_replay")
    verified = exact and not failures and verified_observation["receipt"]["exit_code"] in (0, 2)
    # Producer proof has consumed authenticated provenance; retain only its JSON fields.
    retention = {"plan_sha256": digest(plan), "originals_sha256": args.expected_originals,
        "evidence_sha256": args.expected_evidence, "invocation_sha256": invocation,
        "receipt_sha256": observed["receipt_sha256"],
        "observation": dict(observed if verified_observation is None else verified_observation), "cleanup": cleanup,
        "failure_classes": failures, "decision_raw_sha256": decision_raw_pin,
        "decision_report_sha256": decision["report_sha256"] if exact else None,
        "output_hidden": args.action == "replay", "verified": bool(verified),
        "provider_usage": None, "whole_host_cost": None, "denominator": 9}
    publish_new(root / "decisions" / (args.action + "-retention.json"), retention)
    return retention | {"retention_sha256": digest(retention)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "observe", "originals", "decide", "replay"))
    parser.add_argument("--control", type=Path)
    parser.add_argument("--mode", choices=("limited", "full"), default="limited")
    parser.add_argument("--execution-profile", choices=("legacy", "extended"), default="legacy")
    parser.add_argument("--reporter", type=Path)
    parser.add_argument("--proposals", type=Path)
    for name in ("candidate", "controller", "adapter", "reporter"):
        parser.add_argument("--expected-" + name, required=True)
    for name in ("study", "plan", "config", "proposals-raw", "originals", "evidence", "decision-raw", "replay"):
        parser.add_argument("--expected-" + name)
    parser.add_argument("--role", choices=("primary", "audit"))
    parser.add_argument("--host", choices=ROUTES)
    args = parser.parse_args(argv)
    try:
        require(Path(sys.executable).absolute() == ROOT / ".venv/bin/python", "native_project_interpreter")
        result = freeze_study(args) if args.action == "freeze" else execute(args)
        print(json.dumps(result, sort_keys=True))
        return 2 if (result.get("verified") is False or result.get("cleanup_confirmed") is False
                     or result.get("status") == "failed") else 0
    except (ValueError, OSError, TypeError, KeyError, RuntimeError) as error:
        # No arbitrary exception text, subprocess diagnostics or credentials.
        print(json.dumps({"status": "unavailable", "exception_class": exception_token(error),
                          "provider_usage": None, "denominator": 9}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
