"""Actual scoped proposer commands return inert choices under authenticated usage."""

from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki.acquisition_contracts import instruction
from evidence_wiki.host_context import HostContext
from tests import test_execution_session as mechanics
from tests.test_acquisition_dispatch import selected as canonical_workspace


@pytest.fixture
def proposer(tmp_path, monkeypatch):
    original=mechanics.fixture.__wrapped__(tmp_path,monkeypatch,SimpleNamespace(param={"acquisition_choice":True}))
    root, _modified_policy, execution, configured = canonical_workspace(original)
    # The original policy/profile are those used by actual calibration; workspace
    # instructions are separately prepared before binding the new native session.
    policy = original[1]
    context = HostContext([root], configured.grants | {"research_session.acquire", "research_session.choose_acquisition", "research_session.run_acquisition", "research_session.acquire_evidence",
        "acquisition_operations.prepare", "acquisition_operations.reconcile", "acquisition_operations.fulfill",
        "request_lifecycle_operations.fulfill_current", "source_requests.run_fulfill_request", "source_capture_operations.deliver",
        "request_lifecycle_operations.current"},
        configured.actor_ref, [root], authority_file=configured.authority_file,
        state_directory=None, mutation_control_directory=configured.mutation_control_directory,
        controller_refs=configured.controller_refs, role_controller_refs=configured.role_controller_refs)
    return root, policy, execution, context


def test_actual_scoped_researcher_returns_authenticated_inert_choice_and_replays(proposer):
    root, policy, execution, context = proposer
    with context.activate(), Workspace.open(root) as workspace:
        row = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/source",
            rationale="Need a permitted original", question_slugs=["q"], scope={"facet_id": "primary"})["request"]
        prepared = instruction(row, required_scope={}, alternatives=[{"alternative_id": "selected", "provider_id": "web",
            "resource_ref": "https://example.org/source", "scope": {"facet_id": "primary"}}])
        with workspace.research.session(policy, run_id="choice", execution=execution) as session:
            result = session.choose_acquisition(request_id="select",instruction=prepared,config_ref="researcher")
            assert result["status"] == "ready_for_owner_check", result
            assert result["model_invoked"] and not result["source_accepted"] and not result["rights_inferred"]
            records = execution.records(session)
            attempts = [record for record in records if record["kind"] == "execution_attempt"]
            assert len(attempts) == 1
            assert attempts[0]["data"]["attempt"]["job"]["required_capabilities"] == ["model", "acquisition.propose"]
            assert session.choose_acquisition(request_id="select",instruction=prepared,config_ref="researcher") == result
            assert len([record for record in execution.records(session) if record["kind"] == "execution_attempt"]) == 1
            assert not list((root / "wiki/claims").glob("*.md"))


def test_actual_agent_choice_and_native_capture_fulfillment_one_host_call(proposer,monkeypatch):
    from evidence_wiki.pack_discovery import owner
    from tests.test_acquisition_dispatch import opener
    from tests.test_native_capture_revision import metadata

    root,policy,execution,context=proposer
    transport=owner("_acquisition_transport")
    actual,calls=transport.execute_planned_request,[]
    with context.activate(),Workspace.open(root) as workspace:
        row=workspace.source_requests.add(kind="web",query_or_identifier="https://example.org/source",
            rationale="Need permitted original",question_slugs=["q"],scope={"facet_id":"primary"})["request"]
        prepared=instruction(row,required_scope={},alternatives=[{"alternative_id":"selected","provider_id":"web",
            "resource_ref":"https://example.org/source","scope":{"facet_id":"primary"}}])
        supplied=metadata("plain_text",request_id=row["request_id"],scope={"facet_id":"primary"},origin_url="https://example.org/source")
        with workspace.research.session(policy,run_id="agent-acquisition",execution=execution) as session:
            with monkeypatch.context() as fake:
                fake.setattr(transport,"execute_planned_request",opener(actual,b"Measured reflectance was 0.74 ratio with complete contextual original.\n",calls,root,context.mutation_control_directory))
                result=session.run_acquisition(request_id="journey",instruction=prepared,config_ref="researcher",metadata=supplied,path="raw/web/agent.txt")
                assert result["status"]=="fulfilled",result
                assert result["choice"]["model_invoked"] and not result["source_accepted"]
                repeated=session.run_acquisition(request_id="journey",instruction=prepared,config_ref="researcher",metadata=supplied,path="raw/web/agent.txt")
            assert repeated==result and calls==["https://example.org/source"]
            assert workspace.source_requests.current(request_ids=[row["request_id"]])["requests"][0]["current_ready"]
