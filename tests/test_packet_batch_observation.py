"""A held packet batch lends one evaluation only while every fresh re-check agrees."""

from contextlib import contextmanager
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from evidence_wiki import _current_fact_observation as custody
from evidence_wiki import _review_dependency_inputs as inputs
from evidence_wiki import host_context
from evidence_wiki.pack_discovery import owner
from evidence_wiki.workflow_contracts import WorkflowContractError


class View:
    def __init__(self):
        self.state = SimpleNamespace(document={"events": []}, checkpoint="checkpoint",
            state_id="state", binding="binding")
        self.trust = {"key": "trusted"}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Replace expensive owners, while retaining the actual capability classes."""
    root = tmp_path.resolve()
    config, policy = {"native": True}, {"schema_version": "evidence-strict-policy/v2"}
    holder = inputs._Inputs.__new__(inputs._Inputs)
    session = SimpleNamespace(root=root)
    holder._session, holder._root = session, root
    holder._scope = lambda *args: None
    holder.assert_current = lambda: None
    monkeypatch.setattr(inputs, "current_inputs", lambda: holder)
    monkeypatch.setattr(host_context, "review_dependency_inputs", lambda: holder)
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
    capture = SimpleNamespace(files={"record": b"original"}, revision_id="revision")
    current = {"material": {"claim_id": "claim"}, "snapshot": {"claim": {"id": "claim"}}}
    result = {"basis_id": "basis", "claims": [{"claim": {"id": "claim"}}]}
    producer = object()
    observed = (capture, producer, [current], [("envelope", "payload", current)])
    case = SimpleNamespace(root=root, config=config, policy=policy, holder=holder, session=session,
        calls=calls, strict=strict, capture=capture, producer=producer, result=result, observed=observed)

    @contextmanager
    def active():
        batch = custody._PacketBatch(holder, root, config, policy, View(), result, observed, custody._BATCH_MINT)
        batch.token = custody._ACTIVE.set(batch)
        try:
            yield batch
        finally:
            custody._ACTIVE.reset(batch.token)

    case.active = active
    return case


def consume(case, view=None, root=None, config=None, policy=None):
    return case.strict._application_observation(case.root if root is None else root,
        case.config if config is None else config, case.policy if policy is None else policy,
        View() if view is None else view)


def test_each_borrow_requalifies_and_closes_against_its_own_view(harness):
    case = harness
    with case.active() as batch:
        first = consume(case)
        first[0]["claims"].append("mutated by a caller")
        first[1][2][0]["material"]["claim_id"] = "mutated"
        second = consume(case)
    assert second[0] == case.result
    assert second[1][0] is case.capture and second[1][1] is case.producer
    assert second[1][2][0]["material"]["claim_id"] == "claim"
    assert [call[0] for call in case.calls] == ["qualify", "closing", "qualify", "closing"]
    assert case.calls[1][1][5] == "basis" and case.calls[1][1][4] is case.capture
    assert batch.valid


@pytest.mark.parametrize("field", ["document", "checkpoint", "state_id", "trust", "config", "policy",
                                   "root", "holder", "source", "view_type"])
def test_any_changed_input_returns_the_fresh_path_and_ends_the_batch(harness, field, tmp_path):
    case = harness
    view, kwargs = View(), {}
    with case.active() as batch:
        if field == "trust":
            view.trust["key"] = "revoked"
        elif field == "document":
            view.state.document["events"].append("new")
        elif field in {"checkpoint", "state_id"}:
            setattr(view.state, field, "changed")
        elif field == "config":
            kwargs["config"] = {"native": False}
        elif field == "policy":
            kwargs["policy"] = {"schema_version": "changed"}
        elif field == "root":
            other = tmp_path / "other"
            other.mkdir()
            kwargs["root"] = other
        elif field == "holder":
            batch.holder = object()
        elif field == "source":
            case.capture.files["record"] = b"changed"
        else:
            view = SimpleNamespace(state=view.state, trust=view.trust)
        if field == "holder":
            with pytest.raises(WorkflowContractError):
                consume(case, view, **kwargs)
        else:
            assert consume(case, view, **kwargs) is None
        assert not batch.valid
        if field != "holder":
            assert consume(case) is None
    assert case.calls == []


def test_incomplete_requalification_returns_the_fresh_path_before_closing(harness, monkeypatch):
    case = harness
    original = custody.owner
    publication = SimpleNamespace(qualify_capture=lambda *args, **kwargs: {"complete": False})
    monkeypatch.setattr(custody, "owner", lambda name: publication if name == "_publication_usage" else original(name))
    with case.active() as batch:
        assert consume(case) is None
        assert not batch.valid
    assert case.calls == []


def test_closing_drift_returns_the_fresh_path_and_ends_the_batch(harness, monkeypatch):
    case = harness
    def drifted(*args):
        raise owner("_strict_evidence").ScriptRefusal("selective_dependency_basis_changed")
    monkeypatch.setattr(case.strict, "_dependency_closing", drifted)
    with case.active() as batch:
        assert consume(case) is None
        assert not batch.valid
        assert consume(case) is None


def test_host_deadline_during_closing_is_never_consumed(harness, monkeypatch):
    case = harness
    class Deadline(BaseException):
        pass
    def expired(*args):
        raise Deadline()
    monkeypatch.setattr(case.strict, "_dependency_closing", expired)
    with case.active():
        with pytest.raises(Deadline):
            consume(case)


def test_copied_context_cannot_borrow_and_ends_the_batch(harness):
    case = harness
    with case.active() as batch:
        with pytest.raises(ValueError, match="different Context"):
            copy_context().run(consume, case)
        assert not batch.valid
    assert case.calls == []


@pytest.mark.parametrize("field", ["thread", "task", "inputs"])
def test_custody_mismatch_refuses(harness, monkeypatch, field):
    case = harness
    with case.active() as batch:
        if field == "thread":
            batch.thread = -1
        elif field == "task":
            batch.task = object()
        else:
            monkeypatch.setattr(inputs, "current_inputs", lambda: object())
        with pytest.raises(WorkflowContractError):
            consume(case)
        assert not batch.valid
    assert case.calls == []


def test_batch_needs_its_private_mint(harness):
    case = harness
    with pytest.raises(WorkflowContractError):
        custody._PacketBatch(case.holder, case.root, case.config, case.policy, View(), case.result,
            case.observed, object())


def test_batch_belongs_to_its_holder_root(harness, tmp_path):
    case = harness
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(WorkflowContractError):
        custody._PacketBatch(case.holder, other, case.config, case.policy, View(), case.result,
            case.observed, custody._BATCH_MINT)


def test_application_source_ids_ignore_a_batch(harness):
    case = harness
    with case.active():
        assert custody.source_ids(case.session) is None


@pytest.fixture
def opening(harness, monkeypatch):
    """Native owners used to open a batch, recording every fresh evaluation."""
    case = harness
    evaluations = []
    @contextmanager
    def current_view(root, config):
        yield View()
    def evaluate(root, config, policy, *, view):
        evaluations.append((root, config, policy))
        return case.result, case.observed
    monkeypatch.setattr(case.strict, "configuration", lambda root: dict(case.config))
    monkeypatch.setattr(case.strict, "resolve_policy", lambda root, config: dict(case.policy))
    monkeypatch.setattr(case.strict, "_evaluate_dependency_observations", evaluate)
    real_sibling = case.strict.sibling
    usage = SimpleNamespace(current_view=current_view, UsageView=View)
    monkeypatch.setattr(case.strict, "sibling", lambda name: usage if name == "_evidence_usage" else real_sibling(name))
    case.evaluations = evaluations
    return case


def test_opened_batch_evaluates_once_lends_to_each_packet_and_then_expires(opening):
    case = opening
    with custody.packet_batch(case.root) as batch:
        assert batch is not None and custody._ACTIVE.get() is batch
        for _packet in range(3):
            assert consume(case)[0] == case.result
    assert len(case.evaluations) == 1
    assert custody._ACTIVE.get() is None and not batch.valid
    assert consume(case) is None


def test_without_a_bound_holder_nothing_is_lent(opening, monkeypatch):
    case = opening
    monkeypatch.setattr(inputs, "current_inputs", lambda: None)
    with custody.packet_batch(case.root) as batch:
        assert batch is None and custody._ACTIVE.get() is None
    assert case.evaluations == []


@pytest.mark.parametrize("reason", ["legacy_policy", "no_policy", "failed_evaluation", "other_root"])
def test_unshareable_workspaces_keep_every_fresh_path(opening, monkeypatch, tmp_path, reason):
    case = opening
    root = case.root
    if reason == "legacy_policy":
        monkeypatch.setattr(case.strict, "resolve_policy", lambda root, config: {"schema_version": "evidence-strict-policy/v1"})
    elif reason == "no_policy":
        monkeypatch.setattr(case.strict, "resolve_policy", lambda root, config: None)
    elif reason == "failed_evaluation":
        def refused(*args, **kwargs):
            raise owner("_strict_evidence").ScriptRefusal("strict_inputs_changed")
        monkeypatch.setattr(case.strict, "_evaluate_dependency_observations", refused)
    else:
        root = tmp_path / "other"
        root.mkdir()
    with custody.packet_batch(root) as batch:
        assert batch is None and custody._ACTIVE.get() is None
        assert consume(case) is None


def test_a_batch_cannot_open_inside_an_application_continuation(opening):
    case = opening
    token = custody._ACTIVE.set(object())
    try:
        with pytest.raises(WorkflowContractError):
            with custody.packet_batch(case.root):
                pass
    finally:
        custody._ACTIVE.reset(token)
    assert case.evaluations == []
