"""Native readiness stays decisive for investigation proposals."""

from types import SimpleNamespace

import pytest

from evidence_wiki.execution_continuations import current_gates
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from tests.test_disagreement import selected_policy
from tests.test_execution_disagreement import material


def selection(*, reasons=(), auxiliary=None):
    snapshot, rubric, sources = material()
    profile = SimpleNamespace(role_scopes=ExecutionRoleRegistry((
        ExecutionRoleScope("repair", "repair", ("model",)),)),
        disagreement=SimpleNamespace(adjudicator_config_ref="investigator"))
    session = SimpleNamespace(policy=selected_policy(), execution=SimpleNamespace(profile=profile))
    view = {"authority": {"terminal_grants_available": True},
        "qualification": {"state": "qualified", "auxiliary_profiles": auxiliary or []},
        "execution": {"steps_used": 0, "steps_limit": 20,
            "accounting": {"usage_unknown": False, "overrun": False}},
        "usage": {"repair_cycles": 0, "adjudication_cycles": 0, "acquisition_attempts": 0}}
    row = {"claim": snapshot["claim"], "observation": {"reasons": list(reasons)}}
    prepared = {"snapshot": snapshot, "rubric": rubric,
        "result": {"basis_id": "sha256:" + "b" * 64, "claims": [row]}}
    return session, view, prepared, sources


def test_pure_context_readiness_and_native_failure_do_not_infer_consensus():
    args = selection(reasons=["strict_grounding_failed"])
    gates = current_gates(*args)
    assert gates["mechanical"] == "fail"
    assert gates["context_complete"] is True
    assert gates["acquisition_available"] is False and gates["source_gap_ref"] is None


def test_missing_native_observation_is_unknown_even_with_qualified_reviewers():
    args = selection()
    args[2]["result"]["claims"][0].pop("observation")
    assert current_gates(*args)["mechanical"] == "unknown"


@pytest.mark.parametrize("qualified,independent,expected", [(True, True, True),
    (None, True, False), (True, None, False), (False, True, False)])
def test_optional_investigator_requires_its_own_current_qualification(qualified, independent, expected):
    args = selection(auxiliary=[{"profile_ref": "investigator", "qualified": qualified, "independent": independent}])
    assert current_gates(*args)["adjudicator_available"] is expected
