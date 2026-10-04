"""Native human-required policy stops simulated worker mechanics before dispatch."""

import ast
import json
from types import SimpleNamespace

import pytest
import yaml

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.review_packet_execution import automatic
from tests import test_execution_session as reference


@pytest.fixture(params=[False, True], ids=["ordinary", "complete-delivery"])
def incompatible(tmp_path, monkeypatch, request):
    original = reference.draft_fixture.__wrapped__

    def human_policy(folder, patched):
        root, control, proposal, normalized = original(folder, patched)
        path = root / "research.yml"
        config = yaml.safe_load(path.read_bytes())
        config["strict_evidence"]["human_review"] = True
        path.write_text(yaml.safe_dump(config, sort_keys=False))
        return root, control, proposal, normalized

    monkeypatch.setattr(reference, "draft_fixture", SimpleNamespace(__wrapped__=human_policy))
    values = reference.fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={
        "role_isolation": True, "coverage_examination": True, "simulated_model_usage": True,
        "review_delivery": request.param}))
    root, policy, execution, context = values
    tree = ast.parse(execution.executor._commands["researcher"][-1])
    printed = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "print")
    proposal = json.loads(ast.literal_eval(printed.args[0]))["proposal"]
    with context.activate(), Workspace.open(root) as workspace:
        value = workspace.mutations.prepare(request_id="unaccepted-admission", operation="strict.admit",
            arguments={"proposal": proposal})
        assert workspace.mutations.submit(value)["status"] == "committed"
    return *values, proposal["inventory"]["claims"][0]


def assert_incompatible(error):
    assert "POLICY_INCOMPATIBLE" in error.error_code or error.details.get("reason") == "human_review_required"


def test_actual_human_strict_policy_blocks_new_job_and_automatic_packet(incompatible, monkeypatch):
    root, policy, execution, context, claim = incompatible
    monkeypatch.setattr(execution.executor, "invoke", lambda *_args: pytest.fail("Incompatible policy dispatched a model"))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="incompatible-machine", execution=execution) as session:
            before = session.current()
            next_work = session.next()
            assert next_work["decision"]["outcome"] == "policy_incompatible"
            prior = execution.records(session)
            assert automatic(session, request_id="automatic-review", expected_basis_id=before["basis_id"]) is None
            assert execution.records(session) == prior
            prepared = workspace.strict.prepare_review(claim["id"])
            with pytest.raises(EvidenceWikiError) as caught:
                session.host_job(request_id="new-machine-review", operation="evaluate", payload={
                    "claim_id": claim["id"], "packet_id": native.digest(prepared["snapshot"])})
            assert_incompatible(caught.value)
            records = execution.records(session)
            assert not any(row["kind"] == "execution_attempt" for row in records)
            assert not any(row["kind"] == "execution_native" and row["data"]["operation"] == "review_pages.host_assembly" for row in records)
            step = session.advance(request_id="incompatible-stop", expected_basis_id=session.current()["basis_id"])
            assert step["outcome"] == "policy_incompatible" and step["terminal"]
            assert step["decision"]["outcome"] == "policy_incompatible"
            assert not any(row["kind"] == "execution_attempt" for row in execution.records(session))
            assert session.advance(request_id="incompatible-stop", expected_basis_id=step["before_basis_id"]) == {**step, "historical": True}
