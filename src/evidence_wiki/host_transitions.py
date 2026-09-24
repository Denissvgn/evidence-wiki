"""Explicit resumable host changes between fixed-requirement parent sessions."""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path

from ._pack_io import canonical, json_document, read_file
from .extension_contracts import validate
from .local_journal import Journal
from .pack_discovery import owner
from .pack_migrations import _call
from .planning_contracts import refuse
from .runtime_identity import generation, require_runtime

REQUEST = "evidence-host-transition-request/v1"
PLAN = "evidence-host-transition-plan/v1"


def request(raw):
    from . import pack_migrations, pack_revision_contracts, planning_contracts

    value = validate(raw, REQUEST)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "transition_id", "control_root", "target",
            "previous_session", "next_session", "setup_plan", "revision_plan", "reevaluations"}
            or value["schema_version"] != REQUEST):
        refuse("host_transition_request_shape")
    if (not all(isinstance(value[key], str) and value[key] for key in ("transition_id", "control_root", "target"))
            or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}", value["transition_id"]) is None
            or value["previous_session"] is not None and not isinstance(value["previous_session"], str)
            or not isinstance(value["next_session"], dict) or set(value["next_session"]) != {"agent_id", "orchestration_id"}
            or not all(isinstance(v, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}", v) for v in value["next_session"].values())
            or not isinstance(value["reevaluations"], list) or len(value["reevaluations"]) > 300
            or any(not isinstance(row, dict) for row in value["reevaluations"])
            or any(value[key] is not None and not isinstance(value[key], dict) for key in ("setup_plan", "revision_plan"))):
        refuse("host_transition_request_invalid")
    if value["previous_session"] is not None and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}", value["previous_session"]) is None:
        refuse("host_transition_previous_session_invalid")
    if value["setup_plan"] is not None:
        planning_contracts.decode(canonical(value["setup_plan"]), planning_contracts.PLAN)
    if value["revision_plan"] is not None:
        revised = value["revision_plan"]
        module = pack_migrations if revised.get("schema_version") == pack_migrations.PLAN else pack_revision_contracts
        module.decode(canonical(revised), module.PLAN)
        if not isinstance(revised["owner_plan"].get("pack"), dict) or not isinstance(revised["owner_plan"]["pack"].get("name"), str):
            refuse("host_transition_revision_shape")
    for document in value["reevaluations"]:
        pack_revision_contracts.decode(canonical(document), pack_revision_contracts.REEVALUATION)
    if len({row["slug"] for row in value["reevaluations"]}) != len(value["reevaluations"]):
        refuse("host_transition_duplicate_question_reevaluation")
    return value


def decode(raw):
    value = validate(raw, PLAN)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "request", "before", "generation", "assurance",
            "protected_parent", "release_accepted", "plan_id"} or value["schema_version"] != PLAN
            or value["plan_id"] != owner("_pack_revision_impact").digest({k: v for k, v in value.items() if k != "plan_id"})):
        refuse("host_transition_plan_changed", "ONBOARDING_PLAN_STALE")
    request(canonical(value["request"]))
    return value


def _target(value):
    root = Path(value["target"]).expanduser().absolute()
    control = Path(value["control_root"]).expanduser().absolute()
    if root.is_symlink() or control.is_symlink() or not control.is_dir():
        refuse("host_transition_root_unsafe")
    root, control = root.resolve(), control.resolve()
    if control.is_relative_to(root) or root.is_relative_to(control / "host-transitions"):
        refuse("host_transition_journal_overlaps_workspace")
    return root, control


def _session(root, identifier):
    relative = "runs/orchestrations/" + identifier + "/session.json"
    document = json_document(read_file(root, relative))
    if not isinstance(document, dict) or document.get("orchestration_id") != identifier or not isinstance(document.get("agent_id"), str):
        refuse("host_transition_session_identity_invalid", "ONBOARDING_PLAN_STALE")
    return document


def _files(root):
    if not root.exists():
        return {}
    capture = owner("_evidence_revision").capture_workspace(root)
    guard = owner("_pack_revision_guard")
    return {name: hashlib.sha256(raw).hexdigest() for name, raw in capture.files.items() if guard.research_input(name)}


def _effect_check(before, after, allowed):
    for name in set(before) | set(after):
        if before.get(name) != after.get(name) and not any(name == item or item.endswith("/") and name.startswith(item) for item in allowed):
            refuse("host_transition_unrelated_inputs_changed", "ONBOARDING_PLAN_STALE")


def _state(value, selected, plan_id):
    sequence = (["setup"] if selected["setup_plan"] is not None else []) + (["revision"] if selected["revision_plan"] is not None else [])
    sequence += ["reevaluate-" + str(index) for index in range(len(selected["reevaluations"]))] + ["start"]
    if (not isinstance(value, dict) or set(value) != {"schema_version", "plan_id", "completed", "pending", "results", "status", "start_basis", "last_files", "pending_before"}
            or value["schema_version"] != "evidence-host-transition-state/v1" or value["plan_id"] != plan_id
            or not isinstance(value["completed"], list) or value["completed"] != sequence[:len(value["completed"])]
            or not isinstance(value["last_files"], dict) or not isinstance(value["results"], list)
            or value["status"] not in {"applying", "complete"}
            or (value["status"] == "complete") != (value["completed"] == sequence)
            or value["pending"] is not None and (len(value["completed"]) >= len(sequence) or value["pending"] != sequence[len(value["completed"])])):
        refuse("host_transition_journal_invalid")
    expected = [step for step in value["completed"] if step != "start"]
    if (len(value["results"]) != len(expected) or any(not isinstance(row, dict) or set(row) != {"step", "result"}
            or row["step"] != step or not isinstance(row["result"], dict) for row, step in zip(value["results"], expected, strict=True))):
        refuse("host_transition_journal_results_invalid")


def _completed_proofs(root, selected, completed):
    if "setup" in completed:
        from .setup_application import postconditions
        from .setup_worker import STEPS

        postconditions(root, selected["setup_plan"], STEPS)
    if "revision" in completed:
        from .pack_revisions import verify_replay

        _call(verify_replay, selected["revision_plan"])
    for index, document in enumerate(selected["reevaluations"]):
        if "reevaluate-" + str(index) not in completed:
            continue
        coverage = owner("coverage_manifest")
        config = coverage.load_config(root)
        _, manifest = _call(coverage.load_manifest, root, config, document["slug"])
        if (not manifest or manifest.get("revision_basis", {}).get("migration_id") != owner("_pack_revision_impact").digest(document)
                or owner("_pack_revision_guard").pending_question(root, document["slug"], manifest) is not None):
            refuse("host_transition_reevaluation_postcondition_missing")


def plan(raw):
    from .planning import check_plan

    value = request(raw)
    root, _ = _target(value)
    guard = owner("_pack_revision_guard")
    if value["setup_plan"] is not None:
        setup = value["setup_plan"]
        checked = check_plan(canonical(setup))
        selected = setup["bindings"]["target"]["target"]
        if (Path(selected["writable_root"]) / selected["relative_path"]).resolve() != root or root.exists():
            refuse("host_setup_requires_its_original_absent_target")
        if value["previous_session"] is not None or value["revision_plan"] is not None or value["reevaluations"]:
            refuse("host_setup_cannot_rebind_existing_research")
        before = {"setup_plan_id": setup["plan_id"], "check": checked}
    else:
        require_runtime(root)
        config = _call(owner("_domain_pack_lifecycle").load_mapping, root / "research.yml", "configuration")
        if not owner("_caller_context").builtin_integrations(config):
            refuse("host_transition_registered_execution_requires_separate_qualification", "ONBOARDING_AUTHORITY_REQUIRED")
        before = {"research": _call(guard.idle, root), "controls": guard.controls(root)}
        _call(owner("_usage_gate").require_host_intake, owner("_domain_pack_lifecycle").load_mapping(root / "research.yml", "configuration"))
        if value["previous_session"] is not None:
            previous = _session(root, value["previous_session"])
            if previous.get("status") in {"active", "paused"} or previous.get("pending_action_id"):
                refuse("host_transition_previous_session_active")
        if value["revision_plan"] is not None and Path(value["revision_plan"]["request"]["target"]).resolve() != root:
            refuse("host_transition_revision_target_mismatch")
    if (root / "runs/orchestrations" / value["next_session"]["orchestration_id"]).exists():
        refuse("host_transition_next_session_already_exists", "ONBOARDING_TARGET_CONFLICT")
    result = {"schema_version": PLAN, "request": copy.deepcopy(value), "before": before,
              "generation": generation(), "assurance": "artifact_checked", "protected_parent": "unsupported",
              "release_accepted": False}
    result["plan_id"] = owner("_pack_revision_impact").digest(result)
    return result


def apply(raw):
    from . import orchestration, pack_migrations, pack_revisions, setup_application

    value = decode(raw)
    if (not isinstance(value, dict) or value.get("schema_version") != PLAN
            or value.get("plan_id") != owner("_pack_revision_impact").digest({k: v for k, v in value.items() if k != "plan_id"})
            or value.get("generation") != generation()):
        refuse("host_transition_plan_or_generation_changed", "ONBOARDING_PLAN_STALE")
    selected = request(canonical(value["request"]))
    root, control = _target(selected)
    journal = Journal(control, "host-transitions", selected["transition_id"], value)
    guard = owner("_pack_revision_guard")
    with journal.locked():
        state = journal.read()
        if state is None:
            if plan(canonical(selected)) != value:
                refuse("host_transition_inputs_changed", "ONBOARDING_PLAN_STALE")
            state = {"schema_version": "evidence-host-transition-state/v1", "plan_id": value["plan_id"],
                     "completed": [], "pending": None, "results": [], "status": "applying", "start_basis": None,
                     "last_files": _files(root), "pending_before": None}
            journal.write(state)
        _state(state, selected, value["plan_id"])
        if state["status"] == "complete":
            existing = _session(root, selected["next_session"]["orchestration_id"])
            if (existing.get("host_transition_id") != value["plan_id"][7:] or existing.get("requirement_basis") != state["start_basis"]
                    or existing["agent_id"] != selected["next_session"]["agent_id"]):
                refuse("host_transition_session_changed", "ONBOARDING_PLAN_STALE")
            return {"status": "already_complete", "plan_id": value["plan_id"], "session": existing["orchestration_id"],
                    "release_accepted": False, "receipt": str(journal.path / "state.json")}
        _completed_proofs(root, selected, state["completed"])
        steps = []
        if selected["setup_plan"] is not None:
            steps.append(("setup", lambda: setup_application.apply_plan(canonical(selected["setup_plan"])), None))
        if selected["revision_plan"] is not None:
            revised = selected["revision_plan"]
            handler = pack_migrations.apply if revised["schema_version"] == pack_migrations.PLAN else pack_revisions.apply
            steps.append(("revision", lambda: handler(canonical(revised)),
                ["research.yml", "log.md", "domain-packs/.evidence-wiki-state.yml", "domain-packs/" + revised["owner_plan"]["pack"]["name"] + "/"]))
        for index, document in enumerate(selected["reevaluations"]):
            config = owner("_domain_pack_lifecycle").load_mapping(root / "research.yml", "configuration")
            question = owner("question_status").questions_directory(root, config) / (document["slug"] + ".md")
            frontmatter = owner("question_status").load_frontmatter(question)
            manifest = owner("coverage_manifest").selected_manifest_path(root, config, document["slug"], frontmatter.get("coverage_manifest"))
            steps.append(("reevaluate-" + str(index), lambda document=document: _call(owner("coverage_manifest").run_revision, root, document),
                [question.relative_to(root).as_posix(), manifest.relative_to(root).as_posix(), "runs/pack-revisions/" + document["revision_id"][7:] + "/"]))
        for name, operation, allowed in steps:
            if name in state["completed"]:
                continue
            if state["pending"] is None:
                if _files(root) != state["last_files"]:
                    refuse("host_transition_inputs_changed", "ONBOARDING_PLAN_STALE")
                state["pending"], state["pending_before"] = name, state["last_files"]
                journal.write(state)
            elif state["pending"] != name:
                refuse("host_transition_checkpoint_order_invalid")
            result = operation()
            if name == "setup" and not result.get("setup_ready"):
                return {"status": "blocked_setup", "result": result, "receipt": str(journal.path / "state.json"), "release_accepted": False}
            after = _files(root)
            if allowed is not None:
                _effect_check(state["pending_before"], after, allowed)
            state["last_files"] = after
            state["results"].append({"step": name, "result": result})
            state["completed"].append(name)
            state["pending"] = None
            journal.write(state)
        require_runtime(root)
        identifier = selected["next_session"]["orchestration_id"]
        path = root / "runs/orchestrations" / identifier / "session.json"
        if state["pending"] != "start":
            if _files(root) != state["last_files"]:
                refuse("host_transition_inputs_changed", "ONBOARDING_PLAN_STALE")
            _call(guard.idle, root)
            state["pending"], state["start_basis"] = "start", guard.controls(root)
            state["pending_before"] = state["last_files"]
            journal.write(state)
        if path.exists():
            session = _session(root, identifier)
            if (session.get("host_transition_id") != value["plan_id"][7:]
                    or session.get("requirement_basis") != state["start_basis"]
                    or session.get("agent_id") != selected["next_session"]["agent_id"]):
                refuse("host_transition_session_collision", "ONBOARDING_TARGET_CONFLICT")
        else:
            _call(guard.idle, root)
            if guard.controls(root) != state["start_basis"]:
                refuse("host_transition_start_basis_changed", "ONBOARDING_PLAN_STALE")
            from ._facades.orchestrate import _typed_errors

            with _typed_errors("start"):
                orchestration.protocol_start(root, selected["next_session"]["agent_id"], orchestration_id=identifier,
                                             host_transition_id=value["plan_id"][7:])
            session = _session(root, identifier)
            if session.get("requirement_basis") != state["start_basis"]:
                refuse("host_transition_start_basis_changed", "ONBOARDING_PLAN_STALE")
        _effect_check(state["pending_before"], _files(root), ["runs/orchestrations/" + identifier + "/"])
        state.update(status="complete", pending=None)
        state["completed"].append("start")
        journal.write(state)
        return {"status": "complete", "plan_id": value["plan_id"], "session": identifier,
                "receipt": str(journal.path / "state.json"), "release_accepted": False,
                "next": "Drive the new session through its controller; old approvals retain their original basis."}
