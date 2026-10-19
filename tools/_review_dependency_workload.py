"""Native fixture provisioning and retained review work observations."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from evidence_wiki import (
    BoundedSubprocessExecutor,
    DisagreementPlan,
    ExecutionAttempt,
    ExecutionProfile,
    ExecutionRole,
    ExecutionRoleRegistry,
    ExecutionRoleScope,
    HostReviewSigner,
    HostRuntime,
    NativeInputHandoff,
    Onboarding,
    QualificationAuthority,
    QualifiedEvaluators,
    ResearchExecution,
    ReviewDelivery,
    RunPolicy,
    SigningIdentity,
    Workspace,
)
from evidence_wiki._process_groups import ProcessGroupOwnership, ownership_for
from evidence_wiki.agent_qualification import reference_suite
from evidence_wiki.errors import ConfigError
from evidence_wiki.host_context import HostContext
from evidence_wiki.pack_discovery import owner
from evidence_wiki.research_journal_contracts import canonical_records, digest_record, digest_records
from evidence_wiki.workflow_contracts import EXECUTION_OBSERVATION, JOB_V3

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/unattended-host"
SUPPORTED_CASES = ("unchanged-baseline", "independent-claim-text", "independent-claim-limitations",
    "premise-transitive-edit", "question-demand-body", "unrelated-source-raw")
CLAIM_IDS = {"observed-capacity", "effective-capacity", "observed-demand", "adequacy", "margin", "independent-note"}


_DEADLINES = ContextVar("review_workload_deadlines", default=())
_POPEN_TYPE = subprocess.Popen


@contextmanager
def workload_deadline(seconds):
    """Nested local phases can shorten an outer deadline but never extend it."""
    from tools.decide_release import _HostDeadline
    parents = _DEADLINES.get()
    if type(seconds) not in (int, float) or seconds <= 0:
        raise _HostDeadline()
    if not parents and signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ValueError("workload_existing_host_timer")
    started = time.monotonic()
    target = min((*parents, started + seconds))
    if target <= started:
        raise _HostDeadline()
    prior = signal.getsignal(signal.SIGALRM)
    def expired(_signum, _frame):
        raise _HostDeadline()
    token = _DEADLINES.set((*parents, target))
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(0.000001, target - time.monotonic()))
    deadline_escaping = False
    try:
        yield
    except _HostDeadline:
        deadline_escaping = True
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        _DEADLINES.reset(token)
        signal.signal(signal.SIGALRM, prior)
        closing = time.monotonic()
        if parents and min(parents) > closing:
            signal.setitimer(signal.ITIMER_REAL, min(parents) - closing)
        if target <= closing and not deadline_escaping:
            raise _HostDeadline()


def digest_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return owner("_evidence_revision").canonical_bytes(value)


def digest(value):
    return digest_bytes(canonical(value))


def _example():
    if str(EXAMPLE) not in sys.path:
        sys.path.insert(0, str(EXAMPLE))
    from fixture_host import FixtureMeter
    from support import GRANTS, attest, identity, now, policy, setup_request, sha
    for name in ("fixture_host", "support"):
        if Path(sys.modules[name].__file__).resolve() != EXAMPLE / (name + ".py"):
            raise ValueError("workload_fixture_module_changed")
    return FixtureMeter, GRANTS, attest, identity, now, policy, setup_request, sha


def retain(path, value, *, raw=False):
    data = value if raw else canonical(value)
    if len(data) > 16_777_216:
        raise ValueError("workload_original_output_bound")
    if path.resolve() != path.absolute() or path.exists():
        raise ValueError("workload_original_path_invalid")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
    path.chmod(0o400)
    return {"path": str(path), "raw_sha256": digest_bytes(data), "size_bytes": len(data)}


class OriginalResponses:
    """Pin original bytes immediately, before further owner calls."""

    def __init__(self, directory, *, journal_collection=False):
        if type(journal_collection) is not bool:
            raise ValueError("workload_journal_collection_policy_invalid")
        self.directory = directory
        self.journal_collection = journal_collection
        self.pins = []

    def append(self, entry):
        label, value = entry
        # Public native owners have already validated their child documents.
        # Retention must not charge an extra report wrapper to native child depth.
        raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        pin = retain(self.directory / (str(len(self.pins)) + "-" + label + ".json"), raw, raw=True)
        self.pins.append(dict(pin, label=label))
        return pin

    def append_records(self, label, records):
        try:
            return self._append_records(label, records)
        except Exception as error:
            if getattr(error, "error_code", None) in {"MUTATION_LIMIT", "MUTATION_INVALID"}:
                try:
                    from evidence_wiki.research_journal_collections import collection_summary
                    summary = collection_summary(records)
                    error._review_collection_stats = {key: summary[key] for key in ("records_bytes", "record_count", "physical_nodes", "expanded_bytes")}
                    error._review_collection_stage = "bounded_collection_encode" if self.journal_collection else "legacy_collection_encode"
                except Exception:
                    BaseException.add_note(error, "Collection measurements unavailable.")
            raise

    def _append_records(self, label, records):
        if self.journal_collection:
            from tools._review_journal_artifacts import retain_collection
            return retain_collection(self, label, records, retain)
        raw = canonical_records(records)
        pin = retain(self.directory / (str(len(self.pins)) + "-" + label + ".json"), raw, raw=True)
        self.pins.append(dict(pin, label=label))
        return pin

    def finally_records(self, label, execution, session):
        """Retain a driver tail without replacing an already escaping failure."""
        primary = sys.exc_info()[1]
        try:
            return self.append_records(label, execution.records(session))
        except BaseException as secondary:
            if primary is None:
                raise
            primary._review_retention_failure = failure_document(secondary)

    def finally_close(self, resource):
        """Keep an earlier driver failure when closing its local transport."""
        primary = sys.exc_info()[1]
        try:
            resource.close()
        except BaseException as secondary:
            if primary is None:
                raise
            primary._review_cleanup_failure = failure_document(secondary)

    def extend(self, entries):
        for entry in entries:
            self.append(entry)


def deadline_cause(error):
    """Find only an actual timer exception in the bounded causal chain."""
    from tools.decide_release import _HostDeadline
    seen = set()
    for _ in range(32):
        if error is None or id(error) in seen:
            return None
        seen.add(id(error))
        if isinstance(error, (_HostDeadline, TimeoutError)):
            return error
        error = error.__cause__ if error.__cause__ is not None else error.__context__
    return None


def failure_document(error):
    cause = deadline_cause(error)
    if cause is not None:
        error = cause
    kinds = {"ValueError", "TypeError", "KeyError", "AttributeError", "RuntimeError", "OSError", "FileExistsError",
        "FileNotFoundError", "PermissionError", "SystemExit", "TimeoutError", "ConfigError", "UsageError",
        "QualificationError", "ExecutionError", "MutationError", "WorkflowContractError", "_HostDeadline"}
    allowed_frames = {
        "tools/_execution_budget.py": {"execution_policy", "validate_execution_policy", "validate_execution_binding", "validate_execution_window", "validate_runtime_allocation", "provisioning_window"},
        "tools/run_review_dependency_workload.py": {"run_workload", "main", "validate_plan", "reference_plan", "read_pinned_artifact", "collect_journals", "_owner_read"},
        "tools/_review_dependency_workload.py": {"provision", "drive", "call", "mutation", "retain", "reference_dependencies", "_deposit", "_example", "admit_diagnosed", "question_binding_preflight", "host_read_preflight", "review_packet_preflight"},
        "src/evidence_wiki/coverage_observation.py": {"observe"},
        "src/evidence_wiki/review_page_operations.py": {"_collect_packet"},
        "src/evidence_wiki/request_lifecycle_operations.py": {"_questions"},
        "src/evidence_wiki/research_session.py": {"guarded", "assemble_review_packet", "advance", "intake_observation", "_check"},
        "src/evidence_wiki/host_context.py": {"checked_operation", "require_grant", "require_path", "_refuse"},
        "src/evidence_wiki/_host_qualification.py": {"guarded", "host_qualification", "_refuse"},
        "src/evidence_wiki/agent_qualification.py": {"issue", "registration", "_prepared_envelope", "_fail"},
        "src/evidence_wiki/execution_session.py": {"advance", "_apply", "_apply_owned", "_native_call", "_mutation"},
        "src/evidence_wiki/mutation_contracts.py": {"detached", "canonical", "checked", "refuse"}}
    allowed_frames["tools/_review_dependency_workload.py"] |= {"drive_selective_services", "append_records", "_append_records", "finally_records", "authenticated_journal"}
    allowed_frames["src/evidence_wiki/research_journal_contracts.py"] = {"canonical_records", "detached_records", "canonical_record", "detached_record", "_record_transport", "_serialized"}
    allowed_frames["src/evidence_wiki/research_journal_collections.py"] = {"_prepared", "encode_collection", "decode_collection", "_load", "collection_summary"}
    allowed_frames["tools/_review_journal_artifacts.py"] = {"retain_collection", "read_collection", "verify_receipt"}
    allowed_frames["tools/replay_qualification_native.py"] = {"journal_snapshot", "verify_journal_closing"}
    allowed_frames["tools/_review_dependency_runner.py"] = {"run", "_native_facts", "_resolve"}
    allowed_frames["tools/_review_dependency_cases.py"] = {"run_cases", "drive", "_finish_extended_cases"}
    frames = []
    trace = error.__traceback__
    while trace is not None:
        path = Path(trace.tb_frame.f_code.co_filename)
        if path.is_relative_to(ROOT):
            relative, function = path.relative_to(ROOT).as_posix(), trace.tb_frame.f_code.co_name
            if function in allowed_frames.get(relative, set()):
                frames.append({"path": relative, "function": function, "line": trace.tb_lineno})
        trace = trace.tb_next
    codes = {"RESEARCH_BASIS_CHANGED", "HOST_QUALIFICATION_REFUSED", "HOST_CONTEXT_REFUSED", "COMPUTATION_REFUSED", "STRICT_EVIDENCE_REFUSED", "RESEARCH_SESSION_REFUSED",
        "RESEARCH_RESOLUTION_REFUSED", "REVIEW_PAGE_REFUSED", "CALCULATION_PROPOSAL_REFUSED", "SOURCE_CAPTURE_REFUSED", "DRAFT_ADMISSION_REFUSED",
        "MUTATION_REQUEST_QUESTION_BINDING_CHANGED", "MUTATION_LIMIT", "MUTATION_INVALID"}
    reasons = {"workload_assignment_invalid", "workload_full_scope_invalid", "workload_fixture_lineage_invalid",
        "execution_policy_invalid", "execution_policy_binding_invalid", "execution_observation_profile_invalid",
        "execution_authority_clock_invalid", "execution_runtime_binding_invalid", "execution_allocation_invalid",
        "workload_pilot_mutation_invalid", "workload_measured_allocation_required", "workload_allocation_exceeds_authority",
        "workload_candidate_or_recipe_changed", "workload_owner_current_unavailable", "workload_cross_journal_scope_unsupported",
        "workload_original_witness_expired", "workload_changed_basis_guard_missing", "workload_cleanup_unavailable",
        "workload_job_phase_cap", "workload_reference_authentication_required", "workload_partial_scope_overclaimed",
        "workload_existing_host_timer", "workload_job_timing_binding_invalid", "workload_pilot_measurement_invalid", "workload_owner_two_pass_changed", "workload_child_launch_refused", "workload_child_group_changed",
        "workload_child_cleanup_unconfirmed", "workload_native_dispatch_unavailable",
        "host_mutation_workspace_gate_required", "host_operation_not_granted", "computation_clock_offset_required", "qualification_attempt_mismatch",
        "native_baseline_not_accepted", "native_mcp_cycle_refused", "fixture_mutation_unsupported", "selective_owner_unavailable",
        "workload_candidate_changed", "workload_plan_binding_invalid", "workload_new_private_external_directory_required",
        "qualification_unknown", "workload_reference_mapping_discrepancy", "workload_native_review_application_missing",
        "workload_journal_collection_bound", "workload_artifact_binding_invalid", "workload_artifact_path_invalid",
        "workload_admission_diagnostics_invalid", "workload_admission_evidence_not_verified", "mechanical_grounding_unmatched",
        "workload_question_context_invalid", "workload_question_identity_collision"}
    if getattr(error, "error_code", None) == "REVIEW_PAGE_REFUSED":
        reasons |= {"counter_instruction_claim_changed", "decisive_context_incomplete", "current_owner_changed",
            "host_selection_changed", "complete_packet_requires_supported_paging_executor", "packet_bound"}
    if getattr(error, "error_code", None) == "HOST_QUALIFICATION_REFUSED":
        reasons |= {"attestation_current_context_mismatch", "native_current_review_changed"}
    details = getattr(error, "details", {})
    reason = details.get("reason") if type(details) is dict else None
    if type(reason) is not str or reason not in reasons:
        message = error.args[0] if len(error.args) == 1 and type(error.args[0]) is str else None
        reason = message if message in reasons else None
    code = getattr(error, "error_code", None)
    kind = type(error).__name__
    observed = {"kind": kind if kind in kinds else None, "error_code": code if type(code) is str and code in codes else None,
        "reason": reason, "frames": frames[-8:]}
    if (code == "MUTATION_REQUEST_QUESTION_BINDING_CHANGED" and type(details) is dict
            and details.get("field") == "/questions"):
        observed["field"] = "/questions"
    stats = getattr(error, "_review_collection_stats", None)
    if (type(stats) is dict and set(stats) == {"records_bytes", "record_count", "physical_nodes", "expanded_bytes"}
            and all(type(value) is int and 0 <= value <= 16_777_216 for value in stats.values())
            and getattr(error, "_review_collection_stage", None) in {"legacy_collection_encode", "bounded_collection_encode"}):
        observed["collection"] = stats
        observed["collection_stage"] = error._review_collection_stage
    cleanup = getattr(error, "_review_cleanup_failure", None)
    if type(cleanup) is dict:
        observed["cleanup_failure"] = cleanup
    secondary = getattr(error, "_review_retention_failure", None)
    if type(secondary) is dict:
        observed["retention_failure"] = secondary
    return observed


def render_question_body(context, *, question):
    """Keep logical context as inert body data, outside native identity metadata."""
    if (type(context) is not dict or set(context) != {"question", "metadata", "body"}
            or context["question"] != question or type(context["metadata"]) is not dict
            or type(context["body"]) is not str):
        raise ValueError("workload_question_context_invalid")
    if set(context["metadata"]) & {"research_id", "original_text", "original_ids_json"}:
        raise ValueError("workload_question_identity_collision")
    structured = canonical({"question": context["question"], "metadata": context["metadata"]}).decode()
    body = context["body"]
    separator = "" if not body or body.endswith("\n") else "\n"
    return body + separator + "\nFixture question context (inert data):\n```json\n" + structured + "```\n"


def question_binding_preflight(root, questions, originals):
    """Use assembly's native read-only observation; no facade exposes this read."""
    from evidence_wiki.coverage_observation import observe

    for question in questions:
        try:
            observation = observe(root, question_slug=question["slug"])
        except Exception as error:
            originals.append(("question-binding-preflight-failure", failure_document(error)))
            raise
        originals.append(("question-binding-preflight-" + question["slug"], observation))


def review_packet_preflight(root, profile, source_ids, originals):
    """Consume all actual protected pages before any qualification dispatch."""
    from evidence_wiki.review_page_operations import _collect_packet

    delivery = profile.review_delivery
    for claim_id in sorted(CLAIM_IDS):
        for task_role in ("evaluator", "critic"):
            label = "review-packet-preflight-" + claim_id + "-" + task_role
            try:
                packet = _collect_packet(root, claim_id=claim_id, context_id=label, task_role=task_role,
                    page_chars=delivery.page_chars, source_ids=source_ids,
                    context_version=delivery.context_version, max_packet_bytes=delivery.max_packet_bytes)
                originals.append((label, {"context_version": delivery.context_version,
                    "packet_sha256": digest(packet), "packet": packet,
                    "review_credit": 0, "approval_credit": 0, "worker_consumption": "not_observed"}))
            except (Exception, SystemExit) as error:
                originals.append((label + "-failure", failure_document(error)))
                raise


def host_read_preflight(root, runtime, originals):
    """Exercise the native closing reader before any retained review dispatch."""
    from evidence_wiki import execution_session as native
    from evidence_wiki import host_qualification

    with runtime.context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(runtime.policy, run_id="fixture-read-preflight", execution=runtime.execution) as session:
            try:
                with native._current_facts(session, operation="current"):
                    witness = host_qualification(session)
            except (Exception, SystemExit) as error:
                originals.append(("host-read-preflight-failure", failure_document(error)))
                raise
            originals.append(("host-read-preflight", witness))
            if witness["credentials"]["state"] != "ready":
                raise ValueError("workload_owner_current_unavailable")


def admit_diagnosed(workspace, proposal, originals):
    """Retain native read-only checks and the exact request before admission."""
    try:
        diagnostic = workspace.strict.diagnose(proposal)
    except Exception as error:
        originals.append(("admission-diagnostic-failure", failure_document(error)))
        raise
    originals.append(("admission-diagnostic", diagnostic))
    claims = proposal["inventory"]["claims"]
    rows = diagnostic.get("claims", [])
    valid = (diagnostic.get("schema_version") == "evidence-claim-diagnostics/v1"
        and diagnostic.get("proposal_sha256") == "sha256:" + digest(proposal)
        and diagnostic.get("structural_result") == "valid"
        and diagnostic.get("accepted") is False and diagnostic.get("published") is False
        and diagnostic.get("semantic_approval") == "not_evaluated"
        and diagnostic.get("original_ids") == [row["original_id"] for row in proposal["originals"]]
        and len(rows) == len(claims))
    if valid:
        for claim, row in zip(claims, rows, strict=True):
            valid = (row.get("claim_id") == claim["id"] and row.get("question_slug") == claim["question_slug"]
                and row.get("premises") == claim["premises"]
                and row.get("accepted") is False and row.get("semantic_approval") == "not_evaluated"
                and len(row.get("evidence", [])) == len(claim["evidence"]))
            if not valid:
                break
            for index, (evidence, observed) in enumerate(zip(claim["evidence"], row["evidence"], strict=True)):
                valid = (observed.get("evidence_index") == index and all(
                    observed.get(key) == evidence[key] for key in ("source_id", "record_sha256", "location_hint")))
                if not valid:
                    break
            if not valid:
                break
    if not valid:
        raise ConfigError("DRAFT_ADMISSION_REFUSED", "Native admission diagnostics did not cover the supplied proposal.",
            details={"reason": "workload_admission_diagnostics_invalid"})
    if not all(row.get("mechanical_result") == "verified" and row["evidence"] and all(
            evidence.get("result") == "verified" and evidence.get("reason") is None for evidence in row["evidence"])
            for row in rows):
        unmatched = any(evidence.get("reason") == "mechanical_grounding_unmatched"
            for row in rows for evidence in row["evidence"])
        raise ConfigError("DRAFT_ADMISSION_REFUSED", "Native admission evidence was not verified.", details={
            "reason": "mechanical_grounding_unmatched" if unmatched else "workload_admission_evidence_not_verified"})
    request = workspace.mutations.prepare(request_id="admit-retained", operation="strict.admit", arguments={"proposal": proposal})
    originals.append(("admission-request", request))
    receipt = workspace.mutations.submit(request)
    originals.append(("admission-receipt", receipt))
    return receipt


def file_manifest(root):
    return {path.relative_to(root).as_posix(): digest_bytes(path.read_bytes())
        for path in sorted(root.rglob("*")) if path.is_file() and "__pycache__" not in path.parts}


def _computation(source_paths, clock):
    aggregations = {}
    for name in ("shared", "demand"):
        aggregations[name] = {"description": "Retained native input", "selector": {
            "source_glob": source_paths[name], "records_pointer": "/records", "row_key": "/id", "allow_empty": False},
            "filter": None, "group_by": [], "metrics": {"amount": {
                "op": "sum", "expr": "record.amount", "denominator": None, "unit": "units"}}, "output_target": None}
    return {"version": "1.0", "arithmetic": {"mode": "exact", "precision": 64, "scale": None, "rounding": "ROUND_HALF_EVEN"},
        "clock": {"as_of": clock, "timezone": "UTC", "ambiguous": "refuse", "nonexistent": "refuse", "search_days": 366},
        "tables": {}, "aggregations": aggregations, "graphs": {"capacity": {
            "description": "Retained capacity minus demand", "constants": {}, "inputs": {
                name: {"kind": "aggregation", "id": name, "metric": "amount", "group": [], "reduce": None}
                for name in ("shared", "demand")}, "nodes": {"margin": {"expr": "shared - demand", "unit": "units"}},
            "output_mapping": {"margin": "margin"}, "output_page": None}},
        "invariants": {}, "cadence": {}}


def _deposit(workspace, name, descriptor, files, keys, receipts):
    _, _, attest, identity, now, _, _, sha = _example()
    revision = identity("evidence-artifact-closure/v1", {key: {"content_hash": sha(raw), "size_bytes": len(raw)}
        for key, raw in files.items()})
    redaction = {"id": "fixture-scrub", "revision": "1"}
    grant = {"schema_version": "evidence-usage-grant/v1", "source_id": descriptor["source_id"],
        "source_revision": revision, "permissions": dict.fromkeys(("retrieval", "training", "export"), True),
        "purposes": ["research", "qa-export"], "consumers": ["evidence-wiki"], "not_before": now(),
        "expires_at": keys["expires_at"], "redaction_policy": redaction, "retention": "host-managed"}
    scrub = {"schema_version": "evidence-scrub-receipt/v1", "sanitized_revision": revision,
        "redaction_policy": redaction, "tool": {"name": "fixture-host-scrubber", "version": "1"},
        "outcome": "passed", "completed_at": now()}
    status = workspace.usage.status()
    command = {"schema_version": "evidence-usage-command/v1", "state_id": status["state_id"],
        "workspace_binding": status["workspace_binding"], "expected_checkpoint": status["checkpoint"],
        "request_id": "deposit-" + name, "action": "deposit", "body": {
            "source_id": descriptor["source_id"], "source_revision": revision,
            "grant": attest(grant, "usage", "usage", keys), "scrub": attest(scrub, "scrubber", "scrubber", keys)}}
    envelope = attest(command, "usage", "usage", keys)
    receipt = workspace.usage.transact(envelope, artifacts=files)
    receipts.append({"command": envelope, "artifacts": {key: sha(raw) for key, raw in files.items()}, "receipt": receipt})
    return revision


def fixture_capture_metadata(name, retrieved_at):
    """The exact bundled capture recipe; timestamps come from its native clock."""
    return {"schema_version": "evidence-source-capture-input/v1", "capture_id": "retained-" + name,
                "tool_id": "fixture-input", "tool_version": "1", "origin_url": "https://example.org/" + name,
                "title": "Retained " + name, "retrieved_at": retrieved_at, "capture_method": "manual_transcription",
                "content_format": "markdown", "content_kind": "primary", "completeness": "complete",
                "completeness_note": "Complete synthetic fixture text.", "rights": {
                    "status": "allowed", "license": "CC0-1.0", "terms_url": None, "note": "Fixture only."},
                "scope": {}, "request_id": None, "parents": [], "sanitization": {
                    "required": False, "approved_revision_id": None, "receipt_ref": None}}


def fixture_setup_request(base, fixture):
    """Use the native setup declaration for an exact prospective fixture target."""
    setup_request = _example()[6]
    request = setup_request(base, "Assess the retained synthetic capacity, demand and service note.")
    questions = fixture["inventory"]["questions"]
    request["request"]["payload"]["questions"] = [{"id": row["original_id"], "text": row["question"]} for row in questions]
    request["request"]["payload"]["budgets"]["questions"] = 2
    template = request["decisions"]["question_plans"][0]
    request["decisions"]["question_plans"] = [dict(copy.deepcopy(template), question_id=row["original_id"]) for row in questions]
    return request


def compile_fixture_policy(base, fixture, mode):
    """Derive protected instruction bytes from the actual read-only native plan."""
    from evidence_wiki._script_host import shared_assets_root
    request = fixture_setup_request(base, fixture)
    allowed = base.parent
    while not allowed.exists():
        allowed = allowed.parent
    with Onboarding.open(allowed_roots=[allowed], allow=["plan"]) as onboarding:
        planned = onboarding.plan(request)
    raw = owner("init_research_workspace").frozen_requirements_bytes(planned["profile"]["workspace_init"])
    if raw is None:
        raise ValueError("workload_frozen_requirements_unavailable")
    policy_doc = copy.deepcopy(fixture["global_inputs"]["policy"])
    if mode == "selective_reuse":
        policy_doc.update(schema_version="evidence-strict-policy/v2", review_basis_mode="claim_dependencies_v1")
    elif mode != "legacy_full":
        raise ValueError("workload_review_mode_invalid")
    policy_doc["assurance"] = "artifact_checked"
    policy_doc["instructions"] = {"AGENTS.md": "sha256:" + hashlib.sha256((shared_assets_root() / "workspace-template/AGENTS.md").read_bytes()).hexdigest(),
        "docs/research-requirements.json": "sha256:" + hashlib.sha256(raw).hexdigest()}
    owner("_strict_contract").policy_document(policy_doc)
    return planned, policy_doc


def _run_policy_document(policy_factory, root, objective, run_seconds, *, execution_policy=None, allocation_binding=None):
    """Bind a validated extended allocation without widening fixture defaults."""
    if execution_policy is not None and execution_policy.get("schema_version") == "review-execution-budget-policy/v2":
        from tools._execution_budget import provisioning_window
        provisioning_window(execution_policy, allocation_binding, run_seconds)
        document = policy_factory(root, objective).to_dict()
        document["budgets"]["seconds"] = run_seconds
    else:
        document = policy_factory(root, objective, run_seconds=run_seconds).to_dict()
    document["policy_id"] = None
    return document


def _fixture_preflight_run(allocation_binding):
    """Keep case preflight independent of formal study assignment journals."""
    if (allocation_binding is None
            or allocation_binding.get("schema_version") in {"native-review-case-extended-allocation/v1", "native-review-case-extended-allocation/v2", "native-review-case-extended-allocation/v3", "native-review-case-extended-allocation/v4"}):
        return "fixture-preflight"
    return allocation_binding["assignments"][0]["native_run_id"]


def provision(base, fixture, originals, *, run_seconds=5220, allocation_binding=None, execution_policy=None, children=None, mcp=False, review_mode=None, compiled_profile=None, protected_policy=None, native_case_plan=None):
    """Use admitted native inputs and separately controlled signed services."""
    from tools._review_journal_artifacts import selected as collection_selected
    if getattr(originals, "journal_collection", False) != collection_selected(allocation_binding or {}):
        raise ValueError("workload_journal_collection_policy_invalid")
    if type(mcp) is not bool:
        raise ValueError("workload_mcp_selection_invalid")
    if review_mode not in {None, "legacy_full", "selective_reuse"}:
        raise ValueError("workload_review_mode_invalid")
    application_lease = None
    service = None
    native_extended = (type(allocation_binding) is dict
        and allocation_binding.get("schema_version") in {"native-review-case-extended-allocation/v1", "native-review-case-extended-allocation/v2", "native-review-case-extended-allocation/v3", "native-review-case-extended-allocation/v4"})
    if native_extended:
        from tools._review_dependency_case_budget import validate_allocation
        validate_allocation(execution_policy, allocation_binding, run_seconds,
            fixture=fixture, mode=review_mode, worker_plan=native_case_plan)
    if allocation_binding is not None and "service_policy" in allocation_binding:
        if execution_policy is None or mcp or review_mode != "selective_reuse" or native_case_plan is not None:
            raise ValueError("workload_observational_pilot_only")
        from tools.run_review_dependency_workload import validate_service_policy
        selection = allocation_binding.get("service_selection", {})
        if selection.get("mode") != "selective":
            raise ValueError("workload_observational_pilot_only")
        service = validate_service_policy(allocation_binding["service_policy"], study=selection.get("study"),
            host=selection.get("host"), case_id=selection.get("case_id"),
            observation=allocation_binding.get("observation_policy"), execution=execution_policy)
    if allocation_binding is not None and "formal_application_policy" in allocation_binding:
        from tools.run_review_dependency_workload import validate_formal_application_policy
        selection = ({"mode": allocation_binding["native_case_mode"], "host": "direct"}
            if native_extended else allocation_binding.get("formal_service_selection", {}))
        if (native_case_plan is not None and not native_extended or selection.get("mode") != review_mode
                or selection.get("host") != ("scoped_mcp" if mcp else "direct")):
            raise ValueError("workload_application_policy_invalid")
        service = validate_formal_application_policy(allocation_binding["formal_application_policy"],
            execution_policy, allocation_binding["phase_caps"])
    if native_case_plan is not None and not native_extended and (execution_policy is not None or allocation_binding is not None):
        raise ValueError("workload_measured_worker_recipe_changed")
    if native_case_plan is not None:
        from tools._review_dependency_case_contracts import validate_case_plan
        native_case_plan = validate_case_plan(native_case_plan)
    temporal_override = any(type(row) is dict and "temporal" in row for row in fixture.get("source_material", {}).values())
    if temporal_override and (native_case_plan is None or not native_extended and (execution_policy is not None or allocation_binding is not None)):
        raise ValueError("workload_measured_temporal_recipe_changed")
    if children is not None:
        children.confirm_completed()
    authority_start = authority_end = None
    if execution_policy is not None:
        from tools._execution_budget import provisioning_window
        authority_start, authority_end = provisioning_window(execution_policy, allocation_binding, run_seconds)
    if service is not None:
        from evidence_wiki.review_application_lease import ReviewApplicationLease
        application_lease = ReviewApplicationLease(service["application_lease"]["seconds"],
            min(datetime.fromisoformat(authority_end), datetime.fromisoformat(allocation_binding["authority_expires_at"])).isoformat(),
            schema_version=service["application_lease"]["schema_version"])
    inventory = owner("_strict_contract").claims_document(fixture["inventory"])
    source_names = set(fixture["source_material"])
    required_sources = {"shared", "demand", "unused"}
    valid_sources = source_names == required_sources
    if native_case_plan is not None:
        valid_sources = (required_sources <= source_names and len(source_names) <= 65
            and all(type(name) is str and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name) for name in source_names))
    if (inventory["schema_version"] != "evidence-strict-claims/v2" or len(inventory["claims"]) != 6
            or {row["id"] for row in inventory["claims"]} != CLAIM_IDS
            or {(row["slug"], row["original_id"]) for row in inventory["questions"]} != {("capacity", "capacity"), ("demand", "demand")}
            or len(inventory["questions"]) != 2 or not valid_sources):
        raise ValueError("workload_fixture_inventory_invalid")
    owner("_strict_contract").policy_document(fixture["global_inputs"]["policy"])
    for row in fixture["source_material"].values():
        if type(row) is not dict or type(row.get("raw")) is not str or len(row["raw"].encode()) > 65_536:
            raise ValueError("workload_fixture_source_invalid")
    contexts = fixture.get("question_context", {})
    if type(contexts) is not dict or set(contexts) - {q["slug"] for q in inventory["questions"]}:
        raise ValueError("workload_question_context_invalid")
    question_bodies = {q["slug"]: render_question_body(contexts[q["slug"]], question=q["question"])
        for q in inventory["questions"] if q["slug"] in contexts}
    Meter, grants, attest, identity, now, policy, setup_request, sha = _example()
    base.mkdir(mode=0o700)
    private, control, workers = (base / name for name in ("fixture-private", "fixture-control", "workers"))
    for path in (private, control, workers):
        path.mkdir(mode=0o700)
    keys = {name: secrets.token_bytes(32) for name in ("generator", "evidence", "critic", "usage", "scrubber", "receipt", "observer", "qualifier")}
    keys["expires_at"] = authority_end if execution_policy is not None else (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    objective = "Assess the retained synthetic capacity, demand and service note."
    request = fixture_setup_request(base, fixture)
    questions = fixture["inventory"]["questions"]
    with Onboarding.open(allowed_roots=[base], allow=["apply"]) as onboarding:
        actual_plan = onboarding.plan(request)
        if compiled_profile is not None:
            if actual_plan["profile"] != compiled_profile["profile"] or {key: value for key, value in actual_plan["bindings"].items() if key != "target"} != {key: value for key, value in compiled_profile["bindings"].items() if key != "target"}:
                raise ValueError("workload_compiled_profile_changed")
        setup = onboarding.apply(actual_plan)
    originals.append(("setup", setup))
    if not setup["setup_ready"]:
        raise ValueError("fixture_setup_refused")
    root = base / "workspace"
    for question in questions:
        body = question_bodies.get(question["slug"])
        if body is None:
            continue
        path = root / "wiki/questions" / (question["slug"] + ".md")
        _, front, _ = path.read_text().split("---", 2)
        path.write_text("---" + front + "---\n" + body)
    (root / "sources/jurisdictions.yml").write_text(yaml.safe_dump({"jurisdiction_profiles": [{
        "jurisdiction_id": "fixture-only", "official_domains": ["example.org"], "blocked_domains": []}]}))
    capture_context = HostContext([root], grants, "fixture-host", [root], mutation_control_directory=control)
    captured = {}
    with capture_context.activate(), Workspace.open(root) as workspace:
        for name, material in fixture["source_material"].items():
            metadata = fixture_capture_metadata(name, now())
            delivery = workspace.sources.capture_bytes_document(material["raw"].encode(), metadata)
            prepared = workspace.sources.prepare_capture(request_id="capture-" + name, path="raw/papers/" + name + ".md", delivery=delivery)
            result = workspace.sources.capture_revision(prepared)
            originals.append(("capture-" + name, {"prepared": prepared, "result": result}))
        workspace.sources.inventory()
        originals.append(("normalization", workspace.normalize.sources(all_sources=True)))
    inventory = [json.loads(line) for line in (root / "sources/manifest.jsonl").read_text().splitlines()]
    for name in fixture["source_material"]:
        source = next(row for row in inventory if "raw/papers/" + name + ".md" in row["raw_paths"])
        normalized = root / "sources/normalized" / (owner("normalize_sources").safe_source_id(source["id"]) + ".md")
        captured[name] = {"source_id": source["id"], "normalized_path": normalized.relative_to(root).as_posix(), "capture": source}
        if name in {"shared", "demand"}:
            sidecar = normalized.with_suffix(".structured.json")
            raw = canonical({"records": [{"id": name, "amount": "6" if name == "shared" else "4"}]})
            sidecar.write_bytes(raw)
            _, front, body = normalized.read_text().split("---", 2)
            metadata = yaml.safe_load(front)
            metadata["structured_view"] = {"path": sidecar.relative_to(root).as_posix(), "content_hash": sha(raw)}
            normalized.write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---" + body)
    config_path = root / "research.yml"
    config = yaml.safe_load(config_path.read_bytes())
    config["evidence_trust"] = {"policy_id": "fixture-authority", "policy_revision": "1"}
    config["evidence_usage"] = {"state_id": "fixture-evidence"}
    policy_doc = copy.deepcopy(fixture["global_inputs"]["policy"])
    if review_mode == "selective_reuse":
        policy_doc.update(schema_version="evidence-strict-policy/v2", review_basis_mode="claim_dependencies_v1")
    policy_doc["assurance"] = "artifact_checked"
    policy_doc["instructions"] = {"AGENTS.md": sha((root / "AGENTS.md").read_bytes()),
        "docs/research-requirements.json": sha((root / "docs/research-requirements.json").read_bytes())}
    if protected_policy is not None and policy_doc != protected_policy:
        raise ValueError("workload_prospective_policy_changed")
    config["strict_evidence"] = policy_doc
    config["computation"] = _computation({name: row["normalized_path"] for name, row in captured.items()}, datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    authority = {"schema_version": "evidence-trust-policy/v1", "policy_id": "fixture-authority", "policy_revision": "1",
        "not_before": authority_start if execution_policy is not None else now(), "expires_at": keys["expires_at"], "principals": {
            name: {"controller": name + "-controller", "roles": [role], "keys": {name + "-key": keys[name].hex()}}
            for name, role in (("generator", "generator"), ("evidence", "evaluator"), ("critic", "evaluator"),
                ("usage", "usage"), ("scrubber", "scrubber"))}, "revoked_keys": [], "revoked_envelopes": [],
        "strict_workspaces": {identity("evidence-host-workspace/v1", {"root": str(root)}): policy_doc}}
    authority_path = private / "authority.json"
    authority_path.write_bytes(canonical(authority)); authority_path.chmod(0o600)
    roles = (ExecutionRole("generator", "host_worker", "generator-controller", "simulated/fixture-generator", "generate/v1"),
        ExecutionRole("evidence", "evaluator", "evidence-controller", "simulated/fixture-evidence", "review/v1"),
        ExecutionRole("critic", "evaluator", "critic-controller", "simulated/fixture-critic", "review/v1"))
    registry = ExecutionRoleRegistry(tuple(ExecutionRoleScope(role.config_ref, task, ("model",))
        for role, task in zip(roles, ("researcher", "evaluator", "critic"), strict=True)))
    profile = ExecutionProfile("retained-fixture-only", 512, "EUR", "simulated-no-provider-charge/v1", roles,
        1, 1, 1000, "0", 1, role_scopes=registry, required_diversity=("control", "context"),
        disagreement=DisagreementPlan(), input_handoff=NativeInputHandoff(), review_delivery=ReviewDelivery(context_version="dependencies" if review_mode == "selective_reuse" else "complete"),
        review_application_lease=application_lease)
    grants = grants | {"review_page_operations.prepare", "review_page_operations.page", "review_page_session.assembled",
        "review_packet_execution.bind", "review_packet_execution.reserve", "coverage_observation.observe", "research_session.assemble_review_packet", "host_qualification.current", "service_authentication.status", "claim_diagnostics.current"}
    if mcp:
        grants = grants | {"onboarding.bootstrap"}
    context = HostContext([root], grants, "fixture-host", [root], authority_file=authority_path, state_directory=private,
        mutation_control_directory=control, controller_refs={"host_worker": "generator-controller", "generator": "generator-controller", "evaluator": "evidence-controller"},
        role_controller_refs=tuple((role.config_ref, role.role, role.controller_ref) for role in roles))
    receipts = []
    with context.activate(), Workspace.open(root) as workspace:
        command = {"schema_version": "evidence-usage-command/v1", "state_id": "fixture-evidence",
            "workspace_binding": identity("evidence-host-workspace/v1", {"root": str(root)}),
            "request_id": "initialize", "expected_checkpoint": None, "action": "initialize", "body": {}}
        envelope = attest(command, "usage", "usage", keys)
        receipts.append({"command": envelope, "receipt": workspace.usage.transact(envelope)})
        for name, row in captured.items():
            descriptor = {"schema_version": "evidence-source-revision/v1", "source_id": row["source_id"], "parents": [],
                "normalized_path": row["normalized_path"], "evidence_root": "evidence",
                "temporal": copy.deepcopy(fixture["source_material"][name].get("temporal", {})) if temporal_override else {}}
            files = {"source-record.json": canonical(descriptor), row["normalized_path"]: (root / row["normalized_path"]).read_bytes()}
            sidecar = (root / row["normalized_path"]).with_suffix(".structured.json")
            if sidecar.exists():
                files[sidecar.relative_to(root).as_posix()] = sidecar.read_bytes()
            for path in (root / "raw").rglob("*"):
                if path.is_file() and name in path.relative_to(root).as_posix():
                    files["evidence/" + path.relative_to(root).as_posix()] = path.read_bytes()
            if descriptor["temporal"]:
                owner("_temporal_contract").source_times({"descriptor": descriptor, "files": files, "observed_at": now()})
            elif "temporal" in fixture["source_material"][name] and type(fixture["source_material"][name]["temporal"]) is not dict:
                raise ValueError("workload_acceptance_temporal_invalid")
            row.update(descriptor=descriptor, files=files, revision=_deposit(workspace, name, descriptor, files, keys, receipts))
        originals.append(("source-usage", receipts))
        for q in questions:
            request = workspace.mutations.prepare(request_id="link-" + q["slug"], operation="coverage.link", arguments={
                "slug": q["slug"], "facet_id": "primary", "accepted_source_ids": [row["source_id"] for row in captured.values()]})
            linked = workspace.mutations.submit(request)
            request = workspace.mutations.prepare(request_id="coverage-" + q["slug"], operation="coverage.evaluate", arguments={"slug": q["slug"]})
            evaluated = workspace.mutations.submit(request)
            originals.append(("coverage-" + q["slug"], {"link": linked, "evaluation": evaluated}))
        from evidence_wiki.computation import evaluate
        computed = evaluate(root)
        originals.append(("computation", computed))
        claims = copy.deepcopy(fixture["inventory"])
        for claim in claims["claims"]:
            for evidence in claim["evidence"]:
                row = captured[evidence["source_id"]]
                _, front, _ = (root / row["normalized_path"]).read_text().split("---", 2)
                metadata = yaml.safe_load(front)
                evidence.update(source_id=row["source_id"], record_sha256=sha((root / row["normalized_path"]).read_bytes()),
                    observed_at=metadata["host_capture"]["retrieved_at"])
            for calculation in claim["calculations"]:
                calculation["result_id"] = computed["result_id"]
        margin = next(claim for claim in claims["claims"] if claim["id"] == "margin")
        demand_evidence = next(claim for claim in claims["claims"] if claim["id"] == "observed-demand")["evidence"][0]
        if not any(row["source_id"] == demand_evidence["source_id"] for row in margin["evidence"]):
            margin["evidence"].append(copy.deepcopy(demand_evidence))
        if {row["source_id"] for row in margin["evidence"]} != {captured[name]["source_id"] for name in ("shared", "demand")}:
            raise ValueError("workload_fixture_lineage_invalid")
        proposal = {"schema_version": "evidence-draft-proposal/v1", "originals": [
            {"original_id": q["original_id"], "text": q["question"], "question_slugs": [q["slug"]]} for q in questions], "inventory": claims}
        admit_diagnosed(workspace, proposal, originals)
        question_binding_preflight(root, questions, originals)
    run_doc = _run_policy_document(policy, root, objective, run_seconds,
        execution_policy=execution_policy, allocation_binding=allocation_binding)
    run_doc["scope"]["question_ids"] = [q["original_id"] for q in questions]
    run_doc["budgets"].update(questions=2, claims=10)
    run_policy = RunPolicy(run_doc)
    commands = {role.config_ref: [sys.executable, str(EXAMPLE / "fake_workers.py"), role.config_ref, "retained"] for role in roles}
    if native_case_plan is not None:
        case_path = workers / "review-case-plan.json"
        case_pin = retain(case_path, native_case_plan)
        worker_path = ROOT / "examples/review-cases/review_case_worker.py"
        if not worker_path.is_file():
            raise ValueError("workload_case_worker_unavailable")
        commands = {role.config_ref: [sys.executable, str(worker_path), role.config_ref, str(case_path)] for role in roles}
        originals.append(("native-case-worker-manifest", {"worker_path": str(worker_path), "worker_sha256": digest_bytes(worker_path.read_bytes()), "case_plan": case_pin}))
    runtime_allocation = {"schema_version": "review-workload-runtime-allocation/v2" if execution_policy is not None else "review-workload-runtime-allocation/v1",
        "allocation_binding": allocation_binding, "run_policy": run_policy.to_dict(), "profile": profile.document(),
        "authority_raw_sha256": digest_bytes(authority_path.read_bytes()),
        "authority_start": authority["not_before"], "authority_end": authority["expires_at"],
        "realized_sources": {name: {key: row[key] for key in ("source_id", "normalized_path", "revision")} for name, row in captured.items()},
        "realized_claims_sha256": digest(claims), "realized_input_manifest_sha256": digest(file_manifest(root)),
        "worker_command_manifest": {config: {"argv": command,
            "source_sha256": digest_bytes(Path(command[1]).read_bytes())} for config, command in commands.items()
            if review_mode is None or config in {"evidence", "critic"}},
        "issued_before_worker_outputs": datetime.now(timezone.utc).isoformat()}
    if application_lease is not None:
        runtime_allocation["review_application_binding"] = {"profile_sha256": profile.sha256, "lease": application_lease.document()}
    if execution_policy is not None:
        from tools._execution_budget import credential_expiries
        runtime_allocation.update(execution_policy=execution_policy, execution_policy_sha256=digest(execution_policy),
            credential_expiries=credential_expiries(keys["expires_at"]))
    originals.append(("runtime-allocation", runtime_allocation))
    retain(base.parent / "runtime-allocation.json", runtime_allocation)
    if execution_policy is not None:
        from tools._execution_budget import validate_execution_window
        validate_execution_window(execution_policy, allocation_binding["authority_frozen_at"],
            allocation_binding["authority_expires_at"], instant=datetime.now(timezone.utc))
    if children is not None:
        children.confirm_completed()
    if review_mode != "selective_reuse":
        with context.activate():
            review_packet_preflight(root, profile, [row["source_id"] for row in captured.values()], originals)
    if children is not None:
        children.confirm_completed()
    issuer = QualificationAuthority("fixture-only-issuer", keys["qualifier"], "generator-controller")
    cases = [{"case_id": "case-" + str(i), "family_id": "family-" + str(i // 10),
        "input_document": {"case_id": "case-" + str(i), "supported": i // 2 % 2 == 0, "wording": i % 2},
        "expected_label": "accept" if i // 2 % 2 == 0 else "reject", "critical": i // 2 % 2 != 0,
        "pair_id": "pair-" + str(i // 2)} for i in range(100)]
    suite = reference_suite(cases, suite_ref="fixture-only", split_ref="fixture-held-out", domain_ref="synthetic-local", rubric=policy_doc["rubric"])
    qualifications, reference_observations = [], []
    for config_ref in ("evidence", "critic"):
        executor = BoundedSubprocessExecutor(profile, commands=commands, cwd=workers, receipt_key=keys["receipt"], meter=Meter())
        if children is not None and review_mode is not None:
            children.register_review_executor(executor, "setup_or_owner_worker")
        observations = []
        role = profile.role(config_ref)
        for index, case in enumerate(cases):
            instant = datetime.now(timezone.utc)
            if execution_policy is not None:
                validate_execution_window(execution_policy, allocation_binding["authority_frozen_at"],
                    allocation_binding["authority_expires_at"], instant=instant)
            job = {"schema_version": JOB_V3, "id": "reference-" + str(index), "operation": "evaluate", "role": "evaluator",
                "controller_ref": role.controller_ref, "grant_ref": "fixture-model", "input_id": case["case_id"], "basis_id": "fixture-basis",
                "policy_id": run_policy.policy_id, "policy_version": "1", "prompt_version": role.prompt_version, "revision_precondition": "fixture-basis",
                "original_text": None, "scope": run_policy.to_dict()["scope"]["question_ids"], "required_capabilities": ["model"], "payload": {"claim_id": "reference", "packet_id": case["case_id"]},
                "issued_utc": instant.isoformat(), "deadline_utc": (instant + timedelta(seconds=10)).isoformat(), "timeout_seconds": 5,
                "max_attempts": 1, "expected_result_schema": EXECUTION_OBSERVATION, "budgets": profile.document()["job_budgets"],
                "qualifications": {"assurance": "artifact_checked", "status": "unknown", "evaluator_ref": None, "qualification_ref": None, "context_basis": None},
                "task_role": registry.scope(config_ref).task_role, "role_scope_sha256": registry.scope(config_ref).sha256,
                "context_id": "fixture-reference-" + str(index), "packet_sha256": digest_bytes(canonical(case["input_document"])[:-1])}
            attempt = ExecutionAttempt.create(attempt_id=config_ref + "-reference-" + str(index), nonce="nonce-" + str(index),
                run_id="fixture-references", binding_id="fixture-reference-binding", profile=profile, config_ref=config_ref, job=job, input_document=case["input_document"])
            observed = executor.invoke(attempt)
            reference_observations.append({"attempt": attempt.document(), "observation": executor.authenticate(observed, attempt)})
            originals.append((attempt.attempt_id + "-initial", reference_observations[-1]))
            while observed["state"] == "running":
                time.sleep(0.005)
                observed = executor.poll(attempt)
            authenticated = executor.authenticate(observed, attempt)
            if children is not None:
                children.confirm_completed()
            reference_observations.append({"attempt": attempt.document(), "observation": authenticated})
            originals.append((attempt.attempt_id + "-final", reference_observations[-1]))
            observations.append((attempt, authenticated))
        qualifications.append(issuer.issue(run_policy, profile, config_ref, suite, cases, observations, executor, expires_utc=keys["expires_at"]))
    originals.append(("reference-observations", reference_observations))
    for qualification in qualifications:
        if issuer.verify(qualification) != qualification or len(qualification["observation_refs"]) != len(cases):
            raise ValueError("workload_reference_authentication_required")
    originals.append(("qualifications", qualifications))
    executor = BoundedSubprocessExecutor(profile, commands=commands, cwd=workers, receipt_key=keys["receipt"], meter=Meter())
    if children is not None and review_mode is not None:
        children.register_review_executor(executor, "local_review_worker")
    signer = HostReviewSigner("fixture-authority", "1", SigningIdentity("generator", "generator", "generator-key", "generator-controller", "generator", keys["generator"]),
        tuple(SigningIdentity(name, name, name + "-key", name + "-controller", "evaluator", keys[name]) for name in ("evidence", "critic")), keys["expires_at"],
        **({"not_before_utc": authority_start} if execution_policy is not None else {}))
    if execution_policy is not None:
        expiry = runtime_allocation["credential_expiries"]
        if (authority["expires_at"] != expiry["trust"] or signer.expires_utc != expiry["signing"]
                or any(record["expires_utc"] != expiry["reference_qualification"] for record in qualifications)
                or any(receipt["command"]["authentication"]["expires_at"] != expiry["usage"] for receipt in receipts)):
            raise ValueError("execution_runtime_binding_invalid")
    execution = ResearchExecution(executor, qualifications=QualifiedEvaluators(qualifications, issuer), review_signer=signer, observer_key=keys["observer"])
    runtime = HostRuntime.from_services(run_policy, context, execution)
    if children is not None:
        children.confirm_completed()
    if review_mode == "selective_reuse":
        from evidence_wiki._review_dependency_inputs import bind_session
        selected_run = _fixture_preflight_run(allocation_binding)
        with context.activate(), Workspace.open(root) as workspace:
            with workspace.research.session(runtime.policy, run_id=selected_run, execution=runtime.execution) as session, bind_session(session):
                review_packet_preflight(root, profile, [row["source_id"] for row in captured.values()], originals)
    host_read_preflight(root, runtime, originals)
    if children is not None:
        children.confirm_completed()
    return root, runtime, captured, keys, claims


def strict_rows(result, _claims=None):
    return [{"claim_id": row["claim"]["id"], "canonical_semantic_material_sha256": None,
        "native_claim_snapshot_sha256": digest(row["claim"]),
        "dependency_manifest_sha256": None, "required_review_role": row.get("required_review_role", "evaluator"),
        "current_mechanical_pass": not row["observation"]["reasons"],
        "current_review_pass": row["review"]["passed"], "accepted": row["accepted"],
        "native_observation": row["observation"], "native_review": row["review"],
        "selective_basis_status": "unavailable"} for row in result["claims"]]


def reference_dependencies(root, runtime):
    """Compare fully materialized inputs; these hashes never authorize reuse."""
    with runtime.context.activate():
        core = owner("_strict_evidence")
        config = core.configuration(root)
        policy = core.resolve_policy(root, config)
        with owner("_evidence_usage").current_view(root, config) as view:
            capture, inventory, _ = core.selection_inputs(root, config, policy, view=view)
            sources = owner("_publication_usage").qualify_capture(capture.files, config, view, purpose="qa-export", consumer="evidence-wiki")["selected"]
            computation = owner("_computation_service").required(root, config, view=view)
            authority = owner("_evidence_authority").authority_basis(view.trust)
        claims = {row["id"]: row for row in inventory["claims"]}
        question_dir = owner("question_status").questions_directory(root, config).relative_to(root).as_posix() + "/"
        global_files = {name: digest_bytes(raw) for name, raw in capture.files.items()
            if name != policy["claims_path"] and name not in {"log.md", "index.md"}
            and not name.startswith(("runs/", ".locks/", question_dir + ".locks/"))
            and not (name.startswith(question_dir) and name.endswith(".md"))}
        questions = {}
        for row in inventory["questions"]:
            front, body = owner("verify_quotes").split_page(capture.files[question_dir + row["slug"] + ".md"].decode())
            questions[row["slug"]] = {"record": row, "question": front.get("question") or front.get("summary"),
                "metadata": front.get("metadata"), "body": body}
        global_material = {"files": global_files, "source_revisions": sources, "policy": policy,
            "authority": authority,
            "qualification": runtime.execution.identity(), "producer": owner("_selected_publication").producer_identity(),
            "computation": computation, "claim_membership": sorted(claims), "question_membership": sorted(questions)}
        results = {}
        for identifier in claims:
            closure, stack = set(), [identifier]
            while stack:
                claim_id = stack.pop()
                if claim_id in closure:
                    continue
                closure.add(claim_id)
                stack.extend(claims[claim_id]["premises"])
            slugs = {claims[claim_id]["question_slug"] for claim_id in closure}
            material = {"global": global_material, "claims": [claims[claim_id] for claim_id in sorted(closure)],
                "questions": [questions[slug] for slug in sorted(slugs)]}
            results[identifier] = {"reference_only": True, "material_sha256": digest(material),
                "material": material, "claim_closure": sorted(closure), "question_closure": sorted(slugs)}
        return results


def validate_control_mutation(case):
    paths = {"unrelated-source-raw": "/source_material/unused/raw", "question-demand-body": "/question_context/demand/body"}
    if case["id"] not in paths:
        return
    changes = case["mutations"]
    if (type(changes) is not list or len(changes) != 1 or type(changes[0]) is not dict
            or set(changes[0]) != {"op", "path", "value"} or changes[0]["op"] != "replace"
            or changes[0]["path"] != paths[case["id"]] or type(changes[0]["value"]) is not str):
        raise ValueError("fixture_mutation_unsupported")


def completed_native_cycle(strict, records, publication):
    rows = strict["claims"]
    if len(rows) != 6 or {row["claim"]["id"] for row in rows} != CLAIM_IDS or not all(row["accepted"] for row in rows):
        return False
    applied = [row["data"] for row in records if row["kind"] == "execution_applied"
        and row["data"].get("operation") in {"evaluate", "adjudicate"}]
    expected = {(claim, profile) for claim in CLAIM_IDS for profile in ("evidence", "critic")}
    return (len(applied) == 12 and {(row.get("claim_id"), row.get("profile_ref")) for row in applied} == expected
        and all(row.get("operation") == "evaluate" and row.get("passed") is True for row in applied)
        and publication["verdict"] == "ship")


def mutation(root, fixture, case, captured, keys, runtime, originals):
    """Realize supported mutations at native input coordinates."""
    validate_control_mutation(case)
    mutations = case["mutations"]
    with runtime.context.activate(), Workspace.open(root) as workspace:
        if case["id"] == "unrelated-source-raw":
            row = captured["unused"]
            raw_path = root / "raw/papers/unused.md"
            raw_path.write_text(mutations[0]["value"])
            files = dict(row["files"])
            files["evidence/raw/papers/unused.md"] = raw_path.read_bytes()
            receipts = []
            row["revision"] = _deposit(workspace, "unused-change", row["descriptor"], files, keys, receipts)
            row["files"] = files
            originals.append(("source-mutation", receipts))
        elif case["id"] == "question-demand-body":
            path = root / "wiki/questions/demand.md"
            front = path.read_text().split("---", 2)
            context = copy.deepcopy(fixture["question_context"]["demand"])
            context["body"] = mutations[0]["value"]
            question = next(q["question"] for q in fixture["inventory"]["questions"] if q["slug"] == "demand")
            path.write_text("---" + front[1] + "---\n" + render_question_body(context, question=question))
        else:
            path = root / "docs/research-claims.json"
            inventory = json.loads(path.read_bytes())
            for change in mutations:
                parts = change["path"].strip("/").split("/")
                if parts[:2] != ["inventory", "claims"] or change["op"] != "replace" or len(parts) != 4:
                    raise ValueError("fixture_mutation_unsupported")
                identifier = fixture["inventory"]["claims"][int(parts[2])]["id"]
                next(row for row in inventory["claims"] if row["id"] == identifier)[parts[3]] = copy.deepcopy(change["value"])
            path.write_bytes(canonical(inventory))



def unstarted_job_timing(assignment, cap):
    return {key: assignment[key] for key in ("host_id", "case_id", "cycle", "claim_id", "config_ref", "task_role", "controller_ref", "native_run_id", "journal_key")} | {
        "status": "unstarted", "elapsed_seconds": None, "cap_seconds": cap, "attempt_id": None,
        "application_event_ref": None, "native_record_sha256": None, "reserved_basis_id": None,
        "strict_basis_id": None, "native_passed": None, "started_utc": None, "finished_utc": None,
        "failure": None, "original_timing_pin": None}


@contextmanager
def review_job_timing(originals, collection, *, run_id, claim_id, config_ref, role, controller, cap):
    """Retain exact entered role work, including interrupted and unknown outcomes."""
    matches = [row for row in collection if (row["native_run_id"], row["claim_id"], row["config_ref"]) == (run_id, claim_id, config_ref)]
    if len(matches) > 1 or matches and (matches[0]["status"] != "unstarted" or matches[0]["task_role"] != role
            or matches[0]["controller_ref"] != controller or matches[0]["cap_seconds"] != cap):
        raise ValueError("workload_job_timing_binding_invalid")
    row = matches[0] if matches else {"native_run_id": run_id, "claim_id": claim_id, "config_ref": config_ref,
        "task_role": role, "controller_ref": controller, "cap_seconds": cap, "attempt_id": None,
        "application_event_ref": None, "native_record_sha256": None, "reserved_basis_id": None,
        "strict_basis_id": None, "native_passed": None, "failure": None, "original_timing_pin": None}
    if not matches:
        collection.append(row)
    row.update(status="unknown", started_utc=datetime.now(timezone.utc).isoformat())
    started = time.monotonic()
    try:
        yield row
    except BaseException as error:
        cause = deadline_cause(error)
        row["status"] = "timed_out" if cause is not None else "failed"
        row["failure"] = failure_document(error)
        if cause is not None and cause is not error:
            originals.append((run_id + "-" + config_ref + "-secondary-unwind", {"kind": type(error).__name__[:128]}))
        raise
    finally:
        row.update(elapsed_seconds=time.monotonic() - started, finished_utc=datetime.now(timezone.utc).isoformat())
        if row["elapsed_seconds"] > cap:
            row["status"] = "timed_out"
        raw_row = {key: value for key, value in row.items() if key != "original_timing_pin"}
        row["original_timing_pin"] = originals.append((run_id + "-" + config_ref + "-job-timing", raw_row))



def drive(root, runtime, route, run_id, originals, *, case_id, claim_ids=None, job_cap_seconds=600, job_elapsed=None, children=None):
    """Apply explicitly selected qualified jobs through actual native owners."""
    from evidence_wiki import execution_session as native
    from evidence_wiki.review_packet_execution import reserve_evaluator
    server = None
    call_index = 0
    if route == "scoped_mcp":
        from evidence_wiki.onboarding_mcp import OnboardingMcpServer
        server = OnboardingMcpServer(allowed_roots=[root], runtime=runtime, research_allow=["assemble-review-packet", "advance"])
        response = server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "retained-workload", "version": "1"}}})
        originals.append((run_id + "-mcp-initialize", response))
        server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def call(session, operation, arguments):
        nonlocal call_index
        call_index += 1
        if server is not None:
            response = server.handle_message({"jsonrpc": "2.0", "id": call_index, "method": "tools/call", "params": {
                "name": "research_" + operation.replace("-", "_"), "arguments": {"target": str(root), "run_id": run_id, **arguments}}})
            originals.append((run_id + "-mcp-" + str(call_index), response))
            if "error" in response:
                raise ValueError("native_mcp_cycle_refused")
            envelope = json.loads(response["result"]["content"][0]["text"])
            if envelope["result"] is None:
                raise ValueError("native_mcp_cycle_refused")
            return envelope["result"]
        result = getattr(session, operation.replace("-", "_"))(**arguments)
        originals.append((run_id + "-direct-" + str(call_index), result))
        return result

    steps = []
    job_elapsed = [] if job_elapsed is None else job_elapsed
    try:
        with runtime.context.activate(), Workspace.open(root) as workspace:
            inventory = json.loads((root / "docs/research-claims.json").read_bytes())
            questions = {row["slug"]: row["original_id"] for row in inventory["questions"]}
            with workspace.research.session(runtime.policy, run_id=run_id, execution=runtime.execution) as session:
                with native.gate(session):
                    native._begin(session)
                for original_id in runtime.policy.to_dict()["scope"]["question_ids"]:
                    native._prepare(session, original_id)
                selected = [claim for claim in inventory["claims"] if claim_ids is None or claim["id"] in claim_ids]
                if claim_ids is not None and {row["id"] for row in selected} != set(claim_ids):
                    raise ValueError("workload_assignment_invalid")
                try:
                    for index, claim in enumerate(selected):
                        for config_ref in ("evidence", "critic"):
                            role = runtime.execution.profile.role_scopes.scope(config_ref).task_role
                            controller = runtime.execution.profile.role(config_ref).controller_ref
                            with review_job_timing(originals, job_elapsed, run_id=run_id, claim_id=claim["id"],
                                    config_ref=config_ref, role=role, controller=controller, cap=job_cap_seconds) as timing:
                                with workload_deadline(job_cap_seconds):
                                    job_started = time.monotonic()
                                    identifier = "review-" + digest({"case_id": case_id, "run_id": run_id})[:16] + "-" + str(index) + "-" + config_ref
                                    assembly = identifier + "-packet"
                                    call(session, "assemble-review-packet", {"request_id": assembly, "claim_id": claim["id"], "task_role": role, "page_chars": 16384})
                                    attempt = reserve_evaluator(session, request_id=identifier, assembly_request_id=assembly,
                                        original_id=questions[claim["question_slug"]], claim_id=claim["id"], config_ref=config_ref)
                                    timing.update(attempt_id=attempt.attempt_id, reserved_basis_id=attempt.job["basis_id"])
                                    originals.append((run_id + "-" + identifier + "-attempt", attempt.document()))
                                    originals.append((run_id + "-" + identifier + "-delivery", attempt.worker_document()))
                                    observed, reason = native._external(session, "invoke", attempt)
                                    if reason is not None:
                                        originals.append((run_id + "-" + identifier + "-dispatch-unknown", {"attempt_id": attempt.attempt_id,
                                            "host_return_state": "unavailable", "native_reason": reason}))
                                        raise ValueError("workload_native_dispatch_unavailable")
                                    sequence = 0
                                    while True:
                                        originals.append((run_id + "-" + identifier + "-observation-" + str(sequence), observed))
                                        if observed["state"] != "running" and children is not None:
                                            runtime.execution.executor.authenticate(observed, attempt)
                                            children.confirm_completed()
                                        intake = session.intake_observation(observed, request_id=identifier + "-intake-" + str(sequence))
                                        originals.append((run_id + "-" + identifier + "-intake-" + str(sequence), intake))
                                        if observed["state"] != "running":
                                            break
                                        time.sleep(0.005)
                                        observed, reason = native._external(session, "poll", attempt)
                                        if reason is not None:
                                            originals.append((run_id + "-" + identifier + "-poll-unknown", {"attempt_id": attempt.attempt_id,
                                                "host_return_state": "unavailable", "native_reason": reason}))
                                            raise ValueError("workload_native_dispatch_unavailable")
                                        sequence += 1
                                    step = call(session, "advance", {"request_id": identifier + "-apply", "expected_basis_id": attempt.job["basis_id"]})
                                    if time.monotonic() - job_started > job_cap_seconds:
                                        raise TimeoutError("workload_job_phase_cap")
                                    steps.append(step)
                                    records = runtime.execution.records(session)
                                    applied = [row for row in records if row["kind"] == "execution_applied"
                                        and row["data"].get("operation") == "evaluate" and row["data"].get("attempt_id") == attempt.attempt_id
                                        and row["data"].get("claim_id") == claim["id"] and row["data"].get("profile_ref") == config_ref]
                                    if len(applied) != 1:
                                        raise ValueError("workload_native_review_application_missing")
                                    timing.update(status="applied", application_event_ref=applied[0]["event_ref"],
                                        native_record_sha256=digest_record(applied[0]), strict_basis_id=applied[0]["data"].get("strict_basis_id"),
                                        native_passed=applied[0]["data"].get("passed"))
                    # Question resolution is a global owner obligation after the full cohort.
                    # Claim-local journals must not manufacture a canonical terminal cycle.
                    pass
                finally:
                    originals.finally_records(run_id + "-execution-records", runtime.execution, session)
    finally:
        if server is not None:
            originals.finally_close(server)
    result = {"schema_version": "review-workload-native-steps/v1", "steps": steps,
        "full_autonomous_cycle": "unavailable", "qualification_route": "explicit_complete_packet_evaluator_jobs"}
    originals.append((run_id + "-response", result))
    with runtime.context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(runtime.policy, run_id=run_id, execution=runtime.execution) as session:
            records = runtime.execution.records(session)
        strict = workspace.strict.check()
        publication = workspace.strict.export() if claim_ids is None else None
        config = yaml.safe_load((root / "research.yml").read_bytes())
        with owner("_evidence_usage").current_view(root, config) as view:
            reviews = copy.deepcopy(view.state.strict_reviews)
    originals.extend([(run_id + "-strict", strict), (run_id + "-signed-reviews", reviews)])
    if publication is not None:
        originals.append((run_id + "-publication", publication))
    return result, records, strict, publication



@contextmanager
def service_operation_control(session, route):
    """Borrow one control from the original host window for scoped calls."""
    lease = session.execution.profile.review_application_lease
    if route != "scoped_mcp" or lease is None or lease.document()["schema_version"] != "research-review-application-lease/v2":
        yield None
        return
    from evidence_wiki.operation_control import OperationControl
    from evidence_wiki.review_application_lease import issuance
    frozen = issuance(session)
    end = datetime.fromisoformat(frozen["application_deadline_utc"])
    seconds = min(lease.seconds, (end - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        raise TimeoutError("workload_job_phase_cap")
    control = OperationControl(deadline_utc=frozen["application_deadline_utc"], timeout_seconds=seconds)
    try:
        yield control
    finally:
        control.close()


def drive_selective_services(root, runtime, route, run_id, originals, *, case_id, obligations, job_cap_seconds=600, job_elapsed=None, children=None):
    """Invoke only explicitly frozen fresh services, before any packet reservation."""
    from tools._review_dependency_accounting import validate
    if type(obligations) is not list:
        raise ValueError("workload_service_selection_invalid")
    for obligation in obligations:
        validate("Obligation", obligation)
        coordinate = obligation["coordinate"]
        if obligation["native_run_id"] != run_id or coordinate["case_id"] != case_id or coordinate["host_id"] != route:
            raise ValueError("workload_service_selection_invalid")
    if len({row["obligation_id"] for row in obligations}) != len(obligations):
        raise ValueError("workload_service_selection_duplicate")
    profile = getattr(getattr(runtime, "execution", None), "profile", None)
    lease = getattr(profile, "review_application_lease", None)
    if lease is not None:
        if lease.document()["schema_version"] == "research-review-application-lease/v1":
            if route != "direct" or case_id != "independent-claim-text" or job_cap_seconds != 1800 or lease.seconds != 1800:
                raise ValueError("workload_observational_pilot_only")
        elif lease.document()["schema_version"] != "research-review-application-lease/v2" or lease.seconds != job_cap_seconds:
            raise ValueError("workload_application_policy_invalid")
    if not obligations:
        return {"schema_version": "review-workload-native-steps/v1", "steps": [],
            "full_autonomous_cycle": "unavailable", "qualification_route": "explicit_complete_packet_evaluator_jobs"}
    claim_ids = {row["coordinate"]["claim_id"] for row in obligations}
    from evidence_wiki import execution_session as native
    from evidence_wiki.review_packet_execution import reserve_evaluator
    server = None
    call_index = 0
    service_control = None
    if route == "scoped_mcp":
        from evidence_wiki.onboarding_mcp import OnboardingMcpServer
        server = OnboardingMcpServer(allowed_roots=[root], runtime=runtime, research_allow=["assemble-review-packet", "advance"])
        response = server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "retained-workload", "version": "1"}}})
        originals.append((run_id + "-mcp-initialize", response))
        server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def call(session, operation, arguments):
        nonlocal call_index
        call_index += 1
        if server is not None:
            response = server.handle_message({"jsonrpc": "2.0", "id": call_index, "method": "tools/call", "params": {
                "name": "research_" + operation.replace("-", "_"), "arguments": {"target": str(root), "run_id": run_id, **arguments}}},
                **({"operation_control": service_control} if service_control is not None else {}))
            originals.append((run_id + "-mcp-" + str(call_index), response))
            if "error" in response:
                raise ValueError("native_mcp_cycle_refused")
            envelope = json.loads(response["result"]["content"][0]["text"])
            if envelope["result"] is None:
                raise ValueError("native_mcp_cycle_refused")
            return envelope["result"]
        result = getattr(session, operation.replace("-", "_"))(**arguments)
        originals.append((run_id + "-direct-" + str(call_index), result))
        return result

    steps = []
    job_elapsed = [] if job_elapsed is None else job_elapsed
    try:
        with runtime.context.activate(), Workspace.open(root) as workspace:
            inventory = json.loads((root / "docs/research-claims.json").read_bytes())
            questions = {row["slug"]: row["original_id"] for row in inventory["questions"]}
            with workspace.research.session(runtime.policy, run_id=run_id, execution=runtime.execution) as session:
                with native.gate(session):
                    native._begin(session)
                for original_id in runtime.policy.to_dict()["scope"]["question_ids"]:
                    native._prepare(session, original_id)
                selected = [claim for claim in inventory["claims"] if claim_ids is None or claim["id"] in claim_ids]
                if claim_ids is not None and {row["id"] for row in selected} != set(claim_ids):
                    raise ValueError("workload_assignment_invalid")
                try:
                    for index, claim in enumerate(selected):
                        for config_ref in [row["coordinate"]["config_ref"] for row in obligations if row["coordinate"]["claim_id"] == claim["id"]]:
                            role = runtime.execution.profile.role_scopes.scope(config_ref).task_role
                            controller = runtime.execution.profile.role(config_ref).controller_ref
                            from evidence_wiki.review_application_lease import review_application_window
                            window = review_application_window(session) if runtime.execution.profile.review_application_lease is not None else nullcontext()
                            with window, service_operation_control(session, route) as service_control, review_job_timing(originals, job_elapsed, run_id=run_id, claim_id=claim["id"],
                                    config_ref=config_ref, role=role, controller=controller, cap=job_cap_seconds) as timing:
                                with workload_deadline(job_cap_seconds):
                                    job_started = time.monotonic()
                                    identifier = "review-" + digest({"case_id": case_id, "run_id": run_id})[:16] + "-" + str(index) + "-" + config_ref
                                    assembly = identifier + "-packet"
                                    call(session, "assemble-review-packet", {"request_id": assembly, "claim_id": claim["id"], "task_role": role, "page_chars": 16384})
                                    attempt = reserve_evaluator(session, request_id=identifier, assembly_request_id=assembly,
                                        original_id=questions[claim["question_slug"]], claim_id=claim["id"], config_ref=config_ref)
                                    timing.update(attempt_id=attempt.attempt_id, reserved_basis_id=attempt.job["basis_id"])
                                    originals.append((run_id + "-" + identifier + "-attempt", attempt.document()))
                                    originals.append((run_id + "-" + identifier + "-delivery", attempt.worker_document()))
                                    observed, reason = native._external(session, "invoke", attempt)
                                    if reason is not None:
                                        originals.append((run_id + "-" + identifier + "-dispatch-unknown", {"attempt_id": attempt.attempt_id,
                                            "host_return_state": "unavailable", "native_reason": reason}))
                                        raise ValueError("workload_native_dispatch_unavailable")
                                    sequence = 0
                                    while True:
                                        originals.append((run_id + "-" + identifier + "-observation-" + str(sequence), observed))
                                        if observed["state"] != "running" and children is not None:
                                            runtime.execution.executor.authenticate(observed, attempt)
                                            children.confirm_completed()
                                        intake = session.intake_observation(observed, request_id=identifier + "-intake-" + str(sequence))
                                        originals.append((run_id + "-" + identifier + "-intake-" + str(sequence), intake))
                                        if observed["state"] != "running":
                                            break
                                        time.sleep(0.005)
                                        observed, reason = native._external(session, "poll", attempt)
                                        if reason is not None:
                                            originals.append((run_id + "-" + identifier + "-poll-unknown", {"attempt_id": attempt.attempt_id,
                                                "host_return_state": "unavailable", "native_reason": reason}))
                                            raise ValueError("workload_native_dispatch_unavailable")
                                        sequence += 1
                                    step = call(session, "advance", {"request_id": identifier + "-apply", "expected_basis_id": attempt.job["basis_id"]})
                                    if time.monotonic() - job_started > job_cap_seconds:
                                        raise TimeoutError("workload_job_phase_cap")
                                    steps.append(step)
                                    records = runtime.execution.records(session)
                                    applied = [row for row in records if row["kind"] == "execution_applied"
                                        and row["data"].get("operation") == "evaluate" and row["data"].get("attempt_id") == attempt.attempt_id
                                        and row["data"].get("claim_id") == claim["id"] and row["data"].get("profile_ref") == config_ref]
                                    if len(applied) != 1:
                                        raise ValueError("workload_native_review_application_missing")
                                    timing.update(status="applied", application_event_ref=applied[0]["event_ref"],
                                        native_record_sha256=digest_record(applied[0]), strict_basis_id=applied[0]["data"].get("strict_basis_id"),
                                        native_passed=applied[0]["data"].get("passed"))
                    # Question resolution is a global owner obligation after the full cohort.
                    # Claim-local journals must not manufacture a canonical terminal cycle.
                    pass
                finally:
                    originals.finally_records(run_id + "-execution-records", runtime.execution, session)
    finally:
        if server is not None:
            originals.finally_close(server)
    result = {"schema_version": "review-workload-native-steps/v1", "steps": steps,
        "full_autonomous_cycle": "unavailable", "qualification_route": "explicit_complete_packet_evaluator_jobs"}
    originals.append((run_id + "-response", result))
    from evidence_wiki._review_dependency_inputs import bind_session
    with runtime.context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(runtime.policy, run_id=run_id, execution=runtime.execution) as session, bind_session(session):
            records = runtime.execution.records(session)
            strict = workspace.strict.check()
            publication = workspace.strict.export() if claim_ids is None else None
            config = yaml.safe_load((root / "research.yml").read_bytes())
            with owner("_evidence_usage").current_view(root, config) as view:
                reviews = copy.deepcopy(view.state.strict_reviews)
    originals.extend([(run_id + "-strict", strict), (run_id + "-signed-reviews", reviews)])
    if publication is not None:
        originals.append((run_id + "-publication", publication))
    return result, records, strict, publication


def journal_run_id(journal_key):
    """Use a deterministic native-safe ID for a prospectively selected binding."""
    return "reviews-" + digest_bytes(journal_key.encode())[:24]


def assignment_groups(assignments):
    groups = {}
    subjects = set()
    for row in assignments:
        key = row["journal_key"]
        subject = (row["host_id"], row["case_id"], row["cycle"], row["claim_id"], row["config_ref"])
        if subject in subjects or key != "/".join(subject[:4]) or row["claim_id"] not in CLAIM_IDS:
            raise ValueError("workload_assignment_invalid")
        subjects.add(subject)
        groups.setdefault(key, []).append(row)
    for rows in groups.values():
        if (len(rows) != 2 or {row["config_ref"] for row in rows} != {"evidence", "critic"}
                or any(row["controller_ref"] != row["config_ref"] + "-controller"
                    or row["execution_role"] != "evaluator"
                    or row["task_role"] != ("critic" if row["config_ref"] == "critic" else "evaluator")
                    or row["required"] is not True for row in rows)):
            raise ValueError("workload_assignment_invalid")
    return groups


def authenticated_journal(root, runtime, run_id, originals, *, current=False):
    """Original execution owner authenticates every chain; codec bounds stay native."""
    from tools.replay_qualification_native import journal_snapshot
    with runtime.context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(runtime.policy, run_id=run_id, execution=runtime.execution) as session:
            records = runtime.execution.records(session)
            pin = originals.append_records(run_id + "-authenticated", records)
            snapshot = journal_snapshot(session, records, **({"journal_collection": True} if originals.journal_collection else {}))
            view = session.current() if current else None
    if originals.journal_collection:
        from tools._review_journal_artifacts import RECEIPT, read_collection
        _records, metadata = read_collection(pin)
        summary = {key: metadata[key] for key in ("records_sha256", "records_bytes", "record_count", "physical_nodes", "expanded_bytes", "artifact_kind", "page_pins")}
        summary["schema_version"] = RECEIPT
    else:
        encoded = canonical_records(records)
        summary = {"records_sha256": digest_records(records), "records_bytes": len(encoded)}
    receipt = {**summary, "run_id": run_id,
        "record_count": len(records), "head_ref": digest_record(records[-1]) if records else None,
        "source_sha256": snapshot["source_sha256"], "state_sha256": snapshot["state_sha256"], "original_pin": pin}
    originals.append((run_id + "-owner-receipt", receipt))
    if view is not None:
        originals.append((run_id + "-owner-current", view))
    return records, receipt, snapshot, view


def changed_basis_control(root, runtime, run_id, originals, mutate):
    """Reserve a real complete packet, edit its workspace, and test public advance."""
    from evidence_wiki.review_packet_execution import reserve_evaluator
    with runtime.context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(runtime.policy, run_id=run_id, execution=runtime.execution) as session:
            from evidence_wiki import execution_session as native
            with native.gate(session):
                native._begin(session)
            for original in runtime.policy.to_dict()["scope"]["question_ids"]:
                native._prepare(session, original)
            from evidence_wiki.review_application_lease import review_application_window
            window = review_application_window(session) if runtime.execution.profile.review_application_lease is not None else nullcontext()
            with window:
                request = "changed-basis-control"
                session.assemble_review_packet(request_id=request + "-packet", claim_id="observed-capacity",
                    task_role="evaluator", page_chars=16384)
                attempt = reserve_evaluator(session, request_id=request, assembly_request_id=request + "-packet",
                    original_id="capacity", claim_id="observed-capacity", config_ref="evidence")
                originals.append(("changed-basis-attempt", attempt.document()))
                originals.append(("changed-basis-worker", attempt.worker_document()))
                mutate()
                from evidence_wiki.errors import EvidenceWikiError
                try:
                    session.advance(request_id=request + "-advance", expected_basis_id=attempt.job["basis_id"])
                except EvidenceWikiError as error:
                    if error.error_code != "RESEARCH_BASIS_CHANGED":
                        raise
                    result = {"attempt_id": attempt.attempt_id, "reserved_basis_id": attempt.job["basis_id"],
                        "native_error_code": error.error_code, "native_reason": "BASIS_CHANGED", "applied": False}
                    originals.append(("changed-basis-control", result))
                    return result
                raise ValueError("workload_changed_basis_guard_missing")


@dataclass(frozen=True)
class _ChildCompletion:
    process: object
    ordinal: int
    pid: int
    spawn_pin: str
    plan_sha256: str
    completion_pin: str


class OwnedChildren:
    """Track exact native Popen objects from setup through final cleanup."""

    def __init__(self, originals, plan_sha256):
        import threading
        self.originals, self.plan_sha256 = originals, plan_sha256
        self.owned, self.closed = [], False
        self._spawn_pins, self._completed = {}, {}
        self.lock = threading.RLock()
        self.real_popen = None
        self._review_dispatch_state = None
        self._review_executors = []

    def register_review_executor(self, executor, kind):
        """Retain actual native executor objects for the optional custody route."""
        if type(executor) is not BoundedSubprocessExecutor or kind not in {"local_review_worker", "setup_or_owner_worker"}:
            raise ValueError("workload_dispatch_executor_invalid")
        with self.lock:
            if any(selected is executor for selected, _kind in self._review_executors):
                raise ValueError("workload_dispatch_executor_duplicate")
            self._review_executors.append((executor, kind))

    def bind_review_dispatch(self, plan, runtime):
        from tools._review_dependency_accounting import document_id, validate_plan
        validate_plan(plan)
        with self.lock:
            if self.closed or self._review_dispatch_state is not None or not any(selected is runtime.execution.executor and kind == "local_review_worker" for selected, kind in self._review_executors):
                raise ValueError("workload_dispatch_binding_invalid")
            self._review_dispatch_state = {"plan_id": document_id(plan), "runtime": runtime,
                "identity": "review-tracker-" + secrets.token_hex(16), "executors": tuple(self._review_executors)}

    def review_dispatch_snapshot(self, executor, phase, original_pins):
        from tools._review_dependency_accounting import dispatch_snapshot
        return dispatch_snapshot(self, executor, phase, original_pins)

    def open(self, *, predecessor=None):
        from tools._integration_owner_pass import _defer_signals, _group_alive
        if predecessor is None:
            self.real_popen = subprocess.Popen
        else:
            from tools._review_dependency_runner import _admit_handoff
            self.real_popen = _admit_handoff(predecessor)
        def launch(*args, **kwargs):
            with self.lock, _defer_signals():
                if self.closed or len(self.owned) >= 4096 or kwargs.get("start_new_session") is not True:
                    raise ValueError("workload_child_launch_refused")
                process = self.real_popen(*args, **kwargs)
                if type(process) is _POPEN_TYPE:
                    ProcessGroupOwnership(process, _group_alive)
                self.owned.append(process)
                if os_getpgid(process.pid) != process.pid:
                    raise ValueError("workload_child_group_changed")
                pin = self.originals.append(("child-" + str(len(self.owned)) + "-spawn",
                    {"pid": process.pid, "pgid": process.pid, "plan_sha256": self.plan_sha256}))
                self._spawn_pins[id(process)] = (process, len(self.owned), json.dumps(pin, sort_keys=True))
                return process
        with self.lock, _defer_signals():
            if predecessor is not None:
                from tools._review_dependency_runner import _admit_handoff
                if _admit_handoff(predecessor) is not self.real_popen:
                    raise ValueError("workload_dispatch_handoff_changed")
            self.launch = launch
            subprocess.Popen = launch

    def confirm_completed(self):
        """Retain first actual group absence without reaping or signalling children."""
        from tools._integration_owner_pass import _defer_signals, _group_alive

        with self.lock, _defer_signals():
            if self.closed:
                return
            for process in self.owned:
                if id(process) in self._completed or process.returncode is None:
                    continue
                spawn = self._spawn_pins.get(id(process))
                if (spawn is None or spawn[0] is not process
                        or type(process.pid) is not int or process.pid <= 0):
                    continue
                try:
                    guard = ownership_for(process)
                    if guard is None and type(process) is _POPEN_TYPE:
                        raise ValueError("workload_child_cleanup_unconfirmed")
                    absent = guard.confirm_completed() if guard is not None else _group_alive(process) is False
                except (Exception, SystemExit):
                    absent = False
                if not absent:
                    continue
                _, ordinal, spawn_pin = spawn
                receipt = {"pid": process.pid, "pgid": process.pid, "ordinal": ordinal,
                    "plan_sha256": self.plan_sha256, "spawn_pin": json.loads(spawn_pin),
                    "leader_reaped": True, "returncode": process.returncode, "group_absent": True}
                pin = self.originals.append(("child-" + str(ordinal) + "-completed", receipt))
                self._completed[id(process)] = _ChildCompletion(process, ordinal, process.pid,
                    spawn_pin, self.plan_sha256, json.dumps(pin, sort_keys=True))

    def close(self):
        import os

        from tools._integration_owner_pass import _defer_signals, _group_alive
        results, failed = [], False
        with self.lock, _defer_signals():
            self.closed = True
            owned = tuple(self.owned)
            completed = dict(self._completed)
            spawn_pins = dict(self._spawn_pins)
        for ordinal, process in enumerate(owned, 1):
            try:
                certificate = completed.get(id(process))
                if certificate is not None:
                    if (certificate.process is not process or certificate.ordinal != ordinal
                            or certificate.plan_sha256 != self.plan_sha256
                            or spawn_pins.get(id(process)) != (process, ordinal, certificate.spawn_pin)):
                        raise ValueError("workload_child_cleanup_unconfirmed")
                    result = {"pid": certificate.pid, "leader_reaped": True, "group_absent": True,
                        "completion_pin": json.loads(certificate.completion_pin)}
                else:
                    if type(process.pid) is not int or process.pid <= 0:
                        raise ValueError("workload_child_cleanup_unconfirmed")
                    guard = ownership_for(process)
                    if guard is not None:
                        guard.stop()
                        if not guard.confirm_completed():
                            raise ValueError("workload_child_cleanup_unconfirmed")
                    else:
                        if type(process) is _POPEN_TYPE:
                            raise ValueError("workload_child_cleanup_unconfirmed")
                        # Legacy test doubles have no native wait/ownership guard.
                        with process._waitpid_lock:
                            if process.returncode is None:
                                if os.getpgid(process.pid) != process.pid:
                                    raise ValueError("workload_child_group_changed")
                                os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2)
                        if _group_alive(process) is not False:
                            raise ValueError("workload_child_cleanup_unconfirmed")
                    result = {"pid": process.pid, "leader_reaped": True, "group_absent": True}
            except (Exception, SystemExit) as error:
                failed = True
                result = {"pid": process.pid, "leader_reaped": None, "group_absent": None,
                    "failure": failure_document(error)}
            results.append(result)
            self.originals.append(("child-" + str(len(results)) + "-closed", result))
        # The closed launch wrapper stays installed until this isolated CLI
        # exits. Timed-out trusted callbacks can never launch unowned children.
        if failed:
            raise ValueError("workload_child_cleanup_unconfirmed")
        return {"status": "confirmed", "children": results, "plan_sha256": self.plan_sha256}


def os_getpgid(pid):
    import os
    return os.getpgid(pid)



def native_dependency_rows(root, runtime, run_id):
    """Observe all current native identities without a reference-hash substitute."""
    from evidence_wiki._review_dependency_inputs import bind_session
    with runtime.context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(runtime.policy, run_id=run_id, execution=runtime.execution) as session, bind_session(session):
            core = owner("_strict_evidence")
            config = core.configuration(root)
            policy = core.resolve_policy(root, config)
            if policy is None or policy.get("schema_version") != "evidence-strict-policy/v2":
                raise ValueError("workload_native_dependency_mode_required")
            with owner("_evidence_usage").current_view(root, config) as view:
                result, (_capture, producer, currents, _reviews) = core.typed(
                    core._evaluate_dependency_observations)(root, config, policy, view=view)
            if result.get("schema_version") != "evidence-strict-result/v3":
                raise ValueError("workload_native_dependency_mode_required")
            claims = result["claims"]
            if (len(claims) != 6 or {claim["claim"]["id"] for claim in claims} != CLAIM_IDS
                    or len(currents) != 6 or {current["material"]["claim_id"] for current in currents} != CLAIM_IDS):
                raise ValueError("workload_native_dependency_inventory_invalid")
            if producer is None or result["review_producer_id"] != producer.producer_id:
                raise ValueError("workload_native_dependency_producer_invalid")
            by_claim = {current["material"]["claim_id"]: current for current in currents}
            rows = []
            for claim in claims:
                identifier = claim["claim"]["id"]
                current = by_claim[identifier]
                basis = current["dependency_basis"]
                if (basis["target_claim_id"] != identifier or basis["basis_id"] != claim["claim_basis_id"]
                        or current["material"]["producer_id"] != producer.producer_id
                        or current["observation"] != claim["observation"]
                        or current["observation"]["producer_id"] != producer.producer_id
                        or current["observation"]["claim_id"] != identifier
                        or current["observation"]["basis_id"] != basis["basis_id"]
                        or current["required_review_role"] != claim["required_review_role"]):
                    raise ValueError("workload_native_dependency_current_mismatch")
                rows.append({"claim_id": identifier, "basis_id": basis["basis_id"],
                    "semantic_material_id": basis["semantic_material_id"], "review_producer_id": producer.producer_id,
                    "dependency_basis": basis, "accepted": claim["accepted"],
                    "required_review_role": claim["required_review_role"], "observation": claim["observation"]})
            if len(rows) != 6 or {row["claim_id"] for row in rows} != CLAIM_IDS:
                raise ValueError("workload_native_dependency_inventory_invalid")
            return rows


def selective_rows(result):
    """Expose exact native selective fields, preserving unavailable states."""
    if result.get("schema_version") != "evidence-strict-result/v3":
        raise ValueError("workload_native_dependency_mode_required")
    return [{"claim_id": row["claim"]["id"], "claim_basis_id": row["claim_basis_id"],
        "review_basis_mode": result["review_basis_mode"], "review_producer_id": result["review_producer_id"],
        "claim_schema_version": result["claim_schema_version"], "accepted": row["accepted"],
        "native_observation": row["observation"], "native_review": row["review"]} for row in result["claims"]]


def completed_obligation_cycle(report, plan, strict, publication, *, cycle):
    """Preserve useful native completion with every required service visible."""
    from tools._review_dependency_accounting import validate_report
    validate_report(report, plan)
    chosen = [resolution for obligation, resolution in zip(plan["obligations"], report["resolutions"], strict=True)
              if obligation["coordinate"]["cycle"] == cycle]
    return (len(chosen) == 12 and all(row["status"] in {"fresh_applied", "reused_validated"} for row in chosen)
        and len(strict["claims"]) == 6 and {row["claim"]["id"] for row in strict["claims"]} == CLAIM_IDS
        and all(row["accepted"] is True for row in strict["claims"])
        and publication["verdict"] == "ship" and publication["selection_complete"] is True
        and {row["original_id"] for row in publication["original_outcomes"]} == {"capacity", "demand"}
        and all(row["accepted"] is True for row in publication["original_outcomes"]))
