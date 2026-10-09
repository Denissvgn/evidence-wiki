"""Native finite semantic conformance with explicitly synthetic host observations."""

from __future__ import annotations

import copy
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import (
    CalibratedEvaluators,
    ExecutionRole,
    ResearchExecution,
    RunPolicy,
    Workspace,
    calibration_probes,
)
from evidence_wiki import execution_session as driver
from evidence_wiki import semantic_judging as judging
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.disagreement import semantic_adjudication_action
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.finite_domain import observe_domain
from evidence_wiki.pack_discovery import owner
from evidence_wiki.qualification_calibration import digest
from evidence_wiki.semantic_contracts import DIMENSIONS, checked_findings
from tests._execution_fixture import KEYS
from tests._semantic_fixture import ORIGINAL, REVIEWERS, native_fixture
from tests.test_agent_execution import attempt as protocol_attempt
from tests.test_agent_execution import executor as protocol_executor
from tests.test_agent_execution import final as protocol_final
from tests.test_agent_execution import profile as protocol_profile
from tests.test_disagreement import gates
from tests.test_finite_domain import inputs as finite_inputs
from tests.test_semantic_contracts import findings, worker_input


def record_acceptance_owner(request, owner_name, result):
    plugin = request.config.pluginmanager.get_plugin("acceptance-records")
    if plugin is not None:
        plugin.record_owner_result(request.node.nodeid, owner_name, result)


def journal_digest(records):
    """Commit every authenticated record in order without nesting its payload again."""
    return digest({"record_count": len(records), "ordered_record_sha256": [digest(record) for record in records]})


@pytest.fixture(scope="module")
def accepted_native(tmp_path_factory):
    """Pay protected native setup once for the related boundary assertions."""
    with pytest.MonkeyPatch.context() as patch:
        root, policy, execution, context = native_fixture(tmp_path_factory.mktemp("semantic-native"), patch)
        with context.activate(), Workspace.open(root) as workspace:
            with workspace.research.session(policy, run_id="semantic-native", execution=execution) as session:
                before = session.current()
                unassessed = session.semantic_report()
                result = session.run_until_terminal(request_id="drive", expected_basis_id=before["basis_id"], max_steps=64)
                assert result["terminal"] and result["outcome"] == "accepted", _terminal_diagnostics(session, result)
                return SimpleNamespace(root=root, policy=policy, execution=execution, context=context,
                    before=before, unassessed=unassessed, result=result)


@pytest.fixture
def accepted_session(accepted_native, request):
    selected = accepted_native
    with selected.context.activate(), Workspace.open(selected.root) as workspace:
        with workspace.research.session(selected.policy, run_id="semantic-native", execution=selected.execution) as session:
            calibration_probes.refresh(session, "current-" + digest(request.node.nodeid)[:16])
            yield selected, session


def _semantic_rows(execution, session):
    return [row["data"] for row in execution.records(session)
        if row["kind"] == "execution_attempt" and "semantic_binding" in row["data"]]


def _terminal_diagnostics(session, result):
    """Expose native failure metadata without outputs, credentials or signing data."""
    records = session.execution.records(session)
    probes = [row["data"]["probe"] for row in records if row["kind"] == "execution_qualification_probe"]
    observations = [row["data"]["observation"] for row in records if row["kind"] == "execution_observation"]
    failures = [row["data"] for row in records if row["kind"] == "execution_application_failed"]
    view = session.current()
    return json.dumps({
        "terminal": {key: result.get(key) for key in ("outcome", "reason", "canonical_run_state", "action")},
        "qualification": {key: view["qualification"].get(key) for key in ("state", "reason")},
        "latest_probe": None if not probes else {key: probes[-1].get(key) for key in ("reasons", "configuration_reasons")},
        "attempts": [{key: row.get(key) for key in ("attempt_id", "config_ref", "state", "reason")}
            for row in observations[-8:]],
        "application_failures": [{key: row.get(key) for key in ("operation", "attempt_id", "error_code", "owner_reason")}
            for row in failures[-4:]],
    }, sort_keys=True)


def test_public_native_three_fresh_passes_advisory_report_and_historical_replay(accepted_session, monkeypatch):
    selected, session = accepted_session
    execution = selected.execution
    report = session.semantic_report()
    assert execution.profile.document()["schema_version"] == "research-execution-profile/v13"
    assert execution.qualifications._legacy is None
    assert len(json.loads(execution.qualifications._records)) == 4
    records = execution.records(session)
    rows = _semantic_rows(execution, session)
    first = [row for row in rows if row["purpose"] == "evaluate"]
    advisory = [row for row in rows if row["purpose"] == "semantic_adjudicate"]
    assert len(first) == 3 and len(advisory) == 1
    assert {row["semantic_binding"]["role"] for row in first} == {"evidence", "scope_units", "counterevidence"}
    assert len({row["semantic_binding"]["basis_id"] for row in rows}) == 1
    assert len({row["attempt"]["input_sha256"] for row in first}) == 3
    with driver._current_facts(session) as current:
        source_bodies = {row["record_sha256"]: row["content"] for row in current.packets}
        candidate = owner("strict_evidence").prepare_review(selected.root, "observation")["snapshot"]["claim"]
        for row in first:
            attempt = execution.attempt(row["attempt"])
            envelope = attempt.worker_document()
            captured = json.loads((selected.root.parent / "workers" / (attempt.config_ref + "-input.json")).read_bytes())
            assert captured == envelope
            assert attempt.job["original_text"] == ORIGINAL
            supplement = envelope["input"]["semantic"]
            assert supplement["original_text"] == ORIGINAL
            assert supplement["required_units"] == [{"unit_id": "unit-1", "text": ORIGINAL}]
            assert supplement["candidate_behavior"] == "asserted"
            assert supplement["units"]["support"] == [{"unit_id": "claim-1", "applicable": True}]
            assert supplement["units"]["missing_material_evidence"] == [{"unit_id": "unit-1", "applicable": True}]
            assert supplement["units"]["appropriate_abstention"] == [{"unit_id": "original-1", "applicable": False}]
            assert supplement["units"]["unnecessary_refusal"] == [{"unit_id": "original-1", "applicable": False}]
            assert {source["record_sha256"]: source["content"] for source in supplement["sources"]} == source_bodies
            assert set(supplement["candidate_units"][0]) == {"text", "scope", "time", "units", "evidence"}
            assert all(supplement["candidate_units"][0][key] == candidate[key] for key in ("text", "scope", "time", "units"))
            assert supplement["candidate_units"][0]["evidence"][0]["source_id"] == supplement["sources"][0]["source_id"]
            assert supplement["sources"][0]["source_id"].startswith("source-")
            assert row["semantic_binding"]["units"] == {"unit-1": "q"}
            data = json.dumps(envelope)
            assert not any(key in data for key in (*KEYS.values(), *(item[-1] for item in REVIEWERS)))
            assert "expected_label" not in data and "first_passes" not in envelope["input"]
            assert "prior_verdicts" not in data and "generator" not in envelope["input"]["packet"]
            assert "synthetic/finite-generator-process" not in data and '"runner"' not in data
            assert not any(config in data for config, *_ in REVIEWERS)
            assert "observation" != envelope["input"]["packet"]["claim"]["id"]
        advisory_input = execution.attempt(advisory[0]["attempt"]).input_document
        assert advisory_input["semantic"]["role"] == "adjudicator"
        assert {row["role"] for row in advisory_input["first_passes"]} == {"evidence", "scope_units", "counterevidence"}
        for prior in advisory_input["first_passes"]:
            original_row = next(row for row in first if row["semantic_binding"]["role"] == prior["role"])
            output = next(row["data"]["semantic_assessment"]["findings"] for row in records
                if row["kind"] == "execution_application" and row["data"]["attempt_id"] == original_row["attempt"]["attempt_id"])
            assert prior["findings"]["verdicts"] == output["verdicts"]
            assert prior["findings"]["rationale"] == output["rationale"].replace(original_row["attempt"]["config_ref"], "[identity-masked]")
            checked_findings(prior["findings"], execution.attempt(original_row["attempt"]).input_document)
            assert not any(config in json.dumps(prior) for config, *_ in REVIEWERS)
        current.require(session)
    assert report["qualification"]["state"] == "qualified"
    assert report["counts"]["expected"] == report["counts"]["completely_adjudicated"] == 1
    assert report["counts"]["missing"] == report["counts"]["incomplete"] == 0
    assessment = report["assessments"][0]
    assert assessment["current"] and assessment["scorable"]
    assert len(assessment["first_passes"]) == 3 and assessment["adjudication"] is not None
    assert assessment["reference"]["tier"] == "executable_oracle" and assessment["reference"]["label"] == "accept"
    for dimension in DIMENSIONS[:3]:
        assert report["dimensions"][dimension]["score"] == 1
        assert report["dimensions"][dimension]["denominator"] == 1
    for dimension in DIMENSIONS[3:]:
        assert report["dimensions"][dimension]["score"] is None
        assert report["dimensions"][dimension]["denominator"] == 0
    assert not report["authorized"] and not report["evidence_accepted"] and not report["accuracy_claim"]
    assert session.current()["canonical_release"]["research_complete"]
    before_records = execution.records(session)
    before_accounting = session.report()["accounting"]
    before_steps = session.current()["execution"]["steps_used"]
    monkeypatch.setattr(execution.executor, "invoke", lambda *_: pytest.fail("Historical replay dispatched a worker."))
    monkeypatch.setattr(execution.executor, "poll", lambda *_: pytest.fail("Historical replay polled a worker."))
    monkeypatch.setattr(execution.executor, "qualification_configuration", lambda *_: pytest.fail("Historical replay redispatched a probe."))
    replay = session.run_until_terminal(request_id="drive", expected_basis_id=selected.before["basis_id"], max_steps=64)
    assert replay["historical"] and replay["outcome"] == "accepted"
    assert execution.records(session) == before_records
    assert session.current()["execution"]["steps_used"] == before_steps
    assert session.report()["accounting"] == before_accounting


def test_unassessed_original_retains_unknown_behavior_and_material_denominators(accepted_native):
    report = accepted_native.unassessed
    assert report["counts"]["expected"] == report["counts"]["missing"] == 1
    assert not report["assessments"]
    for dimension in DIMENSIONS:
        assert report["dimensions"][dimension]["denominator"] == 1
        assert report["dimensions"][dimension]["unknown"] == 1
        assert report["dimensions"][dimension]["score"] is None


@pytest.mark.parametrize("change", ["original", "required_unit", "source", "closure", "candidate"])
def test_current_application_refuses_original_unit_source_and_candidate_drift(accepted_session, change, request):
    selected, session = accepted_session
    row = next(row for row in _semantic_rows(selected.execution, session) if row["purpose"] == "evaluate")
    attempt = selected.execution.attempt(row["attempt"])
    before_records = journal_digest(selected.execution.records(session))
    with driver._current_facts(session) as current:
        view, packets = copy.deepcopy(current.view), copy.deepcopy(current.packets)
        observed = SimpleNamespace(requirements=copy.deepcopy(current.observed.requirements), strict=copy.deepcopy(current.observed.strict))
        prepared = owner("strict_evidence").prepare_review(selected.root, row["claim_id"])
        if change == "original":
            view["accounting"]["original_outcomes"][0]["original_text"] += " Changed exact original."
        elif change == "required_unit":
            observed.requirements["request"]["payload"]["questions"][0]["text"] += " Changed required text."
        elif change == "source":
            packets[0]["content"] += "\n"
        elif change == "closure":
            packets[0]["source_binding"]["closure_id"] = "sha256:" + "f" * 64
        else:
            observed.strict["result"]["claims"][0]["claim"]["text"] += " Changed candidate."
        with pytest.raises(EvidenceWikiError) as refused:
            judging.require_current(session, row, attempt, view, prepared, packets, observed)
        assert refused.value.error_code == "RESEARCH_SEMANTIC_BASIS_CHANGED"
        current.require(session)
        assert journal_digest(selected.execution.records(session)) == before_records
        input_parts = {"view": view, "packets": packets, "prepared": prepared,
                       "requirements": observed.requirements, "strict": observed.strict}
        input_commitments = {key: digest(value) for key, value in input_parts.items()}
        record_acceptance_owner(request, "semantic_judging.require_current", {
            "attempt_id": attempt.attempt_id, "claim_id": row["claim_id"], "change": change,
            "workspace": str(selected.root), "basis_id": current.view["basis_id"],
            "input_sha256": digest(input_commitments), "input_parts_sha256": input_commitments,
            "input_commitment": "named-native-input-digests/v1",
            "error_code": refused.value.error_code, "reason": refused.value.details,
            "before_records_sha256": before_records,
            "after_records_sha256": journal_digest(selected.execution.records(session)),
            "records_commitment": "ordered-native-record-digests/v1",
            "accepted": False,
        })


def test_forged_exact_source_anchor_is_refused_before_native_review_application(accepted_session, request):
    selected, session = accepted_session
    row = next(row for row in _semantic_rows(selected.execution, session) if row["purpose"] == "evaluate")
    attempt = selected.execution.attempt(row["attempt"])
    records = selected.execution.records(session)
    output = copy.deepcopy(next(item["data"]["semantic_assessment"]["findings"] for item in records
        if item["kind"] == "execution_application" and item["data"]["attempt_id"] == attempt.attempt_id))
    output["checks"]["support"][0]["anchors"][0]["quote"] = "A forged contrary observation."
    with driver._current_facts(session) as current:
        prepared = owner("strict_evidence").prepare_review(selected.root, row["claim_id"])
        with pytest.raises(ExecutionError, match="anchor_quote_invalid") as refused:
            judging.assess(session, row, attempt, output, current.view, prepared, current.packets, current.observed)
        current.require(session)
    assert selected.execution.records(session) == records
    record_acceptance_owner(request, "semantic_judging.assess", {
        "attempt_id": attempt.attempt_id, "claim_id": row["claim_id"],
        "workspace": str(selected.root), "input_sha256": digest(output),
        "reason": str(refused.value), "exception_type": type(refused.value).__name__,
        "before_records_sha256": journal_digest(records),
        "after_records_sha256": journal_digest(selected.execution.records(session)),
        "records_commitment": "ordered-native-record-digests/v1", "accepted": False,
    })


@pytest.mark.parametrize("condition", ["absent", "expired", "out_of_domain"])
def test_actual_calibration_registry_blocks_unknown_or_unqualified_judging(accepted_session, condition):
    selected, session = accepted_session
    registry = selected.execution.qualifications
    with driver._current_facts(session) as current:
        rubric = current.observed.policy["rubric"]
        sources = copy.deepcopy(current.packets)
        if condition == "out_of_domain":
            sources[0]["content"] = "An unrestricted prose claim is outside the finite language."
        domain = observe_domain(originals=current.view["accounting"]["original_outcomes"],
            strict_claims=[row["claim"] for row in current.observed.strict["result"]["claims"]], rubric=rubric, sources=sources)
        probe = copy.deepcopy(next(row["data"]["probe"] for row in reversed(current.records)
            if row["kind"] == "execution_qualification_probe"))
        now = datetime.now(timezone.utc).isoformat()
        if condition == "absent":
            registry = CalibratedEvaluators([], registry.authority)
            probe["registry_sha256"] = registry.sha256
        elif condition == "expired":
            now = max(row["expires_utc"] for row in json.loads(registry._records))
            probe["observed_utc"] = now
            probe["expires_utc"] = (datetime.fromisoformat(now) + timedelta(seconds=20)).isoformat()
        profiles = registry.observations(selected.policy, selected.execution.profile, now, rubric["id"],
            rubric_sha256=digest(rubric), rubric=rubric, domain=domain, probe=probe)
        assert len(profiles) == 3 and all(row["qualified"] is None for row in profiles)
        view = copy.deepcopy(current.view)
        view["qualification"].update(state="unknown", profiles=profiles)
        for config, task_role, *_ in REVIEWERS:
            if task_role != "adjudicator":
                with pytest.raises(EvidenceWikiError) as refused:
                    judging.judge_metadata(session, view, config)
                assert refused.value.error_code == "RESEARCH_SEMANTIC_QUALIFICATION_UNKNOWN"
        current.require(session)


def test_human_required_policy_returns_incompatible_without_operator_or_dispatch(accepted_native, monkeypatch):
    selected = accepted_native
    supplied = selected.policy.to_dict()
    supplied["policy_id"] = None
    supplied["required_evaluators"][0]["human_required"] = True
    human_policy = RunPolicy(supplied)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(selected.execution.executor, "invoke", lambda *_: pytest.fail("Incompatible policy dispatched a worker."))
    monkeypatch.setattr(selected.execution.executor, "qualification_configuration", lambda *_: pytest.fail("Incompatible policy dispatched a probe."))
    with selected.context.activate(), Workspace.open(selected.root) as workspace:
        with workspace.research.session(human_policy, run_id="semantic-human-required", execution=selected.execution) as session:
            result = session.run_until_terminal(request_id="drive", expected_basis_id=session.current()["basis_id"], max_steps=8)
            assert result["terminal"] and result["outcome"] == "policy_incompatible"
            assert result["reason"] == "human_review_required"
            assert not any(row["kind"] == "execution_attempt" for row in selected.execution.records(session))


def test_public_absent_protected_records_block_judging_without_worker_dispatch(accepted_native, monkeypatch):
    selected = accepted_native
    execution = ResearchExecution(selected.execution.executor,
        qualifications=CalibratedEvaluators([], selected.execution.qualifications.authority),
        review_signer=selected.execution.review_signer, observer_key=b"j" * 32)
    monkeypatch.setattr(execution.executor, "invoke", lambda *_: pytest.fail("Unqualified registry dispatched a worker."))
    with selected.context.activate(), Workspace.open(selected.root) as workspace:
        with workspace.research.session(selected.policy, run_id="semantic-absent-records", execution=execution) as session:
            result = session.run_until_terminal(request_id="drive", expected_basis_id=session.current()["basis_id"], max_steps=8)
            assert result["terminal"] and result["outcome"] == "blocked_external"
            assert result["reason"] == "qualification_unknown"
            assert not any(row["kind"] == "execution_attempt" for row in execution.records(session))


def test_public_finite_wrong_value_cannot_release_synthetic_model_passes(tmp_path, monkeypatch, request):
    root, policy, execution, context = native_fixture(tmp_path, monkeypatch, semantic_wrong_value=True)
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="semantic-wrong-value", execution=execution) as session:
            basis = session.current()["basis_id"]
            result = session.run_until_terminal(request_id="drive", expected_basis_id=basis, max_steps=64)
            assert result["terminal"] and result["outcome"] != "accepted"
            records = execution.records(session)
            assert any(row["kind"] == "execution_application_failed"
                and "SEMANTIC_REFERENCE_PASS_CONFLICT" in row["data"]["error_code"] for row in records)
            proposed = [row["data"]["observation"]["output"] for row in records
                if row["kind"] == "execution_observation" and (row["data"]["observation"].get("output") or {}).get("schema_version") == "research-semantic-findings/v1"]
            assert proposed and all(set(output["verdicts"].values()) == {"pass"} for output in proposed)
            assert session.current()["accounting"]["accepted_count"] == 0
            assert not session.current()["canonical_release"]["research_complete"]
            report = session.semantic_report()
            assert report["dimensions"]["support"]["score"] is None
            assert report["dimensions"]["support"]["unknown"] == 1
            for dimension in DIMENSIONS[3:]:
                assert report["dimensions"][dimension]["denominator"] == 0
            count = len(records)
            replay = session.run_until_terminal(request_id="drive", expected_basis_id=basis, max_steps=64)
            assert replay["historical"] and replay["outcome"] == result["outcome"]
            assert len(execution.records(session)) == count
            record_acceptance_owner(request, "ResearchSession.run_until_terminal", {
                "workspace": str(root),
                "result": {key: result[key] for key in ("outcome", "reason", "terminal", "historical")},
                "replay": {key: replay[key] for key in ("outcome", "reason", "terminal", "historical")},
                "result_sha256": digest(result), "replay_sha256": digest(replay),
                "basis_id": session.current()["basis_id"],
                "proposed_output_sha256": [digest(output) for output in proposed],
                "all_proposed_verdicts_pass": True,
                "reference_conflict_observed": True, "accepted_count": 0,
                "research_complete": False, "records_count": count,
                "records_sha256": journal_digest(execution.records(session)),
                "records_commitment": "ordered-native-record-digests/v1",
                "support_score": report["dimensions"]["support"]["score"],
                "support_unknown": report["dimensions"]["support"]["unknown"],
                "native_workers": "synthetic",
            })


def test_public_unknown_advisory_cannot_release_after_three_native_passes(tmp_path, monkeypatch):
    root, policy, execution, context = native_fixture(tmp_path, monkeypatch, semantic_advisory="unknown")
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="semantic-unknown-advisory", execution=execution) as session:
            result = session.run_until_terminal(request_id="drive", expected_basis_id=session.current()["basis_id"], max_steps=64)
            assert result["terminal"] and result["outcome"] == "abstained"
            assert result["reason"] == "review_disagreement"
            records = execution.records(session)
            applications = [row["data"] for row in records if row["kind"] == "execution_application" and "semantic_assessment" in row["data"]]
            assert len([row for row in applications if row["operation"] == "evaluate" and row["passed"]]) == 3
            assert len([row for row in applications if row["operation"] == "adjudicate"]) == 1
            assert not session.current()["canonical_release"]["research_complete"]
            report = session.semantic_report()
            assert report["dimensions"]["support"]["unknown"] == 1
            assert report["dimensions"]["support"]["score"] is None
            for dimension in DIMENSIONS[3:]:
                assert report["dimensions"][dimension]["denominator"] == 0
            assert report["originals"][0]["original_text_sha256"] == "sha256:" + digest(ORIGINAL)


@pytest.mark.parametrize("boundary", ["missing_first_pass", "unqualified_adjudicator", "claim_allowance", "run_budget"])
def test_advisory_absence_qualification_and_shared_allowances_do_not_return_release(accepted_native, boundary):
    selected = accepted_native
    basis = "sha256:" + "a" * 64
    reviews = [{"review_ref": config + "-review", "profile_ref": config, "role": task_role,
        "controller_ref": controller, "basis_id": basis,
        "verdicts": dict.fromkeys(("support", "source_suitability", "scope", "time", "units", "counterevidence"), "pass"),
        "objection_refs": []} for config, task_role, controller, *_ in REVIEWERS if task_role != "adjudicator"]
    bounded = gates()
    ready = semantic_adjudication_action(policy=selected.policy, claim_id="observation", basis_id=basis,
        generator_controller_ref="runner", reviews=reviews, history=[], gates=bounded)
    assert ready["operation"] == "adjudicate"
    if boundary == "missing_first_pass":
        reviews.pop()
    elif boundary == "unqualified_adjudicator":
        bounded["adjudicator_available"] = False
    elif boundary == "claim_allowance":
        bounded["run_adjudication_cycles"] = selected.policy.to_dict()["continuation_limits"]["adjudication_cycles"]
    else:
        bounded["budget_available"] = False
    result = semantic_adjudication_action(policy=selected.policy, claim_id="observation", basis_id=basis,
        generator_controller_ref="runner", reviews=reviews, history=[], gates=bounded)
    assert result["operation"] in {"collect_first_passes", "terminal"}
    assert not result["authorized"] and not result["evidence_accepted"]


def test_historical_native_judges_withhold_content_and_keep_execution_commitments(accepted_session):
    _, session = accepted_session
    rows = judging._assessments(session)
    assert len(rows) == 4
    for row in rows:
        current = judging._public_judge(row)
        historical = judging._public_judge(row, content=False)
        assert any(current["checks"].values())
        assert all(not value for value in historical["checks"].values())
        assert all(metric["score"] is None and metric["denominator"] == 0
            for metric in historical["metrics"].values())
        for name in ("profile_ref", "controller_ref", "qualification_ref", "input_ref", "output_ref", "observation_ref"):
            assert historical[name] == current[name]
        assert "Exact supplied field" not in json.dumps(historical)


def test_advisory_native_observation_cannot_follow_a_different_first_pass_epoch(accepted_session):
    _, session = accepted_session
    rows = judging._assessments(session)
    first = {row["assessment"]["role"]: row for row in rows if not row["adjudication"]}
    assert len(judging._completed(rows, first)) == 1
    changed = copy.deepcopy(first)
    changed["evidence"]["observation"]["nonce"] = "different-first-pass-epoch"
    assert judging._completed(rows, changed) == []


def _failed_start_receipts(tmp_path):
    """Actual process failure supplies a start receipt without a review result."""
    profile = protocol_profile(roles=(ExecutionRole("researcher", "evaluator", "registered-controller",
        "local-process", "prompt/v1"),))
    attempt = protocol_attempt(profile, job_changes={"operation": "evaluate", "role": "evaluator",
        "payload": {"claim_id": "claim-1", "packet_id": "packet-1"}, "original_text": None})
    host = protocol_executor(tmp_path, "raise RuntimeError('Controlled worker failure.')", profile)
    host.invoke(attempt)
    observation = host.authenticate(protocol_final(host, attempt), attempt)
    assert observation["state"] == "failed" and observation["started_utc"] is not None
    session = SimpleNamespace(execution=SimpleNamespace(profile=profile, attempt=lambda _: attempt))
    records = [{"kind": "execution_attempt", "data": {"attempt": {"attempt_id": attempt.attempt_id},
        "semantic_binding": {}, "original_id": "original-a"}},
        {"kind": "execution_observation", "data": {"attempt_id": attempt.attempt_id, "observation": observation}}]
    return session, records


def test_failed_unapplied_native_worker_start_is_distinct_from_missing_observation(tmp_path):
    session, records = _failed_start_receipts(tmp_path)
    assert judging._started_original_ids(session, records) == ["original-a"]
    assert judging._started_original_ids(session, records[:1]) == []
    unrelated = copy.deepcopy(records)
    unrelated[0]["data"].pop("semantic_binding")
    assert judging._started_original_ids(session, unrelated) == []


@pytest.mark.parametrize("field", ["nonce", "controller_ref", "input_sha256"])
def test_native_start_inventory_refuses_uncorrelated_observation(tmp_path, field):
    session, records = _failed_start_receipts(tmp_path)
    records[1]["data"]["observation"][field] = "b" * 64 if field == "input_sha256" else "unrelated"
    with pytest.raises(EvidenceWikiError) as caught:
        judging._started_original_ids(session, records)
    assert caught.value.error_code == "RESEARCH_SEMANTIC_START_OBSERVATION_CONFLICT"


def test_actual_native_first_pass_start_precedes_applied_judgment(accepted_session):
    _, session = accepted_session
    records = session.execution.records(session)
    first_attempt = next(row["data"] for row in records
        if row["kind"] == "execution_attempt" and "semantic_binding" in row["data"])
    identifier = first_attempt["attempt"]["attempt_id"]
    index = next(index for index, row in enumerate(records) if row["kind"] == "execution_observation"
        and row["data"]["attempt_id"] == identifier and row["data"]["observation"]["started_utc"] is not None)
    prefix = records[:index + 1]
    assert not any(row["kind"] == "execution_application" and row["data"]["attempt_id"] == identifier
        for row in prefix)
    assert judging._started_original_ids(session, prefix) == [first_attempt["original_id"]]
    report = session.semantic_report()
    assert report["counts"]["started"] == 1 and report["counts"]["missing"] == 0


def test_advisory_remapping_preserves_source_order_length_and_negation_without_cascading():
    old = worker_input()
    old["semantic"]["sources"][0]["content"] = "Observed benefit in the exact declared cohort."
    second = copy.deepcopy(old["semantic"]["sources"][0])
    second.update(source_id="source-2", record_sha256="sha256:" + "f" * 64,
        content="No observed benefit in the exact declared cohort. " + "Longer independent context. " * 10)
    old["semantic"]["sources"].append(second)
    new = copy.deepcopy(old)
    new["semantic"]["sources"] = list(reversed(new["semantic"]["sources"]))
    for index, source in enumerate(new["semantic"]["sources"], 1):
        source["source_id"] = "source-" + str(index)
    value = findings(old)
    value["checks"]["support"][0]["anchors"] = [{"pointer": f"/semantic/sources/{index}/content",
        "start": 0, "end": len(source["content"]), "quote": source["content"]}
        for index, source in enumerate(old["semantic"]["sources"])]
    value["objections"] = [{"criterion": "counterevidence", "source_refs": ["source-1", "source-2"],
        "rationale": "The exact retained statements conflict.", "material": False}]
    retained = copy.deepcopy(value)
    remapped = judging._remap(value, old, new)
    anchors = remapped["checks"]["support"][0]["anchors"]
    assert [anchor["pointer"] for anchor in anchors] == ["/semantic/sources/1/content", "/semantic/sources/0/content"]
    assert [anchor["quote"] for anchor in anchors] == [row["content"] for row in old["semantic"]["sources"]]
    assert remapped["objections"][0]["source_refs"] == ["source-2", "source-1"]
    assert value == retained
    assert remapped["verdicts"] == value["verdicts"]
    checked_findings(remapped, new)


@pytest.mark.parametrize("change", ["missing", "ambiguous"])
def test_advisory_remapping_refuses_unbound_or_ambiguous_source_correspondence(change):
    old, new = worker_input(), worker_input()
    if change == "missing":
        new["semantic"]["sources"] = []
    else:
        new["semantic"]["sources"].append(copy.deepcopy(new["semantic"]["sources"][0]))
    with pytest.raises(EvidenceWikiError) as refused:
        judging._remap(findings(old), old, new)
    assert refused.value.error_code == "RESEARCH_SEMANTIC_ANCHOR_AMBIGUOUS"


def test_exact_original_units_include_derived_questions_without_collapsing_text():
    derived = "Assess the exact declared finite observation for observed-cohort-2."
    observed = SimpleNamespace(requirements={"request": {"payload": {"questions": [{"id": "q", "text": ORIGINAL}],
        "derived_questions": [{"id": "detail", "text": derived, "original_ids": ["q"]}]}}})
    original = {"original_id": "q", "original_text": ORIGINAL,
        "questions": [{"slug": "q", "question_id": "q"}, {"slug": "detail", "question_id": "detail"}]}
    assert judging._units(observed, original) == [{"unit_id": "unit-1", "text": ORIGINAL}, {"unit_id": "unit-2", "text": derived}]
    observed.requirements["request"]["payload"]["derived_questions"] = []
    with pytest.raises(EvidenceWikiError) as refused:
        judging._units(observed, original)
    assert refused.value.error_code == "RESEARCH_SEMANTIC_ORIGINAL_UNAVAILABLE"


@pytest.mark.parametrize("invariant", ["support", "scope", "time", "units", "counterevidence"])
def test_independently_reproduced_finite_failure_is_named_even_when_model_votes_pass(invariant):
    supplied = finite_inputs()
    claim = supplied["strict_claims"][0]
    source = supplied["sources"][0]
    if invariant == "support":
        claim["text"] = claim["text"].replace("value=5", "value=999")
    elif invariant == "counterevidence":
        declaration = json.loads(source["content"])
        declaration["facts"]["contradiction"] = True
        source["content"] = json.dumps(declaration)
    else:
        before = claim[invariant]
        claim[invariant] = "other-" + before
        claim["text"] = claim["text"].replace(invariant + "=" + before, invariant + "=" + claim[invariant])
    reference, failed = judging.reference({"snapshot": {"claim": claim}}, supplied["sources"])
    assert reference["tier"] == "executable_oracle" and reference["label"] == "reject"
    assert reference["label_ref"].startswith("sha256:") and failed[invariant] == "fail"
