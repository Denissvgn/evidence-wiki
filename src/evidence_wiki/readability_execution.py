"""One bounded critic purpose uses the existing authenticated execution owners."""

import secrets
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import execution_accounting as accounting
from . import execution_session as native
from .agent_execution import BoundedSubprocessExecutor, ExecutionAttempt, _utc
from .brief_projection import project, substance
from .calibration_registry import CalibratedEvaluators
from .errors import EvidenceWikiError
from .mutation_contracts import canonical, detached, digest
from .readability_context import create, reduce
from .readability_contracts import (
    CAPABILITIES,
    check_persisted_record,
    output_document,
    refuse,
    restored_preparation,
    stored_attempt,
    stored_preparation,
)
from .readability_reference import observe_domain
from .resolution_operations import _held_current
from .service_authentication import owned, readability_credential
from .workflow_contracts import EXECUTION_OBSERVATION, JOB_V3, validate_job

_INPUT_DISPATCH = ContextVar("owned_readability_input_dispatch", default=None)


class _InputDispatch:
    """One new committed identity, never cached acceptance or known consumption."""

    def __init__(self, session, request_id, attempt, records):
        self.session, self.request_id, self.attempt = session, request_id, attempt
        self.records = canonical(records)
        self.thread = threading.get_ident()
        self.state = "created"
        self.invalidated = False
        self.lease = None
        self._origin = self
        self._parent_lease = None

    def __copy__(self):
        refuse("input_dispatch_not_fresh")

    def __deepcopy__(self, memo):
        refuse("input_dispatch_not_fresh")

    def require(self, session, attempt):
        if (self._origin is not self or session is not self.session or self.request_id != self.attempt.job["id"]
                or self.request_id != self.attempt.attempt_id or attempt.document() != self.attempt.document() or self.invalidated
                or canonical(native.retained(session)) != self.records):
            refuse("input_dispatch_identity_changed")

    def transfer(self, session, attempt):
        self.require(session, attempt)
        if _INPUT_DISPATCH.get() is not self or self.thread != threading.get_ident() or self.state != "created":
            refuse("input_dispatch_not_fresh")
        self.state = "transferred"
        _INPUT_DISPATCH.set(None)

    @contextmanager
    def custody(self, lease):
        self.require(lease.session, lease.attempt)
        if self.state != "guard_scheduled" or self._parent_lease is not lease or self.lease is not None or _INPUT_DISPATCH.get() is not None:
            refuse("input_dispatch_not_fresh")
        self.state, self.lease, self.thread = "held", lease, threading.get_ident()
        token = _INPUT_DISPATCH.set(self)
        try:
            yield
        finally:
            self.state = "finished"
            _INPUT_DISPATCH.reset(token)

    def begin_configuration(self, lease):
        self.require(lease.session, lease.attempt)
        if self.state != "transferred" or self._parent_lease is not None or self.thread != threading.get_ident():
            refuse("input_dispatch_not_fresh")
        self._parent_lease = lease
        self.state = "configuration_ready"
        _INPUT_DISPATCH.set(self)

    @contextmanager
    def configuration_custody(self, lease):
        self.require(lease.session, lease.attempt)
        if self.state != "configuration_scheduled" or self._parent_lease is not lease or _INPUT_DISPATCH.get() is not None:
            refuse("input_dispatch_not_fresh")
        parent_thread = self.thread
        self.state, self.thread = "observing_configuration", threading.get_ident()
        token = _INPUT_DISPATCH.set(self)
        try:
            yield
            self.require(lease.session, lease.attempt)
            self.state = "configuration_completed"
        finally:
            self.thread = parent_thread
            _INPUT_DISPATCH.reset(token)

    def configuration_completed(self, lease):
        self.require(lease.session, lease.attempt)
        if self.state != "configuration_completed" or self._parent_lease is not lease or self.thread != threading.get_ident():
            refuse("input_dispatch_not_fresh")
        self.state = "configuration_observed"
        _INPUT_DISPATCH.set(self)

    def ready_guard(self, lease):
        self.require(lease.session, lease.attempt)
        if self.state != "configuration_observed" or self._parent_lease is not lease or _INPUT_DISPATCH.get() is not self or self.thread != threading.get_ident():
            refuse("input_dispatch_not_fresh")
        self.state = "guard_ready"

    def enter_host_call(self, function, arguments):
        from .readability_handoff import ReadabilityHandoffLease
        self.require(self.session, self.attempt)
        lease = self._parent_lease
        if self.thread != threading.get_ident() or type(lease) is not ReadabilityHandoffLease:
            refuse("input_dispatch_not_fresh")
        bound = getattr(function, "__self__", None)
        implementation = getattr(function, "__func__", None)
        if (self.state == "configuration_ready" and bound is lease
                and implementation is ReadabilityHandoffLease._configuration_callback and arguments == ()):
            self.state = "configuration_scheduled"
        elif (self.state == "guard_ready" and bound is self.session.execution.executor
                and type(bound) is BoundedSubprocessExecutor and implementation is BoundedSubprocessExecutor.invoke_readability_guarded
                and type(arguments) is tuple and len(arguments) == 2 and arguments[0] is self.attempt and arguments[1] is lease):
            self.state = "guard_scheduled"
        else:
            self.invalidated = True
            return
        _INPUT_DISPATCH.set(None)
        return self

    def host_call_failed(self):
        """A scheduling/unknown exit never restores this one-use admission."""
        self.invalidated = True
        self.state = "failed"

    def held(self, session):
        self.require(session, self.attempt)
        if (_INPUT_DISPATCH.get() is not self or self.state != "held" or self.thread != threading.get_ident()
                or self.lease is None):
            refuse("input_dispatch_not_held")
        self.lease._require_started_identity()


def invalidate_input_dispatch():
    value = _INPUT_DISPATCH.get()
    if value is not None:
        value.invalidated = True


def enter_input_host_call(function, arguments):
    """Only exact fixed owner waits consume admission at actual call entry."""
    value = _INPUT_DISPATCH.get()
    if value is not None:
        return value.enter_host_call(function, arguments)


def _admit_input_dispatch(session, request_id, attempt):
    records = native.retained(session)
    if _INPUT_DISPATCH.get() is not None or len(records) < 3:
        refuse("input_dispatch_not_fresh")
    rows = records[-3:]
    if ([row["kind"] for row in rows] != ["execution_advance", "execution_readability_probe", "execution_attempt"]
            or rows[0]["data"]["advance_id"] != request_id
            or [row["request_id"] for row in rows] != [native.child_id(request_id, key) for key in ("advance_intent", "readability_probe", "intent")]):
        refuse("input_dispatch_identity_changed")
    prepared = restored_preparation(rows[0]["data"]["readability_preparation"], session.execution)
    if (prepared["attempt_record"] != rows[2]["data"] or prepared["probe_record"] != rows[1]["data"]
            or prepared["predecessor_records_ref"] != digest(records[:-3])
            or stored_attempt(attempt, session.execution.profile) != rows[2]["data"]["attempt"]):
        refuse("input_dispatch_identity_changed")
    _INPUT_DISPATCH.set(_InputDispatch(session, request_id, attempt, records))


def take_input_dispatch(session, attempt):
    value = _INPUT_DISPATCH.get()
    if type(value) is not _InputDispatch:
        refuse("input_dispatch_not_fresh")
    value.transfer(session, attempt)
    return value


def input_constraints(session, view, records, proofs, admission):
    """A reserved input may proceed under committed caps; usage stays unknown."""
    admission.held(session)
    aid = admission.attempt.attempt_id
    policy = session.policy.to_dict()
    ledger = session.execution.ledger(session, records)
    projection = accounting.projection(ledger, limits=native._limits(session))
    pending = view["execution"]["pending"]
    if (not pending or pending["attempt_id"] != aid or pending["state"] != "dispatch_unknown"
            or pending["reservation_occupied"] is not True or pending["applied"] or ledger["closure"] is not None
            or projection["incomplete_attempt_ids"] != [aid] or projection["missing_attempt_ids"] != [aid]
            or projection["conflicting_attempt_ids"] or projection["missing_sequence_ranges"]
            or set(projection["missing_slots"]) - {aid + ":" + key for key in ("dispatch", "result", "usage")}
            or any(row["kind"] in {"execution_observation", "execution_input_handoff", "execution_host_unknown", "execution_cancel",
                "execution_readability_handoff_failed", "execution_application", "execution_applied"}
                and row["data"].get("attempt_id") == aid for row in records)):
        refuse("input_dispatch_accounting_unknown")
    from .run_policy import _derived_scope_failure, _hard_policy_failure, _owner_observation, _profiles_qualified
    observation = _owner_observation(policy, native.policy_observation(session, view))
    failure = _hard_policy_failure(policy, observation)
    if (failure not in (None, ("stop_budget", "usage_unknown")) or not _profiles_qualified(policy, observation)
            or observation["basis_id"] != observation["expected_basis_id"] or observation["basis_id"] != view["basis_id"]
            or view["qualification"]["state"] != "qualified"
            or "model" not in policy["allowed_effects"] or policy["host_capability_refs"]["model"] is None):
        refuse("input_dispatch_policy_or_qualification_changed")
    budgets, upper = policy["budgets"], projection["committed"]
    counters = policy["continuation_limits"]
    if (projection["overrun"] or native.execution_budget_overrun(session, records) or _remaining(session, records)["overrun"]
            or view["execution"]["steps_used"] > view["execution"]["steps_limit"]
            or any(upper[key] > budgets[key] for key in ("model_calls", "tool_calls", "tokens"))
            or Decimal(upper["cost"]) > Decimal(budgets["cost_limit"]) or Decimal(upper["seconds"]) > Decimal(str(budgets["seconds"]))
            or view["usage"]["seconds"] >= budgets["seconds"]
            or any(view["usage"][key] is None or view["usage"][key] > budgets[key] for key in ("claims", "sources"))
            or any(view["usage"][key] is None or view["usage"][key] > limit for key, limit in counters.items())
            or datetime.now(timezone.utc) >= min(native.deadline(session), _utc(admission.attempt.job["deadline_utc"]))):
        refuse("input_dispatch_budget_or_deadline_exhausted")
    claims = [claim for question in (view["canonical_release"]["publication"] or {}).get("questions", [])
        if proofs.get(question["slug"], {}).get("eligible") for claim in question["claims"]]
    if _derived_scope_failure(policy, observation, claim_refs=[claim["id"] for claim in claims],
            source_refs=[evidence["source_id"] for claim in claims for evidence in claim["evidence"]]):
        refuse("input_dispatch_scope_changed")
    return []

def _state(state="pending", qualified=False):
    return {"state": state, "qualification": "current_protected_purpose" if qualified else "not_observed",
        "scope": "finite_scalar_preservation" if qualified else "not_qualified", "semantic_approval": False}


def qualification(session, observed, *, application_attempt_id=None):
    observed.require(session)
    if type(session.execution.qualifications) is not CalibratedEvaluators:
        refuse("protected_issuer_required")
    from .resolution_operations import _OwnedReadabilityInput
    domain = observe_domain(observed.input_brief() if type(observed) is _OwnedReadabilityInput else project(observed.document()))
    probe_id = (native.child_id(native.child_id(application_attempt_id, "readability_application"), "readability_probe")
        if application_attempt_id is not None else None)
    records = native.retained(session)
    probes = [row["data"]["probe"] for row in records if row["kind"] == "execution_readability_probe"
        and (probe_id is None or row["request_id"] == probe_id)]
    if probe_id is not None and len(probes) != 1:
        probes = []
    from .readability_probes import selected
    in_flight = selected(session, observed) if application_attempt_id is None else None
    if type(observed) is _OwnedReadabilityInput:
        in_flight = observed._input_admission.lease.configuration_probe(observed)
    elif application_attempt_id is not None:
        in_flight = None
    now = datetime.now(timezone.utc).isoformat()
    if application_attempt_id is not None:
        applications = [row["data"] for row in records if row["kind"] == "execution_application"
            and row["data"].get("operation") == "readability" and row["data"]["attempt_id"] == application_attempt_id]
        if len(applications) != 1 or len(probes) != 1:
            return {"state": "unknown", "qualified": None, "independent": None,
                "qualification_ref": None, "reason": "readability_qualification_unknown"}
        _row, attempt, receipt = owned(session, application_attempt_id)
        checked = _application_checked_at(applications[0], probes[0], attempt, receipt, now)
        if checked is None:
            return {"state": "unknown", "qualified": None, "independent": None,
                "qualification_ref": None, "reason": "readability_qualification_unknown"}
        result = session.execution.qualifications.readability_application_observation(session.policy, session.execution.profile,
            now, domain=domain, probe=probes[0], checked_utc=checked)
    else:
        result = session.execution.qualifications.readability_observation(session.policy, session.execution.profile,
            now, domain=domain, probe=in_flight if in_flight is not None else probes[-1] if probes else None)
    if result["state"] == "qualified":
        readability_credential(session)
    return result


def _application_checked_at(application, probe, attempt, receipt, now):
    """No unsigned event time or caller timestamp stands in for the native row."""
    try:
        checked = application["checked_utc"]
        at = _utc(checked)
        if (type(checked) is not str or receipt["completed_utc"] is None
                or not _utc(attempt.job["issued_utc"]) <= _utc(receipt["completed_utc"]) <= at <= _utc(now)
                or at >= _utc(attempt.job["deadline_utc"])
                or not _utc(probe["observed_utc"]) <= at < _utc(probe["expires_utc"])):
            return None
        return checked
    except (ValueError, TypeError, KeyError):
        return None


def _attempts(session, records):
    return [row["data"] for row in records if row["kind"] == "execution_attempt"
        and row["data"].get("retry_purpose", row["data"].get("purpose")) == "readability"]


def _remaining(session, records):
    selected = {row["attempt"]["attempt_id"] for row in _attempts(session, records)}
    full = session.execution.ledger(session, records)
    part = deepcopy(full)
    part["closure"] = None
    part["attempts"] = [row for row in part["attempts"] if row["attempt_id"] in selected]
    part["observations"] = [row for row in part["observations"]
        if row["envelope"]["attempt_id"] is None or row["envelope"]["attempt_id"] in selected]
    declared = session.execution.profile.readability.document()["limits"]
    return accounting.projection(part, limits={key: str(declared[key]) if key == "seconds" else declared[key]
        for key in ("seconds", "model_calls", "tool_calls", "tokens", "cost")})


def _current_application(session, observed):
    observed.require(session)
    plan = session.execution.profile.readability
    brief = project(observed.document())
    records = native.retained(session)
    effects = {row["data"]["attempt_id"] for row in records if row["kind"] == "execution_applied"
        and row["data"].get("operation") == "readability"}
    for event in reversed(records):
        app = event["data"]
        if event["kind"] != "execution_application" or app.get("operation") != "readability" or app["attempt_id"] not in effects:
            continue
        row, attempt, observation = owned(session, app["attempt_id"])
        parent = row.get("readability_parent")
        if (not parent or row.get("purpose") != "readability" or attempt.config_ref != plan.config_ref
                or parent["basis_id"] != observed._basis_id or parent["substance_sha256"] != digest(substance(brief))):
            continue
        if qualification(session, observed, application_attempt_id=attempt.attempt_id)["state"] != "qualified":
            return None
        _model_observed(session, attempt, observation)
        result = reduce(observation["output"], attempt.input_document["packet"], parent["binding"],
            current_brief=brief, order_seed=bytes.fromhex(parent["order_seed"]),
            max_input_bytes=plan.max_input_bytes, max_output_bytes=plan.max_output_bytes)
        if result != app["result"] or parent["plan_sha256"] != plan.sha256:
            refuse("owned_application_changed")
        return app
    return None


def current_report(session, observed):
    """No callbacks, journal writes, dispatch or renewal during a brief read."""
    observed.require(session)
    try:
        app = _current_application(session, observed)
        if app is None:
            return _state()
        result = app["result"]
        return _state("observed" if result["state"] == "issues" and not result["issues"] else "refused", True)
    except (EvidenceWikiError, ValueError, KeyError, TypeError):
        return _state("refused")


def closing_vector(session, observed):
    """Exact callback-free purpose vector at the final native read boundary."""
    observed.require(session)
    try:
        app = _current_application(session, observed)
        report = (_state("observed" if app["result"]["state"] == "issues" and not app["result"]["issues"] else "refused", True)
            if app is not None else _state())
    except (EvidenceWikiError, ValueError, KeyError, TypeError):
        app, report = None, _state("refused")
    result = {"report": report, "qualification_ref": None, "application_ref": None, "authority_ref": None}
    if report["qualification"] == "current_protected_purpose":
        qualified = qualification(session, observed, application_attempt_id=app["attempt_id"])
        if qualified["state"] != "qualified":
            return {"report": _state("refused"), "qualification_ref": None, "application_ref": None, "authority_ref": None}
        config = native.owner("_strict_evidence").configuration(session.root)
        authority = native.owner("_evidence_authority")
        trust = authority.load_trust(session.root, config, datetime.now(timezone.utc))
        result.update(qualification_ref=qualified["qualification_ref"], application_ref=digest(app),
            authority_ref=authority.authority_basis(trust))
    return result


def qualification_vector(session, observed):
    """Pending input/application custody has no completed report to stand in."""
    observed.require(session)
    result = qualification(session, observed)
    value = {"state": result["state"], "qualification_ref": result["qualification_ref"], "authority_ref": None}
    if result["state"] == "qualified":
        config = native.owner("_strict_evidence").configuration(session.root)
        authority = native.owner("_evidence_authority")
        trust = authority.load_trust(session.root, config, datetime.now(timezone.utc))
        value["authority_ref"] = authority.authority_basis(trust)
    return value


def _model_observed(session, attempt, observation):
    role = session.execution.profile.role(attempt.config_ref)
    usage = observation["observed_usage"]
    if (observation["state"] != "succeeded" or not observation["final_accounting"]
            or observation["reason"] == "offline_fixture" or role.model_ref.startswith("offline/")
            or type(usage["model_calls"]) is not int or usage["model_calls"] <= 0
            or any(value is None for value in usage.values()) or observation["started_utc"] is None
            or usage["seconds"] > attempt.job["timeout_seconds"]
            or observation["completed_utc"] is None or not _utc(attempt.job["issued_utc"]) <= _utc(observation["started_utc"]) <= _utc(observation["completed_utc"])
            or _utc(observation["completed_utc"]) > _utc(attempt.job["deadline_utc"])
            or native.execution_budget_overrun(session, native.retained(session))
            or _remaining(session, native.retained(session))["overrun"]):
        refuse("model_execution_or_budget_unqualified")


def _eligible(session, observed):
    observed.require(session)
    result = observed.document()
    if result["global_blockers"] or result["verdict"] not in {"accept", "partial"} or observe_domain(project(result))["state"] != "observed":
        refuse("current_brief_not_supported")
    return result


def next_action(session, view, records):
    """A proposal only; current owner facts never reserve or dispatch here."""
    if session.execution.profile.readability is None:
        return None
    with _held_current(session) as observed:
        return _next_from_owned(session, view, records, observed)


def _next_from_owned(session, view, records, observed):
    observed.require(session)
    plan = session.execution.profile.readability
    value = observed.document()
    if value["global_blockers"] or value["verdict"] not in {"accept", "partial"}:
        fresh = observed.native_current()
        from .run_policy import _hard_policy_failure, _owner_observation
        policy = session.policy.to_dict()
        failure = _hard_policy_failure(policy, _owner_observation(policy, native.policy_observation(session, fresh)))
        if failure is None and native.execution_budget_overrun(session, native.retained(session)):
            failure = ("stop_budget", "budget_exhausted")
        if failure is None:
            failure = ("blocked_external", "qualification_unknown") if fresh["qualification"]["state"] != "qualified" else (
                "abstained", "unsupported_claim")
        return native._terminal_action(session, fresh, *failure)
    if value["verdict"] in {"accept", "partial"} and not value["global_blockers"] and (
            len(value["original_outcomes"]) > 64 or observe_domain(project(value))["state"] != "observed"):
        return native._terminal_action(session, view, "abstained", "qualification_unknown")
    _eligible(session, observed)
    app = _current_application(session, observed)
    if app is not None:
        if app["result"]["state"] != "issues":
            return native._terminal_action(session, view, "abstained", "unsupported_claim")
        issues = app["result"]["issues"]
        if not issues:
            return None
        if any(issue["claim_id"] is None for issue in issues):
            return native._terminal_action(session, view, "abstained", "unsupported_claim")
        target = issues[0]["claim_id"]
        return _repair_action(session, observed, target, app)
    rows = _attempts(session, records)
    if len(rows) >= plan.max_attempts:
        return native._terminal_action(session, view, "abstained", "unsupported_claim")
    if rows:
        _row, _attempt, final = owned(session, rows[-1]["attempt"]["attempt_id"])
        if final["state"] in {"failed", "unavailable", "expired"}:
            if not final["final_accounting"] or final["reason"] not in {"executor_unavailable", "service_credentials_unavailable"}:
                return native._terminal_action(session, view, "blocked_external", "executor_unavailable")
            ready = _utc(final["observed_utc"]) + timedelta(seconds=plan.backoff_seconds)
            if datetime.now(timezone.utc) < ready:
                return {"operation": "reconcile", "attempt_id": rows[-1]["attempt"]["attempt_id"],
                    "retry_after_utc": ready.isoformat(), "reason": "retry_backoff"}
    return {"operation": "evaluate", "purpose": "readability", "original_id": session.binding["original_ids"][0],
        "config_ref": plan.config_ref, "reason": "independent_review_required"}


def before_soft_terminal(session, view, records, action):
    """Inspect a proven derived subset without changing coarse original counts."""
    if (session.execution.profile.readability is None or action["operation"] != "terminal"
            or action["decision"]["outcome"] != "abstained"
            or action["reason"] not in {"current_outcomes", "unsupported_claim", "insufficient_evidence"}):
        return action
    with _held_current(session) as observed:
        value = observed.document()
        if value["verdict"] not in {"accept", "partial"} or value["global_blockers"]:
            return action
    return next_action(session, view, records) or action


def reserve(session, request_id, action, view):
    """Independent internal reservation still obtains its own complete owner read."""
    with _held_current(session) as observed:
        retained = _prepare_reservation(session, request_id, action, observed.native_current(), observed)
    if native.current(session)["basis_id"] != view["basis_id"]:
        refuse("basis_changed_before_reservation")
    native.append(session, "execution_attempt", native.child_id(request_id, "intent"), retained)
    return session.execution.attempt(retained["attempt"])


def _prepare_reservation(session, request_id, action, view, observed):
    """Reserve exact current full input using the existing native ledger and journal."""
    observed.require(session)
    from .readability_probes import require_request
    require_request(session, request_id)
    session._check("advance", write=True)
    session.context.require_grant("research_session.host_job.evaluate")
    native.require_machine_compatible(session)
    if native._document(session)["state"]["current"] in native.owner("run_controller").TERMINAL_STATES:
        refuse("run_terminal")
    from .run_policy import _hard_policy_failure, _owner_observation
    snapshot = native.policy_observation(session, view)
    if _hard_policy_failure(session.policy.to_dict(), _owner_observation(session.policy.to_dict(), snapshot)) is not None:
        refuse("preterminal_policy_not_eligible")
    frozen, profile = session.policy.to_dict(), session.execution.profile
    plan = profile.readability
    if plan is None or action.get("config_ref") != plan.config_ref or type(session.execution.executor) is not BoundedSubprocessExecutor:
        refuse("typed_bundled_critic_required")
    plan.selected_profile(session.policy, profile)
    if "model" not in frozen["allowed_effects"] or frozen["host_capability_refs"]["model"] is None:
        refuse("model_grant_missing")
    records = native.retained(session)
    if len(session.binding["original_ids"]) > 64:
        refuse("whole_job_original_scope_bound")
    if len(_attempts(session, records)) >= plan.max_attempts:
        refuse("attempt_limit")
    ledger = session.execution.ledger(session, records)
    global_usage = accounting.projection(ledger, limits=native._limits(session))
    local = _remaining(session, records)
    if any(row["usage_unknown"] or row["overrun"] for row in (global_usage, local)):
        refuse("occupied_or_overrun_budget")
    remaining = [global_usage["remaining"], local["remaining"]]
    if profile.schedule is not None:
        role_usage = native.role_projection(profile, ledger, records, plan.config_ref)
        if role_usage["usage_unknown"] or role_usage["overrun"]:
            refuse("occupied_or_overrun_budget")
        remaining.append(role_usage["remaining"])
    cap = profile.document()["job_budgets"]
    cap = {key: min([cap[key], *(row[key] for row in remaining)]) for key in ("model_calls", "tool_calls", "tokens")}
    cap["cost"] = str(min([Decimal(profile.cost_limit), *(Decimal(row["cost"]) for row in remaining)]))
    now = datetime.now(timezone.utc)
    timeout = min(plan.timeout_seconds, int((native.deadline(session) - now).total_seconds()),
        *(int(Decimal(row["seconds"])) for row in remaining))
    if cap["model_calls"] < 1 or cap["tokens"] < 1 or timeout < 1:
        refuse("budget_exhausted")
    _eligible(session, observed)
    if observed._basis_id != view["basis_id"] or qualification(session, observed)["state"] != "qualified":
        refuse("current_qualification_or_basis_unknown")
    observed.bind_readability_eligibility()
    brief = project(observed.document())
    seed = secrets.token_bytes(32)
    packet, binding = create(brief, context_id="readability-" + secrets.token_hex(16), order_seed=seed,
        max_input_bytes=plan.max_input_bytes)
    packets = observed.source_packets()
    parent = {"basis_id": observed._basis_id, "substance_sha256": digest(substance(brief)), "binding": binding,
        "order_seed": seed.hex(), "plan_sha256": plan.sha256,
        "sources": [{key: detached(row[key]) for key in ("source_id", "record_sha256", "source_binding")} for row in packets]}
    role, scope = profile.role(plan.config_ref), profile.role_scopes.scope(plan.config_ref)
    document = {"purpose": "readability", "packet": packet}
    job = validate_job({"schema_version": JOB_V3, "id": request_id, "operation": "evaluate", "role": role.role,
        "controller_ref": role.controller_ref, "grant_ref": frozen["host_capability_refs"]["model"],
        "input_id": session.binding["request_binding"], "basis_id": view["basis_id"], "policy_id": session.policy.policy_id,
        "policy_version": frozen["schema_version"].rsplit("v", 1)[-1], "prompt_version": role.prompt_version,
        "revision_precondition": view["basis_id"], "original_text": "Current permitted brief for every frozen original.",
        "scope": list(session.binding["original_ids"]), "required_capabilities": list(CAPABILITIES),
        "payload": {"qualification_purpose": "readability"}, "issued_utc": now.isoformat(),
        "deadline_utc": (now + timedelta(seconds=timeout)).isoformat(), "timeout_seconds": timeout, "max_attempts": 1,
        "expected_result_schema": EXECUTION_OBSERVATION, "budgets": cap,
        "qualifications": {"assurance": frozen["assurance"], "status": "unknown", "evaluator_ref": None,
            "qualification_ref": None, "context_basis": None}, "task_role": "critic", "role_scope_sha256": scope.sha256,
        "context_id": packet["context_id"], "packet_sha256": digest(document).removeprefix("sha256:")}, now_utc=now.isoformat())
    attempt = ExecutionAttempt.create(attempt_id=request_id, nonce=secrets.token_hex(16), run_id=session.run_id,
        binding_id=digest(session.binding), profile=profile, config_ref=plan.config_ref, job=job, input_document=document)
    if len(canonical(attempt.worker_document())) > plan.max_input_bytes:
        refuse("complete_worker_input_bound")
    reservation = {"attempt_id": request_id, "job_id": request_id, "controller_ref": "session-observer",
        "issued_at": job["issued_utc"], "reservation": {**cap, "seconds": str(timeout)},
        "cost_unit": {"currency": profile.currency, "pricing_ref": profile.pricing_ref}, "required_slots": ["dispatch", "result", "usage"]}
    accounting.reserve_attempt(ledger, reservation)
    return {"attempt": stored_attempt(attempt, profile), "reservation": reservation, "purpose": "readability",
        "original_id": session.binding["original_ids"][0], "claim_id": None, "readability_parent": parent}


def prefix_attempt(session, request_id, expected_basis_id):
    """Authenticate durable prefixes before callback or any new random identity."""
    records = native.retained(session)
    if any(row["kind"] == "execution_step" and row["data"]["step"]["request_id"] == request_id for row in records):
        return None
    intents = [row["data"] for row in records if row["kind"] == "execution_advance"
        and row["data"]["advance_id"] == request_id]
    attempts = [row["data"] for row in records if row["kind"] == "execution_attempt"
        and row["data"]["attempt"]["job"]["id"] == request_id]
    if intents and intents[0]["before_basis_id"] != expected_basis_id:
        refuse("commit_prefix_changed")
    if attempts:
        if len(attempts) != 1:
            refuse("commit_prefix_changed")
        row = attempts[0]
        if row.get("purpose") != "readability":
            return None
        if intents and "readability_preparation" in intents[0]:
            prepared = restored_preparation(intents[0]["readability_preparation"], session.execution)
            if prepared["attempt_record"] != row:
                refuse("commit_prefix_changed")
            probes = [event["data"] for event in records if event["kind"] == "execution_readability_probe"
                and event["request_id"] == native.child_id(request_id, "readability_probe")]
            if probes != [prepared["probe_record"]]:
                refuse("commit_prefix_changed")
        return session.execution.attempt(row["attempt"])
    if any("readability_preparation" in row or row["action"].get("purpose") == "readability" for row in intents) or any(
            row["kind"] == "execution_readability_probe" and row["request_id"] == native.child_id(request_id, "readability_probe")
            for row in records):
        refuse("commit_prefix_requires_reconciliation")
    return None


def _commit_current(session, witness, observed_inputs, expected_view):
    """Fresh actual owner checks, never approval inferred from prepared bytes."""
    witness.require_request(session, witness._request_id)
    session._check("advance", write=True)
    current = native.current(session)
    witness.require_request(session, witness._request_id)
    observed_inputs.finish()
    from .run_policy import _hard_policy_failure, _owner_observation
    policy = session.policy.to_dict()
    if _hard_policy_failure(policy, _owner_observation(policy, native.policy_observation(session, current))) is not None:
        refuse("preterminal_policy_not_eligible")
    if (current["basis_id"] != expected_view["basis_id"] or not current["policy_compatible"]
            or current["qualification"] != expected_view["qualification"]
            or current["execution"]["source_bindings"] != expected_view["execution"]["source_bindings"]
            or (current["run"] or {}).get("terminal") or native.execution_budget_overrun(session, native.retained(session))):
        refuse("current_qualification_or_basis_changed")
    comparable = lambda publication: {key: value for key, value in (publication or {}).items() if key != "evaluated_at"}
    if comparable(current["canonical_release"]["publication"]) != comparable(expected_view["canonical_release"]["publication"]):
        refuse("current_qualification_or_basis_changed")
    config = native.owner("verify_quotes").load_config(session.root)
    if config != observed_inputs.config or native.owner("_caller_context").capture(session.root) != observed_inputs.controls.get("result"):
        refuse("current_qualification_or_basis_changed")
    usage = native.owner("_evidence_usage")
    with usage.current_view(session.root, config) as live:
        qualified = native.owner("_publication_usage").qualify_capture(observed_inputs.capture.files, config, live,
            purpose="qa-export", consumer="evidence-wiki")
        if qualified.get("complete") is not True:
            refuse("current_qualification_or_basis_changed")
        for row in native.retained(session):
            if row["kind"] == "execution_application" and row["data"].get("operation") == "evaluate":
                envelope = row["data"]["envelope"]
                if row["data"].get("strict_basis_id") == (current["canonical_release"]["publication"] or {}).get("basis_id"):
                    native._verify_review_controller(session, session.execution.attempt(native._attempt_rows(native.retained(session))[row["data"]["attempt_id"]]["attempt"]), envelope)
                    native.owner("_strict_evidence").validate_review(session.root, config, envelope, live)
        live.revalidate(session.root, config)
    readability_credential(session)
    if witness.qualification(session, witness._request_id)["state"] != "qualified":
        refuse("current_qualification_or_basis_changed")
    witness.require_request(session, witness._request_id)
    return current


def prepare_step(session, request_id, expected_basis_id):
    """One fresh post-callback resolution prepares one recoverable native commit."""
    from .readability_probes import prepared
    with native.gate(session):
        existing = prefix_attempt(session, request_id, expected_basis_id)
        if existing is not None:
            return existing, {"operation": "reconcile", "attempt_id": existing.attempt_id, "reason": "retained_attempt_reconciliation"}
        native._begin(session)
    with prepared(session, request_id) as witness:
        witness.require_request(session, request_id)
        with native.gate(session):
            with _held_current(session) as observed:
                view, records = observed.native_current(), native.retained(session)
                if view["basis_id"] != expected_basis_id:
                    refuse("basis_changed_before_reservation")
                action = _next_from_owned(session, view, records, observed)
                if action is None or action.get("purpose") != "readability":
                    # A proposal can lose eligibility during the configuration
                    # callback. Hand the freshly checked refusal to the normal
                    # coordinator without retaining a probe or reserving work.
                    if action is not None and action["operation"] == "terminal":
                        return None, action
                    return None, native._terminal_action(session, view, "abstained", "unsupported_claim")
                attempt_record = _prepare_reservation(session, request_id, action, view, observed)
                owner_inputs = observed._native_observation
            _commit_current(session, witness, owner_inputs, view)
            job = attempt_record["attempt"]["job"]
            if datetime.now(timezone.utc) >= min(native.deadline(session), _utc(job["deadline_utc"])):
                refuse("application_deadline_expired")
            probe_record = witness.consume(session)
            preparation = {"attempt_record": attempt_record, "probe_record": probe_record,
                "predecessor_records_ref": digest(records)}
            preparation = stored_preparation(preparation, session.execution)
            restored_preparation(preparation, session.execution)
            intent_data = {"advance_id": request_id, "before_basis_id": expected_basis_id, "action": action,
                "consumes_step": view["execution"]["steps_used"] < session.execution.profile.total_steps,
                "readability_preparation": preparation}
            commit_rows = [("execution_advance", native.child_id(request_id, "advance_intent"), intent_data),
                ("execution_readability_probe", native.child_id(request_id, "readability_probe"), probe_record),
                ("execution_attempt", native.child_id(request_id, "intent"), attempt_record)]
            simulated = list(records)
            for kind, identity, data in commit_rows:
                record = session.execution.seal_record(session, kind, identity, data, records=simulated)
                check_persisted_record(record)
                simulated.append(record)
            # This intent is the durable identity point. Any partial prefix is
            # refused or reconciled; no job/nonce/packet is regenerated under it.
            for kind, identity, data in commit_rows:
                native.append(session, kind, identity, data)
            attempt = session.execution.attempt(attempt_record["attempt"])
            _admit_input_dispatch(session, request_id, attempt)
            return attempt, action


def _recover_applied_marker(session, row, attempt, observation):
    """Finish an exact retained historical application; confer no current eligibility."""
    existing = [event["data"] for event in native.retained(session) if event["kind"] == "execution_application"
        and event["data"]["attempt_id"] == attempt.attempt_id]
    if not existing:
        return False
    plan = session.execution.profile.readability
    if len(existing) != 1 or plan is None or row.get("purpose") != "readability":
        refuse("application_replay_changed")
    if (observation["state"] != "succeeded" or not observation["final_accounting"]
            or type(observation["observed_usage"]["model_calls"]) is not int
            or observation["observed_usage"]["model_calls"] <= 0 or observation["reason"] == "offline_fixture"):
        refuse("model_execution_or_budget_unqualified")
    deliveries = [event["data"] for event in native.retained(session) if event["kind"] == "execution_input_handoff"
        and event["data"]["attempt_id"] == attempt.attempt_id]
    if (len(deliveries) != 1 or deliveries[0]["boundary"] != "native_readability_stdin_complete"
            or deliveries[0]["worker_sha256"] != digest(attempt.worker_document()).removeprefix("sha256:")):
        refuse("native_input_delivery_missing")
    parent = row["readability_parent"]
    packet = attempt.input_document["packet"]
    binding = parent["binding"]
    value = output_document(observation["output"], packet, max_output_bytes=plan.max_output_bytes)
    result = {"state": value["state"], "issues": [{**issue, "claim_id": binding["field_targets"].get(issue["pointer"])}
        for issue in value["issues"]], "output_sha256": digest(value), "substance_sha256": binding["substance_sha256"],
        "packet_sha256": binding["packet_sha256"]}
    expected = {"attempt_id": attempt.attempt_id, "operation": "readability", "original_id": row["original_id"], "result": result}
    if "checked_utc" in existing[0]:
        probes = [event["data"]["probe"] for event in native.retained(session) if event["kind"] == "execution_readability_probe"
            and event["request_id"] == native.child_id(native.child_id(attempt.attempt_id, "readability_application"), "readability_probe")]
        if len(probes) != 1 or _application_checked_at(existing[0], probes[0], attempt, observation,
                datetime.now(timezone.utc).isoformat()) is None:
            refuse("application_replay_changed")
        expected["checked_utc"] = existing[0]["checked_utc"]
    if (existing != [expected] or binding["packet_sha256"] != digest(packet)
            or parent["plan_sha256"] != plan.sha256):
        refuse("application_replay_changed")
    native.append(session, "execution_applied", native.child_id(attempt.attempt_id, "applied"), {
        "attempt_id": attempt.attempt_id, "operation": "readability", "original_id": row["original_id"], "profile_ref": attempt.config_ref})
    return True


def apply(session, attempt_id):
    """Owned current intake proposes issues; it never signs a semantic review."""
    with native.gate(session):
        session._check("advance", write=True)
        row, attempt, observation = owned(session, attempt_id)
        plan = session.execution.profile.readability
        if plan is None or row.get("purpose") != "readability" or attempt.config_ref != plan.config_ref:
            refuse("purpose_changed")
        applied = [event["data"] for event in native.retained(session) if event["kind"] == "execution_applied"
            and event["data"]["attempt_id"] == attempt_id]
        if applied:
            if len(applied) != 1 or applied[0]["operation"] != "readability":
                refuse("application_replay_changed")
            return
        if _recover_applied_marker(session, row, attempt, observation):
            return
        if native._document(session)["state"]["current"] in native.owner("run_controller").TERMINAL_STATES:
            refuse("run_terminal")
        if datetime.now(timezone.utc) >= min(native.deadline(session), _utc(attempt.job["deadline_utc"])):
            refuse("application_deadline_expired")
        _model_observed(session, attempt, observation)
    probe_request_id = native.child_id(attempt_id, "readability_application")
    from .readability_probes import prepared
    with prepared(session, probe_request_id) as witness:
        with native.gate(session):
            witness.require_request(session, probe_request_id)
            row, attempt, observation = owned(session, attempt_id)
            plan = session.execution.profile.readability
            if row.get("purpose") != "readability" or attempt.config_ref != plan.config_ref:
                refuse("purpose_changed")
            _model_observed(session, attempt, observation)
            deliveries = [record["data"] for record in native.retained(session) if record["kind"] == "execution_input_handoff"
                and record["data"]["attempt_id"] == attempt_id]
            if len(deliveries) != 1 or deliveries[0]["worker_sha256"] != digest(attempt.worker_document()).removeprefix("sha256:"):
                refuse("native_input_delivery_missing")
            parent = row["readability_parent"]
            with _held_current(session) as observed:
                _eligible(session, observed)
                if observed._basis_id != parent["basis_id"] or qualification(session, observed)["state"] != "qualified":
                    refuse("current_qualification_or_basis_changed")
                observed.bind_readability_eligibility()
                result = reduce(observation["output"], attempt.input_document["packet"], parent["binding"],
                    current_brief=project(observed.document()), order_seed=bytes.fromhex(parent["order_seed"]),
                    max_input_bytes=plan.max_input_bytes, max_output_bytes=plan.max_output_bytes)
                domain = observe_domain(project(observed.document()))
            _commit_current(session, witness, observed._native_observation, observed._native_view)
            if datetime.now(timezone.utc) >= min(native.deadline(session), _utc(attempt.job["deadline_utc"])):
                refuse("application_deadline_expired")
            probe_record = witness.consume(session)
            existing = [event["data"] for event in native.retained(session) if event["kind"] == "execution_application"
                and event["data"]["attempt_id"] == attempt_id]
            checked_utc = datetime.now(timezone.utc).isoformat()
            if (session.execution.qualifications.readability_observation(session.policy, session.execution.profile,
                    checked_utc, domain=domain, probe=probe_record["probe"])["state"] != "qualified"
                    or _application_checked_at({"checked_utc": checked_utc}, probe_record["probe"], attempt, observation, checked_utc) is None):
                refuse("current_qualification_or_basis_changed")
            readability_credential(session)
            application = {"attempt_id": attempt_id, "operation": "readability", "original_id": row["original_id"],
                "result": result, "checked_utc": checked_utc}
            if existing and existing != [application]:
                refuse("application_replay_changed")
            native.append(session, "execution_readability_probe", native.child_id(probe_request_id, "readability_probe"), probe_record)
            if not existing:
                native.append(session, "execution_application", native.child_id(attempt_id, "application"), application)
            native.append(session, "execution_applied", native.child_id(attempt_id, "applied"),
                {"attempt_id": attempt_id, "operation": "readability", "original_id": row["original_id"], "profile_ref": attempt.config_ref})



def _defects(app, target):
    return [{"code": issue["code"], "field": "/" + "/".join(issue["pointer"].split("/")[8:]),
        "field_sha256": issue["field_sha256"], "report_ref": app["result"]["output_sha256"]}
        for issue in app["result"]["issues"] if issue["claim_id"] == target]


def _repair_action(session, observed, target, app):
    from .execution_continuations import authenticated_gates, claim_sources, current_gates
    from .execution_disagreement import driver_action, project_claim
    view = native.current(session)
    prepared = native.owner("strict_evidence").prepare_review(session.root, target)
    packets, _bindings = native._sources(session)
    sources = claim_sources(prepared, packets)
    projection = project_claim(session, claim_id=target, prepared=prepared, sources=sources)
    gates = authenticated_gates(session, projection, current_gates(session, view, prepared, sources), prepared=prepared)
    original = next(row["original_id"] for row in observed.document()["original_outcomes"]
        for question in row["eligible_questions"] if any(claim["id"] == target for claim in question["claims"]))
    return driver_action(session, projection, gates=gates, original_id=original, presentation_defects=_defects(app, target))


def repair_defects(session, target):
    with _held_current(session) as observed:
        app = _current_application(session, observed)
        if app is None:
            refuse("current_owned_issues_missing")
        return _defects(app, target)
