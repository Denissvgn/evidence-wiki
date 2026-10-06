"""Typed actual-thread source custody for the complete blind readability input."""

import hashlib
import json
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import execution_session as native
from .errors import UsageError
from .mutation_contracts import canonical, digest
from .readability_context import create
from .readability_contracts import (
    check_persisted_record,
    dispatch_observation_document,
    dispatch_request_document,
    refuse,
    stored_attempt,
)
from .readability_execution import _InputDispatch, qualification
from .readability_plan import ReadabilityInputHandoff
from .resolution_operations import _held_readability_input

_READABILITY_REASONS = frozenset({"handoff_deadline_or_clock_invalid", "owned_handoff_attempt_changed", "run_terminal",
    "worker_bytes_changed", "complete_worker_input_bound", "native_usage_required", "current_handoff_basis_or_qualification_changed",
    "current_handoff_context_changed", "current_handoff_dependency_changed", "handoff_custody_not_held",
    "handoff_material_changed", "handoff_qualification_expired", "protected_issuer_required",
    "input_dispatch_identity_changed", "input_dispatch_not_fresh", "input_dispatch_not_held", "input_dispatch_executor_changed",
    "input_dispatch_already_begun", "input_dispatch_started_identity_changed", "input_dispatch_accounting_unknown",
    "input_dispatch_policy_or_qualification_changed", "input_dispatch_budget_or_deadline_exhausted", "input_dispatch_scope_changed",
    "dispatch_configuration_invalid", "dispatch_configuration_unavailable", "input_basis_changed", "input_domain_unknown",
    "input_domain_changed", "dispatch_probe_not_yet_valid", "dispatch_probe_expired", "input_current_purpose_or_configuration_unknown"})
_RESOLUTION_REASONS = frozenset({"resolution_custody_not_held", "resolution_readability_currency_changed",
    "resolution_readability_qualification_unknown", "resolution_current_inputs_changed", "bound_execution_session_required"})
_EPOCH_CODES = frozenset({"RESEARCH_CURRENT_OWNER_EPOCH_CHANGED", "RESEARCH_CURRENT_OWNER_EPOCH_NESTED"})
_USAGE_REASONS = frozenset({"host_trust_changed", "host_view_selection_changed", "host_state_clock_regressed",
    "source_revision_content_mismatch"})


class ReadabilityHandoffLease:
    """An actual native owner, never a caller permit or returned adapter ACK."""

    def __init__(self, session, attempt, *, wait_seconds, started_monotonic, admission):
        from .research_session import ResearchSession
        if (type(session) is not ResearchSession or session.execution.profile.readability is None
                or type(session.execution.profile.readability.handoff) is not ReadabilityInputHandoff
                or type(wait_seconds) not in (int, float) or not 0 < wait_seconds <= 86400
                or type(started_monotonic) not in (int, float) or not 0 < started_monotonic <= time.monotonic()
                or type(admission) is not _InputDispatch or admission.state != "transferred"):
            refuse("typed_handoff_invalid")
        admission.require(session, attempt)
        self.session, self.attempt = session, attempt
        self._admission, self._executor, self._running = admission, None, None
        self._deadline = started_monotonic + min(wait_seconds, session.execution.profile.readability.handoff.delivery_seconds)
        self._clock = datetime.now(timezone.utc)
        self._wall_deadline = min(native.deadline(session), datetime.fromisoformat(attempt.job["deadline_utc"]))
        self._thread = None
        self._view = self._config = self._capture = self._owned = None
        self._worker_sha256 = self._completed_sha256 = self.delivered_sha256 = None
        self._failure = None
        self._stage = "preflight"
        self._usage_error = native.owner("_evidence_usage").EvidenceInvalid
        self._configuration = None

    def observe_configuration(self):
        """The only configuration callback occurs before actual native custody."""
        self._stage = "dispatch_configuration"
        self._time_check()
        self.session._check("advance", write=True)
        self.session.context.require_grant("research_session.host_job.evaluate")
        executor = self.session.execution.executor
        self._bind_executor(executor)
        self._admission.begin_configuration(self)
        result, reason = native.bounded_host_call(self._configuration_callback, (),
            min(self.session.execution.profile.readability.probe_seconds, max(0, self._deadline - time.monotonic())))
        self._time_check()
        if reason is not None or type(result) is not _DispatchObservation or result._lease is not self:
            refuse("dispatch_configuration_unavailable")
        self._configuration = result
        self._admission.configuration_completed(self)
        record = self.session.execution.seal_record(self.session, "execution_input_handoff",
            native.child_id(self.attempt.attempt_id, "input_handoff"), self.handoff_document(planned=True))
        check_persisted_record(record)

    def _configuration_callback(self):
        try:
            return self._configuration_observed()
        except Exception as error:
            self._record_failure(error)
            raise

    def _configuration_observed(self):
        with self._admission.configuration_custody(self):
            self._time_check()
            now = datetime.now(timezone.utc)
            plan = self.session.execution.profile.readability
            expires = min(now + timedelta(seconds=plan.probe_lifetime_seconds), self._wall_deadline)
            parent = json.loads(self._admission.records)[-1]["data"]["readability_parent"]
            request = {"schema_version": "research-readability-dispatch-request/v1", "request_id": native.child_id(self.attempt.attempt_id, "dispatch_configuration"),
                "attempt_id": self.attempt.attempt_id, "job_sha256": self.attempt.job_sha256,
                "input_sha256": self.attempt.input_sha256, "binding_id": self.attempt.binding_id,
                "nonce": secrets.token_hex(16), "qualification_purpose": "readability", "config_ref": plan.config_ref,
                "profile_sha256": self.session.execution.profile.sha256, "registry_sha256": self.session.execution.qualifications.sha256,
                "semantic_basis_id": parent["substance_sha256"], "expires_utc": expires.isoformat()}
            function = getattr(self._executor, "qualification_configuration", None)
            if not callable(function):
                refuse("dispatch_configuration_unavailable")
            frozen_request = canonical(request)
            dispatch_request_document(request, self.attempt, self.session.execution.profile, self.session.execution.qualifications.sha256)
            returned = function(json.loads(frozen_request))
            if type(returned) is not dict or set(returned) != {"request", "configuration"} or returned["request"] != request:
                refuse("dispatch_configuration_invalid")
            value = {"schema_version": "research-readability-dispatch-observation/v1", "attempt_id": self.attempt.attempt_id,
                "job_sha256": self.attempt.job_sha256, "input_sha256": self.attempt.input_sha256, "binding_id": self.attempt.binding_id,
                "request_id": request["request_id"], "nonce": request["nonce"], "request": request, "probe": {"schema_version": "research-readability-probe/v1",
                    **{key: request[key] for key in ("profile_sha256", "registry_sha256", "semantic_basis_id", "expires_utc")},
                    "observed_utc": now.isoformat(), "configuration": returned["configuration"], "reasons": []}}
            result = _DispatchObservation(self, dispatch_observation_document(value, self.attempt,
                self.session.execution.profile, self.session.execution.qualifications.sha256))
            self._time_check()
            return result

    def ready_guard(self):
        self._time_check()
        if self._configuration is None:
            refuse("dispatch_configuration_unavailable")
        self._admission.ready_guard(self)

    def configuration_probe(self, observed):
        self._admission.held(self.session)
        observed.require(self.session)
        if self._configuration is None or self._configuration._lease is not self:
            refuse("dispatch_configuration_unavailable")
        from .readability_reference import observe_domain
        value = self._configuration.document()["probe"]
        domain = observe_domain(observed.input_brief())
        if domain["state"] != "observed":
            refuse("input_domain_unknown")
        if domain["semantic_basis_id"] != value["semantic_basis_id"]:
            refuse("input_domain_changed")
        now = datetime.now(timezone.utc)
        if now < datetime.fromisoformat(value["observed_utc"]):
            refuse("dispatch_probe_not_yet_valid")
        if now >= datetime.fromisoformat(value["expires_utc"]):
            refuse("dispatch_probe_expired")
        return value

    def handoff_document(self, *, planned=False):
        worker = hashlib.sha256(canonical(self.attempt.worker_document())).hexdigest() if planned else self.delivered_sha256
        if worker is None or self._configuration is None:
            refuse("worker_bytes_changed")
        value = self._configuration.document()
        dispatch_observation_document(value, self.attempt, self.session.execution.profile, self.session.execution.qualifications.sha256)
        return {"attempt_id": self.attempt.attempt_id, "job_sha256": self.attempt.job_sha256, "input_sha256": self.attempt.input_sha256,
            "worker_sha256": worker, "boundary": "native_readability_stdin_complete", "accepted": False,
            "dispatch_configuration_observation": value}

    def _bind_executor(self, executor):
        from .agent_execution import BoundedSubprocessExecutor
        if (type(executor) is not BoundedSubprocessExecutor or executor is not self.session.execution.executor
                or self._executor is not None and (self._executor is not executor or self._admission.state != "guard_scheduled")):
            refuse("input_dispatch_executor_changed")
        self._admission.require(self.session, self.attempt)
        self._executor = executor
        self._require_started_identity()

    def _require_started_identity(self):
        if self._executor is None:
            refuse("input_dispatch_executor_changed")
        with self._executor._lock:
            actual = self._executor._attempts.get(self.attempt.attempt_id)
            if self._running is None:
                if actual is not None:
                    refuse("input_dispatch_already_begun")
            elif (actual is not self._running or actual.attempt.document() != self.attempt.document()
                    or not actual.protected_delivery or actual.handoff_deadline != self._deadline):
                refuse("input_dispatch_started_identity_changed")

    def _before_start(self, executor):
        if executor is not self._executor or self._running is not None:
            refuse("input_dispatch_already_begun")
        self._admission.held(self.session)
        self.checkpoint()

    def _started(self, executor, running):
        if executor is not self._executor or self._running is not None or self._thread != threading.get_ident():
            refuse("input_dispatch_started_identity_changed")
        self._running = running
        self._require_started_identity()

    def failure_diagnostic(self):
        """Closed inert owner metadata, never a permit or a usage observation."""
        return dict(self._failure) if self._failure is not None else {}

    def _record_failure(self, error):
        if self._failure is not None:
            return
        code = None
        if isinstance(error, UsageError):
            reason = error.details.get("reason") if type(error.details) is dict else None
            if (error.error_code == "READABILITY_REFUSED" and type(reason) is str and reason in _READABILITY_REASONS
                    or error.error_code == "RESEARCH_RESOLUTION_REFUSED" and type(reason) is str and reason in _RESOLUTION_REASONS):
                code = reason
            elif type(error.error_code) is str and error.error_code in _EPOCH_CODES:
                code = "owner_epoch_changed" if error.error_code == "RESEARCH_CURRENT_OWNER_EPOCH_CHANGED" else "owner_epoch_nested"
        elif type(error) is self._usage_error:
            if len(error.args) == 1 and type(error.args[0]) is str and error.args[0] in _USAGE_REASONS:
                code = "native_usage_" + error.args[0]
        if code is not None:
            self._failure = {"stage": self._stage, "owner_reason": code}

    def _time_check(self):
        now = datetime.now(timezone.utc)
        if time.monotonic() >= self._deadline or now >= self._wall_deadline or now < self._clock:
            refuse("handoff_deadline_or_clock_invalid")
        self._clock = now

    @contextmanager
    def custody(self, payload):
        self._failure = None
        self._stage = "preflight"
        try:
            with self._admission.custody(self):
                with self._custody(payload) as owned:
                    self._stage = "transfer_body"
                    yield owned
        except Exception as error:
            self._record_failure(error)
            raise

    @contextmanager
    def _custody(self, payload):
        self._thread = threading.get_ident()
        self.delivered_sha256 = self._completed_sha256 = None
        with self.session.context.activate():
            self._time_check()
            self.session._check("advance", write=True)
            self.session.context.require_grant("research_session.host_job.evaluate")
            rows = [row["data"] for row in native.retained(self.session) if row["kind"] == "execution_attempt"
                and row["data"]["attempt"]["attempt_id"] == self.attempt.attempt_id]
            if (len(rows) != 1 or rows[0]["purpose"] != "readability"
                    or rows[0]["attempt"] != stored_attempt(self.attempt, self.session.execution.profile)):
                refuse("owned_handoff_attempt_changed")
            if native._document(self.session)["state"]["current"] in native.owner("run_controller").TERMINAL_STATES:
                refuse("run_terminal")
            if payload != canonical(self.attempt.worker_document()):
                refuse("worker_bytes_changed")
            if len(payload) > self.session.execution.profile.readability.max_input_bytes:
                refuse("complete_worker_input_bound")
            self._worker_sha256 = hashlib.sha256(payload).hexdigest()
            self._stage = "native_opening"
            with _held_readability_input(self.session, self._admission) as observed:
                view, config = observed._usage_view, observed._config
                if view is None:
                    refuse("native_usage_required")
                self._view, self._config, self._owned = view, config, observed
                self._capture = native.owner("_evidence_revision").capture_workspace(self.session.root)
                parent = rows[0]["readability_parent"]
                self._stage = "purpose_qualification"
                if observed._basis_id != parent["basis_id"]:
                    refuse("input_basis_changed")
                if qualification(self.session, observed)["state"] != "qualified":
                    refuse("input_current_purpose_or_configuration_unknown")
                observed.bind_readability_eligibility()
                self._stage = "context_binding"
                packet, binding = create(observed.input_brief(), context_id=parent["binding"]["context_id"],
                    order_seed=bytes.fromhex(parent["order_seed"]), max_input_bytes=self.session.execution.profile.readability.max_input_bytes)
                if packet != self.attempt.input_document["packet"] or binding != parent["binding"]:
                    refuse("current_handoff_context_changed")
                self._stage = "source_dependencies"
                packets = observed.source_packets()
                current = {row["source_id"]: row for row in packets}
                for expected in parent["sources"]:
                    actual = current.get(expected["source_id"])
                    if actual is None or any(actual.get(key) != expected[key] for key in ("record_sha256", "source_binding")):
                        refuse("current_handoff_dependency_changed")
                try:
                    self.checkpoint()
                    yield self
                    self.checkpoint()
                finally:
                    self._view = self._owned = None
                self._stage = "native_closing"
            self._stage = "final_preflight"
            self._time_check()
            self.session._check("advance", write=True)
        self._thread = None
        self._admission.held(self.session)
        self._time_check()
        self.delivered_sha256 = self._completed_sha256

    def checkpoint(self):
        self._stage = "checkpoint_time"
        self._time_check()
        if self._view is None or self._thread != threading.get_ident():
            refuse("handoff_custody_not_held")
        self._owned.require(self.session)
        self._admission.held(self.session)
        self._stage = "checkpoint_rights"
        self.session._check("advance", write=True)
        self.session.context.require_grant("research_session.host_job.evaluate")
        if native._document(self.session)["state"]["current"] in native.owner("run_controller").TERMINAL_STATES:
            refuse("run_terminal")
        self._view.revalidate(self.session.root, self._config)
        self._stage = "checkpoint_material"
        if native.owner("_evidence_revision").capture_workspace(self.session.root).revision_id != self._capture.revision_id:
            refuse("handoff_material_changed")
        self._stage = "checkpoint_qualification"
        if qualification(self.session, self._owned)["state"] != "qualified":
            refuse("handoff_qualification_expired")
        self._stage = "checkpoint_time"
        self._time_check()

    def complete(self, payload):
        self.checkpoint()
        self._stage = "worker_binding"
        if hashlib.sha256(payload).hexdigest() != self._worker_sha256:
            refuse("worker_bytes_changed")
        self._completed_sha256 = self._worker_sha256
        return {"attempt_id": self.attempt.attempt_id, "job_sha256": self.attempt.job_sha256,
            "input_sha256": self.attempt.input_sha256, "worker_sha256": self._completed_sha256,
            "binding_ref": digest(self.session.binding), "boundary": "native_readability_stdin_complete", "accepted": False}


class _DispatchObservation:
    """Only the fixed callback completion creates this private exact witness."""
    def __init__(self, lease, value):
        self._lease, self._bytes = lease, canonical(value)

    def document(self):
        return json.loads(self._bytes)
