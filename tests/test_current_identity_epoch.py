"""Current native identity reuse has real owner custody and fresh eligibility."""

import contextvars
import copy
import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_session as owner
from evidence_wiki.errors import UsageError
from tests.test_calibration_registry import calibrated as _calibrated_fixture
from tests.test_current_owner_epoch import epoch as _epoch_fixture


@pytest.fixture
def epoch(monkeypatch):
    session, state = _epoch_fixture.__wrapped__(monkeypatch)
    original = owner._observe_current

    def observe(current):
        view, observed, packets, records = original(current)
        view.update(basis_id="original-basis", policy_id="original-policy", policy_compatible=True,
            canonical_release={"publication": {"evaluated_at": "original"}}, usage={"seconds": 0})
        return view, observed, packets, records

    monkeypatch.setattr(owner, "_observe_current", observe)
    return session, state


@pytest.fixture(scope="module", params=("artifact_checked", "host_enforced"))
def calibrated(request):
    return _calibrated_fixture.__wrapped__(request)


def test_held_identity_is_small_detached_and_eligibility_is_recomputed(epoch, monkeypatch):
    session, state = epoch
    rows = []
    original = owner._qualifications

    def qualified(*args, **kwargs):
        rows.append(original(*args, **kwargs))
        return rows[-1]

    monkeypatch.setattr(owner, "_qualifications", qualified)
    with owner._current_facts(session):
        first = owner._current_identity(session)
        assert set(first) == {"basis_id", "policy_id", "policy_compatible", "qualification"}
        first["qualification"]["clock"] = "caller-mutation"
        assert rows[-1] == {"clock": 0}
        state["clock"] = 1
        second = owner._current_identity(session)
        assert second["qualification"] == {"clock": 1}
        assert first is not second
    assert state["reads"] == 1 and len(rows) == 2


def test_outside_epoch_preserves_original_full_session_current(monkeypatch):
    full = {"accounting": {"original_outcomes": []}, "usage": {"seconds": 0}}
    calls = []
    session = SimpleNamespace(current=lambda: calls.append("full") or full)
    assert owner._current_identity(session) is full
    assert calls == ["full"]


def test_supplied_mapping_cannot_substitute_for_actual_owner_epoch(epoch):
    session, _state = epoch
    token = owner._CURRENT_FACTS.set({"basis_id": "forged", "qualification": {"state": "qualified"}})
    try:
        with pytest.raises(UsageError) as caught:
            owner._current_identity(session)
        assert caught.value.error_code == "RESEARCH_CURRENT_OWNER_EPOCH_INVALID"
    finally:
        owner._CURRENT_FACTS.reset(token)


@pytest.mark.parametrize("boundary", ["child", "thread", "nested", "closed"])
def test_owner_identity_cannot_cross_session_thread_nested_or_closed_boundary(epoch, monkeypatch, boundary):
    session, _state = epoch
    if boundary == "closed":
        with owner._current_facts(session) as facts:
            assert owner._current_identity(session)["basis_id"] == "original-basis"
        token = owner._CURRENT_FACTS.set(facts)
        try:
            with pytest.raises(UsageError):
                owner._current_identity(session)
        finally:
            owner._CURRENT_FACTS.reset(token)
        return
    with pytest.raises(UsageError):
        with owner._current_facts(session):
            if boundary == "child":
                owner._current_identity(copy.copy(session))
            elif boundary == "thread":
                monkeypatch.setattr(owner.threading, "get_ident", lambda: -1)
                owner._current_identity(session)
            else:
                with owner._current_facts(session):
                    pytest.fail("Nested native epochs were admitted")


def test_copied_context_cannot_borrow_native_epoch_from_foreign_thread(epoch):
    from threading import Thread

    session, _state = epoch
    failures = []
    with owner._current_facts(session):
        context = contextvars.copy_context()
        def borrowed():
            try:
                owner._current_identity(session)
            except UsageError as error:
                failures.append(error.error_code)
        thread = Thread(target=lambda: context.run(borrowed))
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert failures == ["RESEARCH_CURRENT_OWNER_EPOCH_CHANGED"]


@pytest.mark.parametrize("change", ["controls", "journal", "source", "callback", "authority"])
def test_held_identity_preserves_live_source_journal_authority_and_callback_guards(epoch, monkeypatch, change):
    session, state = epoch
    with pytest.raises(UsageError):
        with owner._current_facts(session):
            assert owner._current_identity(session)["qualification"] == {"clock": 0}
            if change == "controls":
                state["controls"] = {"context_id": "changed"}
            elif change == "journal":
                state["records"].append({"kind": "changed"})
            elif change == "source":
                state["changed"] = True
            elif change == "callback":
                from evidence_wiki.research_execution import bounded_host_call
                assert bounded_host_call(lambda: None, (), 1) == (None, None)
            else:
                def denied(*args, **kwargs):
                    raise UsageError("AUTHORITY_CHANGED", "Current native authority was withdrawn.")
                monkeypatch.setattr(session, "_check", denied)
            owner._current_identity(session)


def test_genuine_signed_calibration_probe_expiry_is_live_inside_same_epoch(calibrated, monkeypatch):
    policy, profile, registry, context, _domain, probe = calibrated
    session = SimpleNamespace(root="finite-owned-root", policy=policy,
        execution=SimpleNamespace(profile=profile, qualifications=registry), _check=lambda *_: None)
    view = {"basis_id": "owned-basis", "policy_id": policy.policy_id, "policy_compatible": True,
        "accounting": {"original_outcomes": copy.deepcopy(context["originals"])}}
    observed = SimpleNamespace(policy={"rubric": context["rubric"]}, config={},
        controls={"result": {"context_id": "owned"}},
        strict={"result": {"claims": [{"claim": row} for row in context["strict_claims"]]}}, finish=lambda: None)
    records = [{"kind": "execution_qualification_probe", "data": {"probe": copy.deepcopy(probe)}}]
    monkeypatch.setattr(owner, "retained", lambda *_: copy.deepcopy(records))
    monkeypatch.setattr(owner, "_observe_current", lambda *_: (view, observed, copy.deepcopy(context["sources"]), copy.deepcopy(records)))
    monkeypatch.setattr(owner, "owner", lambda name: SimpleNamespace(capture=lambda *_: {"context_id": "owned"})
        if name == "_caller_context" else SimpleNamespace(configured=lambda *_: False))
    monkeypatch.setattr(owner, "now_utc", lambda: "2026-10-01T12:00:00Z")
    with owner._current_facts(session):
        assert owner._current_identity(session)["qualification"]["state"] == "qualified"
        after_expiry = (datetime.fromisoformat(probe["expires_utc"].replace("Z", "+00:00")) + timedelta(seconds=1)).isoformat()
        monkeypatch.setattr(owner, "now_utc", lambda: after_expiry)
        closing = owner._current_identity(session)
        assert closing["qualification"]["state"] == "unknown"
        assert closing["qualification"]["profiles"][0]["qualified"] is None


@pytest.mark.parametrize("change", ["qualification_expiry", "authority"])
def test_native_credential_closing_cannot_reuse_opening_readiness(tmp_path, monkeypatch, change):
    from evidence_wiki import Workspace
    from evidence_wiki import service_authentication as authentication
    from evidence_wiki.errors import EvidenceWikiError
    from tests import test_execution_session as reference

    root, policy, execution, context = reference.fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"role_isolation": True, "simulated_model_usage": True, "review_services": True}))
    context = replace(context, grants=context.grants | {"research_session.resolution", "resolution_operations.current"})
    original = authentication._credential
    changed = []
    def challenged(*args, **kwargs):
        result = original(*args, **kwargs)
        if not changed:
            changed.append(True)
            if change == "qualification_expiry":
                signed = json.loads(execution.qualifications._records)
                end = max(datetime.fromisoformat(row["expires_utc"].replace("Z", "+00:00")) for row in signed)
                monkeypatch.setattr(owner, "now_utc", lambda: (end + timedelta(seconds=1)).isoformat())
            else:
                authority = json.loads(context.authority_file.read_bytes())
                authority["revoked_keys"] = [execution.review_signer.reviewers[0].key_id]
                context.authority_file.write_text(json.dumps(authority, sort_keys=True, separators=(",", ":")))
        return result
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="closing-native-identity", execution=execution) as session:
            closing_guard = pytest.raises(EvidenceWikiError) if change == "authority" else nullcontext()
            with closing_guard as outer:
                with owner._current_facts(session):
                    assert authentication.status(session)["state"] == "ready"
                    monkeypatch.setattr(authentication, "_credential", challenged)
                    with pytest.raises(EvidenceWikiError) as caught:
                        authentication.status(session)
                    assert caught.value.details["reason"] == "service_current_identity_changed"
            if change == "authority":
                assert outer.value.error_code == "RESEARCH_BINDING_CHANGED"
    assert changed == [True]


@pytest.mark.parametrize("change", ["content", "expiry"])
def test_status_rechecks_current_trust_after_closing_identity(epoch, monkeypatch, change):
    from evidence_wiki import service_authentication as authentication
    from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope

    session, state = epoch
    state["authority_clock"] = 0
    state["authority_content"] = "original"
    trust_reads = []
    origin = datetime.fromisoformat("2026-10-01T12:00:00+00:00")
    class Clock:
        @staticmethod
        def now(_zone):
            return origin + timedelta(seconds=state["authority_clock"])
    def trust(_root, _config, now):
        trust_reads.append(now)
        if change == "expiry" and now >= origin + timedelta(seconds=2):
            raise UsageError("AUTHORITY_EXPIRED", "Controlled authority expired during identity capture.")
        return {"content_hash": state["authority_content"]}
    authority = SimpleNamespace(load_trust=trust, authority_basis=lambda *_: "sha256:" + "a" * 64)
    monkeypatch.setattr(authentication, "owner", lambda name: authority if name == "_evidence_authority"
        else SimpleNamespace(configuration=lambda *_: {}))
    monkeypatch.setattr(authentication, "datetime", Clock)
    monkeypatch.setattr(authentication, "ResearchSession", type(session))
    session.context.require_grant = lambda *_: None
    session.policy = SimpleNamespace(to_dict=lambda: {"required_evaluators": [{"profile_ref": "evidence"}]})
    scopes = ExecutionRoleRegistry((ExecutionRoleScope("generator", "researcher", ("model",)),
        ExecutionRoleScope("evidence", "evaluator", ("model",))))
    identities = [SimpleNamespace(config_ref=config, role=role, principal=config, controller_ref=config + "-controller")
        for config, role in (("generator", "generator"), ("evidence", "evaluator"))]
    session.execution = SimpleNamespace(check=lambda *_: None, records=lambda *_: [],
        profile=SimpleNamespace(review_services=None, role_scopes=scopes, sha256="a" * 64),
        review_signer=SimpleNamespace(generator=identities[0], reviewers=identities[1:]))
    monkeypatch.setattr(authentication, "_credential", lambda *_: "sha256:" + "b" * 64)
    monkeypatch.setattr(owner, "_qualifications", lambda *_a, **_k: {"profiles": [{"profile_ref": "evidence",
        "role": "evaluator", "controller_ref": "evidence-controller", "qualified": True, "independent": True}]})
    identity = owner._current_identity
    reads = []
    def closing(*args):
        result = identity(*args)
        reads.append(result)
        if len(reads) == 2:
            state["authority_clock"] = 3
            if change == "content":
                state["authority_content"] = "withdrawn"
        return result
    monkeypatch.setattr(owner, "_current_identity", closing)
    with owner._current_facts(session):
        with pytest.raises(UsageError) as caught:
            authentication.status(session)
        assert caught.value.error_code == ("REVIEW_SERVICE_REFUSED" if change == "content" else "AUTHORITY_EXPIRED")
    assert len(reads) == 2
    assert trust_reads == [origin, origin, origin + timedelta(seconds=3)]
