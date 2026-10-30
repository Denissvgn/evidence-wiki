"""A single actual host journey retains bytes before source-owner fulfillment."""

import json

import pytest

from evidence_wiki import Workspace
from evidence_wiki import acquisition_workflow as workflow
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_contracts import canonical, digest
from evidence_wiki.pack_discovery import owner
from tests.test_acquisition_dispatch import opener, selected
from tests.test_execution_session import fixture as fixture
from tests.test_native_capture_revision import metadata


def test_one_host_transport_capture_inventory_normalization_strict_fulfillment(fixture, monkeypatch):
    root, policy, execution, context = selected(fixture)
    document = json.loads((root / "docs/research-requirements.json").read_bytes())
    document["request"]["payload"]["budgets"].update(bytes=4096, downloads=2)
    (root / "docs/research-requirements.json").write_bytes(canonical(document)+b"\n")
    from tests.test_draft_admission import update_policy
    update_policy(root)
    grants = context.grants | {"research_session.acquire", "research_session.acquire_evidence", "acquisition_operations.prepare", "acquisition_operations.reconcile",
        "acquisition_operations.fulfill", "request_lifecycle_operations.fulfill_current", "source_requests.run_fulfill_request",
        "source_capture_operations.deliver"}
    context = HostContext([root], grants, context.actor_ref, [root], authority_file=context.authority_file,
        state_directory=None, mutation_control_directory=context.mutation_control_directory,
        controller_refs=context.controller_refs, role_controller_refs=context.role_controller_refs,
        credential_refs=context.credential_refs)
    transport = owner("_acquisition_transport")
    actual, called = transport.execute_planned_request, []
    with context.activate(), Workspace.open(root) as workspace:
        source = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/source",
            rationale="Need scoped retained original", question_slugs=["q"], scope={"facet_id": "primary"})["request"]
        with workspace.research.session(policy, run_id="workflow-acquire", execution=execution) as session:
            with workflow.native.gate(session):
                workflow.native._begin(session)
            supplied = metadata("plain_text", request_id=source["request_id"], scope={"facet_id": "primary"},
                origin_url="https://example.org/source")
            with monkeypatch.context() as fixture_transport:
                fixture_transport.setattr(transport, "execute_planned_request", opener(actual,
                    b"Measured reflectance was 0.74 ratio with retained original context.\n", called,
                    root, context.mutation_control_directory))
                result = session.acquire_evidence(request_id="journey", source_request_id=source["request_id"],
                    expected_request_basis=digest(source), metadata=supplied, path="raw/web/acquired.txt")
            assert result["status"] == "fulfilled", result
            assert called == ["https://example.org/source"]
            replay=session.acquire_evidence(request_id="journey",source_request_id=source["request_id"],
                expected_request_basis=digest(source),metadata=supplied,path="raw/web/acquired.txt")
            assert replay==result and called==["https://example.org/source"]
            from evidence_wiki.acquisition_contracts import schema_document
            from evidence_wiki.errors import UsageError
            from evidence_wiki.onboarding_contract import _matches
            _matches(result,schema_document("evidence-acquisition-workflow-result/v1"))
            with pytest.raises(UsageError):
                session.acquire_evidence(request_id="journey",source_request_id=source["request_id"],
                    expected_request_basis=digest(source),metadata=supplied,path="raw/web/other.txt")
            assert not result["source_accepted"] and not result["rights_inferred"]
            current = workspace.source_requests.current(request_ids=[source["request_id"]])["requests"][0]
            assert current["recorded_status"] == "fulfilled" and current["current_ready"]
