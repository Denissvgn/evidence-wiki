"""Component checks for native observation reuse; stubs grant no execution credit."""

from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from evidence_wiki import _review_dependency_inputs
from tools import _review_dependency_workload as workload


@pytest.fixture
def observed_rows(monkeypatch):
    identifiers = sorted(workload.CLAIM_IDS, reverse=True)
    claims, currents = [], []
    for index, identifier in enumerate(identifiers):
        observation = {"claim_id": identifier, "basis_id": "basis-" + identifier,
            "producer_id": "producer", "reasons": []}
        claims.append({"claim": {"id": identifier}, "claim_basis_id": observation["basis_id"],
            "accepted": index % 2 == 0, "required_review_role": "evaluator", "observation": observation})
        currents.append({"material": {"claim_id": identifier, "producer_id": "producer"},
            "dependency_basis": {"target_claim_id": identifier, "basis_id": observation["basis_id"],
                "semantic_material_id": "semantic-" + identifier},
            "required_review_role": "evaluator", "observation": dict(observation)})
    result = {"schema_version": "evidence-strict-result/v3", "claims": claims, "review_producer_id": "producer"}
    calls = {"evaluate": 0, "typed": 0, "view": False, "bound": False}
    view, session = object(), object()

    @contextmanager
    def current_view(root, config):
        calls["view"] = True
        try:
            yield view
        finally:
            calls["view"] = False

    @contextmanager
    def bind(selected):
        assert selected is session
        calls["bound"] = True
        try:
            yield
        finally:
            calls["bound"] = False

    def evaluate(root, config, policy, *, view):
        assert calls["view"] and calls["bound"]
        calls["evaluate"] += 1
        return result, (object(), SimpleNamespace(producer_id="producer"), currents, [])

    def typed(operation):
        assert operation is evaluate
        calls["typed"] += 1
        return operation

    core = SimpleNamespace(configuration=lambda root: {},
        resolve_policy=lambda root, config: {"schema_version": "evidence-strict-policy/v2"},
        typed=typed, _evaluate_dependency_observations=evaluate)
    owners = {"_strict_evidence": core, "_evidence_usage": SimpleNamespace(current_view=current_view)}
    monkeypatch.setattr(workload, "owner", owners.__getitem__)
    workspace = SimpleNamespace(research=SimpleNamespace(session=lambda *args, **kwargs: nullcontext(session)))
    monkeypatch.setattr(workload, "Workspace", SimpleNamespace(open=lambda root: nullcontext(workspace)))
    monkeypatch.setattr(_review_dependency_inputs, "bind_session", bind)
    runtime = SimpleNamespace(context=SimpleNamespace(activate=nullcontext), policy=object(), execution=object())
    return runtime, result, currents, calls


def test_one_native_evaluation_supplies_all_rows_in_result_order(observed_rows):
    runtime, result, currents, calls = observed_rows
    rows = workload.native_dependency_rows("fixture", runtime, "observation")
    assert calls == {"evaluate": 1, "typed": 1, "view": False, "bound": False}
    assert [row["claim_id"] for row in rows] == [claim["claim"]["id"] for claim in result["claims"]]
    for row, claim, current in zip(rows, result["claims"], currents, strict=True):
        assert row == {"claim_id": claim["claim"]["id"], "basis_id": claim["claim_basis_id"],
            "semantic_material_id": current["dependency_basis"]["semantic_material_id"],
            "review_producer_id": "producer", "dependency_basis": current["dependency_basis"],
            "accepted": claim["accepted"], "required_review_role": claim["required_review_role"],
            "observation": claim["observation"]}


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "claim_inventory", "basis", "target", "producer", "observation", "role"])
def test_missing_duplicate_or_mismatched_native_currents_refused(observed_rows, mutation):
    runtime, result, currents, calls = observed_rows
    if mutation == "missing":
        currents.pop()
    elif mutation == "duplicate":
        currents[-1] = currents[0]
    elif mutation == "claim_inventory":
        result["claims"][-1] = result["claims"][0]
    elif mutation == "basis":
        currents[0]["dependency_basis"]["basis_id"] = "changed"
    elif mutation == "target":
        currents[0]["dependency_basis"]["target_claim_id"] = "changed"
    elif mutation == "producer":
        result["review_producer_id"] = "changed"
    elif mutation == "observation":
        currents[0]["observation"]["basis_id"] = "changed"
    else:
        currents[0]["required_review_role"] = "human-review"
    with pytest.raises(ValueError, match="workload_native_dependency_(inventory_invalid|producer_invalid|current_mismatch)"):
        workload.native_dependency_rows("fixture", runtime, "observation")
    assert calls["evaluate"] == 1
