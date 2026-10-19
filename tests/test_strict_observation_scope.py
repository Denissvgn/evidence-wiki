"""Operation custody expires and publication closes actual native observations."""

from contextlib import contextmanager
from contextvars import copy_context
from copy import deepcopy
from types import MappingProxyType, SimpleNamespace

import pytest

from evidence_wiki import _review_dependency_inputs as inputs
from evidence_wiki import _strict_observation_scope as scopes
from evidence_wiki.pack_discovery import owner
from evidence_wiki.workflow_contracts import WorkflowContractError


@pytest.fixture
def observation(tmp_path, monkeypatch):
    config = {"native": True}
    capture = owner("_evidence_revision").WorkspaceRevision(
        MappingProxyType({"research.yml": b"native: true\n"}), ())
    state = SimpleNamespace(checkpoint=7, strict_reviews={})
    view = SimpleNamespace(state=state, trust={"content_hash": "trust"})
    read = SimpleNamespace(active=True, view=view, require=lambda *args: None)
    holder = SimpleNamespace(_root=tmp_path, _scope=lambda *args: None)
    closes = []
    native = SimpleNamespace(capture_workspace=lambda root: capture,
        _dependency_closing=lambda *args: closes.append(args))
    context = SimpleNamespace(live_read=SimpleNamespace(get=lambda: read))
    monkeypatch.setattr(scopes, "owner", lambda name: native if name == "_strict_evidence" else context)
    monkeypatch.setattr(inputs, "assert_current", lambda actual: actual is holder or pytest.fail("wrong owner"))
    scope = scopes._Observation(holder, config, view, scopes._MINT)
    scope.token = scopes._ACTIVE.set(scope)
    try:
        yield scope, config, capture, view, read, closes
    finally:
        scope.valid = False
        scopes._ACTIVE.reset(scope.token)


def register(observation):
    scope, config, capture, view, _, _ = observation
    result = {"basis_id": "basis", "assurance": "artifact_checked", "claims": [{"accepted": True}]}
    current = {"material": {"claim_id": "claim"}}
    scope.register(scope.root, config, {"schema_version": "policy"}, view,
        result, (capture, None, [current], []))
    return result, current


def test_publication_reuses_detached_native_facts_with_each_actual_closing(observation):
    scope, config, _, view, _, closes = observation
    result, current = register(observation)
    result["claims"][0]["accepted"] = False
    current["material"]["claim_id"] = "changed"
    for _ in range(2):
        projected = scope.publication(scope.root, config, scope.policy, view, "artifact_checked")
        assert projected["claims"] == [{"accepted": True}]
        projected["claims"].clear()
    assert len(closes) == 2
    assert closes[0][-2][0]["material"]["claim_id"] == "claim"
    scope.close()
    assert len(closes) == 3


@pytest.mark.parametrize("changed", ["checkpoint", "state", "trust", "view", "config", "read", "closed"])
def test_changed_native_custody_is_sticky(observation, changed):
    scope, config, _, view, read, _ = observation
    register(observation)
    selected = view
    if changed == "checkpoint":
        view.state.checkpoint += 1
    elif changed == "state":
        view.state = deepcopy(view.state)
    elif changed == "trust":
        view.trust["content_hash"] = "changed"
    elif changed == "view":
        selected = deepcopy(view)
    elif changed == "config":
        config["native"] = False
    elif changed == "read":
        read.view = deepcopy(view)
    else:
        read.active = False
    with pytest.raises(WorkflowContractError) as error:
        scope.publication(scope.root, config, scope.policy, selected, "artifact_checked")
    assert error.value.error_code == "RESEARCH_STRICT_OBSERVATION_CHANGED"
    assert not scope.valid


def test_copied_context_and_expired_scope_cannot_consume(observation):
    scope, config, _, view, _, closes = observation
    register(observation)
    with pytest.raises(ValueError, match="different Context"):
        copy_context().run(scope.publication, scope.root, config, scope.policy, view, "artifact_checked")
    assert not scope.valid and not closes


def test_assurance_cannot_be_upgraded_and_constructor_requires_same_capture(observation):
    scope, config, capture, view, _, _ = observation
    with scope.constructor(scope.root, config, capture):
        pass
    register(observation)
    with pytest.raises(WorkflowContractError) as error:
        scope.publication(scope.root, config, scope.policy, view, "host_enforced")
    assert error.value.error_code == "RESEARCH_STRICT_OBSERVATION_CHANGED"


def test_public_dto_cannot_register_native_observation(monkeypatch):
    holder = inputs._Inputs.__new__(inputs._Inputs)
    monkeypatch.setattr(inputs._Inputs, "assert_current", lambda self: None)
    with pytest.raises(WorkflowContractError) as error:
        holder.retain_strict_observation(None, {}, {}, None, {"claims": []}, (None, None, [], []))
    assert error.value.error_code == "RESEARCH_STRICT_OBSERVATION_UNAVAILABLE"


def test_actual_native_publication_helper_matches_guarded_call_frame(tmp_path, monkeypatch):
    from evidence_wiki import host_context

    native = owner("_strict_evidence")
    holder = inputs._Inputs.__new__(inputs._Inputs)
    monkeypatch.setattr(inputs._Inputs, "assert_current", lambda self: None)
    monkeypatch.setattr(host_context, "review_dependency_inputs", lambda: holder)
    calls = []
    result = {"native_observation": "publication"}

    def publication(*args):
        calls.append(args)
        return result

    def current(actual):
        assert actual is holder
        return SimpleNamespace(publication=publication)

    monkeypatch.setattr(scopes, "current", current)
    config, policy, view = {}, {"schema_version": native.POLICY_SCHEMA_V2}, object()
    assert native._publication_evaluation(tmp_path, config, policy, view) is result
    assurance = "host_enforced" if native.strict_host.get() == str(tmp_path) else "artifact_checked"
    assert calls == [(tmp_path, config, policy, view, assurance)]
    with pytest.raises(WorkflowContractError) as error:
        holder.publication_observation(tmp_path, config, policy, view, assurance)
    assert error.value.error_code == "RESEARCH_STRICT_OBSERVATION_UNAVAILABLE"


@pytest.mark.parametrize("reason", ["native_open_unavailable", "authorized_capture"])
def test_unscoped_native_read_preserves_partial_fallback_without_retry(tmp_path, monkeypatch, reason):
    holder = SimpleNamespace(_root=tmp_path)
    config = {"native": True}
    entered = []

    @contextmanager
    def partial_reads(root, selected):
        assert root == tmp_path and selected == config
        entered.append(reason)
        yield

    def forbidden(*args, **kwargs):
        pytest.fail("Unscoped native reads cannot open or mint another view")

    native = SimpleNamespace(POLICY_SCHEMA_V2="policy/v2", configuration=lambda root: config,
        resolve_policy=lambda root, selected: {"schema_version": "policy/v2"})
    usage = SimpleNamespace(configured=lambda selected: True,
        _observation_reads=partial_reads, current_view=forbidden)
    owners = {"_strict_evidence": native, "_evidence_usage": usage,
        "_caller_context": SimpleNamespace(builtin_integrations=lambda selected: True),
        "_publication_context": SimpleNamespace(live_read=SimpleNamespace(get=lambda: None))}
    monkeypatch.setattr(scopes, "owner", owners.__getitem__)
    monkeypatch.setattr(scopes, "_Observation", forbidden)
    with scopes.observe(holder):
        assert scopes._ACTIVE.get() is None
        # Existing owners still report their own partial availability; the
        # absence of a proof conveys no native acceptance or authorization.
        assert scopes.current(holder) is None
    assert entered == [reason]
