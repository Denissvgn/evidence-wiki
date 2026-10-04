"""Configured native arithmetic cannot be replaced by agent agreement."""

import copy

import pytest
import yaml

from evidence_wiki.calculation_contracts import PROPOSAL, choose
from evidence_wiki.computation import evaluate
from evidence_wiki.errors import UsageError
from tests._computation_fixture import definition, workspace


@pytest.fixture
def owner_case(tmp_path):
    declared = definition()
    declared["graphs"]["worksheet"] = {"description":"Declared arithmetic", "constants":{"amount":{"value":"3", "unit":"units"}},
        "inputs":{}, "nodes":{"total":{"expr":"amount * 2", "unit":"units"}}, "output_mapping":{"total":"total"}, "output_page":None}
    workspace(tmp_path, declared)
    result = evaluate(tmp_path)
    proposal = {"schema_version":PROPOSAL, "result_id":result["result_id"], "definition_id":result["definition_id"],
        "pointer":"/graphs/worksheet/outputs/total", "form":"value", "expression":"amount * 2", "unit":"units",
        "as_of":result["clock"]["as_of"], "assumptions":["Amounts use the explicit declared unit."],
        "rationale":"Interpret the configured multiplication; independent verification remains required."}
    return tmp_path, declared, result, proposal


def test_known_native_answer_and_units_are_selected_without_approval(owner_case):
    _root, _declared, result, proposal = owner_case
    selected = choose(result, proposal)
    assert selected["reference"] == {"result_id":result["result_id"], "pointer":proposal["pointer"], "expected":"6",
        "form":"value", "unit":"units", "rounded":False}
    assert selected["binding"]["arithmetic"]["mode"] == "exact"
    assert selected["accepted"] is False and selected["interpretation"]["status"] == "independent_current_verifier_required"


@pytest.mark.parametrize("field,value", [("expression", "amount * 200"), ("unit", "meters"),
    ("definition_id", "sha256:" + "0" * 64), ("result_id", "sha256:" + "0" * 64),
    ("pointer", "/graphs/worksheet/outputs/missing"), ("as_of", "2099-01-01T00:00:00Z"),
    ("form", []), ("assumptions", [{}])])
def test_agent_cannot_relabel_frozen_expression_units_time_or_values(owner_case, field, value):
    _root, _declared, result, proposal = owner_case
    proposal = copy.deepcopy(proposal)
    proposal[field] = value
    with pytest.raises(UsageError):
        choose(result, proposal)


def test_failed_native_invariant_blocks_selection_even_when_value_is_correct(owner_case):
    root, declared, _result, proposal = owner_case
    declared["invariants"]["required"] = {"description":"Required equality", "target":"computed", "selector":None,
        "inputs":{}, "filter":None, "assertion":"1 == 2", "severity":"error", "failure_message":"Required balance failed"}
    config = yaml.safe_load((root / "research.yml").read_bytes())
    config["computation"] = declared
    (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    result = evaluate(root)
    assert result["status"] == "failed"
    proposal.update(result_id=result["result_id"], definition_id=result["definition_id"])
    with pytest.raises(UsageError) as refused:
        choose(result, proposal)
    assert refused.value.details["reason"] == "invariants_not_passed"


def test_input_revision_changes_owner_identity_before_claim_binding(owner_case):
    root, declared, first, proposal = owner_case
    changed = root / "changed"
    workspace(changed, declared, raw=b'{"records":[{"id":"a","amount":7}]}')
    second = evaluate(changed)
    assert first["result_id"] != second["result_id"] and first["input_id"] != second["input_id"]
    with pytest.raises(UsageError):
        choose(second, proposal)


def test_selected_native_and_projected_packets_match_closed_public_schemas(owner_case):
    from evidence_wiki.calculation_context import project
    from evidence_wiki.calculation_contracts import SELECTION, schema_document
    from evidence_wiki.pack_discovery import owner
    result, proposal = owner_case[2:]
    contract = owner("_computation_contract")
    selected = choose(result, proposal)
    contract.validate_shape(selected, schema_document(SELECTION))
    projection = project(result, {row["source_id"]:"source-1" for row in result["sources"]})
    contract.validate_shape(projection, schema_document(projection["schema_version"]))
    selected["binding"]["input_id"] = "changed"
    with pytest.raises(ValueError):
        contract.validate_shape(selected, schema_document(SELECTION))


def test_calculation_versions_are_opt_in_and_keep_prior_scope_profile_bytes():
    from evidence_wiki import ExecutionProfile, ExecutionRole, ExecutionRoleRegistry, ExecutionRoleScope
    plain = ExecutionRoleScope("worker", "researcher", ("model",))
    calculator = ExecutionRoleScope("calc", "calculation", ("model", "calculation.propose"))
    assert plain.document()["schema_version"] == "research-execution-role-scope/v1"
    assert ExecutionRoleRegistry((plain,)).document()["schema_version"] == "research-execution-role-registry/v1"
    assert calculator.document()["schema_version"] == "research-execution-role-scope/v2"
    scopes = ExecutionRoleRegistry((calculator,))
    assert scopes.document()["schema_version"] == "research-execution-role-registry/v2"
    profile = ExecutionProfile("calculation", 16, "EUR", "mechanical/v1",
        (ExecutionRole("calc", "host_worker", "worker", "simulated/reference", "calc/v1"),), 1, 1, 100, "0", 1,
        role_scopes=scopes)
    assert profile.document()["schema_version"] == "research-execution-profile/v6"


def test_explicit_selected_reference_must_appear_in_scoped_inference(owner_case):
    from evidence_wiki.calculation_contracts import require_selected_references
    selection = choose(owner_case[2], owner_case[3])
    source = {"qualification":"supported", "question_slug":"q", "calculations":[]}
    inference = {"qualification":"inference", "question_slug":"q", "calculations":[selection["reference"]]}
    proposal = {"inventory":{"claims":[source, inference]}}
    require_selected_references(proposal, [selection], {"q"})
    inference["question_slug"] = "unrelated"
    with pytest.raises(UsageError):
        require_selected_references(proposal, [selection], {"q"})
    require_selected_references(proposal, [], {"q"})


def test_declared_source_premises_cannot_be_erased_by_quantitative_generation():
    from evidence_wiki.calculation_contracts import require_source_premises
    source = {"id":"observed", "text":"Retained source observation", "calculations":[]}
    proposal = {"inventory":{"claims":[source, {"id":"computed", "calculations":[]}]}}
    require_source_premises(proposal, [source])
    proposal["inventory"]["claims"].pop(0)
    with pytest.raises(UsageError) as refused:
        require_source_premises(proposal, [source])
    assert refused.value.details["reason"] == "calculation_source_premise_changed"
