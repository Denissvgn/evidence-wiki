"""Explicit frozen performance qualification; no scientific state or approval writes."""

import math
import os
import platform
import secrets
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ._pack_io import identity
from .agent_execution import ExecutionAttempt
from .host_context import current_context
from .mutation_contracts import canonical, digest, validate_request_id
from .mutation_operations import _generation, _host_selection
from .performance_contracts import AUDIT, PLAN, REPORT, THRESHOLDS, catalog, checked, gate, select
from .refresh_schedule_store import ScheduleStore
from .research_execution import ResearchExecution, bounded_host_call
from .research_session_contracts import refuse
from .run_policy import RunPolicy
from .workflow_contracts import EXECUTION_OBSERVATION, JOB_V3, validate_job

STATE = "research-performance-state/v1"


def hardware():
    return {"machine": platform.machine(), "platform": platform.platform(), "python": platform.python_version(),
        "cpu_count": os.cpu_count(), "processor": platform.processor() or "unobserved",
        "memory_measurement": "child process RSS and per-call Python tracemalloc; no whole-system allocation claim"}


def run(root, policy, *, execution, request_id, benchmark_config_ref, critic_config_ref, seed=0):
    context = current_context()
    if (context is None or context.mutation_control_directory is None or type(policy) is not RunPolicy
            or type(execution) is not ResearchExecution):
        refuse("PERFORMANCE_CONFIG_INVALID")
    request_id = validate_request_id(request_id)
    frozen = policy.to_dict()
    if (not {"model", "offline_check"} <= set(frozen["allowed_effects"])
            or frozen["host_capability_refs"]["model"] is None):
        refuse("HOST_CAPABILITY_UNAVAILABLE")
    configs = [benchmark_config_ref, critic_config_ref]
    roles = [execution.profile.role(config) for config in configs]
    registry = execution.profile.role_scopes
    if registry is None or roles[0].role != "host_worker" or roles[1].role != "evaluator" or roles[0].controller_ref == roles[1].controller_ref:
        refuse("PERFORMANCE_INDEPENDENT_CONTROLLERS_REQUIRED")
    for index, config in enumerate(configs):
        scope = registry.scope(config)
        if scope.task_role != ("integration" if index == 0 else "evaluator"):
            refuse("PERFORMANCE_ROLE_SCOPE_REQUIRED")
        scope.require("model")
        scope.require("integration.propose" if index == 0 else "verdict.propose")
        if context.controller_for(roles[index].role, config) != roles[index].controller_ref:
            refuse("HOST_CAPABILITY_UNAVAILABLE")
    if frozen["budgets"]["model_calls"] < 2 or execution.profile.total_steps < 2:
        refuse("PERFORMANCE_BUDGET_EXHAUSTED")
    rows = catalog(policy, seed=seed)
    binding = {"request_id": request_id, "policy": frozen, "execution": execution.identity(),
        "catalog": rows, "configs": configs,
        "workspace_binding": {"root": str(root), "root_identity": identity(root), "control": str(context.mutation_control_directory), "control_identity": identity(context.mutation_control_directory)}, "host_context_sha256": _host_selection(), "owner_generation": _generation()}
    store = ScheduleStore(root, context.mutation_control_directory, request_id, namespace="performance-runs")
    with store.locked(write=True):
        state = store.read()
        if state is None:
            if not store.new:
                refuse("PERFORMANCE_RETENTION_UNAVAILABLE")
            state = {"schema_version": STATE, "binding": binding, "attempts": {}, "measurements": [],
                "hardware": hardware(), "created_at": datetime.now(timezone.utc).isoformat(), "report": None, "authentication": None}
        else:
            execution._verify(state)
            if state["binding"] != binding:
                refuse("PERFORMANCE_REQUEST_CONFLICT")
            if state["report"] is not None:
                return checked(REPORT, {**state["report"], "historical": True})
        _save(store, execution, state)
    with store.dispatch_lease():
        with store.locked(write=True):
            state = store.read()
            if state is None:
                refuse("PERFORMANCE_RETENTION_UNAVAILABLE")
            execution._verify(state)
            if state["binding"] != binding:
                refuse("PERFORMANCE_REQUEST_CONFLICT")
            if state["report"] is not None:
                return checked(REPORT, {**state["report"], "historical": True})
        deadline = datetime.fromisoformat(state["created_at"])+timedelta(seconds=min(frozen["budgets"]["seconds"], 600))
        selected = rows
        critic = None
        failure_reason = None
        try:
            selection = _agent(store, execution, state, policy, benchmark_config_ref,
                {"purpose": "performance_workload_selection", "catalog_id": digest(rows), "catalog": rows,
                 "frozen_policy": frozen, "thresholds": THRESHOLDS, "output_schema": PLAN}, deadline)
            selected = select(rows, selection)
            for row in selected:
                if any(item["workload_id"] == row["workload_id"] for item in state["measurements"]):
                    continue
                remaining = (deadline-datetime.now(timezone.utc)).total_seconds()
                if remaining <= 0:
                    break
                started = time.perf_counter()
                result = subprocess.run([sys.executable, "-m", "evidence_wiki.performance_probe"], input=canonical(row),
                    capture_output=True, timeout=min(THRESHOLDS["probe_wall_seconds"], remaining), check=False)
                if result.returncode != 0 or len(result.stdout) > 131072:
                    break
                from ._pack_io import json_document
                measured = json_document(result.stdout)
                measured["probe_wall_seconds"] = time.perf_counter()-started
                state["measurements"].append(measured)
                with store.locked(write=True):
                    _load_same(store, execution, state)
                    _save(store, execution, state)
            critic = checked(AUDIT, _agent(store, execution, state, policy, critic_config_ref,
                {"purpose": "performance_accounting_audit", "measurement_id": digest(state["measurements"]),
                    "measurements": state["measurements"], "expected_workloads": rows, "hardware": state["hardware"],
                    "agent_accounting": _accounting(state), "thresholds": THRESHOLDS, "output_schema": AUDIT}, deadline))
        except (Exception, SystemExit) as error:
            # Retained reservations stay spent. Unknown/absent/incomplete owner
            # or model output can only yield a bounded incomplete qualification.
            critic = None
            failure_reason = getattr(error, "error_code", type(error).__name__)
            field = getattr(error, "details", {}).get("field")
            if type(field) is str:
                failure_reason = (failure_reason+":"+field)[:256]
        accounting = _accounting(state)
        outcome = gate(rows, state["measurements"], critic, state["hardware"], accounting)
        report = checked(REPORT, {"schema_version": REPORT, "request_id": request_id, "binding_id": digest(binding),
            "policy_id": policy.policy_id, **outcome, "reason": "fixed_performance_gate" if outcome["complete"] else (failure_reason or "performance_evidence_incomplete"),
            "hardware": state["hardware"], "catalog_id": digest(rows), "workloads": rows,
            "measurements": state["measurements"], "thresholds": THRESHOLDS, "agent_accounting": accounting,
            "critic": critic, "measurement_scope": "synthetic_native_contract_validation; not evidence acceptance or owner publication",
            "historical": False, "authorized": False, "evidence_accepted": False})
        state["report"] = report
        with store.locked(write=True):
            _load_same(store, execution, state)
            if _host_selection() != binding["host_context_sha256"] or _generation() != binding["owner_generation"] or execution.identity() != binding["execution"]:
                refuse("PERFORMANCE_BINDING_CHANGED")
            _save(store, execution, state)
        return report


def _save(store, execution, state):
    store.write(execution.sign({**state, "authentication": None}))


def _load_same(store, execution, state):
    stored = store.read()
    if stored is None:
        refuse("PERFORMANCE_RETENTION_UNAVAILABLE")
    execution._verify(stored)
    if stored["binding"] != state["binding"]:
        refuse("PERFORMANCE_BINDING_CHANGED")


def _accounting(state):
    result = []
    for config, record in state["attempts"].items():
        observation = record.get("observation")
        usage = observation.get("observed_usage") if observation else None
        complete = bool(observation and observation["final_accounting"] and usage and all(
            usage.get(field) is not None for field in ("model_calls", "tool_calls", "tokens", "cost", "seconds")))
        result.append({"config_ref": config, "complete": complete, "observed_usage": usage,
            "model_ref": observation["model_ref"] if observation else None, "model_time_scope": "host measured attempt lifetime; provider latency not separated"})
    return result


def _agent(store, execution, state, policy, config, packet, deadline):
    record = state["attempts"].get(config)
    fresh = record is None
    profile, frozen = execution.profile, policy.to_dict()
    if fresh:
        clock = datetime.now(timezone.utc)
        timeout = min(60, int((deadline-clock).total_seconds()))
        if timeout <= 0:
            refuse("PERFORMANCE_BUDGET_EXHAUSTED")
        scope, role = profile.role_scopes.scope(config), profile.role(config)
        identifier = state["binding"]["request_id"]+"."+str(len(state["attempts"]))
        cap = {"model_calls": 1, "tool_calls": min(profile.tool_calls, 50, frozen["budgets"]["tool_calls"]//2),
            "tokens": min(profile.tokens, 100000, frozen["budgets"]["tokens"]//2), "cost": str(min(Decimal(profile.cost_limit), Decimal(frozen["budgets"]["cost_limit"])/2))}
        job = validate_job({"schema_version": JOB_V3, "id": identifier, "operation": "generate" if role.role == "host_worker" else "evaluate",
            "role": role.role, "controller_ref": role.controller_ref, "grant_ref": "performance_qualification.run",
            "input_id": digest(packet), "basis_id": digest(state["binding"]), "policy_id": policy.policy_id,
            "policy_version": frozen["schema_version"], "prompt_version": role.prompt_version, "revision_precondition": None,
            "original_text": "Choose every frozen workload in a representative order." if role.role == "host_worker" else "Qualify native contract performance without scientific approval.", "scope": frozen["scope"]["question_ids"],
            "required_capabilities": ["model", "integration.propose" if role.role == "host_worker" else "verdict.propose"],
            "payload": {"question": "Choose every frozen workload in a representative order."} if role.role == "host_worker" else
                {"claim_id": "performance-accounting", "packet_id": digest(packet)},
            "issued_utc": clock.isoformat(), "deadline_utc": (clock+timedelta(seconds=timeout)).isoformat(), "timeout_seconds": timeout,
            "max_attempts": 1, "expected_result_schema": EXECUTION_OBSERVATION, "budgets": cap,
            "qualifications": {"assurance": frozen["assurance"], "status": "unknown", "evaluator_ref": None, "qualification_ref": None, "context_basis": None},
            "task_role": scope.task_role, "role_scope_sha256": scope.sha256, "context_id": digest(packet), "packet_sha256": digest(packet)[7:]}, now_utc=clock.isoformat())
        attempt = ExecutionAttempt.create(attempt_id=identifier, nonce=secrets.token_hex(16), run_id=state["binding"]["request_id"],
            binding_id=digest(state["binding"]), profile=profile, config_ref=config, job=job, input_document=packet)
        record = {"attempt": attempt.document(), "observation": None}
        state["attempts"][config] = record
        with store.locked(write=True):
            _load_same(store, execution, state)
            _save(store, execution, state)
    else:
        attempt = execution.attempt(record["attempt"])
        if attempt.input_document != packet:
            refuse("PERFORMANCE_INPUT_CONFLICT")
    observation = record["observation"]
    for _ in range(32):
        if observation is not None and observation["state"] != "running":
            break
        function = execution.executor.invoke if fresh and observation is None else execution.executor.poll
        observed, reason = bounded_host_call(function, (attempt,), min(profile.cleanup_timeout_seconds, max(0, (deadline-datetime.now(timezone.utc)).total_seconds())))
        fresh = False
        if reason is not None:
            refuse("PERFORMANCE_EXECUTION_UNKNOWN")
        observation = execution.authenticate(observed, attempt)
        record["observation"] = observation
        with store.locked(write=True):
            _load_same(store, execution, state)
            _save(store, execution, state)
    usage = (observation or {}).get("observed_usage") or {}
    if (observation is None or observation["state"] != "succeeded" or not observation["final_accounting"]
            or type(usage.get("model_calls")) is not int or usage["model_calls"] < 1 or observation["started_utc"] is None
            or observation["reason"] == "offline_fixture" or profile.role(config).model_ref.startswith("offline/")):
        refuse("PERFORMANCE_EXECUTION_INCOMPLETE")
    if (datetime.now(timezone.utc) >= deadline
            or datetime.fromisoformat(observation["completed_utc"]) >= datetime.fromisoformat(attempt.job["deadline_utc"])
            or usage.get("seconds") is None or usage["seconds"] > attempt.job["timeout_seconds"]):
        refuse("PERFORMANCE_EXECUTION_INCOMPLETE")
    for field, limit in attempt.job["budgets"].items():
        value = usage.get(field)
        if value is None or Decimal(str(value)) > Decimal(str(limit)) or not math.isfinite(float(value)):
            refuse("PERFORMANCE_BUDGET_EXHAUSTED")
    return observation["output"]
