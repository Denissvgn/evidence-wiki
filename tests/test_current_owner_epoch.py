"""Private proof-pass facts invalidate at current changes, effects and waits."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_session as native
from evidence_wiki import research_execution
from evidence_wiki.errors import UsageError
from evidence_wiki.run_policy import RunPolicy
from tests.test_run_policy import evaluator, observation, policy_document


@pytest.fixture
def epoch(monkeypatch):
    state = {"controls": {"context_id": "same"}, "records": [], "changed": False, "clock": 0, "reads": 0}
    session = SimpleNamespace(root="native-root", run_id="run", context=SimpleNamespace(actor_ref="native"),
        binding={}, _check=lambda *_a, **_k: None)
    def finish():
        if state["changed"]:
            raise UsageError("NATIVE_CHANGED", "Controlled current material changed.")
    observed = SimpleNamespace(config={}, controls={"result": {"context_id": "same"}}, finish=finish)
    view = {"accounting": {"original_outcomes": []}}
    def observe(_session):
        state["reads"] += 1
        return view, observed, [], list(state["records"])
    monkeypatch.setattr(native, "_observe_current", observe)
    monkeypatch.setattr(native, "retained", lambda *_: list(state["records"]))
    monkeypatch.setattr(native, "owner", lambda name: SimpleNamespace(capture=lambda *_: state["controls"])
        if name == "_caller_context" else SimpleNamespace(configured=lambda *_: False))
    monkeypatch.setattr(native, "_qualifications", lambda *_a, **_k: {"clock": state["clock"]})
    return session, state


def test_same_epoch_reuses_actual_material_but_refreshes_qualification_clock(epoch):
    session, state = epoch
    with native._current_facts(session):
        assert native.current_qualifications(session) == {"clock": 0}
        state["clock"] = 1
        assert native.current_qualifications(session) == {"clock": 1}
    assert state["reads"] == 1
    monkeypatch_full = {"qualification": {"full": True}}
    # Outside the native context the full current owner remains the fallback.
    from unittest.mock import patch
    with patch.object(native, "current", return_value=monkeypatch_full) as full:
        assert native.current_qualifications(session) == {"full": True}
        full.assert_called_once_with(session)


@pytest.mark.parametrize("change", ["controls", "records", "material"])
def test_current_change_invalidates_the_native_epoch(epoch, change):
    session, state = epoch
    with pytest.raises(UsageError):
        with native._current_facts(session):
            if change == "material":
                state["changed"] = True
            elif change == "controls":
                state["controls"] = {"context_id": "changed"}
            else:
                state["records"].append({"changed": True})
            native.current_qualifications(session)


@pytest.mark.parametrize("boundary", ["append", "callback"])
def test_no_epoch_reuse_after_journal_effect_or_external_wait(epoch, monkeypatch, boundary):
    session, _state = epoch
    with pytest.raises(UsageError) as caught:
        with native._current_facts(session):
            if boundary == "callback":
                assert research_execution.bounded_host_call(lambda: "result", (), 1) == ("result", None)
            else:
                monkeypatch.setattr(session, "execution", SimpleNamespace(seal_record=lambda *_a, **_k: {}), raising=False)
                monkeypatch.setattr("evidence_wiki.research_session._owner", lambda *_a, **_k: {"event_id": "observed"})
                native.append(session, "kind", "request", {})
            native.current_qualifications(session)
    assert caught.value.error_code == "RESEARCH_CURRENT_OWNER_EPOCH_CHANGED"


def test_detached_or_foreign_thread_context_cannot_be_used_as_owner_proof(epoch, monkeypatch):
    session, _state = epoch
    detached = native._CurrentFacts(session)
    with pytest.raises(UsageError):
        detached.require(session)
    with pytest.raises(UsageError):
        with native._current_facts(session):
            monkeypatch.setattr(native.threading, "get_ident", lambda: -1)
            native.current_qualifications(session)


@pytest.mark.parametrize("failure", [None, "denied", "incomplete", "wrong_purpose", "withdrawn", "unknown_revision", "raw_mismatch", "missing_packet", "malformed_reason"])
@pytest.mark.parametrize("consumer", ["qualification", "policy"])
def test_configured_native_capture_qualifier_resolves_usage_identity_and_current_rights(epoch, policy_epoch, monkeypatch, failure, consumer):
    from evidence_wiki.mutation_contracts import canonical
    from evidence_wiki.pack_discovery import owner
    publication = owner("_publication_usage")
    strict_owner = owner("strict_evidence")
    actual_uses = owner("_evidence_usage").USES
    session, _state = epoch
    actual_observe = native._observe_current
    native_revision = "sha256:" + "a" * 64
    composite_revision = "sha256:" + "b" * 64
    source_id = "lab:sample"
    raw = b"Retained observation.\n"
    normalized = b"---\nsource_id: lab:sample\n---\nRetained observation.\n"
    files = {"sources/manifest.jsonl": canonical({"id": source_id, "raw_paths": ["raw/sample.txt"],
        "usage_revision_id": native_revision}), "sources/normalized/lab--sample.md": normalized,
        "raw/sample.txt": raw}
    if failure == "raw_mismatch":
        files["raw/sample.txt"] = b"Different raw bytes.\n"
    record = {"source_id": source_id, "descriptor": {"normalized_path": "normalized.md", "evidence_root": "originals"},
        "files": {"normalized.md": normalized, "originals/raw/sample.txt": raw}}
    live = SimpleNamespace(withdrawn=False, state=SimpleNamespace(revisions={} if failure == "unknown_revision"
        else {native_revision: record}))
    packet = {"source_id": "unknown:packet" if failure == "missing_packet" else source_id,
        "source_binding": {"closure_id": composite_revision}}
    def observe(current):
        view, observed, _packets, records = actual_observe(current)
        observed.capture = SimpleNamespace(files=files)
        return view, observed, [packet], records
    monkeypatch.setattr(native, "_observe_current", observe)
    calls = []
    def check(revision, *, uses, purpose, consumer):
        assert revision == native_revision and revision != composite_revision
        assert set(uses) <= actual_uses and uses == ["retrieval", "export"]
        assert consumer == "evidence-wiki"
        calls.append(purpose)
        return {"eligible": not (failure in {"denied", "wrong_purpose"} and purpose == "qa-export")
            and not live.withdrawn, "complete": failure != "incomplete", "ancestors": [],
            "reasons": ["current_permission_denied"] if failure in {"denied", "wrong_purpose", "withdrawn"} else []}
    live.check = check
    live.revalidate = lambda *_: None
    @contextmanager
    def current_view(*_):
        yield live
    actual_owner = native.owner
    monkeypatch.setattr(native, "owner", lambda name: publication if name == "_publication_usage"
        else strict_owner if name == "strict_evidence"
        else SimpleNamespace(configured=lambda *_: True, current_view=current_view)
        if name == "_evidence_usage" else actual_owner(name))
    if failure == "malformed_reason":
        def malformed(*_a, **_k):
            raise publication.EvidenceInvalid("private source text\n" + "s" * 128)
        monkeypatch.setattr(publication, "qualify_capture", malformed)
    if failure is None:
        with native._current_facts(session):
            if consumer == "qualification":
                assert native.current_qualifications(session) == {"clock": 0}
            else:
                result = native.policy_observation(session, policy_epoch[2])
                assert result["selected_outcomes"][0]["independently_eligible"]
        assert set(calls) == {"research", "qa-export"}
    else:
        expected = UsageError if failure == "missing_packet" else type(strict_owner.refusal("source_usage_unavailable"))
        with pytest.raises(expected) as caught:
            with native._current_facts(session):
                if failure == "withdrawn":
                    if consumer == "qualification":
                        assert native.current_qualifications(session) == {"clock": 0}
                    else:
                        native.policy_observation(session, policy_epoch[2])
                    live.withdrawn = True
                if consumer == "qualification":
                    native.current_qualifications(session)
                else:
                    native.policy_observation(session, policy_epoch[2])
        if failure != "missing_packet":
            assert caught.value.error_code == "STRICT_EVIDENCE_REFUSED"
        if failure == "malformed_reason":
            assert caught.value.details == {"reason": "source_usage_unavailable"}
            assert "private source text" not in str(caught.value)


@pytest.fixture
def policy_epoch(epoch, monkeypatch):
    """Controlled packet/decision collaborators; no native quality authority."""
    session, state = epoch
    session.policy = RunPolicy(policy_document())
    session.execution = SimpleNamespace(profile=SimpleNamespace(disagreement=object(), review_services=None))
    actual = native._observe_current
    view = actual(session)[0]
    state["reads"] = 0
    basis = "sha256:" + "a" * 64
    view.update(basis_id=basis, policy_compatible=True, authority={"terminal_grants_available": True},
        qualification={"state": "qualified", "profiles": [evaluator()]}, usage=observation(session.policy)["spent"],
        execution={"source_bindings": []}, canonical_release={"publication": {"basis_id": basis,
            "questions": [{"slug": "one", "claims": [{"id": "claim", "evidence": [{"source_id": "source-a"}]}]}]}})
    view["accounting"] = {"original_ids": ["question-a", "question-b"], "original_outcomes": [
        {"original_id": "question-a", "accepted": True, "questions": [{"slug": "one"}]},
        {"original_id": "question-b", "accepted": False, "questions": []}]}
    state["records"] = [{"kind": "execution_applied", "request_id": "applied", "data": {"operation": "evaluate", "claim_id": "claim",
        "profile_ref": "profile-a", "strict_basis_id": basis, "passed": True}}]
    packets = [{"source_id": "source-a", "content": "Original retained source.", "metadata": {"kind": "fixture"}}]
    def observe(current):
        result, observed, _packets, records = actual(current)
        return result, observed, packets, records
    monkeypatch.setattr(native, "_observe_current", observe)
    state.update(source_reads=0, actions=0, finishes=0, last_packets=None, action="return_gate")
    original_finish = actual(session)[1].finish
    state["reads"] = 0
    def finish():
        state["finishes"] += 1
        original_finish()
    observed = actual(session)[1]
    observed.finish = finish
    state["reads"] = 0
    def sources(_session, observed=None):
        state["source_reads"] += 1
        from evidence_wiki.mutation_contracts import detached
        return detached(packets), []
    monkeypatch.setattr(native, "_sources", sources)
    def action(_session, actual_view, _claim, _original, supplied):
        assert actual_view is view
        state["actions"] += 1
        state["last_packets"] = supplied
        return {"operation": state["action"]}
    monkeypatch.setattr("evidence_wiki.execution_continuations.claim_action", action)
    return session, state, view, packets


def test_policy_consumer_borrows_detached_packets_only_after_full_owner_require(policy_epoch):
    session, state, view, packets = policy_epoch
    outside = native.policy_observation(session, view)
    assert state["source_reads"] == 1
    with native._current_facts(session):
        before = state["finishes"]
        inside = native.policy_observation(session, view)
        assert inside == outside
        assert state["finishes"] == before + 1 and state["source_reads"] == 1
        assert state["actions"] == 2
        state["last_packets"][0]["content"] = "Mutated detached consumer packet."
        assert packets[0]["content"] == "Original retained source."
        state["action"] = "repair"
        current = native.policy_observation(session, view)
        assert current["selected_outcomes"][0]["independently_eligible"] is False
        assert current["review_pass"] is False and state["actions"] == 3
    assert state["reads"] == 1
    native.policy_observation(session, view)
    assert state["source_reads"] == 2


def test_legacy_no_disagreement_policy_does_not_enter_packet_borrow(policy_epoch):
    session, state, view, _packets = policy_epoch
    session.execution.profile.disagreement = None
    before = native.policy_observation(session, view)
    with native._current_facts(session):
        finishes = state["finishes"]
        assert native.policy_observation(session, view) == before
        assert state["finishes"] == finishes
    assert state["actions"] == 0 and state["source_reads"] == 0


@pytest.mark.parametrize("change", ["copy_view", "foreign_session", "thread", "holder", "material", "controls", "records", "append", "wait", "config"])
def test_policy_packet_consumer_refuses_changed_or_detached_current_owner(policy_epoch, monkeypatch, change):
    session, state, view, _packets = policy_epoch
    with pytest.raises(UsageError):
        with native._current_facts(session) as facts:
            selected_session, selected_view = session, view
            if change == "copy_view":
                selected_view = dict(view)
            elif change == "foreign_session":
                selected_session = SimpleNamespace(policy=session.policy, execution=session.execution)
            elif change == "thread":
                facts.thread += 1
            elif change == "holder":
                facts.invalidated = True
            elif change == "material":
                state["changed"] = True
            elif change == "controls":
                state["controls"] = {"context_id": "changed"}
            elif change == "records":
                state["records"].append({"changed": True})
            elif change == "config":
                def changed(*_a, **_k):
                    raise UsageError("CONFIG_CHANGED", "Controlled binding changed.")
                session._check = changed
            elif change == "wait":
                assert research_execution.bounded_host_call(lambda: "complete", (), 1) == ("complete", None)
            else:
                row = {"kind": "controlled", "request_id": "retained", "data": {}, "event_ref": "observed"}
                state["records"].append(row)
                assert native.append(session, "controlled", "retained", {}) == {"event_id": "observed"}
            native.policy_observation(selected_session, selected_view)
    assert state["source_reads"] == 0 and state["actions"] == 0
