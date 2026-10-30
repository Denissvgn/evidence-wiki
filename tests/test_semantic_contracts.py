"""Cited proposals cannot substitute roles, scope, counts, or prior failures."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from evidence_wiki.agent_execution import ExecutionError, ExecutionRole
from evidence_wiki.disagreement_configuration import DisagreementPlan
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.mutation_contracts import canonical
from evidence_wiki.onboarding_schemas import LIMITS
from evidence_wiki.review_context import create_review_context
from evidence_wiki.run_policy import RunPolicy
from evidence_wiki.semantic_contracts import (
    ADJUDICATION,
    CRITERIA,
    DIMENSIONS,
    FINDINGS,
    INPUT,
    MAX_INPUT_BYTES,
    PLAN,
    SemanticJudgingPlan,
    checked_findings,
    checked_input,
    metrics,
    schema_document,
    schemas,
)
from tests.test_run_policy import policy_document

BASIS = "sha256:" + "a" * 64
RECORD = "sha256:" + "b" * 64
REVISION = "sha256:" + "c" * 64


def public_profile():
    roles = (
        ExecutionRole("researcher", "host_worker", "research-controller", "research-model", "research/v1"),
        ExecutionRole("evidence", "evaluator", "evidence-controller", "evidence-model", "review/v1"),
        ExecutionRole("scope", "evaluator", "scope-controller", "scope-model", "review/v1"),
        ExecutionRole("counter", "evaluator", "counter-controller", "counter-model", "review/v1"),
        ExecutionRole("investigator", "evaluator", "investigation-controller", "investigation-model", "investigate/v1"),
    )
    scopes = ExecutionRoleRegistry(tuple(ExecutionRoleScope(config, role, ("model",)) for config, role in
        (("researcher", "researcher"), ("evidence", "evaluator"), ("scope", "evaluator"),
         ("counter", "critic"), ("investigator", "adjudicator"))))
    selections = {role.config_ref: SimpleNamespace(config_ref=role.config_ref)
                  for role in roles if role.role == "evaluator"}
    profile = SimpleNamespace(roles=roles, role_scopes=scopes,
        calibration=SimpleNamespace(selection=lambda config: selections[config]),
        disagreement=DisagreementPlan("investigator"), review_services=None)

    def role_for(config):
        for role in profile.roles:
            if role.config_ref == config:
                return role
        raise ExecutionError("execution_configuration_unavailable")

    profile.role = role_for
    return profile


def plan():
    return SemanticJudgingPlan("evidence", "scope", "counter")


def semantic_policy():
    value = policy_document()
    value["required_evaluators"] = [
        {"role": role, "profile_ref": config, "controller_ref": config + "-controller", "human_required": False}
        for config, role in (("evidence", "evaluator"), ("scope", "evaluator"), ("counter", "critic"))]
    return RunPolicy(value)


def worker_input(role="evidence"):
    question = "State the observed ratio and its observation year."
    content = "Observed ratio was α = 0.74 in 2026. Public source detail."
    evidence = {"source_id": "measurement", "record_sha256": RECORD,
        "observed_at": "2026-10-01T00:00:00Z", "capture": "primary", "quote": content[:35],
        "location_hint": "measurement table", "anchor": None}
    claim = {"id": "measurement-claim", "question_slug": "measurement-question", "qualification": "supported",
        "text": "The observed ratio was 0.74.", "scope": "The reported experiment.", "time": "2026",
        "units": "ratio", "evidence": [evidence], "premises": [], "derivation": None, "limitations": []}
    packet = create_review_context(context_id="semantic-context", task_role="evaluator", basis_id=BASIS,
        owner_snapshot={"claim": claim, "question": {"slug": "measurement-question", "original_id": "measurement-original",
            "question": question}, "premises": [], "source_revisions": {"measurement": REVISION}, "computation": None},
        rubric={"id": "rubric", "revision": "1", "criteria": dict.fromkeys(CRITERIA, "Assess the exact evidence.")},
        sources=[{"source_id": "measurement", "record_sha256": RECORD,
            "metadata": {"title": "Public measurement report", "authors": ["Published author"]}, "content": content}],
        order_seed=b"r" * 32).document()
    required = [{"unit_id": "unit-1", "text": "State the observed ratio."},
                {"unit_id": "unit-2", "text": "State the observation year."}]
    units = {"support": [{"unit_id": "claim-1", "applicable": True}],
        "scoped_correctness": [{"unit_id": "claim-1", "applicable": True}],
        "missing_material_evidence": [{"unit_id": row["unit_id"], "applicable": True} for row in required],
        "appropriate_abstention": [{"unit_id": "original-1", "applicable": False}],
        "unnecessary_refusal": [{"unit_id": "original-1", "applicable": False}]}
    semantic = {"schema_version": INPUT, "role": role, "original_text": question,
        "required_units": required, "sources": [{name: source[name] for name in
            ("source_id", "record_sha256", "metadata", "content")} for source in packet["sources"]],
        "candidate_units": [{name: packet["claim"][name] for name in ("text", "scope", "time", "units", "evidence")}],
        "candidate_behavior": "asserted", "basis_id": BASIS, "units": units}
    return {"packet": packet, "semantic": semantic, "purpose": "evaluate",
            "instructions": "Assess the cited public material.", "output_contract": schema_document(FINDINGS)}


def findings(supplied):
    anchor = {"pointer": "/semantic/sources/0/content", "start": 0, "end": 8, "quote": "Observed"}
    checks = {dimension: [{"unit_id": row["unit_id"],
        "verdict": "pass" if row["applicable"] else "not_applicable",
        "reason": "The cited material covers this scoped unit." if row["applicable"] else "The owner marks this unit inapplicable.",
        "severity": "none", "uncertainty": "low", "anchors": [deepcopy(anchor)] if row["applicable"] else []}
        for row in supplied["semantic"]["units"][dimension]] for dimension in DIMENSIONS}
    return {"schema_version": FINDINGS, "verdicts": dict.fromkeys(CRITERIA, "pass"),
            "rationale": "The exact cited material supports the proposal.", "objections": [], "checks": checks}


def set_verdict(value, dimension, verdict, index=0):
    row = value["checks"][dimension][index]
    row.update(verdict=verdict, severity="unknown" if verdict == "unknown" else "major" if verdict == "fail" else "none",
               uncertainty="unknown" if verdict == "unknown" else "high")
    if verdict in {"unknown", "not_applicable"}:
        row["anchors"] = []


def adjudication_input():
    supplied = worker_input("adjudicator")
    supplied["packet"]["task_role"] = "adjudicator"
    supplied["first_passes"] = [{"role": role, "findings": findings(supplied)}
                                for role in ("evidence", "scope_units", "counterevidence")]
    supplied["output_contract"] = schema_document(ADJUDICATION)
    return supplied


def adjudication(supplied):
    return {"schema_version": ADJUDICATION, "rationale": "Retain the bounded prior findings.",
            "checks": deepcopy(supplied["first_passes"][0]["findings"]["checks"])}


def test_plan_is_frozen_detached_and_pins_all_protocol_sources():
    selected = plan()
    document = selected.document()
    assert document["schema_version"] == PLAN
    assert tuple(document["dimensions"]) == DIMENSIONS
    assert set(document["protocol_source_sha256"]) == {"semantic_contracts.py", "semantic_judging.py", "semantic_reporting.py"}
    assert all(len(value) == 64 for value in document["protocol_source_sha256"].values())
    assert len(selected.sha256) == 64 and selected.sha256 == plan().sha256
    assert [selected.role_for(config) for config in ("evidence", "scope", "counter")] == ["evidence", "scope_units", "counterevidence"]
    document["dimensions"].clear()
    document["protocol_source_sha256"].clear()
    assert tuple(selected.document()["dimensions"]) == DIMENSIONS
    assert len(selected.document()["protocol_source_sha256"]) == 3
    with pytest.raises(FrozenInstanceError):
        selected.evidence_config_ref = "substitute"
    with pytest.raises(ExecutionError, match="configuration_unavailable"):
        selected.role_for("investigator")


@pytest.mark.parametrize("refs", [("evidence", "evidence", "counter"), ("evidence", "scope", "scope"),
                                  ("", "scope", "counter"), (True, "scope", "counter")])
def test_plan_refuses_duplicate_and_untyped_configurations(refs):
    with pytest.raises(ExecutionError):
        SemanticJudgingPlan(*refs)


@pytest.mark.parametrize("maximum", [True, 1.5, 0, -1, MAX_INPUT_BYTES + 1])
def test_plan_input_ceiling_has_exact_finite_integer_type(maximum):
    with pytest.raises(ExecutionError, match="input_bound_invalid"):
        SemanticJudgingPlan("evidence", "scope", "counter", max_input_bytes=maximum)


def test_profile_and_exact_native_policy_are_checked_without_live_qualification():
    selected, profile, policy = plan(), public_profile(), semantic_policy()
    selected.check_profile(profile)
    selected.check_policy(policy, profile)
    value = policy.to_dict()
    value["policy_id"] = None
    value["human_review"] = True
    value["required_evaluators"][0]["human_required"] = True
    selected.check_policy(RunPolicy(value), profile)


@pytest.mark.parametrize("field", ["calibration", "role_scopes", "disagreement"])
def test_profile_requires_explicit_scoped_calibration_and_investigation(field):
    profile = public_profile()
    setattr(profile, field, None)
    with pytest.raises(ExecutionError, match="scoped_calibration_required"):
        plan().check_profile(profile)


def test_profile_requires_selected_independent_adjudicator_and_no_service_alternates():
    profile = public_profile()
    profile.disagreement = DisagreementPlan()
    with pytest.raises(ExecutionError, match="adjudicator_required"):
        plan().check_profile(profile)
    profile.disagreement = DisagreementPlan("evidence")
    with pytest.raises(ExecutionError, match="adjudicator_is_first_pass"):
        plan().check_profile(profile)
    profile.disagreement = DisagreementPlan("investigator")
    profile.review_services = SimpleNamespace(routes=())
    with pytest.raises(ExecutionError, match="review_services_unsupported"):
        plan().check_profile(profile)


@pytest.mark.parametrize("config,other", [("evidence", "scope"), ("counter", "researcher"),
                                         ("investigator", "evidence"), ("investigator", "researcher")])
def test_controller_separation_cannot_be_restored_by_configuration_names(config, other):
    profile = public_profile()
    controller = profile.role(other).controller_ref
    profile.roles = tuple(replace(row, controller_ref=controller) if row.config_ref == config else row for row in profile.roles)
    with pytest.raises(ExecutionError, match="controller_shared"):
        plan().check_profile(profile)


@pytest.mark.parametrize("config,task", [("evidence", "critic"), ("scope", "adjudicator"),
                                      ("counter", "evaluator"), ("investigator", "critic")])
def test_configured_task_roles_cannot_be_substituted(config, task):
    profile = public_profile()
    profile.role_scopes = ExecutionRoleRegistry(tuple(replace(row, task_role=task) if row.config_ref == config else row
                                                    for row in profile.role_scopes.scopes))
    with pytest.raises(ExecutionError, match="role_invalid"):
        plan().check_profile(profile)


def test_unselected_first_pass_and_mismatched_calibration_are_refused():
    profile = public_profile()
    profile.roles += (ExecutionRole("alternate", "evaluator", "alternate-controller", "alternate-model", "review/v1"),)
    profile.role_scopes = ExecutionRoleRegistry((*profile.role_scopes.scopes, ExecutionRoleScope("alternate", "evaluator", ("model",))))
    with pytest.raises(ExecutionError, match="first_pass_selection_changed"):
        plan().check_profile(profile)
    profile = public_profile()
    profile.calibration = SimpleNamespace(selection=lambda _: SimpleNamespace(config_ref="substitute"))
    with pytest.raises(ExecutionError, match="calibration_selection_changed"):
        plan().check_profile(profile)


def test_protocol_source_drift_is_refused_after_plan_construction(monkeypatch):
    from evidence_wiki import semantic_contracts

    selected = plan()
    original = semantic_contracts._protocol_hashes()
    monkeypatch.setattr(semantic_contracts, "_protocol_hashes", lambda: ((original[0][0], "d" * 64), *original[1:]))
    with pytest.raises(ExecutionError, match="protocol_changed"):
        selected.check_profile(public_profile())
    assert selected.sha256 != plan().sha256


@pytest.mark.parametrize("change", ["role", "config", "controller", "duplicate", "missing", "extra", "human_type", "services"])
def test_policy_drift_cannot_change_first_pass_selection(change):
    value = semantic_policy().to_dict()
    if change == "role":
        value["required_evaluators"][2]["role"] = "evaluator"
    elif change == "config":
        value["required_evaluators"][1]["profile_ref"] = "substitute"
    elif change == "controller":
        value["required_evaluators"][1]["controller_ref"] = "changed-controller"
    elif change == "duplicate":
        value["required_evaluators"][1] = deepcopy(value["required_evaluators"][0])
    elif change == "missing":
        value["required_evaluators"].pop()
    elif change == "extra":
        value["required_evaluators"].append({"role": "adjudicator", "profile_ref": "investigator",
            "controller_ref": "investigation-controller", "human_required": False})
    elif change == "human_type":
        value["required_evaluators"][0]["human_required"] = 1
    else:
        value["review_services"] = {"routes": []}
    with pytest.raises(ExecutionError):
        plan().check_policy(SimpleNamespace(to_dict=lambda: deepcopy(value)), public_profile())


def test_schema_views_are_closed_and_detached():
    documents = schemas()
    assert set(documents) == {PLAN, INPUT, FINDINGS, ADJUDICATION}
    assert all(document["additionalProperties"] is False for document in documents.values())
    documents[FINDINGS]["properties"]["checks"]["properties"].clear()
    assert set(schema_document(FINDINGS)["properties"]["checks"]["properties"]) == set(DIMENSIONS)
    with pytest.raises(ExecutionError, match="version_unsupported"):
        schema_document("unlisted-version")


def test_owner_input_and_checked_findings_are_detached():
    supplied = worker_input()
    raw = findings(supplied)
    input_before, output_before = deepcopy(supplied), deepcopy(raw)
    validated_input = checked_input(supplied)
    validated, counts = checked_findings(raw, supplied)
    assert supplied == input_before and raw == output_before
    assert validated == raw
    assert counts["missing_material_evidence"]["expected"] == counts["missing_material_evidence"]["denominator"] == 2
    assert counts["support"]["score"] == 1.0
    validated_input["semantic"]["required_units"].clear()
    validated["checks"]["support"][0]["anchors"].clear()
    counts["support"]["numerator"] = 999
    assert supplied == input_before and raw == output_before
    assert checked_findings(raw, supplied)[1]["support"]["numerator"] == 1


@pytest.mark.parametrize("field", ["metrics", "expected", "denominator", "qualification", "authorized", "controller_ref", "model_ref", "provenance"])
def test_worker_cannot_report_owner_counts_qualification_or_provenance(field):
    supplied = worker_input()
    value = findings(supplied)
    value[field] = True
    with pytest.raises(ExecutionError, match="findings_invalid"):
        checked_findings(value, supplied)


@pytest.mark.parametrize("field", ["score", "expected", "applicable", "calibrated", "confidence"])
def test_worker_cannot_add_unit_fields_or_self_calibration(field):
    supplied = worker_input()
    value = findings(supplied)
    value["checks"]["support"][0][field] = 1
    with pytest.raises(ExecutionError, match="checks_invalid|findings_invalid"):
        checked_findings(value, supplied)


@pytest.mark.parametrize("change", ["dimension_missing", "dimension_extra", "row_missing", "row_duplicate", "unit_foreign"])
def test_worker_cannot_drop_repeat_or_replace_owner_units(change):
    supplied = worker_input()
    value = findings(supplied)
    if change == "dimension_missing":
        del value["checks"]["unnecessary_refusal"]
    elif change == "dimension_extra":
        value["checks"]["accuracy"] = []
    elif change == "row_missing":
        value["checks"]["missing_material_evidence"].pop()
    elif change == "row_duplicate":
        value["checks"]["missing_material_evidence"][1] = deepcopy(value["checks"]["missing_material_evidence"][0])
    else:
        value["checks"]["missing_material_evidence"][1]["unit_id"] = "unit-3"
    with pytest.raises(ExecutionError):
        checked_findings(value, supplied)


@pytest.mark.parametrize("dimension,verdict", [("support", "not_applicable"), ("missing_material_evidence", "not_applicable"),
                                              ("appropriate_abstention", "pass"), ("unnecessary_refusal", "unknown")])
def test_false_applicability_cannot_launder_the_denominator(dimension, verdict):
    supplied = worker_input()
    value = findings(supplied)
    set_verdict(value, dimension, verdict)
    with pytest.raises(ExecutionError, match="applicability_changed|anchor_required"):
        checked_findings(value, supplied)


def test_unknown_retains_full_denominator_and_suppresses_the_point_score():
    supplied = worker_input()
    value = findings(supplied)
    set_verdict(value, "missing_material_evidence", "unknown", index=1)
    value["verdicts"]["counterevidence"] = "unknown"
    counts = checked_findings(value, supplied)[1]["missing_material_evidence"]
    assert counts == {"expected": 2, "applicable": 2, "passed": 1, "failed": 0, "unknown": 1,
        "not_applicable": 0, "numerator": 1, "denominator": 2, "score": None,
        "uncertainty_bounds": {"lower": 0.5, "upper": 1.0}}


def test_zero_applicable_units_do_not_report_perfect_performance():
    supplied = worker_input()
    counts = checked_findings(findings(supplied), supplied)[1]["unnecessary_refusal"]
    assert counts["expected"] == counts["not_applicable"] == 1
    assert counts["denominator"] == counts["numerator"] == 0
    assert counts["score"] is None and counts["uncertainty_bounds"] == {"lower": None, "upper": None}


@pytest.mark.parametrize("field,value", [("severity", "major"), ("uncertainty", "low")])
def test_unknown_unit_requires_unknown_severity_and_uncertainty(field, value):
    supplied = worker_input()
    output = findings(supplied)
    set_verdict(output, "missing_material_evidence", "unknown")
    output["verdicts"]["counterevidence"] = "unknown"
    output["checks"]["missing_material_evidence"][0][field] = value
    with pytest.raises(ExecutionError, match="check_state_invalid"):
        checked_findings(output, supplied)


@pytest.mark.parametrize("dimension", ["support", "unnecessary_refusal"])
def test_pass_and_inapplicable_units_cannot_report_failure_severity(dimension):
    supplied = worker_input()
    output = findings(supplied)
    output["checks"][dimension][0]["severity"] = "minor"
    with pytest.raises(ExecutionError, match="check_state_invalid"):
        checked_findings(output, supplied)


@pytest.mark.parametrize("pointer", ["/semantic/basis_id", "/semantic/sources/0/metadata/title",
    "/semantic/candidate_units/0/evidence/0/quote", "/packet/manifest/context_complete", "/packet/premises/0/text",
    "/instructions", "/first_passes/0/findings/rationale", "/semantic/units/support/0/unit_id"])
def test_anchors_cannot_cite_control_fields_or_unlisted_material(pointer):
    supplied = worker_input()
    output = findings(supplied)
    output["checks"]["support"][0]["anchors"][0]["pointer"] = pointer
    with pytest.raises(ExecutionError, match="anchor_scope_invalid"):
        checked_findings(output, supplied)


@pytest.mark.parametrize("pointer", ["semantic/original_text", "/semantic/sources/00/content", "/semantic/sources/-/content",
    "/semantic/sources/1/content", "/semantic/sources/0/content~", "/semantic/sources/0/content~2", "/semantic~1sources/0/content"])
def test_malformed_unresolved_and_noncanonical_array_pointers_are_refused(pointer):
    supplied = worker_input()
    output = findings(supplied)
    output["checks"]["support"][0]["anchors"][0]["pointer"] = pointer
    with pytest.raises(ExecutionError, match="anchor_.*invalid"):
        checked_findings(output, supplied)


@pytest.mark.parametrize("pointer", ["/semantic/original_text", "/semantic/required_units/0/text", "/semantic/candidate_units/0/text",
    "/semantic/candidate_units/0/scope", "/semantic/candidate_units/0/time", "/semantic/candidate_units/0/units",
    "/semantic/sources/0/content", "/packet/claim/text", "/packet/claim/scope", "/packet/claim/time", "/packet/claim/units",
    "/packet/question/question"])
def test_each_permitted_material_string_can_be_quoted_exactly(pointer):
    supplied = worker_input()
    target = supplied
    for part in pointer[1:].split("/"):
        target = target[int(part)] if type(target) is list else target[part]
    output = findings(supplied)
    output["checks"]["support"][0]["anchors"] = [{"pointer": pointer, "start": 0, "end": len(target), "quote": target}]
    assert checked_findings(output, supplied)[0]["checks"]["support"][0]["anchors"][0]["quote"] == target


@pytest.mark.parametrize("change", ["start_negative", "start_bool", "end_bool", "end_equal", "end_beyond", "quote_wrong", "quote_empty", "anchor_extra", "anchor_missing", "anchor_many"])
def test_anchor_offsets_quotes_and_closed_shape_are_enforced(change):
    supplied = worker_input()
    output = findings(supplied)
    row = output["checks"]["support"][0]
    anchor = row["anchors"][0]
    if change == "start_negative":
        anchor["start"] = -1
    elif change == "start_bool":
        anchor["start"] = False
    elif change == "end_bool":
        anchor["end"] = True
    elif change == "end_equal":
        anchor["end"] = anchor["start"]
    elif change == "end_beyond":
        anchor["end"] = len(supplied["semantic"]["sources"][0]["content"]) + 1
    elif change == "quote_wrong":
        anchor["quote"] = "Invented"
    elif change == "quote_empty":
        anchor["quote"] = ""
    elif change == "anchor_extra":
        anchor["verified"] = True
    elif change == "anchor_missing":
        row["anchors"] = []
    else:
        row["anchors"] = [deepcopy(anchor) for _ in range(5)]
    with pytest.raises(ExecutionError):
        checked_findings(output, supplied)


def test_unicode_anchor_offsets_are_character_positions():
    supplied = worker_input()
    target = supplied["semantic"]["sources"][0]["content"]
    start = target.index("α")
    output = findings(supplied)
    anchor = {"pointer": "/semantic/sources/0/content", "start": start, "end": start + 1, "quote": "α"}
    output["checks"]["support"][0]["anchors"] = [anchor]
    checked_findings(output, supplied)
    anchor["end"] += 1
    with pytest.raises(ExecutionError, match="anchor_quote_invalid"):
        checked_findings(output, supplied)


@pytest.mark.parametrize("change", ["support", "scoped", "missing_fail", "missing_unknown"])
def test_native_and_semantic_findings_cannot_contradict(change):
    supplied = worker_input()
    output = findings(supplied)
    if change == "support":
        set_verdict(output, "support", "fail")
    elif change == "scoped":
        output["verdicts"]["units"] = "unknown"
    else:
        set_verdict(output, "missing_material_evidence", "fail" if change == "missing_fail" else "unknown")
    with pytest.raises(ExecutionError, match="native_.*conflict"):
        checked_findings(output, supplied)


def test_fail_dominates_unknown_in_scoped_native_conjunction():
    supplied = worker_input()
    output = findings(supplied)
    output["verdicts"].update(scope="fail", units="unknown")
    set_verdict(output, "scoped_correctness", "fail")
    assert checked_findings(output, supplied)[1]["scoped_correctness"]["failed"] == 1


@pytest.mark.parametrize("refs", [["source-2"], ["source-1", "source-1"], ["measurement"]])
def test_native_objections_can_only_name_included_blinded_sources(refs):
    supplied = worker_input()
    output = findings(supplied)
    output["objections"] = [{"criterion": "counterevidence", "source_refs": refs,
        "rationale": "Check the exact cited material.", "material": True}]
    with pytest.raises(ExecutionError):
        checked_findings(output, supplied)


def test_valid_objections_are_detached_and_retained_without_native_authority():
    supplied = worker_input()
    output = findings(supplied)
    objection = {"criterion": "counterevidence", "source_refs": ["source-1"],
                 "rationale": "Check the exact cited material.", "material": True}
    output["objections"] = [objection]
    validated = checked_findings(output, supplied)[0]
    assert validated["objections"] == [objection]
    objection["source_refs"].clear()
    assert validated["objections"][0]["source_refs"] == ["source-1"]


@pytest.mark.parametrize("change", ["extra_semantic", "extra_root", "duplicate_required", "foreign_dimension", "missing_requirement",
    "duplicate_dimension", "applicable_bool", "extra_source_metadata", "foreign_source", "extra_candidate", "candidate_foreign_evidence"])
def test_input_scope_and_public_projection_are_closed(change):
    supplied = worker_input()
    semantic = supplied["semantic"]
    if change == "extra_semantic":
        semantic["expected_count"] = 1
    elif change == "extra_root":
        supplied["qualification"] = True
    elif change == "duplicate_required":
        semantic["required_units"][1]["unit_id"] = "unit-1"
    elif change == "foreign_dimension":
        semantic["units"]["support"][0]["unit_id"] = "unit-1"
    elif change == "missing_requirement":
        semantic["units"]["missing_material_evidence"].pop()
    elif change == "duplicate_dimension":
        semantic["units"]["missing_material_evidence"][1] = deepcopy(semantic["units"]["missing_material_evidence"][0])
    elif change == "applicable_bool":
        semantic["units"]["support"][0]["applicable"] = 1
    elif change == "extra_source_metadata":
        semantic["sources"][0]["metadata"]["expected_label"] = "accept"
    elif change == "foreign_source":
        semantic["sources"][0]["source_id"] = "measurement"
    elif change == "extra_candidate":
        semantic["candidate_units"][0]["generator"] = "unlisted"
    else:
        semantic["candidate_units"][0]["evidence"][0]["source_id"] = "source-2"
    with pytest.raises(ExecutionError):
        checked_input(supplied)


def test_input_limits_measure_canonical_utf8_bytes_within_native_string_bounds():
    supplied = worker_input()
    size = len(canonical(supplied))
    assert checked_input(supplied, max_input_bytes=size) == supplied
    with pytest.raises(ExecutionError, match="input_limit"):
        checked_input(supplied, max_input_bytes=size - 1)
    supplied["instructions"] = "α" * LIMITS["string_characters"]
    supplied["purpose"] = "α" * LIMITS["string_characters"]
    with pytest.raises(ExecutionError, match="input_limit"):
        checked_input(supplied)


@pytest.mark.parametrize("malformed", ["oversized_string", "non_json", "excessive_depth", "excessive_array"])
def test_native_json_refusals_preserve_declared_limit_and_invalid_errors(malformed):
    supplied = worker_input()
    if malformed == "oversized_string":
        supplied["instructions"] = "α" * (LIMITS["string_characters"] + 1)
    elif malformed == "non_json":
        supplied["instructions"] = object()
    elif malformed == "excessive_array":
        supplied["instructions"] = [None] * (LIMITS["array_items"] + 1)
    else:
        nested = []
        for _ in range(LIMITS["depth"] + 1):
            nested = [nested]
        supplied["instructions"] = nested
    refusal = "input_limit" if malformed in {"excessive_depth", "excessive_array"} else "input_invalid"
    with pytest.raises(ExecutionError, match=refusal):
        checked_input(supplied)


@pytest.mark.parametrize("behavior", ["asserted", "abstained", "refused"])
def test_pure_input_codec_preserves_supported_candidate_behavior_labels(behavior):
    supplied = worker_input()
    supplied["semantic"]["candidate_behavior"] = behavior
    assert checked_input(supplied)["semantic"]["candidate_behavior"] == behavior


@pytest.mark.parametrize("dimension", DIMENSIONS)
@pytest.mark.parametrize("previous", ["fail", "unknown"])
def test_adjudication_cannot_promote_applicable_prior_failures_or_unknowns(dimension, previous):
    supplied = adjudication_input()
    if dimension in {"appropriate_abstention", "unnecessary_refusal"}:
        supplied["semantic"]["units"][dimension][0]["applicable"] = True
        supplied["first_passes"] = [{"role": role, "findings": findings(supplied)}
                                    for role in ("evidence", "scope_units", "counterevidence")]
    prior = supplied["first_passes"][1]["findings"]
    set_verdict(prior, dimension, previous)
    if dimension == "support":
        prior["verdicts"]["support"] = previous
        set_verdict(prior, "scoped_correctness", previous)
    elif dimension == "scoped_correctness":
        prior["verdicts"]["scope"] = previous
    elif dimension == "missing_material_evidence":
        prior["verdicts"]["counterevidence"] = previous
    with pytest.raises(ExecutionError, match="adjudication_promotion"):
        checked_findings(adjudication(supplied), supplied, adjudication=True)


def test_adjudication_keeps_native_votes_out_and_retains_prior_failures():
    supplied = adjudication_input()
    for row in supplied["first_passes"]:
        set_verdict(row["findings"], "missing_material_evidence", "fail")
        row["findings"]["verdicts"]["counterevidence"] = "fail"
    output = adjudication(supplied)
    validated, counts = checked_findings(output, supplied, adjudication=True)
    assert validated == output and counts["missing_material_evidence"]["failed"] == 1
    output["verdicts"] = dict.fromkeys(CRITERIA, "pass")
    with pytest.raises(ExecutionError, match="findings_invalid"):
        checked_findings(output, supplied, adjudication=True)


@pytest.mark.parametrize("change", ["missing", "duplicate_role", "foreign_role", "extra_field", "wrong_anchor", "foreign_unit"])
def test_adjudication_revalidates_exact_prior_roles_material_and_units(change):
    supplied = adjudication_input()
    if change == "missing":
        supplied["first_passes"].pop()
    elif change == "duplicate_role":
        supplied["first_passes"][1]["role"] = "evidence"
    elif change == "foreign_role":
        supplied["first_passes"][1]["role"] = "adjudicator"
    elif change == "extra_field":
        supplied["first_passes"][1]["controller_ref"] = "worker-asserted"
    elif change == "wrong_anchor":
        supplied["first_passes"][1]["findings"]["checks"]["support"][0]["anchors"][0]["quote"] = "Invented"
    else:
        supplied["first_passes"][1]["findings"]["checks"]["support"][0]["unit_id"] = "unit-1"
    with pytest.raises(ExecutionError):
        checked_findings(adjudication(supplied), supplied, adjudication=True)


def test_first_pass_and_adjudication_roles_cannot_select_each_others_schema():
    supplied = worker_input()
    with pytest.raises(ExecutionError, match="findings_role_invalid"):
        checked_findings(findings(supplied), supplied, adjudication=True)
    supplied = adjudication_input()
    with pytest.raises(ExecutionError, match="findings_role_invalid"):
        checked_findings(findings(supplied), supplied)
    supplied = worker_input()
    supplied["first_passes"] = []
    with pytest.raises(ExecutionError, match="first_passes_unexpected"):
        checked_input(supplied)


def test_metrics_refuse_unmatched_owner_units_and_worker_count_fields():
    supplied = worker_input()
    checks = findings(supplied)["checks"]
    units = supplied["semantic"]["units"]
    units["missing_material_evidence"].pop()
    with pytest.raises(ExecutionError, match="checks_scope_invalid"):
        metrics(checks, units)
    units = worker_input()["semantic"]["units"]
    checks["support"][0]["numerator"] = 1
    with pytest.raises(ExecutionError):
        metrics(checks, units)
