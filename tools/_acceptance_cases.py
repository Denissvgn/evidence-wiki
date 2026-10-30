"""Fixed finite challenge inventory over current native and boundary owners."""

from __future__ import annotations

import copy


def _case(identifier, categories, selectors, *, evidence="assertions"):
    return {"id": identifier, "categories": list(categories), "severity": "critical",
            "evidence_requirement": evidence, "selectors": list(selectors)}


_CASES = (
    _case("stale-review", ("stale-review",), (
        "tests/test_semantic_judging.py::test_current_application_refuses_original_unit_source_and_candidate_drift",
    ), evidence="owner_receipt"),
    _case("forged-review", ("forged-review",), (
        "tests/test_semantic_judging.py::test_forged_exact_source_anchor_is_refused_before_native_review_application",
    ), evidence="owner_receipt"),
    _case("inert-hostile-input", ("prompt-injection", "hostile-evaluator-generator"), (
        "tests/test_acceptance_attack_paths.py::test_hostile_review_text_preserves_data_without_control_authority",
    ), evidence="owner_receipt"),
    _case("workspace-custody", ("cross-workspace-authority",), (
        "tests/test_historical_gate_adversarial.py::test_borrow_requires_exact_live_context_workspace_and_control",
        "tests/test_execution_isolation_adversarial.py::test_scoped_host_context_cannot_access_sibling_workspace",
    )),
    _case("exact-replay", ("replay",), (
        "tests/test_agent_execution.py::test_duplicate_dispatch_is_idempotent_and_altered_attempt_conflicts",
        "tests/test_mutation_operations.py::test_real_coverage_commit_duplicate_historical_replay_and_detachment",
    )),
    _case("request-race", ("request-race",), (
        "tests/test_acceptance_attack_paths.py::test_concurrent_identical_request_has_one_native_commit",
    ), evidence="owner_receipt"),
    _case("current-revocation", ("permission-revocation",), (
        "tests/test_protected_usability_adversarial.py::test_actual_signed_revision_and_revocation_are_public_current_rights",
        "tests/test_execution_isolation_adversarial.py::test_native_signer_recheck_observes_current_key_revocation",
    )),
    _case("output-correlation", ("output-substitution",), (
        "tests/test_agent_execution.py::test_tamper_replay_and_cross_host_authentication_refuse",
        "tests/test_service_authentication_adversarial.py::test_caller_correlated_observation_cannot_replace_exact_owned_intake",
    )),
    _case("bounded-cancellation", ("cancellation",), (
        "tests/test_agent_execution.py::test_real_cancellation_confirms_exit_and_preserves_unknown_accounting",
        "tests/test_agent_execution.py::test_completion_racing_cancel_retains_observed_completion",
        "tests/test_scoped_research_mcp.py::test_real_cancellation_suppresses_response_and_reconnect_retains_budget",
    )),
    _case("decisive-context", ("context-overload",), (
        "tests/test_packet_delivery_adversarial.py::test_complete_input_bound_includes_owner_appended_instructions",
        "tests/test_packet_delivery_adversarial.py::test_missing_decisive_mechanical_sections_cannot_claim_complete_review",
        "tests/test_review_context.py::test_missing_or_truncated_decisive_source_context_never_becomes_complete",
    )),
    _case("unsupported-output", ("unsupported-format",), (
        "tests/test_agent_execution.py::test_invalid_or_unbounded_worker_output_fails_finitely",
        "tests/test_planner_adversarial.py::test_authenticated_arbitrary_tool_proposal_cannot_reach_native_admission",
    )),
    _case("claimed-identity", ("forged-role",), (
        "tests/test_execution_isolation_adversarial.py::test_native_actual_principal_controller_cannot_hide_behind_selected_metadata",
    )),
    _case("unanimous-wrong-value", ("colluding-output",), (
        "tests/test_semantic_judging.py::test_public_finite_wrong_value_cannot_release_synthetic_model_passes",
    ), evidence="owner_receipt"),
    _case("blind-worker-input", ("hidden-answer-leakage",), (
        "tests/test_execution_isolation_adversarial.py::test_actual_child_only_receives_blinded_worker_projection",
        "tests/test_qualification_calibration.py::test_reference_labels_are_never_in_worker_input_including_nested_data",
    )),
    _case("frozen-client-policy", ("threshold-editing",), (
        "tests/test_acceptance_attack_paths.py::test_hostile_client_fields_refuse_without_native_effects",
    ), evidence="owner_receipt"),
    _case("critical-position", ("judge-order",), (
        "tests/test_acceptance_attack_paths.py::test_signed_position_failure_cannot_be_voted_away_or_lower_thresholds",
    ), evidence="owner_receipt"),
    _case("finite-repair", ("infinite-repair",), (
        "tests/test_acceptance_attack_paths.py::test_failed_planner_exhausts_finite_retries_and_replay_cannot_reset",
    ), evidence="owner_receipt"),
    _case("repair-counter-invariants", ("infinite-repair",), (
        "tests/test_execution_disagreement_adversarial.py::test_targeted_repair_cannot_reset_claim_lineage_or_expand_scope",
        "tests/test_execution_scheduling_adversarial.py::test_model_and_prompt_failover_cannot_reset_a_role_bucket_or_its_time_allowance",
    )),
)


def acceptance_cases():
    """Return a detached complete registry; source is evidence, never authority."""
    return copy.deepcopy(list(_CASES))
