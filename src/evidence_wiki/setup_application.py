"""Plan-bound local setup with conservative recovery and recomputed observations."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ._pack_io import canonical, json_document, read_file, yaml_document
from .agent_resources import installation_metadata
from .errors import EvidenceWikiError
from .host_context import current_context
from .orchestration import _execute_bounded
from .pack_discovery import owner
from .planning import compile_plan, plan_identity
from .planning_contracts import MAX_BYTES, PLAN, decode, digest, refuse
from .planning_inputs import installation_basis, target_basis
from .setup_contracts import CHECKPOINT, RESULT, STATUS, checked
from .setup_effects import verify_effects
from .setup_store import SetupStore, snapshot
from .setup_worker import MUTATIONS, REQUIRED, STEPS, local_paths


def now():
    return datetime.now(timezone.utc).isoformat()


def worker_environment(plan):
    """Contain fixed local setup children without borrowing ambient authority."""
    context = current_context()
    if context is None:
        return {}
    if (context.authority_file is not None or context.state_directory is not None
            or context.mutation_control_directory is not None
            or plan["initialization"]["effective_config"].get("evidence_trust")):
        refuse("setup_explicit_protected_worker_bridge_unavailable", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
    platform_keys = ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP")
    return {"environment": {key: os.environ[key] for key in platform_keys if key in os.environ},
            "inherit_environment": False}


def research_compatibility(plan):
    """Describe frozen scope compatibility without qualifying a claim or reviewer."""
    target = Path(plan["bindings"]["target"]["target"]["writable_root"]) / plan["bindings"]["target"]["target"]["relative_path"]
    payload = plan["request"]["request"]["payload"]
    gaps = []
    if not payload["authority"]["source_scope"]:
        gaps.append("frozen_source_scope_missing")
    if any(row["kind"] != "local_file" for row in payload["sources"]):
        gaps.append("non_native_source_kind_not_supported_by_native_admission_profile")
    for value in [*payload["authority"]["source_scope"], *(row["locator"] for row in payload["sources"])]:
        path = Path(value)
        if "://" in value or not path.is_absolute() or ".." in path.parts or not path.is_relative_to(target):
            gaps.append("frozen_source_scope_outside_native_workspace_profile")
            break
    if plan["computation"] is not None:
        gaps.append("computation_not_supported_by_native_admission_profile")
    if any(len(row["original_ids"]) != 1 for row in plan["questions"]["rows"]):
        gaps.append("multi_origin_mapping_not_supported_by_native_admission_profile")
    return {"profile": "native_draft_admission", "state": "blocked" if gaps else "candidate", "gaps": gaps}


def workspace_versions(target):
    """Read only bounded version declarations from an existing selected target."""
    import re

    installed = installation_metadata()
    result = {"installation": installed, "workspace": None, "skew": None}
    marker = target / "workspace-system.yml"
    if marker.is_symlink():
        refuse("setup_workspace_metadata_invalid", "ONBOARDING_OWNERSHIP_CONFLICT")
    if not marker.exists():
        return result
    try:
        document = yaml_document(read_file(target, "workspace-system.yml", 65536))
    except FileNotFoundError:
        return result
    if type(document) is not dict:
        refuse("setup_workspace_metadata_invalid", "ONBOARDING_OWNERSHIP_CONFLICT")
    metadata = document.get("workspace_system")
    if type(metadata) is not dict:
        refuse("setup_workspace_metadata_invalid", "ONBOARDING_OWNERSHIP_CONFLICT")
    keys = {"starter_version": "starter_version", "schema_version": "profile_schema_version",
            "compatible_research_yml_contract": "research_contract_version"}
    observed = {}
    for name in keys:
        value = metadata.get(name)
        if value is not None and (type(value) is not str or re.fullmatch(r"[0-9][A-Za-z0-9.+-]{0,63}", value) is None):
            refuse("setup_workspace_metadata_invalid", "ONBOARDING_OWNERSHIP_CONFLICT")
        observed[name] = value
    mismatch = any(observed[name] is not None and observed[name] != installed[key] for name, key in keys.items())
    result.update(workspace=observed, skew=True if mismatch else False if all(observed.values()) else None)
    return result


def status_plan(raw):
    """Observe exact existing setup ownership; archived checks never become current readiness."""
    plan = decode(raw, PLAN)
    if plan_identity(plan) != plan["plan_id"]:
        refuse("saved_plan_identity_changed", "ONBOARDING_PLAN_STALE")
    selected = plan["request"]["request"]["payload"]["target"]
    binding = plan["bindings"].get("target")
    if (type(binding) is not dict or set(binding) != {"target", "root_identity", "parent_identity", "state", "directory_identity"}
            or binding.get("target") != selected or type(binding["root_identity"]) is not dict
            or type(binding["parent_identity"]) is not dict or type(binding["state"]) is not str
            or binding["state"] not in {"absent", "empty"}):
        refuse("setup_target_binding_changed", "ONBOARDING_PLAN_STALE")
    result = {"schema_version": STATUS, "plan_id": plan["plan_id"],
        "target": str(Path(selected["writable_root"]) / selected["relative_path"]),
        "status": "not_applied", "transaction_id": None, "checkpoint_state": None, "pending": None,
        "completed": [], "snapshot_matches": None, "ownership_valid": False, "recorded_setup_ready": None,
        "versions": {"installation": installation_metadata(), "workspace": None, "skew": None}, "guidance": ["apply_same_plan"],
        "setup_ready": None, "recovery": "apply", "diagnostics": [], "observed_at": now(),
        "receipt_trust": "local_observation", "research_compatibility": {
            "profile": "native_draft_admission", "state": "unknown", "gaps": ["setup_plan_not_validated"]},
        "actions_executed": False, "authority_verified": False, "research_complete": False, "claims_verified": False}
    try:
        target_basis(selected, owned_basis=binding)
        result["versions"] = workspace_versions(Path(result["target"]))
        current = revalidate(plan, owned=True, read_only=True)
        result["research_compatibility"] = research_compatibility(current)
        store = SetupStore(plan)
        with store.inspected():
            if store.transaction is None:
                if target_basis(selected)[1] != plan["bindings"]["target"]:
                    refuse("setup_target_unowned_or_changed", "ONBOARDING_OWNERSHIP_CONFLICT")
                revalidate(plan, read_only=True)
                result.update(ownership_valid=True, snapshot_matches=True, setup_ready=False)
            else:
                checkpoint = store.read("checkpoint.json")
                checkpoint_valid(checkpoint)
                result.update(transaction_id=store.transaction_id, checkpoint_state=checkpoint["state"],
                              pending=checkpoint["pending"], completed=checkpoint["completed"])
                revalidate(plan, owned=True, read_only=True)
                captured = snapshot(store.target)
                if captured != checkpoint["snapshot"]:
                    result["snapshot_matches"] = False
                    from .requirements_operations import setup_changes_owned
                    try:
                        revised = setup_changes_owned(store.target, checkpoint["snapshot"], captured)
                    except (ValueError, EvidenceWikiError, TypeError, KeyError):
                        revised = False
                    if (checkpoint["state"] != "complete" or checkpoint["pending"] is not None
                            or not revised):
                        refuse("setup_owned_files_changed_or_interrupted_write", "ONBOARDING_OWNERSHIP_CONFLICT")
                postconditions(store.target, plan, checkpoint["completed"])
                try:
                    saved = store.read("receipt.json")
                except FileNotFoundError:
                    saved = None
                if saved is not None:
                    checked(saved, RESULT)
                    if saved["plan_id"] != plan["plan_id"] or saved["transaction_id"] != store.transaction_id:
                        refuse("setup_receipt_binding_changed", "ONBOARDING_OWNERSHIP_CONFLICT")
                    result["recorded_setup_ready"] = saved["setup_ready"]
                state = checkpoint["state"]
                result.update(status="interrupted" if checkpoint["pending"] is not None or state == "running" else state,
                              recovery="recheck" if state == "complete" else "resume",
                              ownership_valid=True, snapshot_matches=captured == checkpoint["snapshot"])
                result["guidance"] = ["recheck_owned_checks"] if state == "complete" else ["apply_same_plan"]
                if snapshot(store.target) != captured or store.read("checkpoint.json") != checkpoint:
                    refuse("setup_status_changed_during_observation", "ONBOARDING_OWNERSHIP_CONFLICT")
            store.verify()
    except EvidenceWikiError as error:
        code = error.error_code
        reason = (error.details or {}).get("field", "setup_observation_refused")
        import re

        if type(reason) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,255}", reason) is None:
            reason = "setup_observation_refused"
        state, action = {"ONBOARDING_PLAN_STALE": ("stale", "reprepare"),
            "ONBOARDING_ENVIRONMENT_INCOMPATIBLE": ("unsupported", "unsupported"),
            "ONBOARDING_LOCK_BUSY": ("busy", "retry_lock")}.get(code, ("conflict", "inspect_conflict"))
        result.update(status=state, recovery=action, ownership_valid=False,
                      diagnostics=[{"error_code": code, "reason": reason}])
        result["guidance"] = {"stale": ["reprepare_current_installation", "prepare_new_target"],
            "busy": ["retry_lock"], "unsupported": ["stop_unsupported"]}.get(state, ["inspect_ownership", "prepare_new_target"])
        if result["versions"]["skew"] is True:
            result["guidance"].append("inspect_upgrade_compatibility")
        elif result["versions"]["workspace"] is not None and result["versions"]["skew"] is None:
            result["guidance"].append("inspect_upgrade_compatibility")
    except OSError:
        result.update(status="conflict", recovery="inspect_conflict", ownership_valid=False,
                      guidance=["inspect_ownership", "prepare_new_target"],
                      diagnostics=[{"error_code": "ONBOARDING_OWNERSHIP_CONFLICT", "reason": "setup_read_unavailable"}])
    return checked(result, STATUS)


def revalidate(plan, *, owned=False, read_only=False):
    if plan_identity(plan) != plan["plan_id"]:
        refuse("saved_plan_identity_changed", "ONBOARDING_PLAN_STALE")
    current = compile_plan(canonical(plan["request"]), _owned_target=plan["bindings"]["target"] if owned else None)
    if current["plan_id"] != plan["plan_id"]:
        refuse("saved_plan_preconditions_changed", "ONBOARDING_PLAN_STALE")
    if not current["setup_ready"]:
        refuse("setup_decisions_unresolved", "ONBOARDING_CHECK_FAILED")
    if current["strict"]["requested_assurance"] == "host_enforced":
        refuse("protected_setup_provisioner_unavailable", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
    if not read_only:
        worker_environment(current)
    target, _ = target_basis(current["bindings"]["target"]["target"], owned_basis=current["bindings"]["target"])
    if owner("_strict_evidence").resolve_policy(target, current["initialization"]["effective_config"]) != current["strict"].get("policy"):
        refuse("setup_external_policy_conflict", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
    selected = os.environ.get("EVIDENCE_WIKI_PYTHON")
    if selected and Path(selected).absolute() != Path(sys.executable).absolute():
        refuse("setup_selected_interpreter_mismatch", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
    if any(row["state"] != "observed" for row in current["bindings"]["dependency_implementations"]):
        refuse("setup_required_dependency_missing", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
    authority = current["request"]["request"]["payload"]["authority"]
    root = Path(current["bindings"]["target"]["target"]["writable_root"])
    if not any(root == Path(value).resolve() or Path(value).resolve() in root.parents for value in authority["writable_roots"]):
        refuse("setup_journal_root_not_authorized")
    budget = current["sources"]["budgets"]["requested"]
    if sum(row["input"]["bytes"] for row in local_paths(current)) > budget["bytes"] or budget["seconds"] <= 0:
        refuse("setup_local_budget_unavailable", "ONBOARDING_LIMIT")
    return current


def checkpoint_valid(checkpoint):
    checked(checkpoint, CHECKPOINT)
    expected = {"schema_version", "plan_id", "transaction_id", "state", "pending", "completed", "snapshot", "observations", "results", "clock"}
    if (not isinstance(checkpoint, dict) or set(checkpoint) != expected or checkpoint["schema_version"] != CHECKPOINT
            or not isinstance(checkpoint["completed"], list) or checkpoint["completed"] != list(STEPS[:len(checkpoint["completed"])])
            or checkpoint["pending"] not in (None, *STEPS) or not isinstance(checkpoint["results"], dict)
            or not isinstance(checkpoint["observations"], dict) or checkpoint["state"] not in {"prepared", "running", "failed", "complete"}):
        refuse("setup_checkpoint_invalid", "ONBOARDING_OWNERSHIP_CONFLICT")
    if checkpoint["pending"] is not None and (len(checkpoint["completed"]) == len(STEPS)
            or checkpoint["pending"] != STEPS[len(checkpoint["completed"])]):
        refuse("setup_checkpoint_sequence_invalid", "ONBOARDING_OWNERSHIP_CONFLICT")
    if not set(checkpoint["completed"]) <= set(checkpoint["results"]) or not set(checkpoint["results"]) <= set(STEPS):
        refuse("setup_checkpoint_results_invalid", "ONBOARDING_OWNERSHIP_CONFLICT")
    if checkpoint["clock"] is not None:
        authority = owner("_evidence_authority")
        try:
            authority.timestamp(checkpoint["clock"])
        except authority.EvidenceInvalid:
            refuse("setup_checkpoint_clock_invalid", "ONBOARDING_OWNERSHIP_CONFLICT")


def postconditions(root, plan, completed):
    """Journal fields never substitute for current owner-visible artifacts."""
    if "initialize" not in completed:
        return
    config = yaml_document(read_file(root, "research.yml"))
    if digest(config) != plan["initialization"]["config_sha256"]:
        refuse("setup_configuration_changed", "ONBOARDING_OWNERSHIP_CONFLICT")
    frozen = owner("init_research_workspace").frozen_requirements_bytes(plan["profile"]["workspace_init"])
    actual = read_file(root, "docs/research-requirements.json")
    if actual != frozen:
        from .requirements_operations import verifylineage
        try:
            lineage = verifylineage(root, json_document(actual))
            expected = owner("_evidence_revision").canonical_bytes(lineage["base"])
            valid = lineage["owner_authentication"] == "protected_mutation_receipts" and expected == frozen
        except (Exception, SystemExit):
            valid = False
        if not valid:
            refuse("setup_requirements_changed", "ONBOARDING_OWNERSHIP_CONFLICT")
    if "intake" in completed:
        status = owner("question_status")
        directory = status.questions_directory(root, config)
        for row in plan["questions"]["rows"]:
            page = status.load_frontmatter(directory / (row["slug"] + ".md"))
            metadata = page.get("metadata", {}) if page else {}
            import json

            if (not page or metadata.get("original_ids_json") != json.dumps(row["original_ids"], ensure_ascii=False)
                    or page.get("question") != row["intake_text"] or metadata.get("research_id") != row["id"] or metadata.get("original_text") != row["original_text"]):
                refuse("setup_question_postcondition_changed", "ONBOARDING_OWNERSHIP_CONFLICT")
    if "coverage" in completed:
        module = owner("coverage_manifest")
        for row in plan["coverage"]:
            _, document = module.load_manifest(root, config, row["question_slug"])
            module.validate_manifest(document, expected_slug=row["question_slug"], policy_vocabularies=module.merged_policy_vocabularies(config))
            if {key: document[key] for key in row["manifest_fields"]} != row["manifest_fields"]:
                refuse("setup_coverage_postcondition_changed", "ONBOARDING_OWNERSHIP_CONFLICT")
    if "sources" in completed:
        for row in local_paths(plan):
            import hashlib

            if hashlib.sha256(read_file(root, row["path"], 16_777_216)).hexdigest() != row["input"]["sha256"]:
                refuse("setup_delivered_source_changed", "ONBOARDING_OWNERSHIP_CONFLICT")


def measured(operation, plan, clock, before, timeout):
    started = now()
    process = _execute_bounded([sys.executable, "-B", "-I", "-m", "evidence_wiki.setup_worker", operation],
        cwd=Path(sys.prefix), stdin_text=canonical({"operation": operation, "plan": plan, "clock": clock}).decode(),
        timeout_seconds=timeout, capture_limit=65536, preserve_stdout=True, **worker_environment(plan))
    if process.timed_out or process.stdout_truncated or process.stderr_truncated:
        result = {"status": "failed", "reason": "setup_check_time_or_output_bound"}
    else:
        try:
            result = json_document(process.stdout.encode())
            if not isinstance(result, dict) or result.get("status") not in {"passed", "failed", "blocked", "not_selected"}:
                raise ValueError("invalid_owner_result")
        except Exception:
            result = {"status": "failed", "reason": "setup_owner_result_invalid"}
    if process.returncode != 0:
        result["status"] = "failed"
    observation = owner("_evidence_authority").observed_execution(operation=operation,
        basis={"plan_id": plan["plan_id"], "workspace_sha256": digest(before), "checker": plan["bindings"]["package_code"]["sha256"],
               "starter": plan["bindings"]["starter"]["sha256"], "interpreter": plan["bindings"]["interpreter"]},
        result=result, started_at=started, finished_at=now(), exit_code=process.returncode)
    return result, observation


def receipt(plan, store, checkpoint):
    results = checkpoint["results"]
    complete = checkpoint["state"] == "complete"
    failure = any(results.get(key, {}).get("status") != "passed" for key in REQUIRED | {"strict"})
    source_rows = list(results.get("source_status", {}).get("sources", []))
    seen = {row["input_id"] for row in source_rows}
    for route in local_paths(plan):
        if route["source_id"] not in seen:
            source_rows.append({"input_id": route["source_id"], "question_ids": route["question_ids"],
                "requirements": route["requirements"], "usable": False, "format_satisfied": False,
                "semantic_scope": "not_evaluated", "observations": [{"raw_paths": [route["path"]],
                    "delivery": "captured" if "sources" in checkpoint["completed"] else "not_confirmed",
                    "inventory": "recorded" if "inventory" in checkpoint["completed"] else "not_run",
                    "usability": "not_ready", "reasons": ["setup_source_checks_incomplete"]}]})
    usable = sum(row["usable"] for row in source_rows)
    blocked_routes = [{"source_id": row["source_id"], "question_ids": row["question_ids"], "reasons": row["gaps"] or ["external_acquisition_not_run"]}
                      for row in plan["sources"]["routes"] if row["route"] != "local_file" or row["state"] != "planned"]
    gaps = (bool(blocked_routes) or any(not row["usable"] for row in source_rows)
            or bool(local_paths(plan)) and results.get("source_status", {}).get("status") != "passed")
    computation = results.get("computation", {"status": "not_run", "executed": False, "result_id": None})
    gaps = gaps or computation["status"] not in {"passed", "not_selected"}
    status = "failed" if not complete or failure else "partially_usable" if gaps and usable else "needs_input" if gaps else "ready"
    strict = results.get("strict", {"status": "not_run", "effective_assurance": None, "authority": "not_checked"})
    target = str(store.target)
    interpreter = plan["bindings"]["interpreter"]["invocation"]
    def command(*args):
        return [interpreter, "-B", "-m", "evidence_wiki", *args]
    result = {"schema_version": "evidence-setup-result/v1", "plan_id": plan["plan_id"], "transaction_id": store.transaction_id,
        "target": target, "checkpoint": str(store.transaction_path / "checkpoint.json"), "status": status,
        "setup_ready": complete and not failure, "research_complete": False, "claims_verified": False,
        "profile_sha256": digest(plan["profile"]), "pack": plan["bindings"]["pack"], "installation": plan["bindings"]["installation"],
        "question_map": [{**row, "outcome": "seeded" if "intake" in checkpoint["completed"] else "pending"}
                         for row in plan["questions"]["original_map"]],
        "checks": [{"operation": key, "required": key in REQUIRED or key in MUTATIONS or key == "strict", "status": results.get(key, {}).get("status", "not_run"),
                    "observation": checkpoint["observations"].get(key)} for key in STEPS],
        "sources": source_rows, "evidence_empty": not results.get("inventory", {}).get("sources"), "usable_source_count": usable, "blocked_routes": blocked_routes,
        "strict": {**strict, "required_checks": plan["strict"].get("checkers", []), "requested_assurance": plan["strict"]["requested_assurance"]},
        "computation": computation, "framework": plan["framework"], "blockers": plan["blockers"],
        "authority": {"boundary": "caller_authorized_local_setup", "writable_root": str(store.root),
                      "research_worker_isolation": "not_established", "receipt_trust": "local_observation", "authenticated": False},
        "next_actions": [*({"action": "inspect_sources", "argv": command("agent", "source-status" if chunk else "inspect", "--target", target, "--format", "json",
                            *[arg for row in chunk for arg in ("--source-path", row["path"])])}
                           for chunk in ([local_paths(plan)[i:i+32] for i in range(0, len(local_paths(plan)), 32)] or [[]])),
                         {"action": "resume_setup", "argv": command("agent", "apply", "--from-file", str(store.transaction_path / "plan.json"))},
                         {"action": "research_guide", "path": str(store.target / "skills/research-run.md")},
                         {"action": "acquire_evidence" if not usable else "review_evidence", "question_slugs": [row["slug"] for row in plan["questions"]["rows"]]}],
        "recovery": "none" if complete else "inspect_conflict" if checkpoint["pending"] in MUTATIONS else "resume",
        "observed_at": now(), "limitations": ["Local observations are editable and unauthenticated; apply rechecks actual artifacts and checks.",
            "Setup readiness does not confirm evidence, reviewer independence, host protection or research completion.",
            "Interrupted unjournaled writes preserve partial state and require inspection; no force or automatic rollback.",
            "Research/release blockers remain until their owners observe support. No acquisition, installs, warning intake or cadence dispatch ran."]}
    return checked(result, RESULT)


def apply_plan(raw):
    plan = decode(raw, PLAN)
    if len(canonical({"operation": "source_status", "plan": plan, "clock": now()})) > MAX_BYTES:
        refuse("setup_worker_input_bound", "ONBOARDING_LIMIT")
    if plan_identity(plan) != plan["plan_id"]:
        refuse("saved_plan_identity_changed", "ONBOARDING_PLAN_STALE")
    # Recompile under the lock, allowing only this journal to account for an
    # initialized target. Preflight here also avoids writes for unsupported policy.
    revalidate(plan, owned=True)
    store = SetupStore(plan)
    with store.locked():
        checkpoint = store.open_transaction()
        checkpoint_valid(checkpoint)
        revalidate(plan, owned=True)
        before = snapshot(store.target)
        if before != checkpoint["snapshot"]:
            refuse("setup_owned_files_changed_or_interrupted_write", "ONBOARDING_OWNERSHIP_CONFLICT")
        postconditions(store.target, plan, checkpoint["completed"])
        deadline = time.monotonic() + plan["sources"]["budgets"]["requested"]["seconds"]
        # Observations cannot authorize a skip. Checks always rerun on resumption;
        # mutation skips require matching file/directory snapshots and postconditions.
        for operation in STEPS:
            if operation in checkpoint["completed"] and operation in MUTATIONS:
                continue
            store.verify()
            if snapshot(store.target) != before:
                refuse("setup_concurrent_workspace_change", "ONBOARDING_OWNERSHIP_CONFLICT")
            installation = installation_basis()
            if installation != {key: plan["bindings"][key] for key in installation}:
                refuse("setup_installation_changed", "ONBOARDING_PLAN_STALE")
            remaining = min(600, int(deadline - time.monotonic()))
            if remaining <= 0:
                refuse("setup_time_budget_exhausted", "ONBOARDING_LIMIT")
            checkpoint["clock"] = now()
            replay = operation in checkpoint["completed"]
            if not replay:
                checkpoint.update(pending=operation, state="running")
            store.write("checkpoint.json", checkpoint)
            result, observation = measured(operation, plan, checkpoint["clock"], before, remaining)
            after = snapshot(store.target, durable=operation in MUTATIONS)
            if operation not in MUTATIONS and after != before:
                refuse("setup_check_changed_workspace", "ONBOARDING_OWNERSHIP_CONFLICT")
            if operation in MUTATIONS and result["status"] == "passed":
                verify_effects(operation, plan, result, before, after)
                postconditions(store.target, plan, [*checkpoint["completed"], operation])
            checkpoint["results"][operation] = result
            store.write("observations/" + operation + ".json", observation)
            checkpoint["observations"][operation] = observation["observation_id"]
            if result["status"] != "passed" and operation in set(MUTATIONS) | REQUIRED | {"strict"}:
                checkpoint["state"] = "failed"
                store.write("checkpoint.json", checkpoint)
                result_receipt = receipt(plan, store, checkpoint)
                store.write("receipt.json", result_receipt)
                return result_receipt
            before = after
            checkpoint["snapshot"] = after
            if not replay:
                checkpoint["completed"].append(operation)
                checkpoint["pending"] = None
            store.write("checkpoint.json", checkpoint)
        revalidate(plan, owned=True)
        if snapshot(store.target) != before:
            refuse("setup_closing_workspace_change", "ONBOARDING_OWNERSHIP_CONFLICT")
        checkpoint.update(state="complete", pending=None)
        store.write("checkpoint.json", checkpoint)
        result = receipt(plan, store, checkpoint)
        store.write("receipt.json", result)
        return result
