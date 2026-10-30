"""Public acquisition schemas retain actual custody and historical uncertainty."""

import os

import pytest

from evidence_wiki import Workspace
from evidence_wiki.acquisition_contracts import schema_document
from evidence_wiki.onboarding_contract import _matches
from evidence_wiki.pack_discovery import owner
from tests.test_acquisition_operations import created, selected
from tests.test_mutation_operations import roots as roots
from tests.test_native_capture_revision import metadata
from tests.test_requirements_owner import setup as setup

pytestmark=pytest.mark.skipif(not hasattr(os,"O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Native descriptor-relative acquisition custody is unavailable.")


def test_public_current_capture_reconciliation_schema_and_fulfillment_replay(setup):
    root,*_=setup
    with selected(setup).activate(),Workspace.open(root) as workspace:
        request_id=created(workspace)
        prepared=workspace.source_requests.prepare_acquisition(request_id=request_id)
        _matches(prepared,schema_document("evidence-acquisition-preparation/v1"))
        delivery=workspace.sources.capture_bytes_document(b"Measured value was 0.74 ratio with retained context.\n",
            metadata("plain_text",request_id=request_id,scope={"facet_id":"primary","jurisdiction":"Spain"}))
        frozen=workspace.sources.prepare_capture(request_id="capture-public",path="raw/web/current.txt",delivery=delivery)
        captured=workspace.sources.capture_revision(frozen)
        workspace.mutations.submit(workspace.mutations.prepare(request_id="inventory-public",operation="sources.inventory",arguments={}))
        workspace.normalize.sources(source_ids=[captured["result"]["source_id"]])
        observed=workspace.source_requests.reconcile_acquisition(request_id=request_id,acquisition_ref="capture-public")
        _matches(observed,schema_document("evidence-acquisition-reconciliation/v1"))
        assert observed["source"]["current_ready"]
        parameters={"request_id":request_id,"acquisition_ref":"capture-public","expected_request_basis":observed["request_basis"],
            "expected_material_basis":observed["source"]["material_basis"],"mutation_request_id":"fulfill-public"}
        first=workspace.source_requests.complete_acquisition(**parameters)
        _matches(first,schema_document("evidence-acquisition-fulfillment/v1"))
        assert first["historical_fulfillment_recorded"] and first["current_evidence_usable"]=="not_established"
        (root/"raw/web/current.txt").unlink()
        replay=workspace.source_requests.complete_acquisition(**parameters)
        assert replay==first and not replay["source_accepted"]
        assert owner("source_requests").run_list_requests(root)["requests"][0]["status"]=="fulfilled"


def test_direct_acquisition_completion_refuses_historical_parent_custody(setup):
    from evidence_wiki import acquisition_operations
    from evidence_wiki.errors import UsageError
    from evidence_wiki.mutation_store import MutationStore

    root,control,*_=setup
    with selected(setup).activate():
        with MutationStore(root,control).locked("historical-acquisition"):
            pass
        with MutationStore(root,control).workspace_locked():
            with MutationStore(root,control).historical_locked("historical-acquisition",borrow_workspace=True):
                with pytest.raises(UsageError) as caught:
                    acquisition_operations.fulfill(root,request_id="request",acquisition_ref="capture",
                        expected_request_basis="sha256:"+"a"*64,expected_material_basis="sha256:"+"b"*64,
                        mutation_request_id="fulfill")
                assert caught.value.details["field"]=="historical_read_custody"
        assert not (control/"requests/fulfill").exists()


def test_missing_manifest_source_is_closed_partial_observation(setup):
    root,*_=setup
    with selected(setup).activate(),Workspace.open(root) as workspace:
        request_id=created(workspace)
        delivery=workspace.sources.capture_bytes_document(b"Retained original with contextual measurement.\n",
            metadata("plain_text",request_id=request_id,scope={"facet_id":"primary","jurisdiction":"Spain"}))
        captured=workspace.sources.prepare_capture(request_id="capture-uninventoried",path="raw/web/uninventoried.txt",delivery=delivery)
        workspace.sources.capture_revision(captured)
        observed=workspace.source_requests.reconcile_acquisition(request_id=request_id,acquisition_ref="capture-uninventoried")
        assert observed["status"]=="partial" and observed["source"]["gaps"]==["source_not_in_manifest"]
        _matches(observed,schema_document("evidence-acquisition-reconciliation/v1"))
