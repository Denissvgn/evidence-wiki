"""Exact selected host configuration is observed outside every native gate."""

import json
import secrets
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from .brief_projection import project
from .calibration_probes import configuration_document
from .mutation_contracts import canonical, detached, digest
from .readability_reference import observe_domain
from .research_execution import bounded_host_call
from .resolution_operations import _held_current


def _observe(session, request_id):
    from . import execution_session as native
    plan = session.execution.profile.readability
    if plan is None:
        return
    with native.gate(session):
        session._check("advance", write=True)
        session.context.require_grant("research_session.host_job.evaluate")
        native.require_machine_compatible(session)
        plan.selected_profile(session.policy, session.execution.profile)
        from .service_authentication import readability_credential
        readability_credential(session)
        document = native._document(session)
        if document["state"]["current"] in native.owner("run_controller").TERMINAL_STATES:
            from .readability_contracts import refuse
            refuse("run_terminal")
        view, records = native.current(session), native.retained(session)
        from .run_policy import _hard_policy_failure, _owner_observation
        policy = session.policy.to_dict()
        observation = _owner_observation(policy, native.policy_observation(session, view))
        if _hard_policy_failure(policy, observation) is not None or native.execution_budget_overrun(session, records):
            from .readability_contracts import refuse
            refuse("preterminal_policy_not_eligible")
    with _held_current(session) as observed:
        domain = observe_domain(project(observed.document()))
        basis = observed._basis_id
    if domain["state"] != "observed":
        return
    now = datetime.now(timezone.utc)
    expires = min(now + timedelta(seconds=plan.probe_lifetime_seconds), native.deadline(session))
    request = {"schema_version": "research-readability-probe-request/v1", "nonce": secrets.token_hex(16),
        "request_id": request_id,
        "qualification_purpose": "readability", "config_ref": plan.config_ref,
        "profile_sha256": session.execution.profile.sha256, "registry_sha256": session.execution.qualifications.sha256,
        "semantic_basis_id": domain["semantic_basis_id"], "expires_utc": expires.isoformat()}
    probe = {"schema_version": "research-readability-probe/v1", **{key: request[key] for key in
        ("profile_sha256", "registry_sha256", "semantic_basis_id", "expires_utc")},
        "observed_utc": now.isoformat(), "configuration": None, "reasons": []}
    function = getattr(session.execution.executor, "qualification_configuration", None)
    if not callable(function):
        probe["reasons"].append("qualification_service_unavailable")
    else:
        timeout = min(plan.probe_seconds, max(0, (expires - now).total_seconds()))
        returned, error = bounded_host_call(function, (detached(request),), timeout)
        if error is not None:
            probe["reasons"].append(error)
        else:
            try:
                if type(returned) is not dict or set(returned) != {"request", "configuration"} or returned["request"] != request:
                    raise ValueError("uncorrelated")
                probe["configuration"] = configuration_document(returned["configuration"])
            except (Exception, SystemExit):
                probe["reasons"].append("qualification_probe_invalid")
    return _ProbeObservation(session, request_id, basis, domain, probe, native.retained(session))


_ACTIVE = ContextVar("native_readability_probe_observation", default=None)


class _ProbeObservation:
    """Actual correlated callback bytes are inert until a fresh owner consumes them."""

    def __init__(self, session, request_id, basis, domain, probe, records):
        self._session, self._request_id = session, request_id
        self._basis, self._domain_bytes, self._probe_bytes = basis, canonical(domain), canonical(probe)
        self._records = detached(records)
        self._profile, self._registry = session.execution.profile.sha256, session.execution.qualifications.sha256
        self._thread, self._consumed = threading.get_ident(), False

    def require(self, session):
        from . import execution_session as native
        from .readability_contracts import refuse
        if (_ACTIVE.get() is not self or self._session is not session or self._thread != threading.get_ident()
                or self._consumed or self._profile != session.execution.profile.sha256
                or self._registry != session.execution.qualifications.sha256 or native.retained(session) != self._records):
            refuse("probe_observation_not_owned")
        now = datetime.now(timezone.utc)
        probe = json.loads(self._probe_bytes)
        if not datetime.fromisoformat(probe["observed_utc"]) <= now < datetime.fromisoformat(probe["expires_utc"]):
            refuse("qualification_probe_expired")

    def for_owned(self, session, observed):
        self.require(session)
        observed.require(session)
        final = observe_domain(project(observed.document()))
        if observed._basis_id != self._basis or final["semantic_basis_id"] != json.loads(self._domain_bytes)["semantic_basis_id"]:
            from .readability_contracts import refuse
            refuse("domain_changed_during_probe")
        return json.loads(self._probe_bytes)

    def require_request(self, session, request_id):
        self.require(session)
        if request_id != self._request_id:
            from .readability_contracts import refuse
            refuse("probe_observation_request_mismatch")

    def retention(self, session):
        self.require(session)
        return {"probe": json.loads(self._probe_bytes), "domain_ref": digest(json.loads(self._domain_bytes))}

    def qualification(self, session, request_id):
        self.require_request(session, request_id)
        return session.execution.qualifications.readability_observation(session.policy, session.execution.profile,
            datetime.now(timezone.utc).isoformat(), domain=json.loads(self._domain_bytes), probe=json.loads(self._probe_bytes))

    def consume(self, session):
        value = self.retention(session)
        self._consumed = True
        return value


@contextmanager
def prepared(session, request_id):
    """Only this fixed native callback path can install an in-flight witness."""
    from . import execution_session as native
    from .readability_contracts import refuse
    records = native.retained(session)
    if any(row["kind"] == "execution_advance" and row["data"]["advance_id"] == request_id
            or row["kind"] == "execution_readability_probe" and row["request_id"] == native.child_id(request_id, "readability_probe")
            or row["kind"] == "execution_attempt" and row["data"]["attempt"]["job"]["id"] == request_id for row in records):
        refuse("commit_prefix_requires_reconciliation")
    if _ACTIVE.get() is not None:
        refuse("probe_observation_nested")
    value = _observe(session, request_id)
    if value is None:
        refuse("current_brief_not_supported")
    token = _ACTIVE.set(value)
    try:
        yield value
    finally:
        value._consumed = True
        _ACTIVE.reset(token)


def selected(session, observed):
    """Return only an actual current in-flight witness, never a supplied DTO."""
    value = _ACTIVE.get()
    return None if value is None else value.for_owned(session, observed)


def require_request(session, request_id):
    value = _ACTIVE.get()
    if value is not None:
        value.require_request(session, request_id)


def invalidate():
    """No installed observation survives another wait or even an idempotent write."""
    value = _ACTIVE.get()
    if value is not None:
        value._consumed = True


def refresh(session, request_id):
    """Retain current observation without granting a semantic application."""
    from . import execution_session as native
    with prepared(session, request_id) as witness:
        with _held_current(session) as closing:
            witness.for_owned(session, closing)
            closing.bind_readability_eligibility()
        with native.gate(session):
            value = witness.consume(session)
            native.append(session, "execution_readability_probe", native.child_id(request_id, "readability_probe"), value)
