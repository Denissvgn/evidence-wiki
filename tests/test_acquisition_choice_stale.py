"""A genuine scoped model cannot apply after its native request changes."""

import json

from evidence_wiki import Workspace
from evidence_wiki.acquisition_contracts import instruction
from evidence_wiki.pack_discovery import owner
from tests.test_acquisition_choice import proposer as proposer


def test_request_change_during_authenticated_model_run_is_inert_and_settled(proposer, monkeypatch):
    root, policy, execution, context = proposer
    with context.activate(), Workspace.open(root) as workspace:
        row = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/source",
            rationale="Need a permitted original", question_slugs=["q"], scope={"facet_id": "primary"})["request"]
        value = instruction(row, required_scope={}, alternatives=[{"alternative_id": "selected", "provider_id": "web",
            "resource_ref": "https://example.org/source", "scope": {"facet_id": "primary"}}])
        path = owner("source_requests").requests_path(root, owner("verify_quotes").load_config(root))
        actual = execution.executor.invoke
        def changed(attempt):
            observation = actual(attempt)
            rows = [json.loads(line) for line in path.read_bytes().splitlines()]
            rows[0]["rationale"] = "Changed while the genuine model was running"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            return observation
        monkeypatch.setattr(execution.executor, "invoke", changed)
        with workspace.research.session(policy, run_id="stale-during-model", execution=execution) as session:
            result = session.choose_acquisition(request_id="choice", instruction=value, config_ref="researcher")
            assert result["status"] == "blocked_external"
            assert result["reason"] in {"choice_basis_stale", "request_basis_changed"}
            assert not result["source_accepted"] and not list((root / "wiki/claims").glob("*.md"))
            records = execution.records(session)
            assert len([row for row in records if row["kind"] == "execution_attempt"]) == 1
            assert session.current()["execution"]["pending"]["applied"]
