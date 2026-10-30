"""Fixed automatic product scenarios, separate from adversarial categories."""

from copy import deepcopy


def _case(category, *selectors, evidence="assertions"):
    return {"id": category, "categories": [category], "severity": "critical",
            "evidence_requirement": evidence, "selectors": list(selectors)}


_CASES = (
    _case("supported-claim", "tests/test_semantic_judging.py::test_public_native_three_fresh_passes_advisory_report_and_historical_replay"),
    _case("reversed-quote", "tests/test_terminal_scenarios.py::test_retained_exact_quote_cannot_support_the_opposite_finite_value", evidence="owner_receipt"),
    _case("missing-evidence", "tests/test_terminal_scenarios.py::test_missing_original_evidence_finishes_partial_without_dropping_original", evidence="owner_receipt"),
    _case("judge-disagreement", "tests/test_disagreement_journey.py::test_targeted_repair_requires_new_material_and_fresh_first_passes"),
    _case("arithmetic-consensus", "tests/test_semantic_judging.py::test_public_finite_wrong_value_cannot_release_synthetic_model_passes", evidence="owner_receipt"),
    _case("unqualified-judge", "tests/test_semantic_judging.py::test_actual_calibration_registry_blocks_unknown_or_unqualified_judging"),
    _case("human-policy", "tests/test_semantic_judging.py::test_human_required_policy_returns_incompatible_without_operator_or_dispatch"),
    _case("spoofed-principal", "tests/test_execution_isolation_adversarial.py::test_native_actual_principal_controller_cannot_hide_behind_selected_metadata"),
    _case("source-drift", "tests/test_semantic_judging.py::test_current_application_refuses_original_unit_source_and_candidate_drift", evidence="owner_receipt"),
    _case("threshold-or-key-access", "tests/test_acceptance_attack_paths.py::test_hostile_client_fields_refuse_without_native_effects",
          "tests/test_execution_isolation_adversarial.py::test_actual_child_only_receives_blinded_worker_projection"),
    _case("provider-timeout", "tests/test_agent_execution.py::test_deadline_enforced_without_polling_or_cooperative_worker"),
    _case("repair-oscillation", "tests/test_disagreement.py::test_no_change_and_basis_oscillation_stop_even_when_fresh_votes_pass",
          "tests/test_execution_disagreement_adversarial.py::test_targeted_repair_cannot_reset_claim_lineage_or_expand_scope"),
    _case("durable-mutation-death", "tests/test_research_recovery.py::test_actual_child_hard_exit_retains_exact_receipt_without_second_effect"),
    _case("unattended-transports", "tests/test_autonomous_commands.py::test_installed_native_journeys_match_public_replay",
          "tests/test_scoped_research_mcp.py::test_full_installed_stdio_journey_and_refresh_without_elicitation"),
    _case("simulated-approval", "tests/test_outcome_observations.py::test_automated_and_simulated_classes_cannot_become_customer_or_human_activity"),
    _case("absent-usage", "tests/test_outcome_observations.py::test_empty_inputs_are_technical_neutral_and_never_claim_actual_business"),
    _case("ungranted-delivery", "tests/test_terminal_scenarios.py::test_ungranted_delivery_refuses_without_creating_publication", evidence="owner_receipt"),
    _case("truncated-context", "tests/test_review_context.py::test_missing_or_truncated_decisive_source_context_never_becomes_complete"),
)


def automatic_cases():
    """Detach all mandatory scenario obligations; no caller-selected omissions."""
    return deepcopy(list(_CASES))
