"""Immutable selections and guarded dispatch checks run no worker or provider."""

from dataclasses import replace

import pytest

from evidence_wiki.agent_execution import BoundedSubprocessExecutor, ExecutionAttempt, ExecutionError, ExecutionRole
from evidence_wiki.calibration_registry import CalibrationPlan, CalibrationSelection
from evidence_wiki.disagreement_configuration import DisagreementPlan
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.qualification_calibration import calibration_suite
from evidence_wiki.readability_plan import ReadabilityCalibrationSelection, ReadabilityPlan
from evidence_wiki.readability_reference import DOMAIN, protected_cases
from evidence_wiki.reference_oracles import DOMAIN as SCIENCE
from evidence_wiki.reference_oracles import protected_cases as science_cases
from evidence_wiki.workflow_contracts import EXECUTION_OBSERVATION, JOB_V3
from tests.test_disagreement_configuration import simple_profile
from tests.test_readability_context import context
from tests.test_workflow_contracts import job


@pytest.fixture(scope="module")
def profile():
    base = simple_profile()
    rubric = {"id": "preservation", "revision": "1", "criteria": {"field": "Preserve exact declared fields and presentation association."}}
    suite = calibration_suite(protected_cases(), suite_ref="preservation", split_ref="held-out-fields", domains=[DOMAIN],
        rubric=rubric, model_settings={"temperature": "0"})
    selection = ReadabilityCalibrationSelection("readability", suite, model_revision="bounded-host-revision")
    science = calibration_suite(science_cases(), suite_ref="science", split_ref="held-out-science", domains=[SCIENCE],
        rubric={"id": "science", "revision": "1", "criteria": {"support": "Source support."}}, model_settings={"temperature": "0"})
    ordinary = CalibrationPlan(tuple(CalibrationSelection(key, science, model_revision="science-revision") for key in ("profile-a", "critic")))
    optional = ExecutionRole("readability", "evaluator", "presentation-controller", "presentation-model", "presentation/v1")
    scopes = ExecutionRoleRegistry((*base.role_scopes.scopes,
        ExecutionRoleScope("readability", "critic", ("model", "review.read", "objection.propose"))))
    chosen = replace(base, roles=(*base.roles, optional), role_scopes=scopes, calibration=ordinary,
        disagreement=DisagreementPlan(), tokens=50000,
        readability=ReadabilityPlan(selection, "presentation-controller", tokens=50000, cost_limit="0", tool_calls=0))
    return base, chosen, suite


def attempt(profile, *, change=None):
    _old, selected, _suite = profile
    _brief, packet, _binding = context()
    document = {"purpose": "readability", "packet": packet}
    scope = selected.role_scopes.scope("readability")
    value = job("evaluate") | {"schema_version": JOB_V3, "expected_result_schema": EXECUTION_OBSERVATION,
        "controller_ref": "presentation-controller", "prompt_version": "presentation/v1", "task_role": "critic",
        "role_scope_sha256": scope.sha256, "context_id": packet["context_id"],
        "required_capabilities": ["model", "review.read", "objection.propose"],
        "payload": {"qualification_purpose": "readability"},
        "packet_sha256": digest(document).removeprefix("sha256:"), "budgets": selected.document()["job_budgets"]}
    value.update(change or {})
    return ExecutionAttempt.create(attempt_id="bounded-critic", nonce="owned-nonce", run_id="owned-run", binding_id="owned-binding",
        profile=selected, config_ref="readability", job=value, input_document=document)


def test_absent_option_keeps_legacy_bytes_and_successor_pins_distinct_purpose(profile):
    old, selected, _suite = profile
    before = old.document()
    assert before["schema_version"] == "research-execution-profile/v3"
    assert selected.document()["schema_version"] == "research-execution-profile/v11"
    assert old.document() == before and "readability" not in before
    assert selected.readability.document()["handoff"]["route"] == "new_protected_readability_invocation"
    assert selected.readability.document()["qualification_purpose"] == "readability"


def test_context_relabeling_and_shared_controller_are_rejected(profile):
    with pytest.raises(ExecutionError):
        attempt(profile, change={"context_id": "foreign-context"})
    _old, selected, _suite = profile
    with pytest.raises(ExecutionError, match="readability_role_invalid|readability_controller_shared"):
        replace(selected, readability=replace(selected.readability, controller_ref="controller-a"))


def test_public_unguarded_executor_cannot_send_readability_bytes(profile, monkeypatch, tmp_path):
    value = attempt(profile)
    selected = profile[1]
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
    executor = BoundedSubprocessExecutor(selected, commands={}, environment={}, cwd=tmp_path, meter=None, receipt_key=b"r" * 32)
    monkeypatch.setattr(executor, "_invoke", lambda *_: pytest.fail("unguarded invocation entered"))
    with pytest.raises(ExecutionError, match="readability_guarded_handoff_required"):
        executor.invoke(value)


def test_reservation_diagnostic_retains_only_closed_owner_reasons():
    from evidence_wiki.errors import UsageError
    from evidence_wiki.readability_contracts import reservation_reason
    reason = "current_qualification_or_basis_unknown"
    assert reservation_reason(UsageError("READABILITY_REFUSED", "ignored", details={"reason": reason})) == reason
    assert reservation_reason(UsageError("READABILITY_REFUSED", "ignored", details={"reason": "private caller text"})) is None
    assert reservation_reason(UsageError("FOREIGN", "ignored", details={"reason": reason})) is None
    assert reservation_reason(ValueError(reason)) is None


def test_optional_reservation_diagnostic_preserves_legacy_failure_bytes():
    from evidence_wiki.errors import UsageError
    from evidence_wiki.mutation_contracts import canonical
    from evidence_wiki.readability_contracts import reservation_diagnostic
    row = {"operation": "evaluate", "error_code": "OLD_FAILURE", "field": None}
    legacy = UsageError("OLD_FAILURE", "ignored")
    unknown = UsageError("READABILITY_REFUSED", "ignored", details={"reason": "unrecognized"})
    for error in (legacy, unknown, ValueError("private text")):
        assert canonical({**row, **reservation_diagnostic(error)}) == canonical(row)
        assert "owner_reason" not in reservation_diagnostic(error)
    reason = "current_qualification_or_basis_unknown"
    assert reservation_diagnostic(UsageError("READABILITY_REFUSED", "ignored", details={"reason": reason})) == {"owner_reason": reason}
