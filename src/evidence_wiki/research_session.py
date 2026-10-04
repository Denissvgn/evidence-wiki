"""Explicit bounded research steps over canonical runs and current release owners."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from ._facades._base import _process_exit, _translated, call_seam
from .errors import EvidenceWikiError
from .host_context import checked_operation, current_context
from .mutation_contracts import canonical, detached, digest, validate_request_id
from .mutation_operations import _generation, _host_selection
from .mutation_store import MutationStore
from .research_observation import ResearchObservation
from .research_session_contracts import BINDING, BINDING_V2, CURRENT, NEXT, STEP, checked, refuse
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


def _serialized(function):
    """Reserve canonical work under the host's existing workspace gate."""
    @wraps(function)
    def guarded(self, *args, **kwargs):
        self._check(function.__name__, write=True)
        if self.execution is not None:
            return function(self, *args, **kwargs)
        root, context = _scope(self.root, function.__name__, write=True)
        with MutationStore(root, context.mutation_control_directory).workspace_locked():
            return function(self, *args, **kwargs)
    return guarded


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


def _request_binding(root):
    """Bind canonical original mappings without computing unrelated publication."""
    from types import MappingProxyType

    from ._pack_io import yaml_document
    from .pack_discovery import owner

    revisions = owner("_evidence_revision")
    before = revisions.capture_workspace(root)
    config = yaml_document(before.files["research.yml"])
    questions = owner("question_status").questions_directory(root, config).relative_to(root).as_posix() + "/"
    selected = lambda name: name in {"research.yml", "docs/research-requirements.json"} or (
        name.startswith(questions) and name.endswith(".md"))
    files = {name: raw for name, raw in before.files.items() if selected(name)}
    directories = {".", questions.rstrip("/")}
    for name in [*files, *directories]:
        directories.update(parent.as_posix() for parent in Path(name).parents if parent.as_posix() != ".")
    captured = revisions.WorkspaceRevision(MappingProxyType(files), tuple(sorted(directories)))
    with captured.materialize() as snapshot:
        mapping = owner("_caller_context").original_questions(snapshot, config)
    if mapping["gaps"]:
        refuse("ORIGINALS_CHANGED")
    result = digest([{"original_id": row["id"], "original_text": row["text"],
        "question_ids": [identifier for identifier in row["question_ids"] if identifier in mapping["expected"]],
        "missing_question_ids": [identifier for identifier in row["question_ids"] if identifier not in mapping["expected"]]}
        for row in mapping["originals"]])
    after = revisions.capture_workspace(root)
    if {name: raw for name, raw in after.files.items() if selected(name)} != files:
        refuse("ORIGINALS_CHANGED")
    return result


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
def current(project_root, policy, *, run_id=None, execution=None):
    """Return current owner accounting; qualification and missing usage stay unknown."""
    root, context = _scope(project_root, "current")
    if execution is not None:
        return ResearchSession(root, policy, run_id=run_id, execution=execution).current()
    frozen = _policy(policy)
    for grant in ("workspace.open", "workspace_status.run_status_report", "source_requests.run_list_requests",
                  "strict_evidence.run_operation.check", "strict_evidence.run_operation.export"):
        context.require_grant(grant)
    if run_id is not None:
        validate_request_id(run_id)
    view = ResearchObservation(root, run_id=run_id)
    return _current_observation(root, context, frozen, run_id, view, close=True)

def _current_observation(root, context, frozen, run_id, view, *, close=False):
    """Share captured inputs only inside the same live current observation."""
    from .research_completion import _completion_observation

    release = _completion_observation(view, allow_partial=frozen.to_dict()["source_rules"]["allow_partial"], run_id=run_id)
    mapping = view.mapping.get("result")
    originals = mapping.get("originals", []) if mapping else []
    ids = [row["id"] for row in originals]
    compatible = bool(mapping and not mapping["gaps"] and set(ids) == set(frozen.to_dict()["scope"]["question_ids"]))
    if close:
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
def next_work(project_root, policy, *, run_id=None, execution=None):
    _scope(project_root, "next_work")
    if execution is not None:
        return ResearchSession(project_root, policy, run_id=run_id, execution=execution).next()
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
def session(project_root, policy, *, run_id, execution=None):
    """Construct the bound session through the guarded public owner seam."""
    return ResearchSession(project_root, policy, run_id=run_id, execution=execution)


class ResearchSession:
    """One immutable policy binding on the existing canonical run and event journal."""

    @_boundary
    def __init__(self, project_root, policy, *, run_id, execution=None):
        root, context = _scope(project_root, "session")
        self._root, self._policy, self._run_id = root, _policy(policy), validate_request_id(run_id)
        self._context = context
        from .research_execution import ResearchExecution
        if execution is not None and type(execution) is not ResearchExecution:
            refuse("EXECUTION_CONFIG_INVALID")
        self._execution = execution
        self._closed = False
        self._owner_workspace = None
        view = current(root, self.policy, run_id=None)
        if not view["policy_compatible"]:
            refuse("SCOPE_ACCOUNTING")
        info = root.stat()
        version = BINDING_V2 if execution is not None else BINDING
        binding = checked(version, {"schema_version": version, "run_id": self.run_id,
            "actor_ref": context.actor_ref, "policy": self.policy.to_dict(), "policy_id": self.policy.policy_id,
            "target": str(root), "workspace_binding": {"path": str(root), "device": str(info.st_dev), "inode": str(info.st_ino)},
            "request_binding": _original_binding(view), "host_context_sha256": _host_selection(),
            "owner_generation": _generation(), "original_ids": view["accounting"]["original_ids"],
            **(execution.identity() if execution is not None else {}), "authorized": False, "evidence_accepted": False})
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
    def execution(self):
        return self._execution

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
        if self.execution is not None:
            self.execution.check(self.binding)
        if _request_binding(root) != self.binding["request_binding"]:
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
        if self.execution is not None:
            from . import execution_session
            return execution_session.current(self)
        controller = _load(self.root, "run_controller")
        selected = self.run_id if controller.run_state_path(self.root, self.run_id).exists() else None
        return current(self.root, self.policy, run_id=selected)

    @_boundary
    def next(self):
        self._check("next_work")
        if self.execution is not None:
            from . import execution_session
            return execution_session.next_work(self)
        controller = _load(self.root, "run_controller")
        selected = self.run_id if controller.run_state_path(self.root, self.run_id).exists() else None
        return next_work(self.root, self.policy, run_id=selected)

    def _run(self):
        return _owner(self.root, "run_session_start", run_id=self.run_id, agent_id=self.context.actor_ref, binding=self.binding)

    def _pending(self):
        """Read retained reservation facts without treating them as authority."""
        from .pending_work_contracts import create, validate_transition
        controller = _load(self.root, "run_controller")
        if not controller.run_state_path(self.root, self.run_id).exists():
            return None
        document = controller._session_state(self.root, self.run_id)
        if document.get("research_session") != self.binding:
            refuse("BINDING_CHANGED")
        retained = None
        for event in controller._bounded_journal(controller.events_path(self.root, self.run_id))["events"]:
            record = (event.get("data") or {}).get("record", {})
            if event.get("event_type") not in {"custom.research.decision", "custom.research.pending"}:
                continue
            if record.get("kind") == "host_job":
                job = record["job"]
                try:
                    run_deadline = datetime.fromisoformat(document["started_at"].replace("Z", "+00:00")) + timedelta(seconds=self.policy.to_dict()["budgets"]["seconds"])
                except OverflowError:
                    refuse("CLOCK_INVALID")
                retained = create(job, run_id=self.run_id, binding_id=digest(self.binding), request_id=job["id"],
                    run_deadline_utc=run_deadline.isoformat(), observed_utc=job["issued_utc"])
            elif record.get("kind") == "pending_work":
                if retained is None:
                    refuse("REQUEST_CONFLICT")
                retained = validate_transition(retained, record["receipt"])
        return retained

    @_boundary
    def pending(self):
        """Return a retained pending-work receipt; reading never refreshes its lease."""
        self._check("pending")
        if self.execution is not None:
            from . import execution_session
            records = execution_session.retained(self)
            value = execution_session.pending(self, records)
            if value is not None:
                value = {"schema_version": "research-execution-pending/v1", **value,
                    "attempt": execution_session._attempt_rows(records)[value["attempt_id"]]["attempt"]}
            return value
        return self._pending()

    def _settle_pending(self, request_id, operation):
        from .pending_work_contracts import PENDING, fallback, observe, request_cancellation, validate_document
        receipt = self._pending()
        if receipt is None:
            refuse("JOB_UNKNOWN")
        controller = _load(self.root, "run_controller")
        for event in controller._bounded_journal(controller.events_path(self.root, self.run_id))["events"]:
            data = event.get("data") or {}
            if event.get("event_type") == "custom.research.pending" and data.get("request_id") == request_id:
                record = data.get("record", {})
                if record.get("kind") != "pending_work" or record.get("operation") != operation:
                    refuse("REQUEST_CONFLICT")
                saved = validate_document(PENDING, record["receipt"])
                self._finish(controller._session_state(self.root, self.run_id), saved["fallback_reason"] or "pending_work_terminal")
                return saved
        now = datetime.now(timezone.utc).isoformat()
        updated = receipt if receipt["terminal"] else observe(receipt, observed_utc=now)
        if not updated["terminal"]:
            if operation == "cancel":
                updated = request_cancellation(updated, observed_utc=now)
                reason = "cancellation_unconfirmed"
            else:
                reason = "executor_unavailable"
            updated = fallback(updated, observed_utc=now, reason=reason)
        _owner(self.root, "run_session_record", run_id=self.run_id, agent_id=self.context.actor_ref,
            request_id=request_id, binding=self.binding,
            record={"kind": "pending_work", "operation": operation, "receipt": updated,
                    "authorized": False, "evidence_accepted": False})
        self._finish(controller._session_state(self.root, self.run_id), updated["fallback_reason"] or "pending_work_terminal")
        return updated

    def _settle_for_advance(self, request_id):
        receipt = self._pending()
        if receipt is None:
            return
        if receipt["terminal"]:
            controller = _load(self.root, "run_controller")
            self._finish(controller._session_state(self.root, self.run_id), receipt["fallback_reason"] or "pending_work_terminal")
        else:
            self._settle_pending(request_id, "reconcile")

    @_boundary
    @_serialized
    def reconcile_pending(self, *, request_id):
        """Durably expire work or stop on unavailable execution without guessing completion."""
        self._check("reconcile_pending", write=True)
        for grant in TERMINAL_GRANTS:
            self.context.require_grant(grant)
        if self.execution is not None:
            from . import execution_session
            return execution_session.control_pending(self, request_id=request_id)
        return self._settle_pending(validate_request_id(request_id), "reconcile")

    @_boundary
    @_serialized
    def cancel_pending(self, *, request_id):
        """Retain an unconfirmed cancellation request without claiming rollback."""
        self._check("cancel_pending", write=True)
        for grant in TERMINAL_GRANTS:
            self.context.require_grant(grant)
        if self.execution is not None:
            from . import execution_session
            return execution_session.control_pending(self, request_id=request_id, cancel=True)
        return self._settle_pending(validate_request_id(request_id), "cancel")

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
    @_serialized
    def advance(self, *, request_id, expected_basis_id):
        self._check("advance", write=True)
        request_id = validate_request_id(request_id)
        for grant in TERMINAL_GRANTS:
            self.context.require_grant(grant)
        if self.execution is not None:
            from . import execution_session
            return execution_session.advance(self, request_id=request_id, expected_basis_id=expected_basis_id)
        controller = _load(self.root, "run_controller")
        path = controller.run_state_path(self.root, self.run_id)
        if path.exists():
            document = controller._session_state(self.root, self.run_id)
            if document.get("research_session") != self.binding:
                refuse("BINDING_CHANGED")
            for event in controller._bounded_journal(controller.events_path(self.root, self.run_id))["events"]:
                data = event.get("data") or {}
                if event["event_type"] == "custom.research.decision" and data.get("request_id") == request_id:
                    record = data["record"]
                    if record.get("schema_version") != STEP or record["before_basis_id"] != expected_basis_id:
                        refuse("REQUEST_CONFLICT")
                    if self._pending() is not None:
                        self._settle_for_advance(request_id)
                        document = controller._session_state(self.root, self.run_id)
                    document = self._finish(document, record["reason"])
                    result = detached(record)
                    result.update(historical=True, event_ref=event["event_id"], canonical_run_state=document["state"]["current"])
                    return checked(STEP, result)
            if document["state"]["current"] in controller.TERMINAL_STATES:
                refuse("RUN_TERMINAL")
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
        if self._pending() is not None:
            self._settle_for_advance(request_id)
            document = controller._session_state(self.root, self.run_id)
        document = self._finish(document, decision["reason"])
        record["event_ref"] = event["event_id"]
        record["canonical_run_state"] = document["state"]["current"]
        return checked(STEP, record)

    @_boundary
    def run_until_terminal(self, *, request_id, expected_basis_id, max_steps=1):
        self._check("run_until_terminal", write=True)
        if self.execution is not None:
            from . import execution_session
            return execution_session.run_until_terminal(self, request_id=request_id,
                expected_basis_id=expected_basis_id, max_steps=max_steps)
        if type(max_steps) is not int or not 1 <= max_steps <= 64:
            refuse("LIMIT", "/max_steps")
        result = self.advance(request_id=request_id, expected_basis_id=expected_basis_id)
        return result

    @_boundary
    def prepare_acquisition(self, *, request_id, alternative_ids=()):
        """Observe current original allowance without dispatching external work."""
        self._check("prepare_acquisition")
        from .acquisition_operations import prepare_session
        return prepare_session(self, request_id=request_id, alternative_ids=alternative_ids)

    @_boundary
    def choose_acquisition(self, *, request_id, instruction, config_ref):
        """Execute a scoped proposer; its result remains an inert owner-check choice."""
        self._check("choose_acquisition", write=True)
        from .acquisition_choice import choose
        return choose(self,request_id=request_id,instruction=instruction,config_ref=config_ref)

    @_boundary
    def run_acquisition(self, *, request_id, instruction, config_ref, metadata, path):
        """Select bounded acquisition work and apply only native authorized steps."""
        self._check("run_acquisition", write=True)
        from .acquisition_workflow import run
        return run(self,request_id=request_id,instruction=instruction,config_ref=config_ref,metadata=metadata,path=path)

    @_boundary
    def acquire_evidence(self, *, request_id, source_request_id, expected_request_basis, metadata, path):
        """Join explicitly permitted transport and native capture/source owners."""
        self._check("acquire_evidence", write=True)
        from .acquisition_workflow import acquire
        return acquire(self, request_id=request_id, source_request_id=source_request_id,
            expected_request_basis=expected_request_basis, metadata=metadata, path=path)

    @_boundary
    def acquire(self, *, request_id, source_request_id, expected_request_basis):
        """Run one explicitly authorized native acquisition with retained budgets."""
        self._check("acquire", write=True)
        for grant in TERMINAL_GRANTS:
            self.context.require_grant(grant)
        from .acquisition_dispatch import dispatch
        return dispatch(self, request_id=request_id, source_request_id=source_request_id,
            expected_request_basis=expected_request_basis)

    @_boundary
    def run_cycle(self, *, request_id, expected_basis_id=None, max_steps=64, planner_config_ref=None):
        """Drive one immutable host request to a finite canonical outcome."""
        self._check("run_cycle",write=True)
        for grant in TERMINAL_GRANTS:
            self.context.require_grant(grant)
        from .research_cycle import drive
        return drive(self,request_id=request_id,expected_basis_id=expected_basis_id,
            max_steps=max_steps,planner_config_ref=planner_config_ref)

    @_boundary
    @_serialized
    def examine_coverage(self, *, request_id, question_slug, config_ref, source_ids=()):
        """Run a scoped coverage worker and apply its mechanically checked links."""
        self._check("advance", write=True)
        from .coverage_session import examine
        return examine(self, request_id=request_id, question_slug=question_slug,
            config_ref=config_ref, source_ids=source_ids, kind="coverage")

    @_boundary
    @_serialized
    def examine_counterevidence(self, *, request_id, question_slug, claim_id, config_ref, source_ids=()):
        """Run an independent critic; qualified findings can block release."""
        self._check("advance", write=True)
        from .coverage_session import examine
        return examine(self, request_id=request_id, question_slug=question_slug,
            config_ref=config_ref, claim_id=claim_id, source_ids=source_ids, kind="counterevidence")

    @_boundary
    @_serialized
    def host_job(self, *, request_id, operation, payload, original_text=None):
        """Retain an inert bounded job; execution and authentication stay in the host."""
        self._check("host_job", write=True)
        if self.execution is not None:
            from . import execution_session
            return execution_session.host_job(self, request_id=request_id, operation=operation,
                payload=payload, original_text=original_text)
        request_id = validate_request_id(request_id)
        payload = detached(payload)
        from .source_commands import _no_secret_values
        _no_secret_values(payload)
        effect = {"generate": "model", "acquire": "acquire", "evaluate": "adjudicate"}.get(operation)
        policy = self.policy.to_dict()
        if effect is None or effect not in policy["allowed_effects"] or policy["host_capability_refs"][effect] is None:
            refuse("HOST_CAPABILITY_UNAVAILABLE")
        self.context.require_grant("research_session.host_job." + operation)
        for grant in TERMINAL_GRANTS:
            self.context.require_grant(grant)
        if operation == "acquire":
            refuse("SOURCE_MAPPING_UNSUPPORTED")
        if policy["budgets"]["model_calls"] == 0 or policy["budgets"]["tokens"] == 0:
            refuse("BUDGET_EXHAUSTED")
        controller = _load(self.root, "run_controller")
        if controller.run_state_path(self.root, self.run_id).exists():
            document = controller._session_state(self.root, self.run_id)
            if document.get("research_session") != self.binding:
                refuse("BINDING_CHANGED")
            jobs = []
            for event in controller._bounded_journal(controller.events_path(self.root, self.run_id))["events"]:
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
        role = "evaluator" if operation == "evaluate" else "host_worker"
        controller_ref = dict(self.context.controller_refs).get(role)
        if controller_ref is None:
            refuse("HOST_CAPABILITY_UNAVAILABLE")
        document = self._run()
        now = datetime.now(timezone.utc)
        started = datetime.fromisoformat(document["started_at"].replace("Z", "+00:00"))
        last_observed = datetime.fromisoformat(document["updated_at"].replace("Z", "+00:00"))
        if now < max(started, last_observed):
            refuse("CLOCK_REGRESSED")
        try:
            run_deadline = started + timedelta(seconds=policy["budgets"]["seconds"])
        except OverflowError:
            refuse("CLOCK_INVALID")
        remaining = (run_deadline - now).total_seconds()
        if remaining < 1:
            self._finish(document, "run_deadline_expired")
            refuse("BUDGET_EXHAUSTED")
        timeout = min(600, int(remaining))
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
        _owner(self.root, "run_session_record", run_id=self.run_id, agent_id=self.context.actor_ref,
            request_id=request_id, binding=self.binding, record={"kind": "host_job", "job": job, "authorized": False, "evidence_accepted": False})
        return job

    @_boundary
    def intake_observation(self, observation, *, request_id):
        """Retain authenticated execution history without granting current acceptance."""
        self._check("intake_observation", write=True)
        if self.execution is None:
            refuse("HOST_CAPABILITY_UNAVAILABLE")
        from . import execution_session
        return execution_session.intake(self, observation, request_id=validate_request_id(request_id))

    @_boundary
    def report(self, *, after_sequence=0, max_records=64):
        """Return bounded redacted decision and measurement metadata."""
        self._check("report")
        if self.execution is None:
            refuse("HOST_CAPABILITY_UNAVAILABLE")
        from . import execution_session
        return execution_session.report(self, after_sequence=after_sequence, max_records=max_records)
