"""Independent current permission and native bookkeeping boundary oracles."""

import json
import os
from datetime import datetime, timezone

import pytest
import yaml

from evidence_wiki import Workspace
from evidence_wiki.host_context import HostContext
from evidence_wiki.pack_discovery import owner
from tests._execution_fixture import authenticate, identifier
from tests._usage_fixture import UsageFixture
from tests.test_mutation_operations import roots as roots
from tests.test_request_lifecycle_owner import ready_source, writer_context
from tests.test_requirements_owner import setup as setup

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Native descriptor-relative lifecycle custody is unavailable.")


def protected_source(setup, tmp_path, monkeypatch, *, retrieval=True, expires=None):
    root, *_ = setup
    ready_source(root)
    with writer_context(setup).activate(), Workspace.open(root) as workspace:
        created = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/study",
            rationale="Need current evidence", question_slugs=["q1"], scope={"facet_id": "primary"})
        workspace.normalize.sources(source_ids=["sample-source"])
    directory = tmp_path / "permission-owner"
    directory.mkdir()
    fixture = UsageFixture(directory, monkeypatch)
    fixture.root = root
    fixture.binding = identifier("evidence-host-workspace/v1", {"root": str(root)})
    config = yaml.safe_load((root / "research.yml").read_bytes())
    config.update(fixture.config)
    fixture.config = config
    (root / "research.yml").write_text(yaml.safe_dump(config))
    native = owner("_evidence_usage")
    fixture.transact(native, "initialize")
    normalized = (root / "sources/normalized/sample-source.md").read_bytes()
    body, files = fixture.source("sample-source", normalized=normalized, retrieval=retrieval,
        evidence={"source.html": (root / "raw/web/source.html").read_bytes()})
    if expires:
        grant = body["grant"]["payload"]
        grant["expires_at"] = expires
        body["grant"] = authenticate(grant, "owner", "usage")
    fixture.transact(native, "deposit", body, files)
    return created["request"]["request_id"], fixture, body


def test_signed_current_usage_supports_strict_bookkeeping(setup, tmp_path, monkeypatch):
    root, *_ = setup
    request_id, fixture, body = protected_source(setup, tmp_path, monkeypatch)
    selected = writer_context(setup)
    context = HostContext([root], selected.grants | {"source_requests.run_fulfill_current_request"}, "caller", [root],
        mutation_control_directory=setup[1], authority_file=fixture.policy_path, state_directory=fixture.host)
    with context.activate(), Workspace.open(root) as workspace:
        checked = owner("_usage_gate").source_decision(root, yaml.safe_load((root / "research.yml").read_bytes()),
            "sample-source", [], normalized=(root / "sources/normalized/sample-source.md").read_bytes(),
            uses=["retrieval"], purpose="research")
        assert checked["eligible"] and checked["complete"], checked
        assert checked["source_revision"] == body["source_revision"]
        from evidence_wiki import request_lifecycle_operations as lifecycle
        view = lifecycle._ScopedView(root)
        readiness = lifecycle._source(view, "sample-source", {"facet_id": "primary", "jurisdiction": "Spain"})
        view.finish()
        assert readiness["current_ready"], str(readiness)
        request = workspace.mutations.prepare(request_id="strict-protected", operation="source_requests.fulfill_current",
            arguments={"request_id": request_id, "source_id": "sample-source"})
        receipt = workspace.source_requests.fulfill_current(request)
        assert receipt["status"] == "committed", receipt
        assert receipt["result"]["source"]["permission_basis"] == "native_current_usage"
        assert not receipt["evidence_accepted"]


def test_legacy_fulfillment_keeps_protected_intake_refusal(setup, tmp_path, monkeypatch):
    root, *_ = setup
    request_id, fixture, body = protected_source(setup, tmp_path, monkeypatch)
    selected = writer_context(setup)
    context = HostContext([root], selected.grants | {"source_requests.run_fulfill_current_request"}, "caller", [root],
        mutation_control_directory=setup[1], authority_file=fixture.policy_path, state_directory=fixture.host)
    with context.activate():
        native = owner("source_requests")
        checked = owner("_usage_gate").source_decision(root, fixture.config, "sample-source", [],
            normalized=(root / "sources/normalized/sample-source.md").read_bytes(), uses=["retrieval"], purpose="research")
        assert checked["eligible"] and checked["source_revision"] == body["source_revision"]
        with pytest.raises(Exception) as caught:
            native.run_fulfill_request(root, request_id=request_id,
                source_id="sample-source", require_scope=True)
        assert native.run_fulfill_request.__globals__["ScriptRefusal"] is type(caught.value)
        assert caught.value.__cause__.details["reason"] == "protected_capture_requires_host_sanitization"


def test_frozen_required_scope_cannot_be_omitted_even_with_incomplete_flag(setup):
    root, *_ = setup
    with writer_context(setup).activate(), Workspace.open(root) as workspace:
        created = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/study",
            rationale="Need scoped evidence", question_slugs=["q1"], scope={"facet_id": "primary"})
        workspace.normalize.sources(source_ids=["sample-source"])
        store = owner("source_requests").requests_path(root, yaml.safe_load((root / "research.yml").read_bytes()))
        before = store.read_bytes()
        request = workspace.mutations.prepare(request_id="missing-scope", operation="source_requests.fulfill_current",
            arguments={"request_id": created["request"]["request_id"], "source_id": "sample-source", "needs_complete": False})
        receipt = workspace.source_requests.fulfill_current(request)
        assert receipt["status"] != "committed", receipt
        assert receipt["error"]["error_code"] == "MUTATION_REQUEST_EVIDENCE_NOT_READY"
        assert store.read_bytes() == before


def test_coverage_only_unknown_blocker_prevents_reopen(setup):
    root, *_ = setup
    ready_source(root)
    with writer_context(setup).activate(), Workspace.open(root) as workspace:
        created = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/study",
            rationale="Need evidence", question_slugs=["q1"], scope={"facet_id": "primary"})
        identifier = created["request"]["request_id"]
        workspace.normalize.sources(source_ids=["sample-source"])
        fulfilled = workspace.mutations.prepare(request_id="fulfill", operation="source_requests.fulfill_current",
            arguments={"request_id": identifier, "source_id": "sample-source"})
        assert workspace.source_requests.fulfill_current(fulfilled)["status"] == "committed"
        workspace.questions.claim(slug="q1", agent_id="caller")
        blocked = workspace.mutations.prepare(request_id="block", operation="questions.block_current",
            arguments={"slug": "q1", "blocking_request_ids": [identifier], "reason": "Check every blocker"})
        assert workspace.questions.block_current(blocked)["status"] == "committed"
        coverage = root / "sources/coverage/q1.yml"
        document = yaml.safe_load(coverage.read_bytes())
        document["required_facets"][0]["blocking_request_ids"] = ["missing-request"]
        coverage.write_text(yaml.safe_dump(document))
        observed = workspace.source_requests.current(question_slugs=["q1"])
        assert "missing-request" in observed["questions"][0]["blocking_request_ids"]
        assert "missing-request" in observed["questions"][0]["unresolved_request_ids"]
        assert not observed["questions"][0]["current_ready"]
        before = (root / "wiki/questions/q1.md").read_bytes()
        request = workspace.mutations.prepare(request_id="reopen", operation="questions.reopen_current",
            arguments={"slug": "q1", "needs_complete": False})
        result = workspace.questions.reopen_current(request)
        assert result["status"] != "committed", result
        assert (root / "wiki/questions/q1.md").read_bytes() == before


@pytest.mark.parametrize("changed", ["original", "request", "coverage"])
def test_native_before_write_rejects_changed_input(setup, monkeypatch, changed):
    root, *_ = setup
    ready_source(root)
    with writer_context(setup).activate(), Workspace.open(root) as workspace:
        created = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/study",
            rationale="Need evidence", question_slugs=["q1"], scope={"facet_id": "primary"})
        workspace.normalize.sources(source_ids=["sample-source"])
        native = owner("source_requests")
        store = native.requests_path(root, yaml.safe_load((root / "research.yml").read_bytes()))
        original_store = store.read_bytes()
        request = workspace.mutations.prepare(request_id="race", operation="source_requests.fulfill_current",
            arguments={"request_id": created["request"]["request_id"], "source_id": "sample-source"})
        actual = native.run_fulfill_request
        seen = []
        def racing(*args, **kwargs):
            original_guard = kwargs["_before_write"]
            def before_write():
                if changed == "original":
                    (root / "raw/web/source.html").unlink()
                elif changed == "request":
                    rows = [json.loads(line) for line in store.read_bytes().splitlines()]
                    rows[0]["scope"]["jurisdiction"] = "Portugal"
                    store.write_text("".join(json.dumps(row) + "\n" for row in rows))
                else:
                    coverage = root / "sources/coverage/q1.yml"
                    document = yaml.safe_load(coverage.read_bytes())
                    document["required_facets"][0]["blocking_request_ids"] = ["new-blocker"]
                    coverage.write_text(yaml.safe_dump(document))
                seen.append(store.read_bytes())
                return original_guard()
            kwargs["_before_write"] = before_write
            return actual(*args, **kwargs)
        with monkeypatch.context() as faults:
            faults.setattr(native, "run_fulfill_request", racing)
            result = workspace.source_requests.fulfill_current(request)
        assert seen, result
        assert result["status"] != "committed", result
        assert result["error"]["error_code"] == "MUTATION_REQUEST_INPUTS_CHANGED", result
        assert store.read_bytes() == seen[0]
        assert all(json.loads(line)["status"] == "open" for line in store.read_bytes().splitlines())
        if changed != "request":
            assert store.read_bytes() == original_store


def test_derived_request_inherits_actual_original_scope(setup):
    root, *_ = setup
    from tests.test_requirements_owner import submit
    submit(setup)
    with writer_context(setup).activate(), Workspace.open(root) as workspace:
        created = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/derived",
            rationale="Need derived evidence", question_slugs=["derived1"], scope={"facet_id": "primary"})
        observed = workspace.source_requests.current(request_ids=[created["request"]["request_id"]],
            question_slugs=["derived1"])
        assert observed["requests"][0]["required_scope"] == {"jurisdiction": "Spain"}
        assert not observed["requests"][0]["current_ready"]


@pytest.mark.parametrize("withdrawn", ["denied", "revoked"])
def test_signed_usage_denial_cannot_change_historical_request(setup, tmp_path, monkeypatch, withdrawn):
    root, *_ = setup
    request_id, fixture, body = protected_source(setup, tmp_path, monkeypatch, retrieval=withdrawn != "denied")
    if withdrawn == "revoked":
        fixture.revoke(owner("_evidence_usage"), body)
    selected = writer_context(setup)
    context = HostContext([root], selected.grants | {"source_requests.run_fulfill_current_request"}, "caller", [root],
        mutation_control_directory=setup[1], authority_file=fixture.policy_path, state_directory=fixture.host)
    with context.activate(), Workspace.open(root) as workspace:
        store = owner("source_requests").requests_path(root, fixture.config)
        before = store.read_bytes()
        request = workspace.mutations.prepare(request_id="denied", operation="source_requests.fulfill_current",
            arguments={"request_id": request_id, "source_id": "sample-source"})
        result = workspace.source_requests.fulfill_current(request)
        assert result["status"] != "committed", result
        assert result["error"]["error_code"] == "MUTATION_REQUEST_EVIDENCE_NOT_READY", result
        assert store.read_bytes() == before


def test_current_read_requires_explicit_owner_grant(setup):
    root, *_ = setup
    selected = writer_context(setup)
    context = HostContext([root], selected.grants - {"request_lifecycle_operations.current"}, "caller", [root],
        mutation_control_directory=setup[1])
    from evidence_wiki.errors import EvidenceWikiError
    with context.activate(), Workspace.open(root) as workspace, pytest.raises(EvidenceWikiError) as caught:
        workspace.source_requests.current()
    assert caught.value.error_code == "HOST_CONTEXT_REFUSED"
    assert caught.value.details["reason"] == "host_operation_not_granted"


def protected_context(setup, fixture):
    root, control, *_ = setup
    selected = writer_context(setup)
    return HostContext([root], selected.grants | {"source_requests.run_fulfill_current_request"}, "caller", [root],
        mutation_control_directory=control, authority_file=fixture.policy_path, state_directory=fixture.host)


def test_protected_fulfillment_holds_usage_lock_through_actual_store_write(setup, tmp_path, monkeypatch):
    root, *_ = setup
    request_id, fixture, body = protected_source(setup, tmp_path, monkeypatch)
    native = owner("source_requests")
    actual = native._write_requests_unlocked
    attempted = []
    def writer(*args, **kwargs):
        with pytest.raises(ValueError, match="host_state_lock_unavailable"):
            fixture.revoke(owner("_evidence_usage"), body)
        attempted.append(True)
        return actual(*args, **kwargs)
    with protected_context(setup, fixture).activate(), Workspace.open(root) as workspace:
        request = workspace.mutations.prepare(request_id="locked", operation="source_requests.fulfill_current",
            arguments={"request_id": request_id, "source_id": "sample-source"})
        with monkeypatch.context() as faults:
            faults.setattr(native, "_write_requests_unlocked", writer)
            receipt = workspace.source_requests.fulfill_current(request)
        assert receipt["status"] == "committed", receipt
        assert attempted == [True]
        fixture.revoke(owner("_evidence_usage"), body)
        observed = workspace.source_requests.current(request_ids=[request_id])["requests"][0]
        assert observed["recorded_status"] == "fulfilled" and not observed["current_ready"]


@pytest.mark.parametrize("changed", ["clock", "trust"])
def test_protected_native_revalidates_after_callback_before_effect(setup, tmp_path, monkeypatch, changed):
    root, *_ = setup
    request_id, fixture, _body = protected_source(setup, tmp_path, monkeypatch, expires="2026-12-15T00:00:00Z")
    native = owner("source_requests")
    actual = native.run_fulfill_current_request
    injected = []
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 12, 20, tzinfo=timezone.utc).astimezone(tz)
    with protected_context(setup, fixture).activate(), Workspace.open(root) as workspace:
        store = native.requests_path(root, fixture.config)
        before = store.read_bytes()
        request = workspace.mutations.prepare(request_id="fresh", operation="source_requests.fulfill_current",
            arguments={"request_id": request_id, "source_id": "sample-source"})
        with monkeypatch.context() as faults:
            def racing(*args, **kwargs):
                guard = kwargs["_before_write"]
                def before_write():
                    guard()
                    if changed == "clock":
                        faults.setitem(native.current_view.__wrapped__.__globals__, "datetime", Later)
                    else:
                        fixture.policy["revoked_keys"] = ["owner-key"]
                        fixture.save_policy()
                    injected.append(True)
                kwargs["_before_write"] = before_write
                return actual(*args, **kwargs)
            faults.setattr(native, "run_fulfill_current_request", racing)
            result = workspace.source_requests.fulfill_current(request)
        assert injected == [True], result
        assert result["status"] != "committed", result
        assert store.read_bytes() == before


def test_protected_reopen_holds_usage_lock_through_actual_page_write(setup, tmp_path, monkeypatch):
    root, *_ = setup
    request_id, fixture, body = protected_source(setup, tmp_path, monkeypatch)
    with protected_context(setup, fixture).activate(), Workspace.open(root) as workspace:
        request = workspace.mutations.prepare(request_id="fulfill", operation="source_requests.fulfill_current",
            arguments={"request_id": request_id, "source_id": "sample-source"})
        assert workspace.source_requests.fulfill_current(request)["status"] == "committed"
        workspace.questions.claim(slug="q1", agent_id="caller")
        request = workspace.mutations.prepare(request_id="block", operation="questions.block_current",
            arguments={"slug": "q1", "blocking_request_ids": [request_id], "reason": "Check current permission"})
        assert workspace.questions.block_current(request)["status"] == "committed"
        request = workspace.mutations.prepare(request_id="reopen", operation="questions.reopen_current",
            arguments={"slug": "q1"})
        question = owner("question_resolve").load_sibling_module("question_claim")
        actual = question.write_page_atomic
        attempted = []
        def writer(*args, **kwargs):
            with pytest.raises(ValueError, match="host_state_lock_unavailable"):
                fixture.revoke(owner("_evidence_usage"), body)
            attempted.append(True)
            return actual(*args, **kwargs)
        with monkeypatch.context() as faults:
            faults.setattr(question, "write_page_atomic", writer)
            result = workspace.questions.reopen_current(request)
        assert result["status"] == "committed", result
        assert attempted == [True]
