"""The compact native fixture stays inside the plain legacy_full run of the initial cases."""

import pytest

from evidence_wiki.pack_discovery import owner
from tests._review_dependency_fixture import fixture_document
from tools import _review_dependency_cases as cases
from tools import _review_dependency_workload as workload
from tools._review_dependency_case_contracts import (
    CLAIM_IDS,
    COMPACT_CLAIM_IDS,
    INITIAL_CASES,
    case_plan,
    fixture_claim_ids,
)


class Reached(Exception):
    """The call passed every gate under test and reached the next owner."""


def _reached(*_args, **_kwargs):
    raise Reached()


def test_compact_fixture_differs_from_the_full_fixture_only_by_its_claims():
    full, compact = fixture_document(), fixture_document(compact=True)
    assert fixture_claim_ids(full["inventory"]) == CLAIM_IDS
    assert fixture_claim_ids(compact["inventory"]) == COMPACT_CLAIM_IDS
    assert workload.CLAIM_IDS == CLAIM_IDS and workload.COMPACT_CLAIM_IDS == COMPACT_CLAIM_IDS
    owner("_strict_contract").claims_document(compact["inventory"])
    rows = {row["id"]: row for row in full["inventory"]["claims"]}
    for claim in compact["inventory"]["claims"]:
        assert claim == dict(rows[claim["id"]], premises=claim["premises"])
    margin = next(row for row in compact["inventory"]["claims"] if row["id"] == "margin")
    assert margin["premises"] == ["observed-capacity"] and margin["calculations"]
    assert {row["source_id"] for row in margin["evidence"]} == {"shared", "demand"}
    full["inventory"].pop("claims")
    compact["inventory"].pop("claims")
    assert compact == full


@pytest.mark.parametrize("shape", ["five", "duplicate", "extra", "renamed"])
def test_any_other_claim_set_is_refused(shape):
    full = fixture_document()["inventory"]
    inventory = fixture_document(compact=True)["inventory"]
    if shape == "five":
        inventory = full
        inventory["claims"] = [row for row in full["claims"] if row["id"] != "adequacy"]
    elif shape == "duplicate":
        inventory["claims"].append(dict(inventory["claims"][0]))
    elif shape == "extra":
        inventory["claims"].append(next(row for row in full["claims"] if row["id"] == "observed-demand"))
    else:
        inventory["claims"][-1]["id"] = "other-note"
    with pytest.raises(AssertionError, match="native_fixture_claim_set_invalid"):
        fixture_claim_ids(inventory)


@pytest.fixture
def gate(monkeypatch):
    """Stop run_cases at its first owner after the selection gates."""
    monkeypatch.setattr("tools._review_dependency_case_evidence.freeze_bindings", _reached)


@pytest.mark.parametrize("cases_selected", [INITIAL_CASES, ("premise-transitive-edit",)])
def test_plain_legacy_initial_run_admits_the_compact_fixture(tmp_path, gate, cases_selected):
    with pytest.raises(Reached):
        cases.run_cases(tmp_path / "out", fixture_document(compact=True), mode="legacy_full", cases=cases_selected)


@pytest.mark.parametrize("route", ["selective", "refresh", "other_case", "extended_selection", "extended_recipe",
                                   "bindings", "journal"])
def test_every_other_route_refuses_the_compact_fixture(tmp_path, gate, route):
    options = {"mode": "legacy_full", "cases": INITIAL_CASES}
    if route == "selective":
        options["mode"] = "selective_reuse"
    elif route == "refresh":
        options["refresh"] = True
    elif route == "other_case":
        options["cases"] = (*INITIAL_CASES, "independent-claim-scope")
    elif route == "extended_selection":
        options["extended_selection"] = {}
    elif route == "extended_recipe":
        options["extended_recipe_pin"] = {}
    elif route == "bindings":
        options["evidence_bindings"] = {"recipes_pin": {}}
    else:
        options["journal_collection"] = True
    with pytest.raises(AssertionError, match="native_compact_fixture_scope_invalid"):
        cases.run_cases(tmp_path / "out", fixture_document(compact=True), **options)
    assert not (tmp_path / "out").exists()
    if route != "other_case":
        with pytest.raises(Reached):
            cases.run_cases(tmp_path / "out", fixture_document(), **options)


def _harness(root, compact):
    return cases.NativeCases(root, None, None, None, "legacy_full", fixture_document(compact=compact)["inventory"],
        {}, {}, {}, {})


def test_compact_harness_runs_and_drives_only_its_own_claims(tmp_path):
    compact, full = _harness(tmp_path, True), _harness(tmp_path, False)
    assert compact.claim_ids == COMPACT_CLAIM_IDS and full.claim_ids == CLAIM_IDS
    with pytest.raises(AssertionError, match="native_compact_fixture_scope_invalid"):
        compact.run_case("independent-claim-scope")
    for harness, case_id in ((full, "independent-claim-scope"), (compact, "independent-claim-text")):
        with pytest.raises(AssertionError, match="native_baseline_required"):
            harness.run_case(case_id)
    with pytest.raises(AssertionError, match="unknown_native_claim_selection"):
        compact.drive({"adequacy"}, cycle="baseline")


@pytest.fixture
def provisioning(monkeypatch):
    """Stop provision at its first native owner after the inventory gate."""
    monkeypatch.setattr(workload, "_example", _reached)


@pytest.mark.parametrize("variant", ["selective", "no_case_plan", "plain_workload", "extended"])
def test_provision_admits_the_compact_fixture_only_for_the_plain_native_legacy_run(tmp_path, monkeypatch,
                                                                                    provisioning, variant):
    options = {"review_mode": "legacy_full", "native_case_plan": case_plan()}
    if variant == "selective":
        options["review_mode"] = "selective_reuse"
    elif variant == "no_case_plan":
        options.pop("native_case_plan")
    elif variant == "plain_workload":
        options = {}
    else:
        monkeypatch.setattr("tools._review_dependency_case_budget.validate_allocation", lambda *args, **kwargs: None)
        monkeypatch.setattr("tools._execution_budget.provisioning_window",
            lambda *args: ("2026-11-09T00:00:00+00:00", "2026-11-10T00:00:00+00:00"))
        options.update(execution_policy={}, allocation_binding={
            "schema_version": "native-review-case-extended-allocation/v4"})
    with pytest.raises(ValueError, match="workload_fixture_inventory_invalid"):
        workload.provision(tmp_path / "base", fixture_document(compact=True), [], **options)
    assert not (tmp_path / "base").exists()
    with pytest.raises(Reached):
        workload.provision(tmp_path / "base", fixture_document(), [], **options)


def test_provision_admits_the_compact_fixture_for_the_plain_native_legacy_run(tmp_path, provisioning):
    with pytest.raises(Reached):
        workload.provision(tmp_path / "base", fixture_document(compact=True), [], review_mode="legacy_full",
            native_case_plan=case_plan())
