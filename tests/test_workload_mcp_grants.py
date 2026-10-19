"""Real startup grants and MCP gates, with downstream session storage stubbed."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from evidence_wiki import HostRuntime, ResearchExecution
from evidence_wiki.host_context import HostContext, current_context
from evidence_wiki.onboarding_mcp import OnboardingMcpServer
from tests.test_onboarding_mcp import initialize, message
from tests.test_review_dependency_workload import plan as plan
from tools import _review_dependency_workload as native


@pytest.mark.parametrize("selection", [None, 0, 1, "true", [], {}])
def test_mcp_startup_selection_requires_exact_boolean_before_effects(tmp_path, selection):
    base = tmp_path / "fixture"
    with pytest.raises(ValueError, match="^workload_mcp_selection_invalid$"):
        native.provision(base, {}, [], mcp=selection)
    assert not base.exists()


def test_fixture_mcp_grant_reaches_real_public_dispatch_without_broadening_other_authority(plan, tmp_path, monkeypatch):
    fixture = json.loads(Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).read_bytes())["fixture"]
    contexts = {}

    class ContextCaptured(Exception):
        pass

    def capture_context(*args, **kwargs):
        context = HostContext(*args, **kwargs)
        # The first context captures sources. Stop at the runtime context, before
        # admission, worker execution, qualification or any review work.
        if "authority_file" in kwargs:
            contexts[label] = context
            raise ContextCaptured
        return context

    monkeypatch.setattr(native, "HostContext", capture_context)
    for label, selection in (("default", {}), ("direct", {"mcp": False}), ("mcp", {"mcp": True})):
        with pytest.raises(ContextCaptured):
            native.provision(tmp_path / label, fixture, [], **selection)

    baseline = contexts["default"].grants
    assert contexts["direct"].grants == baseline
    assert contexts["mcp"].grants == baseline | {"onboarding.bootstrap"}
    assert not any(grant.startswith("onboarding.") for grant in baseline)
    assert "onboarding.bootstrap" not in native._example()[1]

    from evidence_wiki import research_session, review_page_session

    dispatched, assembled = [], []
    execution = object.__new__(ResearchExecution)
    root = contexts["mcp"].allowed_roots[0]
    policy = native._example()[5](root, "Assess retained synthetic records.")

    def session_binding(self, project_root, policy, *, run_id, execution):
        dispatched.append((project_root, run_id, execution, current_context()))
        self._root, self._execution, self._closed = project_root, execution, False
        self._owner_workspace = None

    def assemble(session, **arguments):
        assembled.append((session.root, arguments, current_context()))
        return {"fixture_dispatch_reached": True}

    # Keep real Workspace, command validation/dispatch, session owner boundary,
    # assembly owner boundary and HostContext checks. Stub only the downstream
    # session binding and review storage; no completed review is asserted here.
    monkeypatch.setattr(research_session.ResearchSession, "__init__", session_binding)
    monkeypatch.setattr(research_session.ResearchSession, "_check", lambda *args, **kwargs: None)
    monkeypatch.setattr(review_page_session, "assembled", assemble)

    def call(context, tool="research_assemble_review_packet", arguments=None):
        runtime = HostRuntime.from_services(policy, context, execution)
        server = OnboardingMcpServer(allowed_roots=[root], allow=["apply"], runtime=runtime,
            research_allow=["assemble-review-packet", "advance"])
        try:
            server.handle_message(initialize())
            server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
            selected = {"target": str(root), "run_id": "fixture-review", "request_id": "fixture-packet",
                "claim_id": "observed-capacity", "task_role": "evaluator", "page_chars": 16384} if arguments is None else arguments
            with context.activate():
                response = server.handle_message(message(2, "tools/call", {"name": tool, "arguments": selected}))
                assert current_context() is context
            return response
        finally:
            server.close()

    context = contexts["mcp"]
    missing = replace(context, grants=context.grants - {"onboarding.bootstrap"})
    refused = call(missing)
    assert refused["error"]["data"]["error_code"] == "HOST_CONTEXT_REFUSED"
    assert refused["error"]["data"]["details"]["reason"] == "host_operation_not_granted"
    assert dispatched == assembled == []

    response = call(context)
    assert response["result"]["isError"] is False
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["status"] == "returned"
    assert payload["result"] == {"fixture_dispatch_reached": True}
    assert dispatched == [(root, "fixture-review", execution, context)]
    assert assembled == [(root, {"request_id": "fixture-packet", "claim_id": "observed-capacity",
        "task_role": "evaluator", "page_chars": 16384}, context)]

    without_assembly = replace(context, grants=context.grants - {"research_session.assemble_review_packet"})
    refused = json.loads(call(without_assembly)["result"]["content"][0]["text"])
    assert refused["status"] == "refused"
    assert refused["error"]["error_code"] == "HOST_CONTEXT_REFUSED"
    assert len(assembled) == 1

    refused = call(context, "onboarding_apply", {"value": {}})
    assert refused["result"]["isError"] is True
    payload = json.loads(refused["result"]["content"][0]["text"])
    assert payload["error_code"] == "HOST_CONTEXT_REFUSED"
    assert payload["details"]["reason"] == "host_operation_not_granted"

    for supplied in ({"mcp": True}, {"grants": ["onboarding.bootstrap"]}):
        refused = call(missing, arguments={"target": str(root), "run_id": "fixture-review", **supplied})
        assert refused["error"]["data"]["error_code"] == "HOST_CONTEXT_REFUSED"
        assert missing.grants == context.grants - {"onboarding.bootstrap"}
    assert len(assembled) == 1
