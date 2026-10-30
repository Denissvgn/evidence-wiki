"""Actual observation custody stays inside its native application owner."""

import threading
from contextlib import contextmanager
from contextvars import copy_context
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import _current_fact_observation as custody
from evidence_wiki import _review_dependency_inputs as inputs
from evidence_wiki import execution_session as native
from evidence_wiki import host_context
from evidence_wiki._facades._base import _translated
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.pack_discovery import owner
from evidence_wiki.workflow_contracts import WorkflowContractError
from tests.test_application_validation_epoch import application as application


@pytest.mark.parametrize("metadata,reason", [
    (("source_revision_changed",), "source_revision_changed"),
    (("host_state_lock_unavailable",), "host_state_lock_unavailable"),
    (("SOURCE INVALID",), "source_usage_unavailable"),
    (({"untrusted": True},), "source_usage_unavailable"),
    (("source_revision_changed", "extra"), "source_usage_unavailable"),
])
def test_current_owner_qualification_refusal_uses_sdk_error_boundary(tmp_path, monkeypatch, metadata, reason):
    session = SimpleNamespace(root=tmp_path, _check=lambda *args, **kwargs: None)
    facts = native._CurrentFacts.__new__(native._CurrentFacts)
    facts.session, facts.operation, facts.invalidated = session, "advance", False
    facts.thread, facts.records, facts.packets = threading.get_ident(), [], []
    calls = []
    facts.observed = SimpleNamespace(config={}, controls={"result": {}},
        capture=SimpleNamespace(files={}), finish=lambda: calls.append("finish"))
    publication = owner("_publication_usage")
    strict = owner("strict_evidence")
    def qualify(*args, **kwargs):
        calls.append("qualify")
        raise publication.EvidenceInvalid(*metadata)
    @contextmanager
    def live_view(*args):
        yield SimpleNamespace(revalidate=lambda *args: calls.append("revalidate"))
    owners = {"_caller_context": SimpleNamespace(capture=lambda root: {}),
        "_evidence_usage": SimpleNamespace(configured=lambda config: True, current_view=live_view),
        "_publication_usage": SimpleNamespace(EvidenceInvalid=publication.EvidenceInvalid, qualify_capture=qualify),
        "strict_evidence": strict}
    monkeypatch.setattr(native, "owner", owners.__getitem__)
    monkeypatch.setattr(native, "retained", lambda session: [])
    token = native._CURRENT_FACTS.set(facts)
    try:
        with pytest.raises(EvidenceWikiError) as caught:
            facts.require(session)
    finally:
        native._CURRENT_FACTS.reset(token)
    expected = _translated(strict.refusal(reason))
    assert type(caught.value) is type(expected)
    for field in ("error_code", "message", "details", "exit_code", "recoverable", "remediation"):
        assert getattr(caught.value, field) == getattr(expected, field)
    assert calls == ["finish", "qualify"]


@pytest.fixture
def continuation(tmp_path, monkeypatch):
    """Replace expensive owners, while retaining the actual capability classes."""
    config, policy = {"native": True}, {"schema_version": "policy"}
    holder = inputs._Inputs.__new__(inputs._Inputs)
    session = SimpleNamespace(root=tmp_path)
    holder._session, holder._root = session, tmp_path
    holder._scope = lambda *args: None
    holder.assert_current = lambda: None
    monkeypatch.setattr(inputs, "current_inputs", lambda: holder)
    facts = native._CurrentFacts.__new__(native._CurrentFacts)
    facts.session, facts.operation, facts.invalidated = session, "advance", False
    facts.packets = [{"source_id": "source"}]
    facts.require = lambda session: None
    class View:
        def __init__(self):
            self.state = SimpleNamespace(document={"events": []}, checkpoint="checkpoint",
                state_id="state", binding="binding")
            self.trust = {"key": "trusted"}
    capture = SimpleNamespace(files={"record": b"original"})
    current = {"material": {"claim_id": "claim"}, "snapshot": {"claim": {"id": "claim"}}}
    reviews = [("envelope", "payload", current)]
    scope = SimpleNamespace(holder=holder, config=config, policy=policy,
        view=View(), result={"basis_id": "basis", "claims": []})
    producer = object()
    value = custody._Continuation(facts, scope, capture, producer, [current], reviews)
    value.packets = deepcopy(facts.packets)
    facts._review_observation = value
    facts_context = native._CURRENT_FACTS
    facts_token = facts_context.set(facts)
    epoch = custody._OwnerEpoch(facts)
    epoch.token = custody._OWNER.set(epoch)
    value.token = custody._ACTIVE.set(value)
    active_token = value.token
    calls = []
    strict = owner("_strict_evidence")
    monkeypatch.setattr(strict, "_dependency_closing", lambda *args: calls.append(("closing", args)))
    def qualify(*args, **kwargs):
        calls.append(("qualify", args, kwargs))
        return {"complete": True, "selected": {"source": "revision"}}
    owners = {"_strict_evidence": strict, "strict_evidence": owner("strict_evidence"),
        "_evidence_usage": SimpleNamespace(UsageView=View),
        "_publication_usage": SimpleNamespace(qualify_capture=qualify)}
    monkeypatch.setattr(custody, "owner", owners.__getitem__)
    monkeypatch.setattr(host_context, "review_dependency_inputs", lambda: holder)
    try:
        yield SimpleNamespace(value=value, facts=facts, holder=holder, session=session, config=config,
            policy=policy, View=View, capture=capture, producer=producer, calls=calls, strict=strict)
    finally:
        custody._ACTIVE.reset(value.token if value.token is not None else active_token)
        custody._OWNER.reset(epoch.token)
        facts_context.reset(facts_token)


def consume(case, view=None):
    return case.strict._application_observation(case.session.root, case.config, case.policy,
        case.View() if view is None else view)


def test_actual_plain_native_helper_matches_guard_and_qualifies_each_new_view(continuation):
    case = continuation
    views = [case.View(), case.View()]
    results = [consume(case, view) for view in views]
    assert [call[0] for call in case.calls] == ["qualify", "closing", "qualify", "closing"]
    for index, view in enumerate(views):
        closing = case.calls[index * 2 + 1][1]
        assert closing[3] is view and closing[4] is case.capture
        assert closing[6] is case.producer
        assert closing[7] == case.value.currents and closing[8] == case.value.reviews
    results[0][0]["claims"].append({"changed": True})
    results[0][1][2].clear()
    assert results[1][0]["claims"] == [] and len(results[1][1][2]) == 1
    with pytest.raises(WorkflowContractError):
        case.holder.application_observation(case.session.root, case.config, case.policy, case.View())
    with pytest.raises(WorkflowContractError):
        custody.consume(case.holder, case.session.root, case.config, case.policy, case.View())


@pytest.mark.parametrize("namespace", ["observation", "strict"])
@pytest.mark.parametrize("changed", [None, "state", "trust"])
def test_real_native_usage_namespaces_requalify_and_close(continuation, monkeypatch, namespace, changed):
    case = continuation
    usage = owner("_evidence_usage")
    strict_usage = case.strict.sibling("_evidence_usage")
    assert usage.UsageView is not strict_usage.UsageView
    original = custody.owner
    monkeypatch.setattr(custody, "owner", lambda name: usage if name == "_evidence_usage" else original(name))
    module = usage if namespace == "observation" else strict_usage
    def fresh():
        state = module.UsageState(case.session.root, "state", None)
        return module.UsageView(state, {"key": "trusted"}, datetime.now(timezone.utc))
    views = [fresh(), fresh()]
    case.value.state = deepcopy(vars(views[0].state))
    case.value.trust = deepcopy(views[0].trust)
    if changed is not None:
        if changed == "state":
            views[0].state.document["events"].append({"changed": True})
        else:
            views[0].trust["key"] = "revoked"
        with pytest.raises(WorkflowContractError):
            consume(case, views[0])
        assert not case.value.valid and not case.calls
        with pytest.raises(WorkflowContractError):
            consume(case, views[1])
        return
    for view in views:
        consume(case, view)
    assert [call[0] for call in case.calls] == ["qualify", "closing", "qualify", "closing"]
    assert case.calls[1][1][3] is views[0]
    assert case.calls[3][1][3] is views[1]


@pytest.mark.parametrize("namespace", ["observation", "strict", "foreign"])
def test_native_usage_subclasses_and_foreign_lookalikes_are_rejected(continuation, monkeypatch, namespace):
    case = continuation
    usage = owner("_evidence_usage")
    strict_usage = case.strict.sibling("_evidence_usage")
    original = custody.owner
    monkeypatch.setattr(custody, "owner", lambda name: usage if name == "_evidence_usage" else original(name))
    base = usage.UsageView if namespace == "observation" else strict_usage.UsageView
    if namespace == "foreign":
        view = case.View()
    else:
        class Derived(base):
            pass
        view = Derived(usage.UsageState(case.session.root, "state", None),
            {"key": "trusted"}, datetime.now(timezone.utc))
    case.value.state = deepcopy(vars(view.state))
    case.value.trust = deepcopy(view.trust)
    with pytest.raises(WorkflowContractError):
        consume(case, view)
    assert not case.value.valid and not case.calls


@pytest.mark.parametrize("field", ["document", "checkpoint", "state_id", "binding", "trust"])
def test_changed_fresh_usage_state_or_trust_is_sticky(continuation, field):
    case, view = continuation, continuation.View()
    if field == "trust":
        view.trust["key"] = "revoked"
    elif field == "document":
        view.state.document["events"].append("new")
    else:
        setattr(view.state, field, "changed")
    with pytest.raises(WorkflowContractError):
        consume(case, view)
    assert not case.value.valid
    assert case.calls == []


@pytest.mark.parametrize("field", ["session", "facts", "invalidated", "thread", "task", "source", "config", "policy"])
def test_exact_owner_and_live_inputs_required(continuation, monkeypatch, field):
    case = continuation
    if field == "session":
        case.holder._session = object()
    elif field == "facts":
        monkeypatch.setattr(native, "_CURRENT_FACTS", SimpleNamespace(get=lambda: object()))
    elif field == "invalidated":
        case.facts.invalidated = True
    elif field == "thread":
        case.value.thread = -1
    elif field == "task":
        case.value.task = object()
    elif field == "source":
        case.capture.files["record"] = b"changed"
    elif field == "config":
        case.config["native"] = False
    else:
        case.policy["schema_version"] = "changed"
    with pytest.raises(WorkflowContractError):
        consume(case)
    assert not case.calls


def test_copied_context_cannot_consume_and_invalidates(continuation):
    with pytest.raises(ValueError, match="different Context"):
        copy_context().run(consume, continuation)
    assert not continuation.value.valid
    assert not continuation.calls


def test_holder_host_generation_checks_remain_mandatory(continuation):
    case = continuation
    def changed():
        raise WorkflowContractError("RESEARCH_REVIEW_DEPENDENCY_INPUTS_CHANGED", "changed")
    case.holder.assert_current = changed
    with pytest.raises(WorkflowContractError):
        consume(case)
    assert not case.value.valid and not case.calls


def test_fresh_qualification_failure_never_reaches_closing(continuation, monkeypatch):
    case = continuation
    original = custody.owner
    publication = SimpleNamespace(qualify_capture=lambda *args, **kwargs: {"complete": False})
    monkeypatch.setattr(custody, "owner", lambda name: publication if name == "_publication_usage" else original(name))
    with pytest.raises(WorkflowContractError):
        consume(case)
    assert not case.value.valid and not case.calls


def test_source_ids_only_inside_exact_application_and_expire(continuation):
    case = continuation
    assert custody.source_ids(case.session) == {"source"}
    with pytest.raises(WorkflowContractError):
        custody.source_ids(SimpleNamespace())
    custody.invalidate(case.facts)
    with pytest.raises(WorkflowContractError):
        consume(case)


def test_no_active_continuation_preserves_fresh_native_path(continuation):
    case = continuation
    token = custody._ACTIVE.set(None)
    try:
        assert consume(case) is None
        assert custody.source_ids(case.session) is None
    finally:
        custody._ACTIVE.reset(token)
    assert not case.calls


def test_public_dto_cannot_start_constructor_handoff():
    with pytest.raises(WorkflowContractError):
        with custody.constructing(SimpleNamespace()):
            pytest.fail("DTO cannot construct owner facts")


def test_caught_application_body_failure_cannot_reenter(continuation, monkeypatch):
    case = continuation
    monkeypatch.setattr(native, "retained", lambda session: [])
    monkeypatch.setattr(native, "_attempt_rows", lambda records: {"attempt": {"eligible": True}})
    monkeypatch.setattr(native, "_ordinary_contextual_review", lambda *args: True)
    token = custody._ACTIVE.set(None)
    try:
        with pytest.raises(RuntimeError, match="body failed"):
            with custody.application_scope(case.session, SimpleNamespace(attempt_id="attempt"), object()):
                raise RuntimeError("body failed")
        assert not case.value.valid
        with pytest.raises(WorkflowContractError):
            with custody.application_scope(case.session, SimpleNamespace(attempt_id="attempt"), object()):
                pytest.fail("Failed application body must expire its continuation")
    finally:
        custody._ACTIVE.reset(token)


@pytest.mark.parametrize("eligible", [False, None])
def test_noneligible_application_body_does_not_enable_continuation(continuation, monkeypatch, eligible):
    case = continuation
    monkeypatch.setattr(native, "retained", lambda session: [])
    monkeypatch.setattr(native, "_attempt_rows", lambda records: {} if eligible is None else {"attempt": {}})
    monkeypatch.setattr(native, "_ordinary_contextual_review", lambda *args: eligible)
    token = custody._ACTIVE.set(None)
    try:
        with custody.application_scope(case.session, SimpleNamespace(attempt_id="attempt"), object()):
            assert consume(case) is None
    finally:
        custody._ACTIVE.reset(token)
    assert not case.calls


@pytest.mark.parametrize("exit_error", [False, True])
def test_only_successful_genuine_constructor_can_publish_after_scope_exit(tmp_path, monkeypatch, exit_error):
    from evidence_wiki import _strict_observation_scope as scopes
    session = SimpleNamespace(root=tmp_path, _check=lambda *args, **kwargs: None)
    holder = SimpleNamespace(_root=tmp_path, _session=session)
    config, policy = {}, {"schema_version": "policy"}
    view = SimpleNamespace(state=SimpleNamespace(checkpoint="checkpoint"), trust={})
    capture = SimpleNamespace(files={"record": b"original"})
    events = []
    pending = []
    @contextmanager
    def reads(*args):
        yield
    @contextmanager
    def fresh_view(*args):
        yield view
        events.append("view_exit")
        if exit_error:
            raise RuntimeError("view exit failed")
    class Scope:
        def __init__(self, *args):
            self.holder, self.root, self.view = holder, tmp_path, view
            self.config, self.policy = config, policy
            self.valid, self.token = True, None
            self.result = {"basis_id": "basis"}
            self.observed = capture, object(), [], []
        def check(self, *args):
            assert self.valid
        def close(self):
            events.append("close")
    strict = SimpleNamespace(POLICY_SCHEMA_V2="policy", configuration=lambda root: config,
        resolve_policy=lambda *args: policy)
    owners = {"_strict_evidence": strict,
        "_evidence_usage": SimpleNamespace(configured=lambda config: True,
            _observation_reads=reads, current_view=fresh_view),
        "_caller_context": SimpleNamespace(builtin_integrations=lambda config: True),
        "_publication_context": SimpleNamespace(live_read=SimpleNamespace(get=lambda: object()))}
    monkeypatch.setattr(scopes, "owner", owners.__getitem__)
    monkeypatch.setattr(scopes, "_Observation", Scope)
    def observe(actual):
        assert actual is session
        with scopes.observe(holder):
            facts = custody._PENDING.get()
            assert type(facts) is native._CurrentFacts
            pending.append(facts)
            assert facts._review_observation is None
            assert native._CURRENT_FACTS.get() is None
        events.append("observed")
        return {}, SimpleNamespace(), [], []
    monkeypatch.setattr(native, "_observe_current", observe)
    if exit_error:
        with pytest.raises(RuntimeError, match="view exit failed"):
            native._CurrentFacts(session, "advance")
        assert native._CURRENT_FACTS.get() is None
        assert custody._PENDING.get() is None
        # An offered value cannot be consumed without successful installation.
        value = pending[0]._review_observation
        assert value is not None and not value.valid
        with pytest.raises(WorkflowContractError):
            value.check()
        assert events == ["close", "view_exit"]
    else:
        facts = native._CurrentFacts(session, "advance")
        assert facts is pending[0]
        assert facts._review_observation is not None
        assert native._CURRENT_FACTS.get() is None
        assert events == ["close", "view_exit", "observed"]
    assert custody._PENDING.get() is None


def test_offer_rejects_direct_non_native_caller():
    with pytest.raises(WorkflowContractError):
        custody.offer(SimpleNamespace())


def test_corrupt_held_packets_cannot_supply_source_ids(continuation):
    case = continuation
    case.facts.packets[0]["source_id"] = "injected"
    with pytest.raises(WorkflowContractError):
        custody.source_ids(case.session)
    assert not case.value.valid and not case.calls


def test_unknown_material_claim_retains_fresh_path_refusal(tmp_path, monkeypatch):
    strict = owner("_strict_evidence")
    policy = {"schema_version": strict.POLICY_SCHEMA_V2}
    monkeypatch.setattr(strict, "configuration", lambda root: {})
    monkeypatch.setattr(strict, "resolve_policy", lambda *args: policy)
    usage = strict.sibling("_evidence_usage")
    @contextmanager
    def view(*args):
        yield object()
    monkeypatch.setattr(usage, "current_view", view)
    selections = []
    def selection(*args, **kwargs):
        selections.append(True)
        raise strict.refusal("strict_claim_unknown")
    monkeypatch.setattr(strict, "selection_inputs", selection)
    kwargs = dict(expected_workspace_basis_id="basis", expected_claim_basis_id="claim-basis",
        expected_producer_id="producer", expected_native_snapshot_sha256="sha256:" + "0" * 64)
    reasons = []
    for borrowed in [None, ({"basis_id": "basis"}, (object(), object(), [], []))]:
        monkeypatch.setattr(strict, "_application_observation", lambda *args, value=borrowed: value)
        with pytest.raises(Exception) as error:
            strict.prepare_dependency_review_material(tmp_path, "unknown", **kwargs)
        reasons.append((type(error.value), getattr(error.value, "details", None), str(error.value)))
    assert reasons[0] == reasons[1]
    assert len(selections) == 2


def test_application_continuation_covers_validation_and_signing_then_expires(application, monkeypatch):
    session, state, _, attempt, _, _ = application
    holder = inputs._Inputs.__new__(inputs._Inputs)
    holder._session, holder._root = session, session.root
    holder.assert_current = lambda: None
    monkeypatch.setattr(inputs, "current_inputs", lambda: holder)
    original_observe = native._observe_current
    values, phases = [], []

    def observe(actual):
        result = original_observe(actual)
        facts = custody._PENDING.get()
        scope = SimpleNamespace(holder=holder, config={}, policy={},
            view=SimpleNamespace(state=SimpleNamespace(), trust={}), result={"basis_id": "basis"})
        value = custody._Continuation(facts, scope, SimpleNamespace(files={}), object(), [], [])
        facts._review_observation = value
        values.append(value)
        return result

    original_validate = native._validate_application_owned
    original_review = native._review_application

    def validate(*args, **kwargs):
        if native._CURRENT_FACTS.get() is not None:
            assert custody._ACTIVE.get() is values[0]
            phases.append("validation")
        else:
            assert custody._ACTIVE.get() is None
        return original_validate(*args, **kwargs)

    def review(*args, **kwargs):
        assert custody._ACTIVE.get() is values[0]
        phases.append("signing")
        return original_review(*args, **kwargs)

    original_owner = native.owner
    strict = original_owner("strict_evidence")
    original_closing = strict.run_operation

    def closing(*args, **kwargs):
        assert custody._ACTIVE.get() is None and not values[0].valid
        phases.append("closing")
        return original_closing(*args, **kwargs)

    strict.run_operation = closing
    original_append = native.append

    def append(*args, **kwargs):
        assert custody._ACTIVE.get() is None and not values[0].valid
        phases.append("effect")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(native, "_observe_current", observe)
    monkeypatch.setattr(native, "_validate_application_owned", validate)
    monkeypatch.setattr(native, "_review_application", review)
    monkeypatch.setattr(native, "append", append)
    native._apply_owned(session, attempt.attempt_id)
    assert phases[:4] == ["validation", "signing", "closing", "effect"]
    assert state.effects == ["strict.register_review"]
    assert custody._ACTIVE.get() is None and native._CURRENT_FACTS.get() is None


@pytest.mark.parametrize("copied", [False, True])
def test_native_epoch_binds_context_before_first_application_activation(application, monkeypatch, copied):
    session, _, _, attempt, _, _ = application
    holder = inputs._Inputs.__new__(inputs._Inputs)
    holder._session, holder._root = session, session.root
    holder.assert_current = lambda: None
    monkeypatch.setattr(inputs, "current_inputs", lambda: holder)
    original_observe = native._observe_current

    def observe(actual):
        result = original_observe(actual)
        facts = custody._PENDING.get()
        scope = SimpleNamespace(holder=holder, config={}, policy={},
            view=SimpleNamespace(state=SimpleNamespace(), trust={}), result={"basis_id": "basis"})
        facts._review_observation = custody._Continuation(
            facts, scope, SimpleNamespace(files={}), object(), [], [])
        return result

    monkeypatch.setattr(native, "_observe_current", observe)
    with native._current_facts(session, operation="advance") as facts:
        assert custody._ACTIVE.get() is None
        epoch = custody._OWNER.get()
        original_token = epoch.token
        foreign = copy_context()

        def activate():
            with custody.application_scope(session, attempt, None):
                assert custody.source_ids(session) == {row["source_id"] for row in facts.packets}

        if copied:
            with pytest.raises(ValueError, match="different Context"):
                foreign.run(activate)
            assert epoch.token is original_token
            assert custody._OWNER.get() is epoch
            assert custody._ACTIVE.get() is None
            assert not epoch.valid and not facts._review_observation.valid
            with pytest.raises(WorkflowContractError):
                activate()
        else:
            activate()
            assert epoch.token is not original_token
            assert epoch.valid and facts._review_observation.valid
    assert custody._OWNER.get() is None and custody._ACTIVE.get() is None
    assert native._CURRENT_FACTS.get() is None


def test_owner_epoch_cannot_be_bound_by_non_native_caller():
    with pytest.raises(WorkflowContractError):
        custody.owner_epoch(SimpleNamespace())


@pytest.mark.parametrize("export", ["prepare_review", "prepare_dependency_review_material"])
@pytest.mark.parametrize("changed", [None, "state", "trust"])
def test_public_export_actual_native_namespace_consumes_freshly(continuation, export, changed):
    case = continuation
    public = owner("strict_evidence")
    implementation = getattr(public, export).__wrapped__
    private = getattr(case.strict, export).__wrapped__
    assert implementation.__code__ is private.__code__
    assert implementation.__globals__ is not private.__globals__
    usage = implementation.__globals__["sibling"]("_evidence_usage")
    assert usage.UsageView is not owner("_evidence_usage").UsageView
    assert usage.UsageView is not case.strict.sibling("_evidence_usage").UsageView
    helper = implementation.__globals__["_application_observation"]
    assert helper.__code__ is case.strict._application_observation.__code__
    def fresh():
        return usage.UsageView(usage.UsageState(case.session.root, "state", None),
            {"key": "trusted"}, datetime.now(timezone.utc))
    views = [fresh(), fresh()]
    case.value.state = deepcopy(vars(views[0].state))
    case.value.trust = deepcopy(views[0].trust)
    if changed is not None:
        if changed == "state":
            views[0].state.document["events"].append({"changed": True})
        else:
            views[0].trust["key"] = "revoked"
        with pytest.raises(WorkflowContractError):
            helper(case.session.root, case.config, case.policy, views[0])
        assert not case.value.valid and not case.calls
        with pytest.raises(WorkflowContractError):
            helper(case.session.root, case.config, case.policy, views[1])
        return
    for view in views:
        helper(case.session.root, case.config, case.policy, view)
    assert [call[0] for call in case.calls] == ["qualify", "closing", "qualify", "closing"]
    assert case.calls[1][1][3] is views[0]
    assert case.calls[3][1][3] is views[1]


@pytest.mark.parametrize("export", ["prepare_review", "prepare_dependency_review_material"])
def test_public_export_usage_subclass_is_refused(continuation, export):
    case = continuation
    implementation = getattr(owner("strict_evidence"), export).__wrapped__
    usage = implementation.__globals__["sibling"]("_evidence_usage")
    class Derived(usage.UsageView):
        pass
    view = Derived(usage.UsageState(case.session.root, "state", None),
        {"key": "trusted"}, datetime.now(timezone.utc))
    case.value.state = deepcopy(vars(view.state))
    case.value.trust = deepcopy(view.trust)
    with pytest.raises(WorkflowContractError):
        implementation.__globals__["_application_observation"](
            case.session.root, case.config, case.policy, view)
    assert not case.value.valid and not case.calls
