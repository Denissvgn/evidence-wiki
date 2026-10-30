"""Independent actual proposer scope, stale use and durable application checks."""

import json
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace, acquisition_choice
from evidence_wiki import execution_session as native
from evidence_wiki.acquisition_contracts import instruction
from evidence_wiki.errors import UsageError
from evidence_wiki.host_context import HostContext
from evidence_wiki.pack_discovery import owner
from tests import test_execution_session as mechanics
from tests.test_acquisition_choice import proposer as proposer
from tests.test_acquisition_dispatch import selected
from tests.test_execution_session import fixture as fixture


def prepared(workspace):
    row = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/source",
        rationale="Need a permitted original", question_slugs=["q"], scope={"facet_id": "primary"})["request"]
    value = instruction(row, required_scope={}, alternatives=[{"alternative_id": "selected", "provider_id": "web",
        "resource_ref": "https://example.org/source", "scope": {"facet_id": "primary"}}])
    return row, value


def test_missing_actual_scoped_capability_never_dispatches(fixture, monkeypatch):
    root, policy, execution, configured = fixture
    context = HostContext([root], configured.grants | {"research_session.acquire"}, configured.actor_ref, [root],
        authority_file=configured.authority_file, state_directory=configured.state_directory,
        mutation_control_directory=configured.mutation_control_directory, controller_refs=configured.controller_refs,
        role_controller_refs=configured.role_controller_refs)
    monkeypatch.setattr(execution.executor, "invoke", lambda *_: pytest.fail("Missing scope dispatched model"))
    with context.activate(), Workspace.open(root) as workspace:
        value = instruction({"request_id": "unobserved", "status": "open", "query_or_identifier": "https://example.org/source",
            "scope": {"facet_id": "primary"}}, required_scope={}, alternatives=[])
        with workspace.research.session(policy, run_id="no-proposer", execution=execution) as session:
            result = acquisition_choice.choose(session, request_id="choice", instruction=value, config_ref="researcher")
            assert result["status"] == "blocked_external" and not result["model_invoked"]
            assert not execution.records(session)


def test_changed_actual_request_stops_before_model_invocation(proposer, monkeypatch):
    root, policy, execution, context = proposer
    with context.activate(), Workspace.open(root) as workspace:
        row, value = prepared(workspace)
        path = owner("source_requests").requests_path(root, owner("verify_quotes").load_config(root))
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        rows[0]["rationale"] = "Changed retained request"
        path.write_text("".join(json.dumps(item) + "\n" for item in rows))
        monkeypatch.setattr(execution.executor, "invoke", lambda *_: pytest.fail("Stale request dispatched model"))
        with workspace.research.session(policy, run_id="stale-choice", execution=execution) as session:
            result = session.choose_acquisition(request_id="choice", instruction=value, config_ref="researcher")
            assert result["status"] == "blocked_external" and result["reason"] == "request_basis_changed"
            assert not any(item["kind"] == "execution_attempt" for item in execution.records(session))


def test_non_scalar_connector_target_cannot_be_applied_as_choice(proposer):
    root, policy, execution, context = proposer
    with context.activate(), Workspace.open(root) as workspace:
        _row, value = prepared(workspace)
        value["alternatives"][0]["alternative_id"] = {"command": "ungranted-execution"}
        with workspace.research.session(policy, run_id="hostile-choice", execution=execution) as session:
            with pytest.raises(UsageError) as refusal:
                session.choose_acquisition(request_id="choice", instruction=value, config_ref="researcher")
            assert refusal.value.error_code == "ONBOARDING_INVALID"
            assert refusal.value.details["field"] == "/alternatives/0/alternative_id"
            assert not execution.records(session)


def test_result_before_applied_crash_finishes_same_attempt_without_second_model_call(proposer, monkeypatch):
    root, policy, execution, context = proposer
    with context.activate(), Workspace.open(root) as workspace:
        _row, value = prepared(workspace)
        with workspace.research.session(policy, run_id="application-crash", execution=execution) as session:
            actual = native.append
            def interrupted(session, kind, *args, **kwargs):
                if kind == "execution_applied":
                    raise RuntimeError("Interrupted before applied marker")
                return actual(session, kind, *args, **kwargs)
            with monkeypatch.context() as faults:
                faults.setattr(native, "append", interrupted)
                with pytest.raises(RuntimeError):
                    session.choose_acquisition(request_id="choice", instruction=value, config_ref="researcher")
            attempts = [item for item in execution.records(session) if item["kind"] == "execution_attempt"]
            assert len(attempts) == 1
            result = session.choose_acquisition(request_id="choice", instruction=value, config_ref="researcher")
            assert result["status"] == "ready_for_owner_check", result
            records = execution.records(session)
            assert len([item for item in records if item["kind"] == "execution_attempt"]) == 1
            assert len([item for item in records if item["kind"] == "execution_applied"]) == 1
            conflict = session.choose_acquisition(request_id="choice", instruction={**value, "query_or_identifier": "Changed caller label"}, config_ref="researcher")
            assert conflict["status"] == "blocked_external" and conflict["reason"] == "request_conflict"
            assert len([item for item in execution.records(session) if item["kind"] == "execution_attempt"]) == 1
            assert not list((root / "wiki/claims").glob("*.md"))


def test_authenticated_hostile_output_is_durably_settled_without_admission(tmp_path, monkeypatch):
    original = mechanics.fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"acquisition_choice": True, "acquisition_choice_hostile": True}))
    root, _modified_policy, execution, configured = selected(original)
    policy = original[1]
    context = HostContext([root], configured.grants | {"research_session.acquire", "research_session.choose_acquisition",
        "request_lifecycle_operations.current"}, configured.actor_ref, [root], authority_file=configured.authority_file,
        state_directory=None, mutation_control_directory=configured.mutation_control_directory,
        controller_refs=configured.controller_refs, role_controller_refs=configured.role_controller_refs)
    with context.activate(), Workspace.open(root) as workspace:
        _row, value = prepared(workspace)
        with workspace.research.session(policy, run_id="invalid-output", execution=execution) as session:
            result = session.choose_acquisition(request_id="choice", instruction=value, config_ref="researcher")
            assert result["status"] == "blocked_external" and result["reason"] == "choice_invalid", result
            assert result["model_invoked"] and not result["source_accepted"] and not result["rights_inferred"]
            records = execution.records(session)
            attempts = [item for item in records if item["kind"] == "execution_attempt"]
            assert len(attempts) == 1
            observed = native._latest_observation(records, attempts[0]["data"]["attempt"]["attempt_id"])
            assert observed["state"] == "succeeded" and observed["final_accounting"]
            assert observed["output"] == {"argv": ["arbitrary-tool"], "authorized": True}
            assert len([item for item in records if item["kind"] == "execution_acquisition_choice_result"]) == 1
            assert len([item for item in records if item["kind"] == "execution_applied"]) == 1
            assert session.current()["execution"]["pending"]["applied"]
            assert not list((root / "wiki/claims").glob("*.md"))
            before = len(records)
            monkeypatch.setattr(execution.executor, "invoke", lambda *_: pytest.fail("Invalid-output replay dispatched a second model"))
            assert session.choose_acquisition(request_id="choice", instruction=value, config_ref="researcher") == result
            assert len(execution.records(session)) == before
