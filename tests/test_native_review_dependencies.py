"""Actual native review applications preserve claim-local original custody."""

import pytest

from tests._review_dependency_fixture import execute, require_storage
from tools._review_dependency_cases import CASE_IDS, INITIAL_CASES


@pytest.fixture(scope="module", params=("legacy_full", "selective_reuse"))
def native_cases(tmp_path_factory, request):
    require_storage()
    directory = tmp_path_factory.mktemp("native-review-parent") / request.param
    return execute(directory, mode=request.param)


@pytest.mark.parametrize("case_id", INITIAL_CASES)
def test_actual_complete_review_pipeline_and_reversible_input_custody(native_cases, case_id):
    rows = {row["case_id"]: row for row in native_cases["cases"]}
    assert len(rows) == len(CASE_IDS) == native_cases["case_count"] == 99
    assert set(rows) == set(CASE_IDS)
    assert native_cases["status"] == "completed"
    assert rows[case_id]["status"] == "passed"
    assert "read_only_closing_custody" in rows[case_id]["assertions"]
    assert all(row["status"] == "not_executed" for key, row in rows.items() if key not in INITIAL_CASES)
    if case_id != "unchanged-baseline":
        assert "exact_before_refresh_core_acceptance" in rows[case_id]["assertions"]
        if native_cases["mode"] == "selective_reuse":
            assert rows[case_id]["evidence"]
            assert all(item["state"] == "eligible" for item in rows[case_id]["evidence"])
            assert "signed_historical_workspace_provenance_preserved" in rows[case_id]["assertions"]


def test_changed_claim_requires_actual_authenticated_refresh_for_final_publication(tmp_path):
    require_storage()
    report = execute(tmp_path / "refreshed", cases=("independent-claim-text",), refresh=True)
    row = next(row for row in report["cases"] if row["case_id"] == "independent-claim-text")
    assert row["accepted_before_refresh"] == sorted(
        {"observed-capacity", "effective-capacity", "observed-demand", "adequacy", "margin"}
    )
    assert row["material_invalidated"] == ["independent-note"]
    assert row["refreshed"]
    assert "actual_authenticated_refresh_cohort" in row["assertions"]
    assert "final_native_acceptance_and_publication" in row["assertions"]


@pytest.mark.parametrize("case_id", ("newest-fail-blocks-older-pass", "newest-unknown-blocks-older-pass"))
def test_real_newest_negative_review_blocks_older_pass_without_changing_basis(tmp_path, case_id):
    require_storage()
    report = execute(tmp_path / "history", cases=(case_id,))
    row = next(row for row in report["cases"] if row["case_id"] == case_id)
    assert row["status"] == "passed"
    assert "same_native_claim_and_workspace_bases" in row["assertions"]
    assert "exact_newest_negative_acceptance_set" in row["assertions"]
    assert "native_no_older_pass_resurrection" in row["assertions"]
