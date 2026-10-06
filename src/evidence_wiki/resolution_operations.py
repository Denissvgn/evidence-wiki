"""Bound current resolution reads to existing publication and execution owners."""

import threading
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field

from evidence_wiki import execution_session as native
from evidence_wiki.mutation_contracts import detached, digest
from evidence_wiki.pack_discovery import owner
from evidence_wiki.research_session import ResearchSession

from .resolution_contracts import refuse
from .resolution_evidence import _InputPass, _Pass, questions
from .resolution_projection import _project
from .resolution_rules import blockers

_HELD = ContextVar("native_resolution_custody", default=None)


@dataclass(frozen=True)
class _OwnedResolution:
    """Private facts held by the real native read, never an outward approval."""

    _session: object = field(repr=False)
    _result: dict = field(repr=False)
    _basis_id: str = field(repr=False)
    _proof_sha256: str = field(repr=False)
    _thread_id: int = field(repr=False)
    _usage_view: object = field(default=None, repr=False)
    _config: object = field(default=None, repr=False)
    _readability_state: object = field(default=None, repr=False)
    _readability_kind: str = field(default="report", repr=False)
    _source_packets: object = field(default=None, repr=False)
    _native_view: object = field(default=None, repr=False)
    _native_observation: object = field(default=None, repr=False)

    def document(self):
        self.require(self._session)
        return detached(self._result)

    def require(self, session):
        if _HELD.get() is not self or session is not self._session or threading.get_ident() != self._thread_id:
            refuse("resolution_custody_not_held")

    def source_packets(self):
        """Borrow only actual permitted packets from this still-held read."""
        self.require(self._session)
        if self._usage_view is not None:
            self._usage_view.revalidate(self._session.root, self._config)
        return detached(self._source_packets)

    def native_current(self):
        """Fixed internal consumer input, accessible only inside actual custody."""
        self.require(self._session)
        return detached(self._native_view)

    def bind_readability(self):
        """Select a fixed callback-free purpose check, never caller proof data."""
        self.require(self._session)
        from .readability_execution import closing_vector
        value = closing_vector(self._session, self)
        object.__setattr__(self, "_readability_state", detached(value))
        return detached(value["report"])

    def close_readability(self):
        if self._readability_state is not None:
            self.require(self._session)
            from .readability_execution import closing_vector, qualification_vector
            operation = qualification_vector if self._readability_kind == "eligibility" else closing_vector
            if operation(self._session, self) != self._readability_state:
                refuse("resolution_readability_currency_changed")

    def bind_readability_eligibility(self):
        """Fixed pending-purpose native check, not caller flags or callbacks."""
        self.require(self._session)
        from .readability_execution import qualification_vector
        value = qualification_vector(self._session, self)
        if value["state"] != "qualified":
            refuse("resolution_readability_qualification_unknown")
        object.__setattr__(self, "_readability_kind", "eligibility")
        object.__setattr__(self, "_readability_state", detached(value))


@dataclass(frozen=True)
class _OwnedReadabilityInput(_OwnedResolution):
    """Private content custody does not expose a public resolution approval."""
    _input_admission: object = field(default=None, repr=False)

    def require(self, session):
        super().require(session)
        self._input_admission.held(session)

    def document(self):
        refuse("private_input_is_not_current_resolution")

    def input_brief(self):
        self.require(self._session)
        from .brief_projection import project
        return project(self._result)


@dataclass(frozen=True)
class _OwnedArtifactInput(_OwnedReadabilityInput):
    """Reserved native publication content carries no public approval handle."""

    def document(self):
        refuse("private_publication_input_is_not_current_resolution")


def _input_constraints(session, view, records, proofs, admission):
    from .artifact_delivery_input import _PublicationAdmission
    from .readability_execution import _InputDispatch
    if type(admission) is _InputDispatch:
        from .readability_execution import input_constraints
    elif type(admission) is _PublicationAdmission:
        from .artifact_delivery_input import constraints as input_constraints
    else:
        refuse("private_input_custody_required")
    return input_constraints(session, view, records, proofs, admission)


def _input_type(admission):
    if admission is None:
        return _OwnedResolution
    from .artifact_delivery_input import _PublicationAdmission
    from .readability_execution import _InputDispatch
    if type(admission) is _InputDispatch:
        return _OwnedReadabilityInput
    if type(admission) is _PublicationAdmission:
        return _OwnedArtifactInput
    refuse("private_input_custody_required")


def _input_observation(current, observation, proofs):
    # This private content selection is never retained as a policy observation.
    # Measured spend and original accounting are untouched.
    value = detached(observation)
    originals = {row["original_id"]: row for row in current["accounting"]["original_outcomes"]}
    for row in value["selected_outcomes"]:
        ready = all(proofs.get(question["slug"], {}).get("eligible") is True
            for question in originals[row["question_id"]]["questions"])
        row["independently_eligible"] = row["review_pass"] = ready
    return value


@contextmanager
def _held_owner(session, admission=None):
    """Keep current source custody through a private consumer and fresh closure.

    Public operations accept no caller handle or facts. Every invocation obtains
    actual native observations; neither material hashes nor this object survive
    as reusable acceptance after the read closes.
    """
    if type(session) is not ResearchSession:
        refuse("bound_execution_session_required")
    owned_type = _input_type(admission)
    session._check("resolution")
    session.context.require_grant("resolution_operations.current")
    if session.execution is None:
        refuse("bound_execution_session_required")
    config = owner("verify_quotes").load_config(session.root)
    usage = owner("_evidence_usage")
    manager = usage.current_view(session.root, config) if usage.configured(config) else nullcontext(None)
    token = None
    observed = None
    try:
        with manager as live:
            with native._current_facts(session) as current_facts:
                records, view = current_facts.records, current_facts.view
                packets = current_facts.packets
                observation = native.policy_observation(session, view)
                decision = session.policy.decide(observation)
                facts = _Pass(session, config, packets=packets) if admission is None else _InputPass(session, config, admission, packets=packets)
                proofs = questions(session, view, records, usage_view=live, facts=facts)
            if admission is None:
                constraints = blockers(view, session.policy.to_dict(), observation, proofs)
            else:
                constraints = _input_constraints(session, view, records, proofs, admission)
            if native.execution_budget_overrun(session, records):
                constraints.append("current_execution_budget_overrun")
            # The run summary omits its decision intentionally. Only a retained
            # HMAC-owned terminal step can populate this lifecycle annotation.
            terminal_steps = [row["data"]["step"] for row in records if row["kind"] == "execution_step"
                and type(row["data"]["step"].get("decision")) is dict
                and row["data"]["step"]["decision"].get("terminal")]
            terminal = terminal_steps[-1]["decision"] if terminal_steps and (view["run"] or {}).get("terminal") else None
            result = _project(run_id=session.run_id, binding_id=digest(session.binding), current=view,
                policy_document=session.policy.to_dict(), policy_observation=observation if admission is None else _input_observation(view, observation, proofs), decision=decision,
                derived_proofs=proofs, subset_blockers=constraints,
                lifecycle={"terminal": terminal is not None, "decision": terminal})
            observed = owned_type(session, result, view["basis_id"], digest({
                "basis_id": view["basis_id"], "records": records, "qualification": view["qualification"],
                "observation": observation, "decision": decision, "proofs": proofs, "constraints": constraints,
                "config": config}), threading.get_ident(), _usage_view=live, _config=config,
                _source_packets=detached(packets), **({"_input_admission": admission} if admission is not None else {}))
            object.__setattr__(observed, "_native_view", detached(view))
            object.__setattr__(observed, "_native_observation", current_facts.observed)
            token = _HELD.set(observed)
            yield observed
            if live is not None:
                live.revalidate(session.root, config)
            closing_config = owner("verify_quotes").load_config(session.root)
            with native._current_facts(session) as closing_facts:
                closing = closing_facts.view
                closing_observation = native.policy_observation(session, closing)
                closing_decision = session.policy.decide(closing_observation)
                facts = (_Pass(session, closing_config, packets=closing_facts.packets) if admission is None else
                    _InputPass(session, closing_config, admission, packets=closing_facts.packets))
                closing_proofs = questions(session, closing, records, usage_view=live, facts=facts)
            closing_constraints = (blockers(closing, session.policy.to_dict(), closing_observation, closing_proofs) if admission is None else
                _input_constraints(session, closing, records, closing_proofs, admission))
            if native.execution_budget_overrun(session, records):
                closing_constraints.append("current_execution_budget_overrun")
            if (closing["basis_id"] != view["basis_id"] or native.retained(session) != records
                    or closing_observation["selected_outcomes"] != observation["selected_outcomes"]
                    or closing["qualification"] != view["qualification"]
                    or closing_proofs != proofs or closing_constraints != constraints
                    or closing_facts.packets != packets
                    or (closing_decision["outcome"], closing_decision["reason"], closing_decision["selected_outcome_refs"])
                        != (decision["outcome"], decision["reason"], decision["selected_outcome_refs"])
                    or closing_config != config or owner("verify_quotes").load_config(session.root) != closing_config):
                refuse("resolution_current_inputs_changed")
            if live is not None:
                live.revalidate(session.root, config)
            session._check("resolution")
            observed.close_readability()
            if admission is not None:
                admission.held(session)
        if observed is not None:
            observed.close_readability()
            if admission is not None:
                admission.held(session)
    finally:
        if token is not None:
            _HELD.reset(token)


@contextmanager
def _held_current(session):
    with _held_owner(session) as observed:
        yield observed


@contextmanager
def _held_readability_input(session, admission):
    from .readability_execution import _InputDispatch
    if type(admission) is not _InputDispatch:
        refuse("private_input_custody_required")
    admission.held(session)
    with _held_owner(session, admission) as observed:
        yield observed


def current(session):
    """Read every frozen original; never resolve questions or dispatch effects."""
    with _held_current(session) as observed:
        result = observed.document()
    return result


@contextmanager
def _held_artifact_input(session, admission):
    from .artifact_delivery_input import _PublicationAdmission
    if type(admission) is not _PublicationAdmission:
        refuse("private_publication_custody_required")
    admission.held(session)
    with _held_owner(session, admission) as observed:
        yield observed
