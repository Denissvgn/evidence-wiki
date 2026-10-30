"""Configured reference processes exercise mechanics, never live model quality."""

import copy
import json
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace, calculation_agent
from evidence_wiki import execution_session as native
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.pack_discovery import owner
from tests import test_calculation_execution_fixture as configured


@pytest.mark.parametrize("model_meter", [True, False])
def test_actual_calculation_worker_reduces_native_value_and_requires_verifier(tmp_path, monkeypatch, model_meter):
    root, policy, execution, context = configured.fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"calculation":True, "role_isolation":True, "simulated_model_usage":model_meter}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="calculate", execution=execution) as session:
            arguments = {"request_id":"formula", "original_id":"q", "config_ref":"calculator"}
            if not model_meter:
                with pytest.raises(EvidenceWikiError) as refused:
                    calculation_agent.propose(session, **arguments)
                assert refused.value.details.get("reason") == "calculation_model_provenance_unknown", (
                    refused.value.error_code, refused.value.details)
                records = execution.records(session)
                assert len([row for row in records if row["kind"] == "execution_calculation_refused"]) == 1
                assert native.pending(session)["applied"] and not native.pending(session)["reservation_occupied"]
                with pytest.raises(EvidenceWikiError) as replay:
                    calculation_agent.propose(session, **arguments)
                assert replay.value.details["reason"] == "calculation_model_provenance_unknown"
                assert len(execution.records(session)) == len(records)
                return
            selected = calculation_agent.propose(session, **arguments)
            assert selected["selection"]["reference"]["expected"] == "6"
            assert selected["selection"]["reference"]["unit"] == "units"
            assert selected["accepted"] is False
            assert selected["verifier_status"] == "independent_current_v2_review_required"
            records = execution.records(session)
            attempts = [row for row in records if row["kind"] == "execution_attempt" and row["data"]["purpose"] == "calculation"]
            assert len(attempts) == 1 and attempts[0]["data"]["attempt"]["config_ref"] == "calculator"
            assert calculation_agent.propose(session, **arguments) == selected
            assert len([row for row in execution.records(session) if row["kind"] == "execution_attempt"]) == 1


def test_actual_independent_v2_verifier_and_wrong_native_value_refusal(tmp_path, monkeypatch):
    root, policy, execution, context = configured.fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"calculation":True, "role_isolation":True, "simulated_model_usage":True}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="quantitative", execution=execution) as session:
            selected = calculation_agent.propose(session, request_id="formula", original_id="q", config_ref="calculator")
            inventory = json.loads((root / "docs/research-claims.json").read_bytes())
            inferred = copy.deepcopy(inventory["claims"][0])
            inferred.update(id="derived", qualification="inference", premises=[inventory["claims"][0]["id"]],
                derivation="Apply the explicit configured multiplication.", text="Declared result is 6 units.", units="units",
                calculations=[selected["selection"]["reference"]])
            inventory["claims"].append(inferred)
            question = json.loads((root / "docs/research-requirements.json").read_bytes())["request"]["payload"]["questions"][0]
            proposal = {"schema_version":"evidence-draft-proposal/v1", "inventory":inventory,
                "originals":[{"original_id":question["id"], "text":question["text"], "question_slugs":["q"]}]}
            wrong = copy.deepcopy(proposal)
            wrong["inventory"]["claims"][-1]["calculations"][0]["expected"] = "600"
            before = owner("_evidence_revision").capture_workspace(root).revision_id
            with pytest.raises(EvidenceWikiError):
                workspace.strict.admit(wrong, request_id="wrong-value", expected_revision=workspace.mutations.revision()["revision_id"])
            assert owner("_evidence_revision").capture_workspace(root).revision_id == before
            admitted = workspace.strict.admit(proposal, request_id="native-value", expected_revision=workspace.mutations.revision()["revision_id"])
            assert admitted["status"] == "committed" and admitted["result"]["accepted"] is False
            with native.gate(session):
                view = native.current(session)
                attempt = native.reserve(session, "verify-computation", {"operation":"evaluate", "purpose":"evaluate",
                    "original_id":"q", "claim_id":"derived", "config_ref":"profile-a"}, view)
            assert attempt.job["schema_version"] == "research-host-job/v4"
            assert attempt.input_document["packet"]["schema_version"] == "research-review-context/v2"
            assert attempt.input_document["packet"]["manifest"]["context_complete"]
            assert attempt.job["controller_ref"] != execution.profile.role("calculator").controller_ref
            observation, reason = native._external(session, "invoke", attempt)
            assert reason is None
            observation = native.intake(session, observation, request_id="verifier-return")
            for index in range(32):
                if observation["state"] != "running":
                    break
                observation, reason = native._external(session, "poll", attempt)
                assert reason is None
                observation = native.intake(session, observation, request_id="verifier-poll-" + str(index))
            assert observation["state"] == "succeeded"
            assert execution.authenticate(observation, attempt) == observation
            assert observation["output"]["verdicts"]["units"] == "pass"
            assert not workspace.strict.check()["claims"][-1]["accepted"]


@pytest.mark.parametrize("preloaded", [False, True])
def test_automatic_calculation_generated_refs_and_native_independent_acceptance(tmp_path, monkeypatch, preloaded):
    root, policy, execution, context = configured.fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"calculation":True, "calculated_generation":True, "role_isolation":True,
            "simulated_model_usage":True, "preloaded_calculation_premise":preloaded}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="computed-cycle", execution=execution) as session:
            before = session.current()
            assert before["qualification"]["state"] == "qualified", before["qualification"]
            result = session.run_until_terminal(request_id="calculate-drive", expected_basis_id=before["basis_id"], max_steps=64)
            assert result["terminal"] and result["outcome"] == "accepted", (result, session.current()["canonical_release"], workspace.strict.check())
            claims = workspace.strict.check()["claims"]
            derived = next(row for row in claims if row["claim"]["id"] == "derived")
            assert derived["accepted"]
            assert derived["claim"]["calculations"][0]["expected"] == "6"
            records = execution.records(session)
            assert len([row for row in records if row["kind"] == "execution_calculation_selection"]) == 1
            attempts = [row["data"] for row in records if row["kind"] == "execution_attempt"]
            assert any(row["purpose"] == "calculation" for row in attempts)
            reviews = [row for row in attempts if row["purpose"] == "evaluate" and row.get("claim_id") == "derived"]
            assert len(reviews) == 2
            for row in reviews:
                assert row["attempt"]["job"]["schema_version"] == "research-host-job/v4"
                packet = row["attempt"]["input"]["packet"]
                assert packet["schema_version"] == "research-review-context/v2" and packet["manifest"]["context_complete"]
                assert packet["claim"]["calculations"][0]["expected"] == "6"
            count = len(attempts)
            before_new = len(records)
            with pytest.raises(EvidenceWikiError) as refused:
                session.calculate(request_id="new-after-terminal")
            assert refused.value.details["reason"] == "calculation_run_terminal"
            assert len(execution.records(session)) == before_new
            replay = session.run_until_terminal(request_id="calculate-drive", expected_basis_id=before["basis_id"], max_steps=64)
            assert replay["historical"] and replay["outcome"] == "accepted"
            assert len([row for row in execution.records(session) if row["kind"] == "execution_attempt"]) == count


def test_protected_authorized_capture_calculation_revalidates_original_host(tmp_path, monkeypatch):
    root, _policy, _execution, context = configured.fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"calculation":True, "role_isolation":True, "simulated_model_usage":True}))
    import yaml
    config = yaml.safe_load((root / "research.yml").read_bytes())
    with context.activate(), owner("_evidence_usage").current_view(root, config) as view:
        capture = owner("_evidence_revision").capture_workspace(root)
        direct = owner("_computation_runtime").evaluate(root, view=view)
        with capture.materialize() as retained, owner("_publication_context").authorized_capture(retained, config, view):
            result = owner("_computation_runtime").evaluate(retained)
            assert result["result_id"] == direct["result_id"] and result["status"] == "passed"
            assert result["graphs"]["worksheet"]["outputs"]["total"]["value"] == "6"


def test_invalid_authenticated_formula_is_settled_once_and_never_selected(tmp_path, monkeypatch):
    root, policy, execution, context = configured.fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"calculation":True, "role_isolation":True, "simulated_model_usage":True,
            "invalid_calculation":True}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="refused-formula", execution=execution) as session:
            arguments = dict(request_id="invalid", original_id="q", config_ref="calculator")
            with pytest.raises(EvidenceWikiError) as refused:
                session.propose_calculation(**arguments)
            assert refused.value.details["reason"] == "configured_formula_or_unit_changed"
            records = execution.records(session)
            assert native.pending(session)["applied"] and not native.pending(session)["reservation_occupied"]
            assert not any(row["kind"] == "execution_calculation_selection" for row in records)
            with pytest.raises(EvidenceWikiError) as repeated:
                session.propose_calculation(**arguments)
            assert repeated.value.details["reason"] == refused.value.details["reason"]
            assert len(execution.records(session)) == len(records)
            assert session.next()["actions"][0]["operation"] == "terminal"


def test_authenticated_generator_cannot_omit_selected_quantitative_intent(tmp_path, monkeypatch):
    root, policy, execution, context = configured.fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"calculation":True, "calculated_generation":True, "role_isolation":True,
            "simulated_model_usage":True, "omit_calculation_reference":True}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="omitted-value", execution=execution) as session:
            session.propose_calculation(request_id="formula", original_id="q", config_ref="calculator")
            before = (root / "docs/research-claims.json").read_bytes()
            with native.gate(session):
                view = native.current(session)
                attempt = native.reserve(session, "omit-value", {"operation":"generate", "purpose":"generate",
                    "original_id":"q", "config_ref":"researcher"}, view)
            observed, reason = native._external(session, "invoke", attempt)
            assert reason is None
            observed = native.intake(session, observed, request_id="omit-return")
            for index in range(32):
                if observed["state"] != "running":
                    break
                observed, reason = native._external(session, "poll", attempt)
                assert reason is None
                observed = native.intake(session, observed, request_id="omit-poll-" + str(index))
            assert observed["state"] == "succeeded" and observed["final_accounting"]
            assert execution.authenticate(observed, attempt) == observed
            with pytest.raises(EvidenceWikiError) as refused:
                native._apply(session, attempt.attempt_id)
            assert refused.value.details["reason"] == "selected_calculation_reference_missing"
            assert (root / "docs/research-claims.json").read_bytes() == before
            assert not any(row["kind"] == "execution_application" for row in execution.records(session))


def test_retained_formula_current_and_prewrite_never_call_host_authentication(tmp_path, monkeypatch):
    from evidence_wiki.calculation_session import stop
    root, policy, execution, context = configured.fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"calculation":True, "role_isolation":True, "simulated_model_usage":True}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="pure-current", execution=execution) as session:
            session.propose_calculation(request_id="formula", original_id="q", config_ref="calculator")
            def forbidden(*_args, **_kwargs):
                raise AssertionError("A retained native proof must not call the external host.")
            monkeypatch.setattr(execution.executor, "authenticate", forbidden)
            with native.gate(session):
                current = session.current()
                assert not current["accounting"]["original_outcomes"][0]["accepted"]
                assert "selected_calculation_reference_missing:q" in current["canonical_release"]["gaps"]
                with pytest.raises(EvidenceWikiError) as blocked:
                    stop(session, original_ids=["q"])
                assert blocked.value.details["reason"] == "selected_calculation_reference_missing"
                with pytest.raises(EvidenceWikiError) as terminal_blocked:
                    native._counter_stop(session, original_ids=["q"])
                assert terminal_blocked.value.details["reason"] == "selected_calculation_reference_missing"
