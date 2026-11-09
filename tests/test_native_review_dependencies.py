"""Actual native review applications preserve claim-local original custody.

The grouped suite drives the compact three-claim fixture (six review applications) so the module fits
its hosted group window; the six-claim, twelve-application cohort stays on the qualification routes.
"""

import pytest

from tests._review_dependency_fixture import execute, fixture_document, require_storage
from tools._review_dependency_cases import CASE_IDS, INITIAL_CASES
from tools._review_dependency_workload import digest


@pytest.fixture(scope="module", params=("legacy_full",))
def native_cases(tmp_path_factory, request):
    require_storage()
    directory = tmp_path_factory.mktemp("native-review-parent") / request.param
    return execute(directory, mode=request.param, compact=True)


@pytest.mark.parametrize("case_id", INITIAL_CASES)
def test_actual_complete_review_pipeline_and_reversible_input_custody(native_cases, case_id):
    rows = {row["case_id"]: row for row in native_cases["cases"]}
    assert len(rows) == len(CASE_IDS) == native_cases["case_count"] == 99
    assert set(rows) == set(CASE_IDS)
    assert native_cases["status"] == "completed"
    assert native_cases["fixture_sha256"] == digest(fixture_document(compact=True))
    assert rows[case_id]["status"] == "passed"
    assert "read_only_closing_custody" in rows[case_id]["assertions"]
    assert all(row["status"] == "not_executed" for key, row in rows.items() if key not in INITIAL_CASES)
    if case_id == "unchanged-baseline":
        assert "compact_six_actual_review_applications" in rows[case_id]["assertions"]
    else:
        assert "exact_before_refresh_core_acceptance" in rows[case_id]["assertions"]
        assert rows[case_id]["material_invalidated"] == ["independent-note", "margin", "observed-capacity"]
