"""Canonical current request and private capture receipts precede source use."""

import json
import os

import pytest

from evidence_wiki import Workspace
from evidence_wiki import acquisition_operations as operations
from evidence_wiki.host_context import HostContext
from evidence_wiki.pack_discovery import owner
from tests.test_mutation_operations import roots as roots
from tests.test_native_capture_revision import metadata
from tests.test_request_lifecycle_owner import writer_context
from tests.test_requirements_owner import setup as setup

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Native descriptor-relative acquisition custody is unavailable.")


def selected(setup):
    context = writer_context(setup)
    return HostContext(context.allowed_roots, context.grants | {"acquisition_operations.prepare",
        "acquisition_operations.reconcile", "acquisition_operations.fulfill", "source_capture_operations.deliver"}, "caller", context.writable_roots,
        mutation_control_directory=setup[1])


def created(workspace):
    return workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/study",
        rationale="Need complete scoped evidence", question_slugs=["q1"], scope={"facet_id": "primary"})["request"]["request_id"]


def test_prepare_actual_request_is_readonly_and_unsupported_budget_explicit(setup, monkeypatch):
    root, *_ = setup
    monkeypatch.setattr(owner("source_requests"), "run_plan_fetch", lambda *_: pytest.fail("Readonly preparation invoked plan-fetch"))
    with selected(setup).activate(), Workspace.open(root) as workspace:
        identifier = created(workspace)
        before = owner("_evidence_revision").capture_workspace(root).revision_id
        result = operations.prepare(root, request_id=identifier)
        assert result["required_scope"] == {"facet_id": "primary", "jurisdiction": "Spain"}
        assert result["status"] == "blocked_external" and result["budget_status"] == "owning_run_reservation_required"
        assert result["execution_authority"] == "not_established"
        assert owner("_evidence_revision").capture_workspace(root).revision_id == before


def test_caller_json_receipt_cannot_become_capture_custody(setup):
    root, *_ = setup
    from evidence_wiki.errors import EvidenceWikiError
    with selected(setup).activate(), Workspace.open(root) as workspace:
        identifier = created(workspace)
        with pytest.raises(EvidenceWikiError):
            operations.reconcile(root, request_id=identifier, acquisition_ref={"status": "committed", "authorized": True})
        with pytest.raises(EvidenceWikiError):
            operations.reconcile(root, request_id=identifier, acquisition_ref="invented")


@pytest.mark.parametrize("jurisdiction,ready", [("Spain", True), ("Portugal", False)])
def test_real_capture_custody_stays_partial_until_current_normalization_and_exact_scope(setup, jurisdiction, ready):
    root, *_ = setup
    with selected(setup).activate(), Workspace.open(root) as workspace:
        identifier = created(workspace)
        delivery = workspace.sources.capture_bytes_document(b"Measured reflectance was 0.74 ratio with retained context.\n",
            metadata("plain_text", request_id=identifier, scope={"facet_id": "primary", "jurisdiction": jurisdiction}))
        request = workspace.sources.prepare_capture(request_id="capture", path="raw/web/captured.txt", delivery=delivery)
        receipt = workspace.sources.capture_revision(request)
        assert receipt["status"] == "committed", receipt
        workspace.mutations.submit(workspace.mutations.prepare(request_id="inventory", operation="sources.inventory", arguments={}))
        first = operations.reconcile(root, request_id=identifier, acquisition_ref="capture")
        assert first["status"] == "partial" and not first["source"]["current_ready"]
        workspace.normalize.sources(source_ids=[receipt["result"]["source_id"]])
        before = owner("_evidence_revision").capture_workspace(root).revision_id
        result = operations.reconcile(root, request_id=identifier, acquisition_ref="capture")
        assert result["source"]["current_ready"] is ready, result
        assert result["recorded_status"] == "open" and not result["request_fulfilled_by_reconciliation"]
        assert owner("_evidence_revision").capture_workspace(root).revision_id == before
        if not ready:
            assert "source_scope_mismatch" in result["source"]["gaps"]


def captured(workspace, identifier):
    delivery = workspace.sources.capture_bytes_document(b"Measured reflectance was 0.74 ratio with retained context.\n",
        metadata("plain_text", request_id=identifier, scope={"facet_id": "primary", "jurisdiction": "Spain"}))
    capture = workspace.sources.prepare_capture(request_id="capture", path="raw/web/captured.txt", delivery=delivery)
    receipt = workspace.sources.capture_revision(capture)
    assert receipt["status"] == "committed", receipt
    workspace.mutations.submit(workspace.mutations.prepare(request_id="inventory", operation="sources.inventory", arguments={}))
    workspace.normalize.sources(source_ids=[receipt["result"]["source_id"]])
    return capture, receipt


def test_actual_strict_fulfillment_keeps_source_acceptance_false(setup):
    root, *_ = setup
    with selected(setup).activate(), Workspace.open(root) as workspace:
        identifier = created(workspace)
        _capture, receipt = captured(workspace, identifier)
        observed = operations.reconcile(root, request_id=identifier, acquisition_ref="capture")
        result = operations.fulfill(root, request_id=identifier, acquisition_ref="capture",
            expected_request_basis=observed["request_basis"], expected_material_basis=observed["source"]["material_basis"],
            mutation_request_id="fulfill")
        assert result["status"] == "committed" and result["historical_fulfillment_recorded"], result
        assert result["historical"] and result["current_evidence_usable"] == "not_established"
        assert not result["source_accepted"] and not result["rights_inferred"]
        current = workspace.source_requests.current(request_ids=[identifier])["requests"][0]
        assert current["recorded_status"] == "fulfilled" and current["recorded_source_id"] == receipt["result"]["source_id"]


def test_changed_request_after_reconciliation_refuses_before_fulfillment(setup):
    root, *_ = setup
    from evidence_wiki.errors import UsageError
    with selected(setup).activate(), Workspace.open(root) as workspace:
        identifier = created(workspace)
        captured(workspace, identifier)
        observed = operations.reconcile(root, request_id=identifier, acquisition_ref="capture")
        store = owner("source_requests").requests_path(root, owner("verify_quotes").load_config(root))
        rows = [json.loads(line) for line in store.read_bytes().splitlines()]
        rows[0]["rationale"] = "Revised current request intent"
        store.write_text("".join(json.dumps(row) + "\n" for row in rows))
        before = store.read_bytes()
        with pytest.raises(UsageError) as caught:
            operations.fulfill(root, request_id=identifier, acquisition_ref="capture",
                expected_request_basis=observed["request_basis"], expected_material_basis=observed["source"]["material_basis"],
                mutation_request_id="fulfill")
        assert caught.value.details["field"] == "request_basis_changed"
        assert store.read_bytes() == before


@pytest.mark.parametrize("changed", ["permission", "original"])
def test_current_permission_loss_or_deleted_original_cannot_fulfill_old_reconciliation(setup, changed):
    root, *_ = setup
    from evidence_wiki.errors import EvidenceWikiError
    with selected(setup).activate(), Workspace.open(root) as workspace:
        identifier = created(workspace)
        _capture, receipt = captured(workspace, identifier)
        observed = operations.reconcile(root, request_id=identifier, acquisition_ref="capture")
        source_id = receipt["result"]["source_id"]
        if changed == "permission":
            path = root / "sources/normalized" / (owner("normalize_sources").safe_source_id(source_id) + ".md")
            path.write_text(path.read_text().replace("---\n", "---\nretrieval_eligible: false\n", 1))
        else:
            (root / "raw/web/captured.txt").unlink()
        store = owner("source_requests").requests_path(root, owner("verify_quotes").load_config(root))
        before = store.read_bytes()
        with pytest.raises(EvidenceWikiError):
            operations.fulfill(root, request_id=identifier, acquisition_ref="capture",
                expected_request_basis=observed["request_basis"], expected_material_basis=observed["source"]["material_basis"],
                mutation_request_id="fulfill")
        assert store.read_bytes() == before


def test_repeated_genuine_delivery_observation_never_grows_workspace_or_fulfills_itself(setup):
    root, *_ = setup
    with selected(setup).activate(), Workspace.open(root) as workspace:
        identifier = created(workspace)
        captured(workspace, identifier)
        before = owner("_evidence_revision").capture_workspace(root).revision_id
        first = operations.reconcile(root, request_id=identifier, acquisition_ref="capture")
        second = operations.reconcile(root, request_id=identifier, acquisition_ref="capture")
        assert first == second
        assert first["recorded_status"] == "open" and not first["request_fulfilled_by_reconciliation"]
        assert owner("_evidence_revision").capture_workspace(root).revision_id == before


def test_exact_owned_fulfillment_replay_returns_original_receipt_after_source_deletion(setup):
    root, *_ = setup
    with selected(setup).activate(), Workspace.open(root) as workspace:
        identifier = created(workspace)
        captured(workspace, identifier)
        observed = operations.reconcile(root, request_id=identifier, acquisition_ref="capture")
        values = {"request_id": identifier, "acquisition_ref": "capture", "expected_request_basis": observed["request_basis"],
            "expected_material_basis": observed["source"]["material_basis"], "mutation_request_id": "fulfill"}
        result = operations.fulfill(root, **values)
        assert result["status"] == "committed"
        (root / "raw/web/captured.txt").unlink()
        before = owner("_evidence_revision").capture_workspace(root).revision_id
        assert operations.fulfill(root, **values) == result
        assert owner("_evidence_revision").capture_workspace(root).revision_id == before
        with pytest.raises(Exception) as caught:
            operations.fulfill(root, **{**values, "expected_material_basis": "sha256:" + "0" * 64})
        assert getattr(caught.value, "details", {}).get("field") == "retained_outer_binding_conflict"


def test_failure_after_private_tuple_before_submit_resumes_same_native_intent(setup, monkeypatch):
    root, *_ = setup
    with selected(setup).activate(), Workspace.open(root) as workspace:
        identifier = created(workspace)
        captured(workspace, identifier)
        observed = operations.reconcile(root, request_id=identifier, acquisition_ref="capture")
        values = {"request_id": identifier, "acquisition_ref": "capture", "expected_request_basis": observed["request_basis"],
            "expected_material_basis": observed["source"]["material_basis"], "mutation_request_id": "fulfill"}
        with monkeypatch.context() as faults:
            def interrupted(*args, **kwargs):
                raise RuntimeError("Interrupted before native submission")
            faults.setattr(operations.mutations, "submit", interrupted)
            with pytest.raises(RuntimeError):
                operations.fulfill(root, **values)
        assert workspace.source_requests.current(request_ids=[identifier])["requests"][0]["recorded_status"] == "open"
        result = operations.fulfill(root, **values)
        assert result["status"] == "committed", result


def test_failure_after_intent_before_private_tuple_safely_continues_exact_preflight(setup, monkeypatch):
    root, *_ = setup
    with selected(setup).activate(), Workspace.open(root) as workspace:
        identifier = created(workspace)
        captured(workspace, identifier)
        observed = operations.reconcile(root, request_id=identifier, acquisition_ref="capture")
        values = {"request_id": identifier, "acquisition_ref": "capture", "expected_request_basis": observed["request_basis"],
            "expected_material_basis": observed["source"]["material_basis"], "mutation_request_id": "fulfill"}
        with monkeypatch.context() as faults:
            def interrupted(*args, **kwargs):
                raise RuntimeError("Interrupted after intent before outer tuple")
            faults.setattr(operations.MutationStore, "stage_blob", interrupted)
            with pytest.raises(RuntimeError):
                operations.fulfill(root, **values)
        assert workspace.source_requests.current(request_ids=[identifier])["requests"][0]["recorded_status"] == "open"
        result = operations.fulfill(root, **values)
        assert result["status"] == "committed" and result["historical_fulfillment_recorded"], result
