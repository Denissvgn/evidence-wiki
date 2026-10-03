"""Explicit bounded research steps over canonical runs and current release owners."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from time import perf_counter

from ._facades._base import _process_exit, _translated, call_seam
from .errors import EvidenceWikiError
from .host_context import checked_operation, current_context
from .mutation_contracts import canonical, detached, digest, validate_request_id
from .mutation_operations import _generation, _host_selection
from .research_completion import completion
from .research_observation import ResearchObservation
from .research_session_contracts import BINDING, CURRENT, NEXT, STEP, checked, refuse
from .run_policy import OBSERVATION, RunPolicy
from .workflow_contracts import JOB, validate_job

TERMINAL_GRANTS = ("run_controller.run_session_start", "run_controller.run_session_record",
                   "run_controller.run_session_transition", "run_controller.run_session_finish")


def _boundary(function):
    """Guard the complete public call, then apply existing refusal translation."""
    method = function.__qualname__.startswith("ResearchSession.")
    seam = {"__init__": "session", "next": "next_work"}.get(function.__name__, function.__name__)
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            owner_args = args[1:] if method and seam == "session" else (args[0].root,) if method else args
            with checked_operation("research_session", seam, owner_args, kwargs) as revalidate:
                revalidate()
                return function(*args, **kwargs)
        except (Exception, SystemExit) as error:
            if isinstance(error, EvidenceWikiError):
                raise
            try:
                translated = _translated(error)
            except SystemExit as renderer_exit:
                translated = _process_exit(renderer_exit)
            if translated is None:
                raise
            if translated.error_code == "LIBRARY_PROCESS_EXIT":
                raise translated from None
            raise translated from error
    return guarded


def _policy(value):
    return value if isinstance(value, RunPolicy) else RunPolicy.from_dict(value)


def _scope(project_root, operation, *, write=False):
    context = current_context()
    if context is None or context.mutation_control_directory is None:
        refuse("HOST_CONTEXT_REQUIRED")
    context.require_grant("research_session." + operation)
    try:
        root = context.require_path(Path(project_root).expanduser().resolve(strict=True), write=write)
    except (OSError, ValueError, TypeError, RuntimeError):
        refuse("WORKSPACE_UNAVAILABLE")
    return root, context


def _load(root, stem):
    from .workspace import Workspace
    with Workspace.open(root) as workspace:
        return workspace._script(stem)


def _owner(root, seam, **kwargs):
    return call_seam(lambda stem: _load(root, stem), "run_controller", seam, root, **kwargs)


def _original_binding(view):
    return digest([{"original_id": row["original_id"], "original_text": row["original_text"],
        "question_ids": [question["question_id"] for question in row["questions"]], "missing_question_ids": row["missing_question_ids"]}
        for row in view["accounting"]["original_outcomes"]])


def _run_summary(document):
    """Expose lifecycle facts without transporting canonical inventories."""
    if document is None:
        return None
    return {"run_id": document["run_id"], "actor_ref": document["agent_id"],
        "state": document["state"]["current"], "allowed_next_states": document["state"]["allowed_next_states"],
        "started_utc": document.get("started_at"), "updated_utc": document.get("updated_at"),
        "last_heartbeat_utc": document.get("last_heartbeat_at"),
        "caller_context_id": (document.get("caller_context") or {}).get("context_id"),
        "session_binding_id": digest(document["research_session"]) if document.get("research_session") is not None else None,
        "terminal": document["state"]["current"] in {"complete", "blocked_on_sources", "no_ship", "failed"}}


def _job_original(root, policy, view, operation, payload, original_text):
    """Bind the closed job payload to an observed original and source closure."""
    originals = view["accounting"]["original_outcomes"]
    if type(payload) is not dict:
        refuse("JOB_PAYLOAD_INVALID", "/payload")
    if original_text is not None and type(original_text) is not str:
        refuse("JOB_PAYLOAD_INVALID", "/original_text")
    if operation == "generate":
        if set(payload) != {"question"}:
            refuse("JOB_PAYLOAD_INVALID", "/payload")
        if len(originals) != 1:
            refuse("ORIGINAL_SCOPE_UNSUPPORTED", "/scope")
        original = originals[0]
        if payload["question"] != original["original_text"]:
            refuse("ORIGINAL_TEXT_CHANGED", "/payload/question")
    else:
        if set(payload) != {"claim_id", "packet_id"} or any(type(value) is not str for value in payload.values()):
            refuse("JOB_PAYLOAD_INVALID", "/payload")
        context = current_context()
        context.require_grant("strict_evidence.run_operation.prepare-review")
        prepared = call_seam(lambda stem: _load(root, stem), "strict_evidence", "run_operation", root,
                             operation="prepare-review", claim_id=payload["claim_id"])
        snapshot = prepared["snapshot"]
        if digest(snapshot) != payload["packet_id"]:
            refuse("PACKET_CHANGED", "/payload/packet_id")
        question = snapshot["question"]
        matches = [row for row in originals if row["original_id"] == question["original_id"]
                   and any(item["slug"] == question["slug"] for item in row["questions"])]
        if len(matches) != 1:
            refuse("ORIGINAL_SCOPE_UNSUPPORTED", "/scope")
        original = matches[0]
        sources = set(snapshot["source_revisions"])
        for claim in [snapshot["claim"], *snapshot["premises"]]:
            sources.update(evidence["source_id"] for evidence in claim["evidence"])
        if not sources or not sources <= set(policy["scope"]["source_scope"]):
            refuse("SOURCE_SCOPE_UNSUPPORTED", "/scope")
    if original["missing_question_ids"]:
        refuse("ORIGINAL_SCOPE_UNSUPPORTED", "/scope")
    exact = original["original_text"]
    if original_text is not None and original_text != exact:
        refuse("ORIGINAL_TEXT_CHANGED", "/original_text")
    return [original["original_id"]], exact


@_boundary
def current(project_root, policy, *, run_id=None):
    """Return current owner accounting; qualification and missing usage stay unknown."""
    root, context = _scope(project_root, "current")
    frozen = _policy(policy)
    for grant in ("workspace.open", "workspace_status.run_status_report", "source_requests.run_list_requests",
                  "strict_evidence.run_operation.check", "strict_evidence.run_operation.export"):
        context.require_grant(grant)
    if run_id is not None:
        validate_request_id(run_id)
    view = ResearchObservation(root, run_id=run_id)
    release = completion(root, allow_partial=frozen.to_dict()["source_rules"]["allow_partial"], run_id=run_id)
    mapping = view.mapping.get("result")
    originals = mapping.get("originals", []) if mapping else []
    ids = [row["id"] for row in originals]
    compatible = bool(mapping and not mapping["gaps"] and set(ids) == set(frozen.to_dict()["scope"]["question_ids"]))
    view.finish()
    return checked(CURRENT, {"schema_version": CURRENT, "target": str(root), "run_id": run_id,
        "basis_id": view.basis["workspace_revision"], "policy_id": frozen.policy_id,
        "observed_utc": view.now.isoformat(), "accounting": {
            "original_ids": ids, "original_outcomes": release["original_outcomes"],
            "accounting_complete": release["accounting_complete"], "missing_mapping": mapping is None,
            "accepted_count": sum(row["accepted"] for row in release["original_outcomes"]),
            "unresolved_count": sum(not row["accepted"] for row in release["original_outcomes"])},
        "canonical_release": {"status": release["status"], "research_complete": release["research_complete"],
            "publication": release["publication"], "gaps": release["gaps"], "assurance": view.strict_summary().get("assurance")},
        "run": _run_summary(view.run), "sources": {"inventory": view.status.get("sources", {}), "requests": view.requests},
        "qualification": {"state": "unknown", "reason": "no_authenticated_evaluator_profile_owner"},
        "usage": {"seconds": None, "model_calls": None, "tool_calls": None, "tokens": None, "cost_limit": None,
            "claims": None, "sources": None, "repair_cycles": None, "adjudication_cycles": None,
            "retry_attempts": None, "acquisition_attempts": None},
        "policy_compatible": compatible,
        "authority": {"terminal_grants_available": all(grant in context.grants for grant in TERMINAL_GRANTS),
                      "required_terminal_grants": list(TERMINAL_GRANTS)},
        "limitations": ["Current canonical acceptance is separate from evaluator calibration and workflow execution qualification.",
            "URI-prefix access scope is not a concrete source identity; active policy references are never expanded.",
            "Legacy uninstrumented usage is unknown, not zero."], "authorized": False, "evidence_accepted": False})


def _observation(view, policy):
    """Adapt observed owner facts without constructing missing qualification."""
    count = len(policy.to_dict()["scope"]["question_ids"])
    return {"schema_version": OBSERVATION, "policy_id": policy.policy_id, "basis_id": view["basis_id"],
        "expected_basis_id": view["basis_id"], "observation_kind": "canonical_owner",
        "owner_ref": "canonical-current-release", "receipt_ref": digest(view["canonical_release"]),
        "policy_compatible": view["policy_compatible"], "authority_granted": view["authority"]["terminal_grants_available"],
        "model_required": False, "model_granted": None, "acquisition_granted": None,
        "critical_disagreement": None, "transient_failure": False, "source_current": None,
        "source_present": None, "mechanical_pass": None, "review_pass": None, "qualified": None,
        "context_complete": None, "original_count": count, "accepted_count": 0, "unresolved_count": count,
        "selected_outcomes": [], "evaluators": [], "spent": view["usage"]}


@_boundary
def next_work(project_root, policy, *, run_id=None):
    _scope(project_root, "next_work")
    policy = _policy(policy)
    view = current(project_root, policy, run_id=run_id)
    decision = policy.decide(_observation(view, policy))
    return checked(NEXT, {"schema_version": NEXT, "current": view, "decision": decision,
        "actions": [{"operation": "terminal", "owner": "run_controller", "arguments": {"outcome": decision["outcome"]},
            "preconditions": ["same_policy", "same_context", "current_canonical_basis"],
            "effects": ["canonical_decision_event", "canonical_terminal_run"],
            "required_grants": ["research_session.advance", *TERMINAL_GRANTS],
            "reason": decision["reason"], "authorized": False, "evidence_accepted": False}],
        "authorized": False, "evidence_accepted": False})


@_boundary
def session(project_root, policy, *, run_id):
    """Construct the bound session through the guarded public owner seam."""
    return ResearchSession(project_root, policy, run_id=run_id)


class ResearchSession:
    """One immutable policy binding on the existing canonical run and event journal."""

    @_boundary
    def __init__(self, project_root, policy, *, run_id):
        root, context = _scope(project_root, "session")
        self._root, self._policy, self._run_id = root, _policy(policy), validate_request_id(run_id)
        self._context = context
        self._closed = False
        self._owner_workspace = None
        view = current(root, self.policy, run_id=None)
        if not view["policy_compatible"]:
            refuse("SCOPE_ACCOUNTING")
        info = root.stat()
        binding = checked(BINDING, {"schema_version": BINDING, "run_id": self.run_id,
            "actor_ref": context.actor_ref, "policy": self.policy.to_dict(), "policy_id": self.policy.policy_id,
            "target": str(root), "workspace_binding": {"path": str(root), "device": str(info.st_dev), "inode": str(info.st_ino)},
            "request_binding": _original_binding(view), "host_context_sha256": _host_selection(),
            "owner_generation": _generation(), "original_ids": view["accounting"]["original_ids"],
            "authorized": False, "evidence_accepted": False})
        self._binding_raw = canonical(binding)

    @property
    def root(self):
        return self._root

    @property
    def policy(self):
        return self._policy

    @property
    def run_id(self):
        return self._run_id

    @property
    def context(self):
        return self._context

    @property
    def closed(self):
        return self._closed

    @property
    def binding(self):
        import json
        return json.loads(self._binding_raw)

    def _check(self, operation, *, write=False):
        if self.closed:
            refuse("SESSION_CLOSED")
        if self._owner_workspace is not None:
            self._owner_workspace._check_open()
        root, context = _scope(self.root, operation, write=write)
        if (context is not self.context or root != self.root or _host_selection() != self.binding["host_context_sha256"]
                or _generation() != self.binding["owner_generation"]):
            refuse("BINDING_CHANGED")
        info = root.stat()
        if {"path": str(root), "device": str(info.st_dev), "inode": str(info.st_ino)} != self.binding["workspace_binding"]:
            refuse("BINDING_CHANGED")
        if self.policy.to_dict() != self.binding["policy"] or self.run_id != self.binding["run_id"]:
            refuse("BINDING_CHANGED")
        view = current(root, self.policy, run_id=None)
        if _original_binding(view) != self.binding["request_binding"]:
            refuse("ORIGINALS_CHANGED")

    def close(self):
        self._closed = True

    def __enter__(self):
        if self.closed:
            refuse("SESSION_CLOSED")
        return self

    def __exit__(self, *_args):
        self.close()

    @_boundary
    def current(self):
        self._check("current")
        controller = _load(self.root, "run_controller")
        selected = self.run_id if controller.run_state_path(self.root, self.run_id).exists() else None
        return current(self.root, self.policy, run_id=selected)

    @_boundary
    def next(self):
        self._check("next_work")
        controller = _load(self.root, "run_controller")
        selected = self.run_id if controller.run_state_path(self.root, self.run_id).exists() else None
        return next_work(self.root, self.policy, run_id=selected)

    def _run(self):
        return _owner(self.root, "run_session_start", run_id=self.run_id, agent_id=self.context.actor_ref, binding=self.binding)

    def _finish(self, document, reason):
        controller = _load(self.root, "run_controller")
        if document["state"]["current"] == "initialized":
            document = _owner(self.root, "run_session_transition", run_id=self.run_id, agent_id=self.context.actor_ref,
                to_state="planned", reason=reason)
        if document["state"]["current"] not in controller.TERMINAL_STATES:
            allowed = document["state"]["allowed_next_states"]
            final = "no_ship" if "no_ship" in allowed else "blocked_on_sources" if "blocked_on_sources" in allowed else "failed"
            document = _owner(self.root, "run_session_finish", run_id=self.run_id, agent_id=self.context.actor_ref,
                final_verdict=final, reason=reason)
        return document

    @_boundary
    def advance(self, *, request_id, expected_basis_id):
        self._check("advance", write=True)
        request_id = validate_request_id(request_id)
        for grant in TERMINAL_GRANTS:
            self.context.require_grant(grant)
        controller = _load(self.root, "run_controller")
        path = controller.run_state_path(self.root, self.run_id)
        if path.exists():
            document = controller.load_run_state(self.root, self.run_id)
            if document.get("research_session") != self.binding:
                refuse("BINDING_CHANGED")
            for event in controller.load_events(controller.events_path(self.root, self.run_id)):
                data = event.get("data") or {}
                if event["event_type"] == "custom.research.decision" and data.get("request_id") == request_id:
                    record = data["record"]
                    if record.get("schema_version") != STEP or record["before_basis_id"] != expected_basis_id:
                        refuse("REQUEST_CONFLICT")
                    document = self._finish(document, record["reason"])
                    result = detached(record)
                    result.update(historical=True, event_ref=event["event_id"], canonical_run_state=document["state"]["current"])
                    return checked(STEP, result)
        view = current(self.root, self.policy, run_id=self.run_id if path.exists() else None)
        if view["basis_id"] != expected_basis_id:
            refuse("BASIS_CHANGED")
        decision = self.policy.decide(_observation(view, self.policy))
        document = self._run()
        record = checked(STEP, {"schema_version": STEP, "request_id": request_id, "run_id": self.run_id,
            "binding_id": digest(self.binding), "before_basis_id": expected_basis_id,
            "outcome": decision["outcome"], "reason": decision["reason"], "terminal": True, "decision": decision,
            "job": None, "event_ref": None, "canonical_run_state": "no_ship", "accounting": view["accounting"],
            "historical": False, "authorized": False, "evidence_accepted": False})
        event = _owner(self.root, "run_session_record", run_id=self.run_id, agent_id=self.context.actor_ref,
            request_id=request_id, binding=self.binding, record=record)
        document = self._finish(document, decision["reason"])
        record["event_ref"] = event["event_id"]
        record["canonical_run_state"] = document["state"]["current"]
        return checked(STEP, record)

    @_boundary
    def run_until_terminal(self, *, request_id, expected_basis_id, max_steps=1):
        self._check("run_until_terminal", write=True)
        if type(max_steps) is not int or not 1 <= max_steps <= 64:
            refuse("LIMIT", "/max_steps")
        started = perf_counter()
        result = self.advance(request_id=request_id, expected_basis_id=expected_basis_id)
        if perf_counter() - started > self.policy.to_dict()["budgets"]["seconds"]:
            refuse("BUDGET_EXHAUSTED")
        return result

    @_boundary
    def host_job(self, *, request_id, operation, payload, original_text=None):
        """Retain an inert bounded job; execution and authentication stay in the host."""
        self._check("host_job", write=True)
        request_id = validate_request_id(request_id)
        payload = detached(payload)
        from .source_commands import _no_secret_values
        _no_secret_values(payload)
        effect = {"generate": "model", "acquire": "acquire", "evaluate": "adjudicate"}.get(operation)
        policy = self.policy.to_dict()
        if effect is None or effect not in policy["allowed_effects"] or policy["host_capability_refs"][effect] is None:
            refuse("HOST_CAPABILITY_UNAVAILABLE")
        self.context.require_grant("research_session.host_job." + operation)
        for grant in ("run_controller.run_session_start", "run_controller.run_session_record"):
            self.context.require_grant(grant)
        if operation == "acquire":
            refuse("SOURCE_MAPPING_UNSUPPORTED")
        if policy["budgets"]["model_calls"] == 0 or policy["budgets"]["tokens"] == 0:
            refuse("BUDGET_EXHAUSTED")
        controller = _load(self.root, "run_controller")
        if controller.run_state_path(self.root, self.run_id).exists():
            document = controller.load_run_state(self.root, self.run_id)
            if document.get("research_session") != self.binding:
                refuse("BINDING_CHANGED")
            jobs = []
            for event in controller.load_events(controller.events_path(self.root, self.run_id)):
                data = event.get("data") or {}
                if event["event_type"] == "custom.research.decision" and data.get("record", {}).get("kind") == "host_job":
                    jobs.append(data["record"]["job"])
                if event["event_type"] == "custom.research.decision" and data.get("request_id") == request_id:
                    stored = data.get("record", {})
                    if stored.get("kind") != "host_job":
                        refuse("REQUEST_CONFLICT")
                    job = stored["job"]
                    if (job["operation"] != operation or job["payload"] != payload
                            or original_text is not None and (type(original_text) is not str or job["original_text"] != original_text)):
                        refuse("REQUEST_CONFLICT")
                    return detached(job)
            if jobs:
                refuse("JOB_RESERVATION_PENDING")
            if document["state"]["current"] in controller.TERMINAL_STATES:
                refuse("RUN_TERMINAL")
        view = self.current()
        scope, original_text = _job_original(self.root, policy, view, operation, payload, original_text)
        if self.current()["basis_id"] != view["basis_id"]:
            refuse("BASIS_CHANGED")
        now = datetime.now(timezone.utc)
        timeout = min(600, policy["budgets"]["seconds"])
        role = "evaluator" if operation == "evaluate" else "host_worker"
        controller_ref = dict(self.context.controller_refs).get(role)
        if controller_ref is None:
            refuse("HOST_CAPABILITY_UNAVAILABLE")
        job = validate_job({"schema_version": JOB, "id": request_id, "operation": operation,
            "role": role,
            "controller_ref": controller_ref, "grant_ref": policy["host_capability_refs"][effect],
            "input_id": self.binding["request_binding"], "basis_id": view["basis_id"], "policy_id": self.policy.policy_id,
            "policy_version": "1", "prompt_version": "1", "revision_precondition": view["basis_id"],
            "original_text": original_text, "scope": scope, "required_capabilities": [effect],
            "payload": detached(payload), "issued_utc": now.isoformat(), "deadline_utc": (now + timedelta(seconds=timeout)).isoformat(),
            "timeout_seconds": timeout, "max_attempts": 1, "expected_result_schema": "research-host-result/v1",
            "budgets": {"model_calls": policy["budgets"]["model_calls"], "tool_calls": policy["budgets"]["tool_calls"],
                "tokens": policy["budgets"]["tokens"], "cost": policy["budgets"]["cost_limit"]},
            "qualifications": {"assurance": policy["assurance"], "status": "unknown", "evaluator_ref": None,
                "qualification_ref": None, "context_basis": None}}, now_utc=now.isoformat())
        self._run()
        _owner(self.root, "run_session_record", run_id=self.run_id, agent_id=self.context.actor_ref,
            request_id=request_id, binding=self.binding, record={"kind": "host_job", "job": job, "authorized": False, "evidence_accepted": False})
        return job
