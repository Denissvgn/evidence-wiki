"""Actual owner effects must resume their original authenticated phase inputs."""

import json

import pytest

from evidence_wiki import Workspace
from evidence_wiki import acquisition_workflow as workflow
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_contracts import canonical, digest
from evidence_wiki.pack_discovery import owner
from tests import test_execution_session as execution_cases
from tests.test_acquisition_dispatch import opener, selected
from tests.test_draft_admission import update_policy
from tests.test_native_capture_revision import metadata


@pytest.fixture
def configured(tmp_path, monkeypatch):
    root, policy, execution, context = selected(execution_cases.fixture.__wrapped__(tmp_path, monkeypatch))
    path = root / "docs/research-requirements.json"
    declaration = json.loads(path.read_bytes())
    declaration["request"]["payload"]["budgets"].update(bytes=4096, downloads=2)
    path.write_bytes(canonical(declaration) + b"\n")
    update_policy(root)
    grants = context.grants | {"research_session.acquire_evidence", "acquisition_operations.prepare", "acquisition_operations.reconcile",
        "acquisition_operations.fulfill", "request_lifecycle_operations.fulfill_current", "source_requests.run_fulfill_request",
        "source_capture_operations.deliver"}
    context = HostContext([root], grants, context.actor_ref, [root], authority_file=context.authority_file,
        state_directory=None, mutation_control_directory=context.mutation_control_directory,
        controller_refs=context.controller_refs, role_controller_refs=context.role_controller_refs,
        credential_refs=context.credential_refs)
    return root, policy, execution, context


@pytest.mark.parametrize("phase", ["before_native_observation", "before_workflow_result"])
def test_fulfillment_owner_commit_crash_resumes_exact_phase_without_refetch(configured, monkeypatch, phase):
    root, policy, execution, context = configured
    transport = owner("_acquisition_transport")
    actual, calls = transport.execute_planned_request, []
    monkeypatch.setattr(transport, "execute_planned_request", opener(actual,
        b"Measured reflectance was 0.74 ratio with retained original context.\n", calls,
        root, context.mutation_control_directory))
    with context.activate(), Workspace.open(root) as workspace:
        source = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/source",
            rationale="Need scoped retained original", question_slugs=["q"], scope={"facet_id":"primary"})["request"]
        with workspace.research.session(policy, run_id="crash-workflow", execution=execution) as session:
            parameters = {"request_id":"journey", "source_request_id":source["request_id"], "expected_request_basis":digest(source),
                "metadata":metadata("plain_text", request_id=source["request_id"], scope={"facet_id":"primary"},
                    origin_url="https://example.org/source"), "path":"raw/web/acquired.txt"}
            append = workflow.native.append
            crashed = []

            def interrupted(session, kind, request_id, data):
                target = (phase == "before_native_observation" and kind == "execution_native_observation"
                    and (data.get("result") or {}).get("schema_version") == "evidence-acquisition-fulfillment/v1") or (
                    phase == "before_workflow_result" and kind == "execution_acquisition_workflow_result")
                if target and not crashed:
                    crashed.append(True)
                    raise RuntimeError("Lost return after actual fulfillment owner commit")
                return append(session, kind, request_id, data)

            with monkeypatch.context() as interrupt:
                interrupt.setattr(workflow.native, "append", interrupted)
                with pytest.raises(RuntimeError, match="Lost return"):
                    session.acquire_evidence(**parameters)
            assert crashed == [True]
            assert workspace.source_requests.current(request_ids=[source["request_id"]])["requests"][0]["recorded_status"] == "fulfilled"
            resumed = session.acquire_evidence(**parameters)
            assert resumed["status"] == "fulfilled"
            assert calls == ["https://example.org/source"]
            results = [row for row in execution.records(session) if row["kind"] == "execution_acquisition_workflow_result"]
            assert len(results) == 1
            assert session.acquire_evidence(**parameters) == resumed


@pytest.mark.parametrize("kind", ["html_notice", "declared_partial"])
def test_actual_unusable_shell_retains_partial_normalization_without_fulfillment(configured, monkeypatch, kind):
    root, policy, execution, context = configured
    transport = owner("_acquisition_transport")
    actual, calls = transport.execute_planned_request, []
    monkeypatch.setattr(transport, "execute_planned_request", opener(actual,
        (b"<html><body>Access denied. Please sign in to continue.</body></html>\n" if kind == "html_notice"
            else b"Measured reflectance was 0.74 ratio with retained partial context.\n"), calls,
        root, context.mutation_control_directory))
    with context.activate(), Workspace.open(root) as workspace:
        source = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/source",
            rationale="Need scoped retained original", question_slugs=["q"], scope={"facet_id":"primary"})["request"]
        with workspace.research.session(policy, run_id="partial-workflow", execution=execution) as session:
            parameters = {"request_id":"journey", "source_request_id":source["request_id"], "expected_request_basis":digest(source),
                "metadata":metadata("plain_text", request_id=source["request_id"], scope={"facet_id":"primary"},
                    origin_url="https://example.org/source", completeness="complete" if kind == "html_notice" else "partial"),
                "path":"raw/web/acquired.txt"}
            result = session.acquire_evidence(**parameters)
            assert result["status"] == "partial"
            assert result["source_accepted"] is False and result["semantic_adequacy"] == "not_evaluated"
            assert calls == ["https://example.org/source"]
            assert workspace.source_requests.current(request_ids=[source["request_id"]])["requests"][0]["recorded_status"] == "open"
            assert not any(row["kind"] == "execution_native" and row["data"]["operation"] == "acquisition.fulfill"
                for row in execution.records(session))
            repeated = session.acquire_evidence(**parameters)
            assert repeated == result and calls == ["https://example.org/source"]
