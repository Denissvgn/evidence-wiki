"""Simulated meter verifies critic packet protocol, never real model semantics."""

from types import SimpleNamespace

from evidence_wiki import Workspace
from tests import test_coverage_session_reference as reference


def test_profile_complete_delivery_applies_to_owned_counter_examination(tmp_path, monkeypatch):
    root, policy, execution, context = reference.configured.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"review_delivery": True, "counter_disposition": "examined_no_unresolved_objection"}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="counter-pages", execution=execution) as session:
            reference.seed_current_review(session)
            result = session.examine_coverage(request_id="current-coverage", question_slug="q", config_ref="coverage-worker")
            assert result["status"] == "observed", result
            claim = __import__("json").loads((root / "docs/research-claims.json").read_bytes())["claims"][0]
            for index in range(16):
                basis = workspace.strict.prepare_review(claim["id"])["result"]["basis_id"]
                if any(row["kind"] == "execution_applied" and row["data"].get("operation") == "evaluate"
                        and row["data"].get("claim_id") == claim["id"] and row["data"].get("strict_basis_id") == basis
                        for row in execution.records(session)):
                    break
                step = session.advance(request_id="fresh-native-review-" + str(index), expected_basis_id=session.current()["basis_id"])
                assert not step["terminal"], step
            else:
                raise AssertionError("No current actual native review was retained within the bound.")
            value = session.examine_counterevidence(request_id="current-counter", question_slug="q", claim_id=claim["id"], config_ref="profile-b")
            assert value["status"] == "observed", value
            records = execution.records(session)
            row = next(row["data"] for row in records if row["kind"] == "execution_attempt" and row["data"].get("purpose") == "counterevidence")
            assert row["review_assembly_request_id"]
            supplied = row["attempt"]["input"]
            assert supplied["complete_review_delivery"]["packet"]["complete"]["complete"]
            assert supplied["packet"]["context_id"] == supplied["complete_review_delivery"]["packet"]["header"]["context_id"]
            before = execution.records(session)
            assert session.examine_counterevidence(request_id="current-counter", question_slug="q", claim_id=claim["id"], config_ref="profile-b") == value
            assert execution.records(session) == before
