"""Native read custody shares a replay without retaining usage decisions."""

import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki.host_context import HostContext
from evidence_wiki.pack_discovery import owner
from evidence_wiki.research_observation import ResearchObservation
from tests._execution_fixture import authenticate
from tests._script_loader import load_isolated_module
from tests._usage_fixture import UsageFixture
from tests.test_mutation_operations import roots as roots
from tests.test_request_lifecycle_adversarial import protected_source
from tests.test_requirements_owner import setup as setup
from tests.test_retrieval_permissions import protected_context
from tests.test_strict_evidence import host as host

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template/scripts"


@pytest.fixture
def native(tmp_path, monkeypatch):
    fixture = UsageFixture(tmp_path, monkeypatch)
    usage = load_isolated_module("observation_usage", SCRIPTS / "_evidence_usage.py")
    fixture.transact(usage, "initialize")
    body, files = fixture.source(export=False)
    fixture.transact(usage, "deposit", body, files)
    return usage, fixture, body


def decision(usage, fixture, body, **kwargs):
    with usage.current_view(fixture.root, fixture.config) as view:
        return view.check(body["source_revision"], uses=kwargs.get("uses", ["retrieval"]),
            purpose=kwargs.get("purpose", "research"), consumer=kwargs.get("consumer", "evidence-wiki"))


def replays(call):
    count = 0
    def profile(frame, event, _result):
        nonlocal count
        if (event == "call" and frame.f_code.co_name == "__init__"
                and frame.f_code.co_filename.endswith("/_evidence_usage.py")
                and type(frame.f_locals.get("self")).__name__ == "UsageState"):
            count += 1
    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        result = call()
    finally:
        sys.setprofile(previous)
    return result, count


def test_native_replay_shared_across_isolated_imports_and_not_after_close(native):
    usage, fixture, body = native
    isolated = load_isolated_module("observation_usage_other", SCRIPTS / "_evidence_usage.py")
    def read():
        with usage._observation_reads(fixture.root, fixture.config):
            assert decision(usage, fixture, body)["eligible"]
            assert decision(isolated, fixture, body)["eligible"]
            assert not decision(isolated, fixture, body, uses=["export"])["eligible"]
            assert not decision(usage, fixture, body, purpose="unknown")["eligible"]
            assert not decision(usage, fixture, body, consumer="unknown")["eligible"]
    _, shared = replays(read)
    _, ordinary = replays(lambda: (decision(usage, fixture, body), decision(isolated, fixture, body)))
    assert shared == 1 and ordinary == 2
    fixture.revoke(usage, body)
    assert decision(usage, fixture, body)["reasons"] == ["source_revision_revoked"]


@pytest.mark.parametrize("write", ["exclusive", "transact", "lock", "raw"])
def test_all_host_writers_refuse_before_effect_even_if_caught(native, write):
    usage, fixture, body = native
    store = load_isolated_module("observation_store", SCRIPTS / "_host_evidence_store.py")
    before = (fixture.host / "evidence-state.json").read_bytes()
    with pytest.raises(ValueError, match="usage_observation_is_read_only"):
        with usage._observation_reads(fixture.root, fixture.config):
            with pytest.raises(ValueError, match="usage_observation_is_read_only"):
                if write == "exclusive":
                    with usage.current_view(fixture.root, fixture.config, exclusive=True):
                        pytest.fail("exclusive borrow accepted")
                elif write == "transact":
                    fixture.revoke(usage, body)
                elif write == "lock":
                    with store.locked_state(fixture.root, write=True):
                        pytest.fail("writer lock accepted")
                else:
                    store.write_state(-1, before)
    assert (fixture.host / "evidence-state.json").read_bytes() == before
    assert decision(usage, fixture, body)["eligible"]


@pytest.mark.parametrize("changed", ["root", "config", "selection", "thread", "generation"])
def test_wrong_live_borrow_is_sticky_and_cleanup_restores_native_reads(native, monkeypatch, changed, tmp_path):
    usage, fixture, body = native
    with pytest.raises(ValueError):
        with usage._observation_reads(fixture.root, fixture.config):
            with pytest.raises(ValueError):
                if changed == "root":
                    with usage.current_view(tmp_path, fixture.config):
                        pytest.fail("different root accepted")
                elif changed == "config":
                    with usage.current_view(fixture.root, dict(fixture.config, changed=True)):
                        pytest.fail("different configuration accepted")
                elif changed == "selection":
                    with HostContext([fixture.root], [], "different").activate():
                        decision(usage, fixture, body)
                elif changed == "thread":
                    context = copy_context()
                    with ThreadPoolExecutor(max_workers=1) as worker:
                        worker.submit(context.run, decision, usage, fixture, body).result()
                else:
                    actual = usage._read_generation
                    with monkeypatch.context() as patch:
                        patch.setattr(usage, "_read_generation", lambda: (SCRIPTS, b"different"))
                        decision(usage, fixture, body)
                    assert usage._read_generation is actual
    assert decision(usage, fixture, body)["eligible"]


def test_copied_context_cannot_reuse_closed_scope_or_nest(native):
    usage, fixture, body = native
    with usage._observation_reads(fixture.root, fixture.config):
        retained = copy_context()
        assert decision(usage, fixture, body)["eligible"]
    with pytest.raises(ValueError, match="usage_observation_scope_unavailable"):
        retained.run(decision, usage, fixture, body)
    with pytest.raises(ValueError, match="usage_observation_scope_nested"):
        with usage._observation_reads(fixture.root, fixture.config):
            with pytest.raises(ValueError, match="usage_observation_scope_nested"):
                with usage._observation_reads(fixture.root, fixture.config):
                    pytest.fail("nested scope accepted")


@pytest.mark.parametrize("changed", ["clock", "grant", "trust", "revoked-key"])
def test_closing_and_caught_borrow_revalidate_current_authority(native, monkeypatch, changed):
    usage, fixture, body = native
    started = datetime.now(timezone.utc)
    class Clock:
        value = started
        @classmethod
        def now(cls, _zone):
            return cls.value
    monkeypatch.setattr(usage, "datetime", Clock)
    deadline = started + timedelta(hours=1)
    if changed == "grant":
        body["grant"]["payload"]["expires_at"] = deadline.isoformat()
        body["grant"] = authenticate(body["grant"]["payload"], "owner", "usage")
        fixture.transact(usage, "authorize", body)
    reason = {"clock": "host_state_clock_regressed", "grant": "usage_outside_validity",
        "trust": "host_trust_changed", "revoked-key": "host_trust_changed"}[changed]
    with pytest.raises(ValueError, match=reason):
        with usage._observation_reads(fixture.root, fixture.config):
            assert decision(usage, fixture, body)["eligible"]
            if changed == "clock":
                Clock.value = started - timedelta(seconds=1)
            elif changed == "grant":
                Clock.value = deadline
            else:
                fixture.policy["revoked_keys" if changed == "revoked-key" else "revoked_envelopes"] = ["runner-key" if changed == "revoked-key" else "sha256:" + "a" * 64]
                fixture.save_policy()
            with pytest.raises(ValueError, match=reason):
                decision(usage, fixture, body)
            Clock.value = started


def test_materialized_capture_remains_separate_and_checks_each_exit(native, monkeypatch, tmp_path):
    usage, fixture, body = native
    publication = load_isolated_module("observation_publication", SCRIPTS / "_publication_context.py")
    captured = tmp_path / "captured"
    captured.mkdir()
    calls = []
    with usage._observation_reads(fixture.root, fixture.config):
        with usage.current_view(fixture.root, fixture.config) as actual:
            original = actual.revalidate
            monkeypatch.setattr(actual, "revalidate", lambda *args: (calls.append(True), original(*args))[-1])
            with publication.authorized_capture(captured, fixture.config, actual):
                with usage.current_view(captured, fixture.config) as borrowed:
                    assert borrowed is actual
                    assert len(calls) == 1
                assert len(calls) == 2
            assert publication.captured_view(fixture.root, fixture.config) is None


@pytest.mark.parametrize("matching", [False, True])
def test_capture_scope_cannot_seed_native_observation(native, tmp_path, matching):
    usage, fixture, _body = native
    publication = load_isolated_module("observation_capture_seed", SCRIPTS / "_publication_context.py")
    captured = tmp_path / "captured"
    captured.mkdir()
    supplied = SimpleNamespace(require_selection=lambda: None)
    with publication.authorized_capture(captured, fixture.config, supplied):
        target = captured if matching else fixture.root
        with usage._observation_reads(target, fixture.config):
            assert publication.live_read.get() is None
            with usage.current_view(target, fixture.config) as borrowed:
                assert (borrowed is supplied) is matching


@pytest.mark.parametrize("availability", ["missing", "unsupported"])
def test_initial_unavailable_scope_preserves_ordinary_owner_path(native, monkeypatch, availability):
    usage, fixture, body = native
    if availability == "missing":
        (fixture.host / "evidence-state.json").unlink()
    else:
        store = usage.locked_state.__wrapped__.__globals__
        monkeypatch.setitem(store, "fcntl", None)
        if getattr(store["os"], "native_windows", False):
            pytest.skip("The native Windows owner does not use fcntl.")
    with usage._observation_reads(fixture.root, fixture.config):
        assert usage.live_read.get() is None
        with pytest.raises(ValueError):
            decision(usage, fixture, body)


def test_real_constructor_shares_one_replay_then_releases_before_later_methods(host, monkeypatch):
    root = host.root
    usage = owner("_evidence_usage")
    with monkeypatch.context() as patch:
        patch.setattr(usage, "_observation_reads", lambda *_: nullcontext())
        ordinary, ordinary_count = replays(lambda: ResearchObservation(root))
    observed, shared_count = replays(lambda: ResearchObservation(root))
    assert ordinary_count > 1 and shared_count == 1
    def stable(value):
        if isinstance(value, dict):
            return {key: stable(child) for key, child in value.items()
                if key not in {"evaluated_at", "generated_at", "observation_seconds"}}
        if isinstance(value, list):
            return [stable(child) for child in value]
        return value
    assert stable(observed.status) == stable(ordinary.status)
    assert stable(observed.strict) == stable(ordinary.strict)
    assert usage.live_read.get() is None
    host.revoke(usage, host.body)
    observed.finish()
    assert decision(usage, host, host.body)["reasons"] == ["source_revision_revoked"]


@pytest.mark.parametrize("changed", ["source", "config", "root", "caught"])
def test_constructor_closing_failures_cannot_be_optional(setup, tmp_path, monkeypatch, changed):
    root, *_ = setup
    _request, fixture, body = protected_source(setup, tmp_path, monkeypatch)
    actual = ResearchObservation._read
    def read(self, run_id):
        actual(self, run_id)
        if changed == "source":
            (root / "raw/web/source.html").write_text("Changed original.")
        elif changed == "config":
            (root / "research.yml").write_text((root / "research.yml").read_text() + "\nchanged: true\n")
        elif changed == "root":
            saved = root.with_name("original-root")
            root.rename(saved)
            shutil.copytree(saved, root)
        else:
            with pytest.raises(ValueError):
                owner("_host_evidence_store").write_state(-1, b"blocked")
    monkeypatch.setattr(ResearchObservation, "_read", read)
    with protected_context(setup, fixture).activate():
        with pytest.raises((Exception, SystemExit)) as caught:
            ResearchObservation(root)
        assert getattr(caught.value, "error_code", None) in {"ONBOARDING_PLAN_STALE", "EVIDENCE_USAGE_REFUSED"}
        assert owner("_evidence_usage").live_read.get() is None


def test_base_exception_releases_lock_and_invalidates_scope(native):
    usage, fixture, body = native
    class Interrupted(BaseException):
        pass
    with pytest.raises(Interrupted):
        with usage._observation_reads(fixture.root, fixture.config):
            retained = copy_context()
            raise Interrupted()
    assert usage.live_read.get() is None
    with pytest.raises(ValueError, match="usage_observation_scope_unavailable"):
        retained.run(decision, usage, fixture, body)
    fixture.revoke(usage, body)
    assert decision(usage, fixture, body)["reasons"] == ["source_revision_revoked"]


def test_unsafe_trust_permissions_after_entry_refuse_and_cleanup(native):
    from evidence_wiki._filesystem import os
    usage, fixture, body = native
    if getattr(os, "native_windows", False):
        pytest.skip("Native Windows validates ACL custody independently.")
    with pytest.raises(ValueError):
        with usage._observation_reads(fixture.root, fixture.config):
            assert decision(usage, fixture, body)["eligible"]
            os.chmod(fixture.policy_path, 0o644)
    os.chmod(fixture.policy_path, 0o600)
    assert decision(usage, fixture, body)["eligible"]


@pytest.mark.parametrize("changed", ["capture", "nested", "write", "generation", "closed"])
def test_transaction_read_custody_refuses_invalid_borrows(native, tmp_path, monkeypatch, changed):
    usage, fixture, body = native
    publication = load_isolated_module("transaction_publication", SCRIPTS / "_publication_context.py")
    with usage.current_view(fixture.root, fixture.config, exclusive=True) as actual:
        if changed == "capture":
            with publication.authorized_capture(tmp_path, fixture.config, actual):
                with pytest.raises(ValueError, match="usage_observation_scope_changed"):
                    with usage._transaction_observation_reads(fixture.root, fixture.config, actual):
                        pytest.fail("capture admitted")
        elif changed == "closed":
            with usage._transaction_observation_reads(fixture.root, fixture.config, actual):
                retained = copy_context()
                with usage.current_view(fixture.root, fixture.config) as borrowed:
                    assert borrowed is actual
            with pytest.raises(ValueError, match="usage_observation_scope_unavailable"):
                retained.run(decision, usage, fixture, body)
        else:
            reason = {"nested": "usage_observation_scope_nested", "write": "usage_observation_is_read_only",
                "generation": "usage_observation_generation_changed"}[changed]
            with pytest.raises(ValueError, match=reason):
                with usage._transaction_observation_reads(fixture.root, fixture.config, actual):
                    with pytest.raises(ValueError, match=reason):
                        if changed == "nested":
                            with usage._transaction_observation_reads(fixture.root, fixture.config, actual):
                                pytest.fail("nested scope admitted")
                        elif changed == "write":
                            fixture.revoke(usage, body)
                        else:
                            with monkeypatch.context() as patch:
                                patch.setattr(usage, "_read_generation", lambda: (SCRIPTS, b"different"))
                                decision(usage, fixture, body)
        assert usage.live_read.get() is None
    assert decision(usage, fixture, body)["eligible"]


@pytest.mark.parametrize("changed", ["root", "config", "thread"])
def test_transaction_read_custody_rejects_foreign_execution(native, tmp_path, changed):
    usage, fixture, body = native
    with usage.current_view(fixture.root, fixture.config, exclusive=True) as actual:
        with pytest.raises(ValueError):
            with usage._transaction_observation_reads(fixture.root, fixture.config, actual):
                with pytest.raises(ValueError):
                    if changed == "thread":
                        context = copy_context()
                        with ThreadPoolExecutor(max_workers=1) as worker:
                            worker.submit(context.run, decision, usage, fixture, body).result()
                    else:
                        root = tmp_path if changed == "root" else fixture.root
                        config = dict(fixture.config, foreign=True) if changed == "config" else fixture.config
                        with usage.current_view(root, config):
                            pytest.fail("foreign read admitted")
        assert usage.live_read.get() is None
