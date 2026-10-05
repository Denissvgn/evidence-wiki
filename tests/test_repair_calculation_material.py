"""Native arithmetic fixtures test inert material change detection only."""

import copy

import pytest

from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.execution_disagreement import material_basis
from evidence_wiki.pack_discovery import owner
from tests import test_calculation_context as native_cases
from tests.test_calculation_contracts import owner_case as native_owner_case
from tests.test_review_context import inputs


@pytest.fixture
def computed(tmp_path):
    case = native_owner_case.__wrapped__(tmp_path)
    _review, _context, snapshot, rubric, sources = native_cases.projected.__wrapped__(case)
    return snapshot, rubric, sources


def test_noncomputed_material_digest_is_exact_legacy_bytes():
    supplied = inputs()
    snapshot, rubric, sources = supplied["owner_snapshot"], supplied["rubric"], supplied["sources"]
    assert material_basis(snapshot, rubric, sources) == "sha256:58eb8c84f5ca871b863c10022b562091a7f924e16f750aa4d0b30dbaa32911cf"


def test_native_v2_value_and_same_bytes_are_stable(computed):
    before = material_basis(*computed)
    assert material_basis(*copy.deepcopy(computed)) == before
    assert len(before) == 71


@pytest.mark.parametrize("field", ["units", "scope", "derivation"])
def test_changed_quantitative_claim_fields_change_material_not_approval(computed, field):
    before = material_basis(*computed)
    changed = copy.deepcopy(computed)
    changed[0]["claim"][field] += " Corrected."
    assert material_basis(*changed) != before


def test_typed_wrong_expected_value_is_a_material_defect_not_arithmetic_override(computed):
    before = material_basis(*computed)
    changed = copy.deepcopy(computed)
    changed[0]["claim"]["calculations"][0]["expected"] = "600"
    assert material_basis(*changed) != before
    assert changed[0]["computation"]["graphs"]["worksheet"]["outputs"]["total"]["value"] == "6"


def test_result_artifact_identity_changes_alone_are_not_material_progress(computed):
    before = material_basis(*computed)
    changed = copy.deepcopy(computed)
    result = changed[0]["computation"]
    result["configuration_id"] = "sha256:" + "c" * 64
    contract = owner("_computation_contract")
    result["result_id"] = contract.content_id(contract.RESULT_SCHEMA, {key: value for key, value in result.items() if key != "result_id"})
    changed[0]["claim"]["calculations"][0]["result_id"] = result["result_id"]
    assert material_basis(*changed) == before


def test_missing_native_computational_source_cannot_be_ignored(computed):
    changed = copy.deepcopy(computed)
    changed[2].clear()
    with pytest.raises(ExecutionError):
        material_basis(*changed)


def test_malformed_reference_rounding_cannot_claim_known_material(computed):
    changed = copy.deepcopy(computed)
    changed[0]["claim"]["calculations"][0]["rounded"] = 1
    with pytest.raises(ExecutionError):
        material_basis(*changed)


def test_native_graph_named_like_lineage_is_not_omitted_or_rejected(computed):
    before = copy.deepcopy(computed)
    result = before[0]["computation"]
    graph = result["definition"]["graphs"].pop("worksheet")
    result["definition"]["graphs"]["lineage"] = graph
    result["graphs"]["lineage"] = result["graphs"].pop("worksheet")
    for item in result["lineage"].values():
        item["pointer"] = item["pointer"].replace("/graphs/worksheet/", "/graphs/lineage/")
    contract = owner("_computation_contract")
    result["definition_id"] = contract.content_id(contract.SCHEMA, result["definition"])
    for item in result["lineage"].values():
        if "definition_id" in item:
            item["definition_id"] = result["definition_id"]
    result["result_id"] = contract.content_id(contract.RESULT_SCHEMA, {key: value for key, value in result.items() if key != "result_id"})
    reference = before[0]["claim"]["calculations"][0]
    reference.update(result_id=result["result_id"], pointer="/graphs/lineage/outputs/total")
    # This is an inert, closed native-document fixture, not a current engine or
    # protected host receipt. The parser must preserve every declared graph.
    assert len(material_basis(*before)) == 71


def test_changed_configured_expression_is_recomputed_by_native_engine(tmp_path):
    import yaml

    from evidence_wiki.computation import evaluate
    root, declared, result, proposal = native_owner_case.__wrapped__(tmp_path)
    _review, _context, snapshot, rubric, sources = native_cases.projected.__wrapped__((root, declared, result, proposal))
    before = material_basis(snapshot, rubric, sources)
    declared["graphs"]["worksheet"]["nodes"]["total"]["expr"] = "amount * 3"
    config = yaml.safe_load((root / "research.yml").read_bytes())
    config["computation"] = declared
    (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    current = evaluate(root)
    assert current["graphs"]["worksheet"]["outputs"]["total"]["value"] == "9"
    snapshot["computation"] = current
    snapshot["claim"]["calculations"][0].update(result_id=current["result_id"], expected="9")
    assert material_basis(snapshot, rubric, sources) != before


def test_failed_native_invariant_is_retained_in_material_not_overridden(tmp_path):
    import yaml

    from evidence_wiki.computation import evaluate
    root, declared, result, proposal = native_owner_case.__wrapped__(tmp_path)
    _review, _context, snapshot, rubric, sources = native_cases.projected.__wrapped__((root, declared, result, proposal))
    before = material_basis(snapshot, rubric, sources)
    declared["invariants"]["required"] = {"description": "Required equality", "target": "computed", "selector": None,
        "inputs": {}, "filter": None, "assertion": "1 == 2", "severity": "error", "failure_message": "Required balance failed"}
    config = yaml.safe_load((root / "research.yml").read_bytes())
    config["computation"] = declared
    (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    failed = evaluate(root)
    assert failed["status"] == "failed"
    snapshot["computation"] = failed
    snapshot["claim"]["calculations"][0]["result_id"] = failed["result_id"]
    assert material_basis(snapshot, rubric, sources) != before
    assert next(item for item in snapshot["computation"]["invariants"] if item["id"] == "required")["passed"] is False
