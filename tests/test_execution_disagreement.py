"""Protected journal joins, intrinsic change detection and targeted repair scope."""

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki.agent_execution import DeterministicOfflineExecutor, ExecutionAttempt, ExecutionError, ExecutionRole
from evidence_wiki.execution_disagreement import (
    FINDINGS,
    adjudication_packet,
    attempt_metadata,
    driver_action,
    material_basis,
    project_claim,
    review_findings,
    validate_targeted_repair,
)
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.research_execution import ResearchExecution
from tests.test_agent_execution import attempt, profile
from tests.test_disagreement import gates, selected_policy
from tests.test_review_context import BASIS, inputs


def material():
    value = inputs()
    snapshot, rubric, sources = value["owner_snapshot"], value["rubric"], value["sources"]
    return snapshot, rubric, sources


@pytest.mark.parametrize("field", ["identity", "status", "source_hash", "capture_clock", "controller", "rubric_name", "source_order"])
def test_bookkeeping_identity_and_transport_changes_are_not_material_repairs(field):
    snapshot, rubric, sources = material()
    before = material_basis(snapshot, rubric, sources)
    if field == "identity":
        snapshot["claim"]["id"] = "renamed-identity"
        snapshot["question"].update(slug="renamed-question", original_id="renamed-original")
    elif field == "status":
        snapshot["metadata"]["accepted"] = True
        snapshot["claim"]["qualification"] = "contested"
        sources[0]["metadata"]["status"] = "approved"
    elif field == "source_hash":
        sources[0]["record_sha256"] = "sha256:" + "d" * 64
        snapshot["source_revisions"][sources[0]["source_id"]] = "sha256:" + "e" * 64
    elif field == "capture_clock":
        snapshot["claim"]["evidence"][0]["observed_at"] = "2026-12-31T00:00:00Z"
    elif field == "controller":
        snapshot["metadata"]["generator_identity"] = "another-controller"
        sources[0]["metadata"]["generator_identity"] = "another-controller"
    elif field == "rubric_name":
        rubric.update(id="renamed-rubric", revision="another-label")
    else:
        sources.reverse()
    assert material_basis(snapshot, rubric, sources) == before


@pytest.mark.parametrize("field", ["claim", "question", "quote", "anchor", "source", "scientific_metadata", "rubric"])
def test_relevant_claim_source_context_and_criteria_change_material_basis(field):
    snapshot, rubric, sources = material()
    before = material_basis(snapshot, rubric, sources)
    if field == "claim":
        snapshot["claim"]["text"] += " Changed assertion."
    elif field == "question":
        snapshot["question"]["question"] += " Include units."
    elif field == "quote":
        snapshot["claim"]["evidence"][0]["quote"] = "Different quoted material."
    elif field == "anchor":
        snapshot["claim"]["evidence"][0]["anchor"] = {"pointer": "/value", "expected": "0.74"}
    elif field == "source":
        sources[0]["content"] += " Contrary observation."
    elif field == "scientific_metadata":
        sources[0]["metadata"]["published_at"] = "2025-01-01"
    else:
        rubric["criteria"]["support"] += " Require exact declared context."
    assert material_basis(snapshot, rubric, sources) != before


def test_repair_cannot_rename_delete_add_or_move_target_and_peer_claims():
    snapshot, _, _ = material()
    target = snapshot["claim"]
    peer = copy.deepcopy(target)
    peer["id"] = "peer"
    before = [target, peer]
    after = copy.deepcopy(before)
    after[0]["text"] = "Narrow repaired assertion."
    validate_targeted_repair(before, after, target_claim_id=target["id"])
    mutations = [lambda rows: rows[0].update(id="new-name"), lambda rows: rows.pop(0),
        lambda rows: rows.append(copy.deepcopy(rows[0])), lambda rows: rows[0].update(question_slug="another-question"),
        lambda rows: rows[1].update(text="Changed peer")]
    for mutate in mutations:
        changed = copy.deepcopy(after)
        mutate(changed)
        with pytest.raises(ExecutionError):
            validate_targeted_repair(before, changed, target_claim_id=target["id"])


def test_intrinsic_premise_graph_preserves_renaming_and_detects_rewired_edges():
    snapshot, rubric, sources = material()
    first, second, third = [copy.deepcopy(snapshot["claim"]) for _ in range(3)]
    first.update(id="first", text="First distinct premise.", premises=[], derivation=None)
    second.update(id="second", text="Second distinct premise.", premises=[], derivation=None)
    third.update(id="third", qualification="inference", text="Intermediate inference.", premises=["first"],
                 derivation="Derive from first")
    snapshot["claim"].update(qualification="inference", premises=["third", "second"], derivation="Combine third and second")
    snapshot["premises"] = [first, second, third]
    original = material_basis(snapshot, rubric, sources)
    renamed = copy.deepcopy(snapshot)
    names = {first["id"]: "renamed-first", second["id"]: "renamed-second", third["id"]: "renamed-third"}
    for claim in [renamed["claim"], *renamed["premises"]]:
        claim["id"] = names.get(claim["id"], claim["id"])
        claim["premises"] = [names.get(value, value) for value in claim["premises"]]
        if claim["derivation"]:
            for old, new in names.items():
                claim["derivation"] = claim["derivation"].replace(old, new)
    assert material_basis(renamed, rubric, sources) == original
    snapshot["premises"][2]["premises"] = ["second"]
    assert material_basis(snapshot, rubric, sources) != original


def test_unresolved_or_cyclic_premise_graph_does_not_claim_material_identity():
    snapshot, rubric, sources = material()
    for references in (["not-present"], [snapshot["claim"]["id"]]):
        snapshot["claim"]["premises"] = references
        with pytest.raises(ExecutionError, match="graph_invalid"):
            material_basis(snapshot, rubric, sources)


def test_exact_duplicate_source_and_evidence_do_not_counterfeit_material_progress():
    snapshot, rubric, sources = material()
    original = material_basis(snapshot, rubric, sources)
    clone = copy.deepcopy(sources[0])
    clone["source_id"] = "duplicate-under-new-identity"
    sources.append(clone)
    repeated = copy.deepcopy(snapshot["claim"]["evidence"][0])
    repeated["source_id"] = clone["source_id"]
    snapshot["claim"]["evidence"].extend([repeated, copy.deepcopy(repeated)])
    snapshot["source_revisions"][clone["source_id"]] = "sha256:" + "e" * 64
    assert material_basis(snapshot, rubric, sources) == original
    sources[-1]["metadata"]["url"] = "https://distinct-observation.example/independent"
    assert material_basis(snapshot, rubric, sources) != original


def test_versioned_objections_are_bounded_cited_and_cannot_smuggle_authority():
    output = {"schema_version": FINDINGS, "verdicts": dict.fromkeys(
        ("support", "source_suitability", "scope", "time", "units", "counterevidence"), "pass"),
        "rationale": "Review summary.", "objections": [{"criterion": "counterevidence", "source_refs": ["source-1"],
            "rationale": "A retained contrary passage needs inspection.", "material": True}]}
    parsed = review_findings(output, allowed_source_refs=["source-1"])
    assert len(parsed["objection_refs"]) == 1
    output["objections"][0]["source_refs"] = ["outside-scope"]
    with pytest.raises(ExecutionError, match="source_scope_invalid"):
        review_findings(output, allowed_source_refs=["source-1"])
    output["objections"][0]["source_refs"] = ["source-1"]
    output["objections"][0]["authorized"] = True
    with pytest.raises(ExecutionError, match="objections_invalid"):
        review_findings(output, allowed_source_refs=["source-1"])


@pytest.fixture
def journal(tmp_path, monkeypatch):
    import evidence_wiki.research_session as sessions

    snapshot, rubric, sources = material()
    claim_id = snapshot["claim"]["id"]
    selected = profile(roles=(ExecutionRole("profile-a", "evaluator", "controller-a", "offline/evidence", "prompt/v1"),
        ExecutionRole("critic", "evaluator", "critic-controller", "offline/critic", "prompt/v1"),
        ExecutionRole("repair", "host_worker", "generator", "offline/repair", "prompt/v1")))
    execution = ResearchExecution.__new__(ResearchExecution)
    execution._profile, execution._observer_key = selected, b"o" * 32
    session = SimpleNamespace(root=tmp_path, run_id="journal", binding={"run_id": "journal"},
        execution=execution, policy=selected_policy())
    state_file = tmp_path / "state.json"
    state_file.write_text("{}")
    events = []
    controller = SimpleNamespace(run_state_path=lambda *_: state_file, events_path=lambda *_: Path("events"),
        _session_state=lambda *_: {"research_session": session.binding}, _bounded_journal=lambda *_: {"events": events})
    monkeypatch.setattr(sessions, "_load", lambda *_: controller)
    def append(kind, data):
        records = execution.records(session)
        record = execution.seal_record(session, kind, f"event-{len(events)}", data, records=records)
        events.append({"event_id": f"event-{len(events)}", "data": {"request_id": record["request_id"], "record": record}})
    prepared = {"snapshot": snapshot, "rubric": rubric, "result": {"basis_id": BASIS}}
    return session, append, events, prepared, sources, claim_id


def add_review(journal, config="profile-a", *, support="pass"):
    session, append, _, prepared, sources, claim_id = journal
    selected = session.execution.profile
    role = selected.role(config)
    fields = dict.fromkeys(("support", "source_suitability", "scope", "time", "units", "counterevidence"), "pass")
    fields["support"] = support
    output = {"verdicts": fields, "rationale": "Evidence-linked bounded review."}
    host = DeterministicOfflineExecutor(selected, outputs={config: output}, receipt_key=b"e" * 32)
    reference = attempt(profile(), attempt_id="review-" + config).job
    reference.update(role="evaluator", controller_ref=role.controller_ref, operation="evaluate",
        payload={"claim_id": claim_id, "packet_id": BASIS})
    value = ExecutionAttempt.create(attempt_id="review-" + config, nonce="nonce-" + config, run_id=session.run_id,
        binding_id=digest(session.binding), profile=selected, config_ref=config, job=reference)
    observed = host.authenticate(host.invoke(value), value)
    append("execution_attempt", {"attempt": value.document(), "purpose": "evaluate", "claim_id": claim_id,
        "review_material_basis": material_basis(prepared["snapshot"], prepared["rubric"], sources)})
    append("execution_observation", {"attempt_id": value.attempt_id, "observation": observed})
    native = {"claim_id": claim_id, "basis_id": BASIS, "verdicts": fields, "rationale": output["rationale"]}
    app = {"attempt_id": value.attempt_id, "operation": "evaluate", "claim_id": claim_id, "profile_ref": config,
        "strict_basis_id": BASIS, "envelope": {"payload": {"body": {"review": native}}}}
    append("execution_application", app)
    append("execution_applied", {key: app[key] for key in ("attempt_id", "operation", "claim_id", "profile_ref", "strict_basis_id")})


def test_real_protected_journal_projection_recovers_full_first_passes(journal):
    session, _, events, prepared, sources, claim_id = journal
    add_review(journal)
    add_review(journal, "critic", support="fail")
    result = project_claim(session, claim_id=claim_id, prepared=prepared, sources=sources)
    assert len(result["reviews"]) == 2 and result["reviews"][1]["verdicts"]["support"] == "fail"
    assert result["history"] == []
    events[0]["data"]["record"]["data"]["purpose"] = "forged"
    with pytest.raises(Exception, match="refused"):
        project_claim(session, claim_id=claim_id, prepared=prepared, sources=sources)


def test_all_reserved_failed_and_retry_attempts_are_preserved_in_one_cycle(journal):
    session, append, _, prepared, sources, claim_id = journal
    basis = material_basis(prepared["snapshot"], prepared["rubric"], sources)
    action = {"operation": "repair", "claim_id": claim_id, "basis_id": basis, "defect_sha256": "sha256:" + "d" * 64}
    metadata = attempt_metadata(action, cycle_ref="repair-cycle")
    reference = attempt(profile()).job
    reference.update(controller_ref="generator")
    for index in range(2):
        value = ExecutionAttempt.create(attempt_id=f"repair-{index}", nonce=f"nonce-{index}", run_id=session.run_id,
            binding_id=digest(session.binding), profile=session.execution.profile, config_ref="repair", job=reference)
        append("execution_attempt", {"attempt": value.document(), "purpose": "repair", "disagreement": metadata})
    result = project_claim(session, claim_id=claim_id, prepared=prepared, sources=sources)
    assert len(result["history"]) == 1 and len(result["reservations"]) == 2
    assert result["history"][0]["result_basis_id"] is None
    assert all(row["state"] == "dispatch_unknown" for row in result["reservations"])


def test_named_driver_actions_request_missing_role_without_certifying(journal):
    session, _, _, prepared, sources, claim_id = journal
    session.execution._review_signer = SimpleNamespace(generator=SimpleNamespace(controller_ref="generator"))
    projection = project_claim(session, claim_id=claim_id, prepared=prepared, sources=sources)
    selected = driver_action(session, projection, gates=gates(), original_id="original")
    assert selected["operation"] == "evaluate" and selected["config_ref"] == "profile-a"
    assert selected["disagreement_decision"]["authorized"] is False


def test_status_only_strict_basis_change_still_requires_fresh_first_passes(journal):
    session, _, _, prepared, sources, claim_id = journal
    add_review(journal)
    add_review(journal, "critic", support="fail")
    before = material_basis(prepared["snapshot"], prepared["rubric"], sources)
    prepared["result"]["basis_id"] = "sha256:" + "f" * 64
    prepared["snapshot"]["metadata"]["status"] = "changed"
    projected = project_claim(session, claim_id=claim_id, prepared=prepared, sources=sources)
    assert projected["basis_id"] == before and projected["reviews"] == []


def test_adjudication_context_exposes_anonymous_current_first_passes_only(journal):
    session, _, _, prepared, sources, claim_id = journal
    session.execution._review_signer = SimpleNamespace(generator=SimpleNamespace(controller_ref="generator"))
    kwargs = dict(claim_id=claim_id, prepared=prepared, sources=sources, context_id="adjudication-context",
                  order_seed=b"a" * 32, gates=gates())
    with pytest.raises(ExecutionError, match="not_selected"):
        adjudication_packet(session, **kwargs)
    add_review(journal)
    with pytest.raises(ExecutionError, match="not_selected"):
        adjudication_packet(session, **kwargs)
    add_review(journal, "critic", support="fail")
    bundle = adjudication_packet(session, **kwargs)
    import json

    rendered = json.dumps(bundle["packet"])
    assert "controller-a" not in rendered and "critic-controller" not in rendered and "profile-a" not in rendered
    assert "private-generator" not in rendered and "hidden-key" not in rendered
    assert len(bundle["packet"]["first_passes"]) == 2
    assert len(bundle["binding"]["first_pass_refs"]) == 2
    assert not bundle["packet"]["authorized"] and not bundle["packet"]["evidence_accepted"]


def test_real_signed_history_cannot_claim_material_result_before_successful_application(journal):
    session, append, _, prepared, sources, claim_id = journal
    basis = material_basis(prepared["snapshot"], prepared["rubric"], sources)
    action = {"operation": "repair", "claim_id": claim_id, "basis_id": basis, "defect_sha256": "sha256:" + "d" * 64}
    reference = attempt(profile()).job
    reference.update(controller_ref="generator")
    value = ExecutionAttempt.create(attempt_id="unknown-repair", nonce="nonce", run_id=session.run_id,
        binding_id=digest(session.binding), profile=session.execution.profile, config_ref="repair", job=reference)
    append("execution_attempt", {"attempt": value.document(), "disagreement": attempt_metadata(action, cycle_ref="cycle")})
    append("execution_applied", {"attempt_id": value.attempt_id, "disagreement_result_basis": "sha256:" + "f" * 64})
    with pytest.raises(ExecutionError, match="result_unobserved"):
        project_claim(session, claim_id=claim_id, prepared=prepared, sources=sources)


@pytest.mark.parametrize("field,value", [("nonce", "other-nonce"), ("run_id", "other-run"),
    ("job_id", "other-job"), ("profile_sha256", "e" * 64), ("input_sha256", "d" * 64),
    ("controller_ref", "other-controller"), ("model_ref", "other-model"), ("currency", "USD")])
def test_cycle_result_requires_exact_final_attempt_correlation(journal, field, value):
    session, append, _, prepared, sources, claim_id = journal
    basis = material_basis(prepared["snapshot"], prepared["rubric"], sources)
    action = {"operation": "repair", "claim_id": claim_id, "basis_id": basis, "defect_sha256": "sha256:" + "d" * 64}
    reference = attempt(profile()).job
    reference.update(controller_ref="generator")
    item = ExecutionAttempt.create(attempt_id="correlated-repair", nonce="nonce", run_id=session.run_id,
        binding_id=digest(session.binding), profile=session.execution.profile, config_ref="repair", job=reference)
    host = DeterministicOfflineExecutor(session.execution.profile, outputs={"repair": {}}, receipt_key=b"e" * 32)
    final = host.invoke(item)
    final[field] = value
    append("execution_attempt", {"attempt": item.document(), "disagreement": attempt_metadata(action, cycle_ref="cycle")})
    append("execution_observation", {"attempt_id": item.attempt_id, "observation": final})
    append("execution_application", {"attempt_id": item.attempt_id})
    append("execution_applied", {"attempt_id": item.attempt_id, "disagreement_result_basis": "sha256:" + "f" * 64})
    with pytest.raises(ExecutionError, match="result_unobserved"):
        project_claim(session, claim_id=claim_id, prepared=prepared, sources=sources)
