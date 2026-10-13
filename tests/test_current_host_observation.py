"""Full current snapshots and native host reads share effect-free owner custody."""

import copy
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_session as native
from evidence_wiki import host_qualification
from evidence_wiki.errors import ConfigError, UsageError
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.research_session import ResearchSession
from tests.test_host_qualification import synthetic as _synthetic_fixture


@pytest.fixture
def observation(monkeypatch, tmp_path):
    session, records = _synthetic_fixture.__wrapped__(monkeypatch, tmp_path)
    state = {"reads": 0, "finishes": 0, "checks": [], "controls": {"context_id": "owned"},
        "changed": False, "denied": False, "qualified": True, "strict_reads": 0,
        "strict": {"basis_id": digest("strict-basis"), "claims": [{"accepted": False}], "evaluated_at": "opening"}}
    view = {"basis_id": digest("owned-basis"), "policy_id": session.policy.policy_id, "policy_compatible": True,
        "accounting": {"original_outcomes": [{"original_id": "original", "accepted": False}]},
        "canonical_release": {"publication": {"verdict": "hold"}}, "usage": {"seconds": 1},
        "qualification": {"state": "qualified", "profiles": []}}

    def check(_session, operation):
        state["checks"].append(operation)
        if operation != "current" or state["denied"]:
            raise UsageError("AUTHORITY_CHANGED", "Current native authority unavailable.")

    def finish():
        state["finishes"] += 1
        if state["changed"]:
            raise UsageError("SOURCE_CHANGED", "Current native source changed.")

    def observe(_session):
        state["reads"] += 1
        observed = SimpleNamespace(config={}, controls={"result": {"context_id": "owned"}}, finish=finish,
            policy={"id": "owned"}, integrations_qualified=True,
            strict={"state": "observed", "result": copy.deepcopy(state["strict"])})
        return copy.deepcopy(view), observed, [], copy.deepcopy(records)

    def strict_check(_root, operation):
        assert operation == "check"
        state["strict_reads"] += 1
        return copy.deepcopy(state["strict"]) | {"evaluated_at": "closing"}

    monkeypatch.setattr(ResearchSession, "_check", check)
    monkeypatch.setattr(native, "_observe_current", observe)
    monkeypatch.setattr(native, "owner", lambda name: SimpleNamespace(capture=lambda *_: state["controls"])
        if name == "_caller_context" else SimpleNamespace(configured=lambda *_: False)
        if name == "_evidence_usage" else SimpleNamespace(run_operation=strict_check)
        if name == "strict_evidence" else SimpleNamespace(configuration=lambda *_: {}))
    monkeypatch.setattr(native, "_qualifications", lambda *_a, **_k: {
        "state": "qualified" if state["qualified"] else "unknown", "profiles": []})
    from evidence_wiki import service_authentication
    original_status = service_authentication.status

    def status(selected):
        before = native._current_identity(selected)
        result = original_status(selected)
        after = native._current_identity(selected)
        assert before == after
        return result

    monkeypatch.setattr(service_authentication, "status", status)
    return session, state, records, view


def test_full_current_and_native_witness_share_one_observation_with_repeated_validity_checks(observation):
    session, state, _, expected = observation
    with native._current_facts(session, operation="current") as facts:
        view = native._current_view(session)
        assert view == expected and view is not facts.view
        view["canonical_release"]["publication"]["verdict"] = "caller-mutated"
        assert facts.view["canonical_release"]["publication"]["verdict"] == "hold"
        witness = host_qualification(session)
        assert witness["basis_id"] == expected["basis_id"]
        assert witness["attempts"] == [] and witness["authorized"] is False
        assert native.current_qualifications(session)["state"] == "qualified"
    assert state["reads"] == 1 and state["strict_reads"] == 1
    assert state["finishes"] >= 12 and set(state["checks"]) == {"current"}
    assert facts.invalidated is True


def test_current_grant_is_required_before_observation_without_resolution_permission(observation):
    session, state, _, _ = observation
    state["denied"] = True
    with pytest.raises(UsageError, match="authority unavailable"):
        with native._current_facts(session, operation="current"):
            pytest.fail("Ungrantable observation opened")
    assert state["reads"] == 0 and state["checks"] == ["current"]


@pytest.mark.parametrize("operation", ["resolution", "semantic_report"])
def test_full_view_requires_the_epoch_with_full_semantic_closing(observation, monkeypatch, operation):
    session, _, _, expected = observation
    monkeypatch.setattr(ResearchSession, "_check", lambda *_args, **_kwargs: None)
    with native._current_facts(session, operation=operation):
        with pytest.raises(UsageError):
            native._current_view(session)
        assert native._current_identity(session)["basis_id"] == expected["basis_id"]


@pytest.mark.parametrize("change", ["source", "journal", "context", "authority", "effect"])
def test_current_host_epoch_rechecks_native_changes_before_next_read(observation, change):
    session, state, records, _ = observation
    with pytest.raises(UsageError):
        with native._current_facts(session, operation="current"):
            native._current_view(session)
            if change == "source":
                state["changed"] = True
            elif change == "journal":
                records.append({"kind": "changed"})
            elif change == "context":
                state["controls"] = {"context_id": "changed"}
            elif change == "authority":
                state["denied"] = True
            else:
                native.invalidate_current_facts()
            host_qualification(session)
    assert state["reads"] == 1


def test_full_snapshot_refreshes_qualification_instead_of_reusing_opening_state(observation):
    session, state, _, _ = observation
    with pytest.raises(UsageError):
        with native._current_facts(session, operation="current") as facts:
            assert native._current_view(session)["qualification"]["state"] == "qualified"
            state["qualified"] = False
            assert native._current_view(session)["qualification"]["state"] == "unknown"
            assert facts.view["qualification"]["state"] == "qualified"
    assert state["reads"] == 1


def test_witness_refuses_qualification_change_during_native_credential_read(observation, monkeypatch):
    from evidence_wiki import service_authentication

    session, state, _, _ = observation
    status = service_authentication.status
    def expiring(selected):
        result = status(selected)
        state["qualified"] = False
        return result
    monkeypatch.setattr(service_authentication, "status", expiring)
    with pytest.raises(UsageError):
        with native._current_facts(session, operation="current"):
            with pytest.raises(ConfigError) as caught:
                host_qualification(session)
            assert caught.value.details["reason"] == "observation_changed"


@pytest.mark.parametrize("change", ["source", "journal", "context", "authority", "effect"])
def test_epoch_closing_refuses_changes_after_a_returned_witness(observation, change):
    session, state, records, _ = observation
    with pytest.raises(UsageError):
        with native._current_facts(session, operation="current") as facts:
            witness = host_qualification(session)
            assert witness["authorized"] is False
            if change == "source":
                state["changed"] = True
            elif change == "journal":
                records.append({"kind": "changed"})
            elif change == "context":
                state["controls"] = {"context_id": "changed"}
            elif change == "authority":
                state["denied"] = True
            else:
                native.invalidate_current_facts()
    assert facts.invalidated is True and native._CURRENT_FACTS.get() is None


def test_full_snapshot_cannot_cross_sessions_or_reenter_an_exited_epoch(observation):
    session, state, _, _ = observation
    with native._current_facts(session, operation="current") as facts:
        with pytest.raises(UsageError):
            native._current_view(copy.copy(session))
    token = native._CURRENT_FACTS.set(facts)
    try:
        with pytest.raises(UsageError):
            native._current_view(session)
    finally:
        native._CURRENT_FACTS.reset(token)
    with native._current_facts(session, operation="current"):
        native._current_view(session)
    assert state["reads"] == 2


@pytest.mark.parametrize("change", ["strict_source_expired", "strict_source_outside_effective_interval",
    "strict_source_age_invalid", "strict_review_expired", "qualification", "material"])
def test_native_strict_closing_revalidates_semantic_time_and_owner_boundaries(observation, monkeypatch, change):
    session, state, _, _ = observation
    actual_owner = native.owner

    def strict_check(_root, operation):
        state["strict_reads"] += 1
        assert operation == "check"
        if change == "qualification":
            state["qualified"] = False
        elif change == "material":
            state["changed"] = True
        else:
            raise UsageError("STRICT_EVIDENCE_REFUSED", "Native strict closing refused.", details={"reason": change})
        return copy.deepcopy(state["strict"]) | {"evaluated_at": "closing"}

    monkeypatch.setattr(native, "owner", lambda name: SimpleNamespace(run_operation=strict_check)
        if name == "strict_evidence" else actual_owner(name))
    with pytest.raises(UsageError):
        with native._current_facts(session, operation="current") as facts:
            view = native._current_view(session)
            witness = host_qualification(session)
            assert view["qualification"]["state"] == "qualified" and witness["authorized"] is False
    assert state["strict_reads"] == 1 and facts.invalidated is True


def test_closing_strict_compares_all_semantic_fields_and_only_ignores_evaluation_time(observation):
    session, state, _, _ = observation
    with pytest.raises(UsageError):
        with native._current_facts(session, operation="current"):
            state["strict"]["claims"][0]["accepted"] = True
    assert state["strict_reads"] == 1


@pytest.mark.parametrize("availability", ["not_configured", "unavailable"])
def test_current_epoch_preserves_same_legitimate_strict_availability(observation, monkeypatch, availability):
    session, state, _, _ = observation
    observe = native._observe_current
    strict_state = {"state": availability}
    if availability == "unavailable":
        strict_state.update(reason="strict_policy_missing", error_code="STRICT_EVIDENCE_REFUSED")

    def unavailable(selected):
        view, observed, packets, records = observe(selected)
        observed.strict = strict_state
        if availability == "not_configured":
            observed.policy = None
        return view, observed, packets, records

    actual_owner = native.owner
    def refused(*_args):
        state["strict_reads"] += 1
        raise UsageError("STRICT_EVIDENCE_REFUSED", "Native strict unavailable.", details={"reason": "strict_policy_missing"})

    monkeypatch.setattr(native, "_observe_current", unavailable)
    monkeypatch.setattr(native, "owner", lambda name: SimpleNamespace(run_operation=refused)
        if name == "strict_evidence" else actual_owner(name))
    with native._current_facts(session, operation="current"):
        native._current_view(session)
    assert state["strict_reads"] == (1 if availability == "unavailable" else 0)
