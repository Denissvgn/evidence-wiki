"""Closed observations for complete service cohorts and authenticated reuse."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCHEMA = json.loads(
    '{"$defs":{"ApplicationEvidence":{"additionalProperties":false,"properties":{"application_record_ref"'
    ':{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"applied_record_ref":{"pattern":"^sha256:[0-9a-'
    'f]{64}$","type":"string"},"attempt_id":{"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$","type":"strin'
    'g"},"attempt_pin":{"$ref":"#/$defs/Pin"},"authorship_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"'
    'string"},"basis_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"delivery_pin":{"$ref":"#/$d'
    'efs/Pin"},"envelope_pin":{"$ref":"#/$defs/Pin"},"final_observation_pin":{"$ref":"#/$defs/Pin"},"jour'
    'nal_receipt_pin":{"$ref":"#/$defs/Pin"},"observed_workspace_basis_id":{"pattern":"^sha256:[0-9a-f]{6'
    '4}$","type":"string"},"operation":{"enum":["evaluate","adjudicate"]},"original_custody_pin":{"$ref":'
    '"#/$defs/Pin"},"passed":{"type":"boolean"},"review_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"st'
    'ring"},"review_producer_id":{"anyOf":[{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},{"type":"n'
    'ull"}]},"run_id":{"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$","type":"string"},"semantic_material'
    '_id":{"anyOf":[{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},{"type":"null"}]},"timing_pin":{"'
    '$ref":"#/$defs/Pin"}},"required":["attempt_id","run_id","operation","application_record_ref","applie'
    'd_record_ref","review_id","authorship_id","envelope_pin","attempt_pin","final_observation_pin","deli'
    'very_pin","original_custody_pin","journal_receipt_pin","basis_id","semantic_material_id","observed_w'
    'orkspace_basis_id","review_producer_id","passed","timing_pin"],"type":"object"},"Birth":{"additional'
    'Properties":false,"properties":{"attempt_id":{"anyOf":[{"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}'
    '$","type":"string"},{"type":"null"}]},"command_manifest_pin":{"$ref":"#/$defs/Pin"},"completion_pin"'
    ':{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"config_ref":{"anyOf":[{"enum":["evidence","crit'
    'ic"]},{"type":"null"}]},"kind":{"enum":["local_review_worker","setup_or_owner_worker","unclassified"'
    ']},"obligation_id":{"anyOf":[{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},{"type":"null"}]},"'
    'ordinal":{"maximum":4096,"minimum":1,"type":"integer"},"pgid":{"minimum":1,"type":"integer"},"pid":{'
    '"minimum":1,"type":"integer"},"spawn_pin":{"$ref":"#/$defs/Pin"}},"required":["ordinal","pid","pgid"'
    ',"kind","attempt_id","obligation_id","config_ref","command_manifest_pin","spawn_pin","completion_pin'
    '"],"type":"object"},"ClaimJoin":{"additionalProperties":false,"properties":{"claim_id":{"enum":["obs'
    'erved-capacity","effective-capacity","adequacy","observed-demand","margin","independent-note"]},"leg'
    'acy_after_accepted":{"anyOf":[{"type":"boolean"},{"type":"null"}]},"legacy_before_accepted":{"anyOf"'
    ':[{"type":"boolean"},{"type":"null"}]},"legacy_normalized_content_id":{"anyOf":[{"pattern":"^sha256:'
    '[0-9a-f]{64}$","type":"string"},{"type":"null"}]},"legacy_required_refresh":{"type":"boolean"},"nati'
    've_basis_evidence_pins":{"items":{"$ref":"#/$defs/Pin"},"maxItems":4,"minItems":0,"type":"array"},"n'
    'ormalization_evidence_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"result":{"enum":["equ'
    'al_after_refresh","intentional_before_difference","mismatch","unknown"]},"selective_after_accepted":'
    '{"anyOf":[{"type":"boolean"},{"type":"null"}]},"selective_before_accepted":{"anyOf":[{"type":"boolea'
    'n"},{"type":"null"}]},"selective_normalized_content_id":{"anyOf":[{"pattern":"^sha256:[0-9a-f]{64}$"'
    ',"type":"string"},{"type":"null"}]},"selective_required_refresh":{"type":"boolean"}},"required":["cl'
    'aim_id","legacy_before_accepted","selective_before_accepted","legacy_after_accepted","selective_afte'
    'r_accepted","legacy_normalized_content_id","selective_normalized_content_id","legacy_required_refres'
    'h","selective_required_refresh","native_basis_evidence_pins","normalization_evidence_pin","result"],'
    '"type":"object"},"ComputationTranslation":{"additionalProperties":false,"properties":{"complete_norm'
    'alized_lineage_pin":{"$ref":"#/$defs/Pin"},"complete_normalized_result_pin":{"$ref":"#/$defs/Pin"},"'
    'current_native_computation_gate_pin":{"$ref":"#/$defs/Pin"},"fixture_definition_pin":{"$ref":"#/$def'
    's/Pin"},"fixture_result_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"mode":{"enum":["leg'
    'acy_full","selective_reuse"]},"native_result_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"}'
    ',"native_result_pin":{"$ref":"#/$defs/Pin"}},"required":["mode","native_result_id","fixture_result_i'
    'd","native_result_pin","fixture_definition_pin","complete_normalized_result_pin","complete_normalize'
    'd_lineage_pin","current_native_computation_gate_pin"],"type":"object"},"Control":{"additionalPropert'
    'ies":false,"properties":{"applied_count":{"anyOf":[{"maximum":4096,"minimum":0,"type":"integer"},{"t'
    'ype":"null"}]},"attempt_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"dispatch_count":{"a'
    'nyOf":[{"maximum":4096,"minimum":0,"type":"integer"},{"type":"null"}]},"failure":{"anyOf":[{"$ref":"'
    '#/$defs/Failure"},{"type":"null"}]},"native_refusal_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"n'
    'ull"}]},"slot_id":{"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$","type":"string"},"status":{"enum":'
    '["unstarted","reserved_refused_as_expected","failed","unknown"]}},"required":["slot_id","status","at'
    'tempt_pin","native_refusal_pin","dispatch_count","applied_count","failure"],"type":"object"},"Coordi'
    'nate":{"additionalProperties":false,"properties":{"case_id":{"enum":["independent-claim-text","indep'
    'endent-claim-limitations"]},"claim_id":{"enum":["observed-capacity","effective-capacity","adequacy",'
    '"observed-demand","margin","independent-note"]},"config_ref":{"enum":["evidence","critic"]},"cycle":'
    '{"enum":["baseline","first-change"]},"host_id":{"enum":["direct","scoped_mcp"]}},"required":["host_i'
    'd","case_id","cycle","claim_id","config_ref"],"type":"object"},"Cost":{"additionalProperties":false,'
    '"properties":{"coverage":{"enum":["complete","partial","unknown"]},"elapsed_seconds":{"anyOf":[{"des'
    'cription":"Finite, exact Python int or float, never bool; unknown is null only where permitted.","ma'
    'ximum":21600,"minimum":0,"type":"number"},{"type":"null"}]},"financial_cost_pin":{"anyOf":[{"$ref":"'
    '#/$defs/Pin"},{"type":"null"}]},"known_review_elapsed_subtotal_seconds":{"anyOf":[{"description":"Fi'
    'nite, exact Python int or float, never bool; unknown is null only where permitted.","maximum":21600,'
    '"minimum":0,"type":"number"},{"type":"null"}]},"model_calls":{"anyOf":[{"maximum":1000000,"minimum":'
    '0,"type":"integer"},{"type":"null"}]},"original_cost_report_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"'
    'type":"null"}]},"owner_validation_elapsed_seconds":{"anyOf":[{"description":"Finite, exact Python in'
    't or float, never bool; unknown is null only where permitted.","maximum":21600,"minimum":0,"type":"n'
    'umber"},{"type":"null"}]},"reuse_validation_elapsed_seconds":{"anyOf":[{"description":"Finite, exact'
    ' Python int or float, never bool; unknown is null only where permitted.","maximum":21600,"minimum":0'
    ',"type":"number"},{"type":"null"}]},"tokens":{"anyOf":[{"maximum":1000000000,"minimum":0,"type":"int'
    'eger"},{"type":"null"}]},"tool_calls":{"anyOf":[{"maximum":1000000,"minimum":0,"type":"integer"},{"t'
    'ype":"null"}]},"unknown_obligation_ids":{"items":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"}'
    ',"maxItems":24,"minItems":0,"type":"array"}},"required":["elapsed_seconds","known_review_elapsed_sub'
    'total_seconds","reuse_validation_elapsed_seconds","owner_validation_elapsed_seconds","model_calls","'
    'tool_calls","tokens","financial_cost_pin","original_cost_report_pin","coverage","unknown_obligation_'
    'ids"],"type":"object"},"Counters":{"additionalProperties":false,"properties":{"additional_local_revi'
    'ew_dispatches_changed":{"anyOf":[{"maximum":4096,"minimum":0,"type":"integer"},{"type":"null"}]},"ev'
    'idence_coverage":{"enum":["complete","partial","unknown"]},"failed_or_unknown_obligation_ids":{"item'
    's":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"maxItems":24,"minItems":0,"type":"array"},"f'
    'resh_completed_baseline_applications":{"anyOf":[{"maximum":12,"minimum":0,"type":"integer"},{"type":'
    '"null"}]},"fresh_completed_changed_applications":{"anyOf":[{"maximum":12,"minimum":0,"type":"integer'
    '"},{"type":"null"}]},"planned_baseline_obligations":{"const":12},"planned_changed_obligations":{"con'
    'st":12},"planned_control_journals":{"const":1},"planned_review_journals":{"const":12},"planned_servi'
    'ce_obligations":{"const":24},"status_counts":{"additionalProperties":false,"properties":{"failed":{"'
    'maximum":24,"minimum":0,"type":"integer"},"fresh_applied":{"maximum":24,"minimum":0,"type":"integer"'
    '},"refused":{"maximum":24,"minimum":0,"type":"integer"},"reuse_rejected":{"maximum":24,"minimum":0,"'
    'type":"integer"},"reused_validated":{"maximum":24,"minimum":0,"type":"integer"},"started_stop_unknow'
    'n":{"maximum":24,"minimum":0,"type":"integer"},"timed_out":{"maximum":24,"minimum":0,"type":"integer'
    '"},"unknown":{"maximum":24,"minimum":0,"type":"integer"},"unstarted":{"maximum":24,"minimum":0,"type'
    '":"integer"}},"required":["unstarted","started_stop_unknown","fresh_applied","reused_validated","ref'
    'used","failed","timed_out","reuse_rejected","unknown"],"type":"object"},"unstarted_obligation_ids":{'
    '"items":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"maxItems":24,"minItems":0,"type":"array'
    '"},"validated_reused_changed_obligations":{"anyOf":[{"maximum":12,"minimum":0,"type":"integer"},{"ty'
    'pe":"null"}]}},"required":["planned_service_obligations","planned_baseline_obligations","planned_cha'
    'nged_obligations","planned_review_journals","planned_control_journals","status_counts","fresh_comple'
    'ted_baseline_applications","fresh_completed_changed_applications","validated_reused_changed_obligati'
    'ons","additional_local_review_dispatches_changed","failed_or_unknown_obligation_ids","unstarted_obli'
    'gation_ids","evidence_coverage"],"type":"object"},"Differential":{"additionalProperties":false,"prop'
    'erties":{"accepted_useful_claims_removed":{"anyOf":[{"maximum":6,"minimum":0,"type":"integer"},{"typ'
    'e":"null"}]},"all_current_owners_validated":{"type":"boolean"},"all_planned_obligations_visible":{"t'
    'ype":"boolean"},"avoided_completed_applications":{"anyOf":[{"maximum":12,"minimum":-12,"type":"integ'
    'er"},{"type":"null"}]},"candidate_sha256":{"pattern":"^[0-9a-f]{64}$","type":"string"},"changed_obli'
    'gation_joins":{"items":{"$ref":"#/$defs/ObligationJoin"},"maxItems":12,"minItems":12,"type":"array"}'
    ',"claim_joins":{"items":{"$ref":"#/$defs/ClaimJoin"},"maxItems":6,"minItems":6,"type":"array"},"fail'
    'ure":{"anyOf":[{"$ref":"#/$defs/Failure"},{"type":"null"}]},"frozen_cases_retained":{"const":99},"le'
    'gacy_changed_completed_applications":{"anyOf":[{"maximum":12,"minimum":0,"type":"integer"},{"type":"'
    'null"}]},"legacy_plan_pin":{"$ref":"#/$defs/Pin"},"legacy_report_pin":{"$ref":"#/$defs/Pin"},"normal'
    'ization_contract_pin":{"$ref":"#/$defs/Pin"},"normalization_evidence_pin":{"anyOf":[{"$ref":"#/$defs'
    '/Pin"},{"type":"null"}]},"outcome":{"enum":["unstarted","unknown","failed","pair_pass"]},"pair_spec_'
    'id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"pair_spec_pin":{"$ref":"#/$defs/Pin"},"pros'
    'pective_case_results_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"publication_content_co'
    'mparison_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"qualification":{"const":false},"qu'
    'estion_original_comparison_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"schema_version":'
    '{"const":"review-dependency-native-differential/v1"},"selective_changed_completed_applications":{"an'
    'yOf":[{"maximum":12,"minimum":0,"type":"integer"},{"type":"null"}]},"selective_plan_pin":{"$ref":"#/'
    '$defs/Pin"},"selective_report_pin":{"$ref":"#/$defs/Pin"}},"required":["schema_version","pair_spec_p'
    'in","pair_spec_id","candidate_sha256","legacy_plan_pin","selective_plan_pin","legacy_report_pin","se'
    'lective_report_pin","claim_joins","changed_obligation_joins","question_original_comparison_pin","pub'
    'lication_content_comparison_pin","legacy_changed_completed_applications","selective_changed_complete'
    'd_applications","avoided_completed_applications","accepted_useful_claims_removed","all_planned_oblig'
    'ations_visible","all_current_owners_validated","frozen_cases_retained","prospective_case_results_pin'
    '","outcome","qualification","failure","normalization_contract_pin","normalization_evidence_pin"],"ty'
    'pe":"object"},"DispatchSnapshot":{"additionalProperties":false,"properties":{"attempt_inventory_pin"'
    ':{"$ref":"#/$defs/Pin"},"births":{"items":{"$ref":"#/$defs/Birth"},"maxItems":4096,"minItems":0,"typ'
    'e":"array"},"executor_class":{"const":"evidence_wiki.agent_execution.BoundedSubprocessExecutor"},"fa'
    'ilure":{"anyOf":[{"$ref":"#/$defs/Failure"},{"type":"null"}]},"guard_closed":{"type":"boolean"},"imp'
    'lementation_manifest_pin":{"$ref":"#/$defs/Pin"},"journal_inventory_pin":{"$ref":"#/$defs/Pin"},"obs'
    'erved_utc":{"description":"Timezone-aware UTC, actual valid calendar/time; no coercion; compare as i'
    'nstants.","format":"date-time","maxLength":40,"type":"string"},"phase":{"enum":["opening","baseline_'
    'complete","changed_opening","changed_complete","current_closing","cleanup_closed"]},"plan_id":{"patt'
    'ern":"^sha256:[0-9a-f]{64}$","type":"string"},"schema_version":{"const":"research-local-review-dispa'
    'tch-custody/v1"},"state":{"enum":["complete","unknown","failed"]},"tracker_identity":{"pattern":"^[A'
    '-Za-z0-9][A-Za-z0-9._-]{0,127}$","type":"string"}},"required":["schema_version","plan_id","phase","t'
    'racker_identity","executor_class","implementation_manifest_pin","state","births","attempt_inventory_'
    'pin","journal_inventory_pin","guard_closed","observed_utc","failure"],"type":"object"},"EligibilityR'
    'eceipt":{"additionalProperties":false,"properties":{"claim_id":{"enum":["observed-capacity","effecti'
    've-capacity","adequacy","observed-demand","margin","independent-note"]},"config_ref":{"enum":["evide'
    'nce","critic"]},"controller_ref":{"enum":["evidence-controller","critic-controller"]},"current_basis'
    '_id":{"anyOf":[{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},{"type":"null"}]},"current_custod'
    'y_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"current_journal_receipt_pin":{"anyOf":[{"'
    '$ref":"#/$defs/Pin"},{"type":"null"}]},"current_publication_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"'
    'type":"null"}]},"current_qualification_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"curr'
    'ent_review_producer_id":{"anyOf":[{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},{"type":"null"'
    '}]},"current_selected_review_id":{"anyOf":[{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},{"typ'
    'e":"null"}]},"current_semantic_material_id":{"anyOf":[{"pattern":"^sha256:[0-9a-f]{64}$","type":"str'
    'ing"},{"type":"null"}]},"current_strict_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"cur'
    'rent_usage_authority_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"current_workspace_basi'
    's_id":{"anyOf":[{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},{"type":"null"}]},"failure":{"an'
    'yOf":[{"$ref":"#/$defs/Failure"},{"type":"null"}]},"historical_obligation_id":{"pattern":"^sha256:[0'
    '-9a-f]{64}$","type":"string"},"obligation_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"o'
    'bserved_utc":{"description":"Timezone-aware UTC, actual valid calendar/time; no coercion; compare as'
    ' instants.","format":"date-time","maxLength":40,"type":"string"},"original_application":{"$ref":"#/$'
    'defs/ApplicationEvidence"},"original_basis_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"'
    'original_observed_workspace_basis_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"original_'
    'review_producer_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"original_semantic_material_'
    'id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"plan_id":{"pattern":"^sha256:[0-9a-f]{64}$"'
    ',"type":"string"},"reason":{"enum":["validated_retained_review","basis_changed","semantic_material_c'
    'hanged","producer_changed","original_authentication_failed","current_authentication_failed","role_or'
    '_controller_changed","qualification_unavailable","revoked_or_superseded","review_expired","clock_inv'
    'alid","delivery_incomplete","original_custody_unavailable","current_custody_changed","claim_not_acce'
    'pted","native_owner_unavailable","unsupported_contract","bound_exceeded"]},"round":{"enum":["before_'
    'refresh","original_0","original_1","current_closing"]},"schema_version":{"const":"research-retained-'
    'review-eligibility/v1"},"state":{"enum":["eligible","ineligible","unknown"]},"task_role":{"enum":["e'
    'valuator","critic"]}},"required":["schema_version","plan_id","obligation_id","historical_obligation_'
    'id","claim_id","config_ref","task_role","controller_ref","round","state","reason","original_applicat'
    'ion","original_basis_id","current_basis_id","original_semantic_material_id","current_semantic_materi'
    'al_id","original_observed_workspace_basis_id","current_workspace_basis_id","original_review_producer'
    '_id","current_review_producer_id","current_selected_review_id","current_strict_pin","current_publica'
    'tion_pin","current_usage_authority_pin","current_qualification_pin","current_journal_receipt_pin","c'
    'urrent_custody_pin","observed_utc","failure"],"type":"object"},"ExecutionPolicy":{"additionalPropert'
    'ies":false,"properties":{"authority_seconds":{"enum":[7200,14400,21600]},"credential_lifetime_second'
    's":{"enum":[7200,14400,21600]},"deadline_basis":{"const":"frozen_plan_expiry"},"profile":{"enum":["t'
    'wo-hour","four-hour","six-hour"]},"renewal":{"const":"forbidden"},"schema_version":{"const":"review-'
    'execution-budget-policy/v1"}},"required":["schema_version","profile","authority_seconds","credential'
    '_lifetime_seconds","deadline_basis","renewal"],"type":"object"},"Failure":{"additionalProperties":fa'
    'lse,"properties":{"error_code":{"anyOf":[{"maxLength":128,"minLength":1,"type":"string"},{"type":"nu'
    'll"}]},"kind":{"maxLength":128,"minLength":1,"type":"string"},"raw_pin":{"anyOf":[{"$ref":"#/$defs/P'
    'in"},{"type":"null"}]},"reason":{"anyOf":[{"maxLength":512,"minLength":1,"type":"string"},{"type":"n'
    'ull"}]}},"required":["kind","error_code","reason","raw_pin"],"type":"object"},"JournalObservation":{'
    '"additionalProperties":false,"properties":{"closing_receipt_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"'
    'type":"null"}]},"failure":{"anyOf":[{"$ref":"#/$defs/Failure"},{"type":"null"}]},"opening_receipt_pi'
    'n":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"raw_journal_pins":{"items":{"$ref":"#/$defs/P'
    'in"},"maxItems":4,"minItems":0,"type":"array"},"round_receipt_pins":{"items":{"$ref":"#/$defs/Pin"},'
    '"maxItems":3,"minItems":0,"type":"array"},"run_id":{"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$","'
    'type":"string"},"slot_id":{"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$","type":"string"},"state":{'
    '"enum":["unstarted","authenticated","authenticated_empty","refused","failed","unknown"]}},"required"'
    ':["slot_id","run_id","state","opening_receipt_pin","round_receipt_pins","closing_receipt_pin","raw_j'
    'ournal_pins","failure"],"type":"object"},"JournalReceipt":{"additionalProperties":false,"properties"'
    ':{"head_ref":{"anyOf":[{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},{"type":"null"}]},"origin'
    'al_pin":{"$ref":"#/$defs/Pin"},"record_count":{"maximum":4096,"minimum":0,"type":"integer"},"records'
    '_bytes":{"maximum":1048576,"minimum":0,"type":"integer"},"records_sha256":{"pattern":"^sha256:[0-9a-'
    'f]{64}$","type":"string"},"run_id":{"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$","type":"string"},'
    '"source_sha256":{"pattern":"^[0-9a-f]{64}$","type":"string"},"state_sha256":{"pattern":"^[0-9a-f]{64'
    '}$","type":"string"}},"required":["run_id","records_sha256","records_bytes","record_count","head_ref'
    '","source_sha256","state_sha256","original_pin"],"type":"object"},"JournalSlot":{"additionalProperti'
    'es":false,"properties":{"claim_id":{"anyOf":[{"enum":["observed-capacity","effective-capacity","adeq'
    'uacy","observed-demand","margin","independent-note"]},{"type":"null"}]},"cycle":{"enum":["baseline",'
    '"first-change"]},"kind":{"enum":["review","changed_basis_control"]},"native_run_id":{"pattern":"^[A-'
    'Za-z0-9][A-Za-z0-9._-]{0,127}$","type":"string"},"slot_id":{"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,'
    '127}$","type":"string"}},"required":["slot_id","native_run_id","kind","cycle","claim_id"],"type":"ob'
    'ject"},"NormalizationContract":{"additionalProperties":false,"properties":{"calculation_array_order"'
    ':{"const":"original native calculations order; v1 absence preserved; v2 required including empty arr'
    'ay"},"capture_time_translation_rule":{"const":"generated_host_capture_retrieved_at_to_frozen_fixture'
    '_observed_at_only"},"claim_array_order":{"const":"original validated native manifest order; same ful'
    'l6 claim membership; no accepted-only subset"},"claim_v1_fields":{"const":["id","question_slug","qua'
    'lification","text","scope","time","units","evidence","premises","derivation","limitations"]},"claim_'
    'v2_fields":{"const":["id","question_slug","qualification","text","scope","time","units","evidence","'
    'premises","derivation","limitations","calculations"]},"computation_translation_rule":{"const":"owner'
    '_verified_complete_same_definition_values_units_rounding_normalized_lineage"},"evidence_array_order"'
    ':{"const":"original native evidence order; all fields and null/presence retained"},"excluded_identit'
    'y_checks":{"const":"schema_version/mode/policy/legacy producer_id/new review_producer_id/candidate c'
    'hecked separately before normalization, never ignored for eligibility"},"excluded_top_level_publicat'
    'ion_fields":{"const":["schema_version","basis_id","policy_id","producer_id","review_producer_id","ev'
    'aluated_at"]},"original_outcome_fields":{"const":["original_id","question_slugs","accepted","unselec'
    'ted_question_slugs"]},"output_domain":{"const":"review-dependency-normalized-useful-content/v1"},"pu'
    'blication_fields":{"const":["assurance","verdict","questions","limitations","original_outcomes","sel'
    'ection_complete"]},"question_fields":{"const":["slug","original_id","question","status","accepted","'
    'claims","unresolved_claims","gaps"]},"question_order":{"const":"native manifest order; original outc'
    'omes native sorted original_id order"},"reason_normalization":{"const":"no normalization; preserve e'
    'xact reasons/order and fail unexpected routes"},"schema_version":{"const":"review-dependency-differe'
    'ntial-normalization/v1"},"source_coordinates":{"const":["shared","demand","unused"]},"source_transla'
    'tion_rule":{"const":"owner_verified_frozen_fixture_capture_recipe"},"translated_calculation_fields":'
    '{"const":["result_id"]},"translated_evidence_fields":{"const":["source_id","record_sha256","observed'
    '_at"]},"translation_evidence_required":{"const":true},"unchanged_calculation_fields":{"const":["poin'
    'ter","expected","form","unit","rounded"]},"unchanged_evidence_fields":{"const":["capture","quote","l'
    'ocation_hint","anchor"]},"unknown_field_rule":{"const":"refuse, never omit; validate native input sc'
    'hema then exact listed output keys"},"unresolved_claim_fields":{"const":["id","qualification","reaso'
    'ns"]},"verification_field_handling":{"const":"retain exact native verification originals; require su'
    'ccessful native mechanics/review/live gates separately, compare current outcomes; basis/signature/re'
    'view IDs are not claim content"}},"required":["schema_version","claim_v1_fields","claim_v2_fields","'
    'claim_array_order","evidence_array_order","calculation_array_order","translated_evidence_fields","tr'
    'anslated_calculation_fields","unchanged_evidence_fields","unchanged_calculation_fields","source_coor'
    'dinates","source_translation_rule","computation_translation_rule","capture_time_translation_rule","q'
    'uestion_fields","original_outcome_fields","publication_fields","question_order","unresolved_claim_fi'
    'elds","reason_normalization","verification_field_handling","excluded_top_level_publication_fields","'
    'excluded_identity_checks","unknown_field_rule","output_domain","translation_evidence_required"],"typ'
    'e":"object"},"NormalizationEvidence":{"additionalProperties":false,"properties":{"computation_transl'
    'ations":{"items":{"$ref":"#/$defs/ComputationTranslation"},"maxItems":2,"minItems":2,"type":"array"}'
    ',"legacy_native_publication_pin":{"$ref":"#/$defs/Pin"},"legacy_native_strict_pin":{"$ref":"#/$defs/'
    'Pin"},"legacy_normalized_claims_pin":{"$ref":"#/$defs/Pin"},"legacy_normalized_publication_pin":{"$r'
    'ef":"#/$defs/Pin"},"normalization_contract_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"'
    'normalization_contract_pin":{"$ref":"#/$defs/Pin"},"pair_spec_id":{"pattern":"^sha256:[0-9a-f]{64}$"'
    ',"type":"string"},"schema_version":{"const":"review-dependency-differential-normalization-evidence/v'
    '1"},"selective_native_publication_pin":{"$ref":"#/$defs/Pin"},"selective_native_strict_pin":{"$ref":'
    '"#/$defs/Pin"},"selective_normalized_claims_pin":{"$ref":"#/$defs/Pin"},"selective_normalized_public'
    'ation_pin":{"$ref":"#/$defs/Pin"},"source_translations":{"items":{"$ref":"#/$defs/SourceTranslation"'
    '},"maxItems":6,"minItems":6,"type":"array"}},"required":["schema_version","normalization_contract_pi'
    'n","normalization_contract_id","pair_spec_id","source_translations","computation_translations","lega'
    'cy_native_strict_pin","selective_native_strict_pin","legacy_native_publication_pin","selective_nativ'
    'e_publication_pin","legacy_normalized_claims_pin","selective_normalized_claims_pin","legacy_normaliz'
    'ed_publication_pin","selective_normalized_publication_pin"],"type":"object"},"NormalizedClaimDocumen'
    't":{"additionalProperties":false,"properties":{"claim_schema_version":{"enum":["evidence-strict-clai'
    'ms/v1","evidence-strict-claims/v2"]},"claims":{"items":{"oneOf":[{"$ref":"#/$defs/NormalizedClaimV1"'
    '},{"$ref":"#/$defs/NormalizedClaimV2"}]},"maxItems":6,"minItems":6,"type":"array"}},"required":["cla'
    'im_schema_version","claims"],"type":"object"},"NormalizedClaimV1":{"additionalProperties":false,"pro'
    'perties":{"derivation":{"anyOf":[{"maxLength":4096,"minLength":1,"type":"string"},{"type":"null"}]},'
    '"evidence":{"items":{"additionalProperties":false,"properties":{"anchor":{"anyOf":[{"additionalPrope'
    'rties":false,"properties":{"expected":{"maxLength":512,"minLength":1,"type":"string"},"pointer":{"ma'
    'xLength":512,"minLength":1,"type":"string"}},"required":["pointer","expected"],"type":"object"},{"ty'
    'pe":"null"}]},"capture":{"enum":["primary","excerpt","summary","unknown"]},"location_hint":{"anyOf":'
    '[{"maxLength":512,"minLength":1,"type":"string"},{"type":"null"}]},"observed_at":{"maxLength":40,"mi'
    'nLength":1,"type":"string"},"quote":{"anyOf":[{"maxLength":4096,"minLength":1,"type":"string"},{"typ'
    'e":"null"}]},"record_sha256":{"maxLength":71,"minLength":71,"pattern":"^sha256:[0-9a-f]{64}$","type"'
    ':"string"},"source_id":{"maxLength":256,"minLength":1,"type":"string"}},"required":["source_id","rec'
    'ord_sha256","observed_at","capture","quote","location_hint","anchor"],"type":"object"},"maxItems":16'
    ',"minItems":0,"type":"array"},"id":{"maxLength":128,"minLength":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9.'
    '_-]*$(?![\\\\s\\\\S])","type":"string"},"limitations":{"items":{"maxLength":4096,"minLength":1,"type":"s'
    'tring"},"maxItems":16,"minItems":0,"type":"array"},"premises":{"items":{"maxLength":128,"minLength":'
    '1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]*$(?![\\\\s\\\\S])","type":"string"},"maxItems":32,"minItems":0,"'
    'type":"array"},"qualification":{"enum":["attributed","supported","inference","contested","insufficie'
    'nt_evidence"]},"question_slug":{"maxLength":128,"minLength":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]*'
    '$(?![\\\\s\\\\S])","type":"string"},"scope":{"maxLength":4096,"minLength":1,"type":"string"},"text":{"ma'
    'xLength":4096,"minLength":1,"type":"string"},"time":{"maxLength":4096,"minLength":1,"type":"string"}'
    ',"units":{"maxLength":4096,"minLength":1,"type":"string"}},"required":["id","question_slug","qualifi'
    'cation","text","scope","time","units","evidence","premises","derivation","limitations"],"type":"obje'
    'ct"},"NormalizedClaimV2":{"additionalProperties":false,"properties":{"calculations":{"items":{"addit'
    'ionalProperties":false,"properties":{"expected":{"maxLength":512,"minLength":1,"type":"string"},"for'
    'm":{"enum":["value","formatted"]},"pointer":{"maxLength":512,"minLength":1,"pattern":"^/(graphs/[A-Z'
    'a-z][A-Za-z0-9_]*/outputs/[A-Za-z][A-Za-z0-9_]*|aggregations/[A-Za-z][A-Za-z0-9_]*/groups/[0-9]+/met'
    'rics/[A-Za-z][A-Za-z0-9_]*)$","type":"string"},"result_id":{"maxLength":71,"minLength":71,"pattern":'
    '"^sha256:[0-9a-f]{64}$","type":"string"},"rounded":{"type":"boolean"},"unit":{"maxLength":128,"minLe'
    'ngth":1,"type":"string"}},"required":["result_id","pointer","expected","form","unit","rounded"],"typ'
    'e":"object"},"maxItems":16,"minItems":0,"type":"array"},"derivation":{"anyOf":[{"maxLength":4096,"mi'
    'nLength":1,"type":"string"},{"type":"null"}]},"evidence":{"items":{"additionalProperties":false,"pro'
    'perties":{"anchor":{"anyOf":[{"additionalProperties":false,"properties":{"expected":{"maxLength":512'
    ',"minLength":1,"type":"string"},"pointer":{"maxLength":512,"minLength":1,"type":"string"}},"required'
    '":["pointer","expected"],"type":"object"},{"type":"null"}]},"capture":{"enum":["primary","excerpt","'
    'summary","unknown"]},"location_hint":{"anyOf":[{"maxLength":512,"minLength":1,"type":"string"},{"typ'
    'e":"null"}]},"observed_at":{"maxLength":40,"minLength":1,"type":"string"},"quote":{"anyOf":[{"maxLen'
    'gth":4096,"minLength":1,"type":"string"},{"type":"null"}]},"record_sha256":{"maxLength":71,"minLengt'
    'h":71,"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"source_id":{"maxLength":256,"minLength":1,'
    '"type":"string"}},"required":["source_id","record_sha256","observed_at","capture","quote","location_'
    'hint","anchor"],"type":"object"},"maxItems":16,"minItems":0,"type":"array"},"id":{"maxLength":128,"m'
    'inLength":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]*$(?![\\\\s\\\\S])","type":"string"},"limitations":{"it'
    'ems":{"maxLength":4096,"minLength":1,"type":"string"},"maxItems":16,"minItems":0,"type":"array"},"pr'
    'emises":{"items":{"maxLength":128,"minLength":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]*$(?![\\\\s\\\\S])"'
    ',"type":"string"},"maxItems":32,"minItems":0,"type":"array"},"qualification":{"enum":["attributed","'
    'supported","inference","contested","insufficient_evidence"]},"question_slug":{"maxLength":128,"minLe'
    'ngth":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]*$(?![\\\\s\\\\S])","type":"string"},"scope":{"maxLength":4'
    '096,"minLength":1,"type":"string"},"text":{"maxLength":4096,"minLength":1,"type":"string"},"time":{"'
    'maxLength":4096,"minLength":1,"type":"string"},"units":{"maxLength":4096,"minLength":1,"type":"strin'
    'g"}},"required":["id","question_slug","qualification","text","scope","time","units","evidence","prem'
    'ises","derivation","limitations","calculations"],"type":"object"},"Obligation":{"additionalPropertie'
    's":false,"properties":{"baseline_obligation_id":{"anyOf":[{"pattern":"^sha256:[0-9a-f]{64}$","type":'
    '"string"},{"type":"null"}]},"controller_ref":{"enum":["evidence-controller","critic-controller"]},"c'
    'oordinate":{"$ref":"#/$defs/Coordinate"},"execution_role":{"const":"evaluator"},"journal_key":{"maxL'
    'ength":512,"minLength":1,"type":"string"},"native_run_id":{"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,1'
    '27}$","type":"string"},"obligation_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"original'
    '_id":{"enum":["capacity","demand"]},"required":{"const":true},"task_role":{"enum":["evaluator","crit'
    'ic"]}},"required":["obligation_id","coordinate","original_id","execution_role","task_role","controll'
    'er_ref","journal_key","native_run_id","required","baseline_obligation_id"],"type":"object"},"Obligat'
    'ionJoin":{"additionalProperties":false,"properties":{"claim_id":{"enum":["observed-capacity","effect'
    'ive-capacity","adequacy","observed-demand","margin","independent-note"]},"config_ref":{"enum":["evid'
    'ence","critic"]},"legacy_completed_changed_application_ref":{"anyOf":[{"pattern":"^sha256:[0-9a-f]{6'
    '4}$","type":"string"},{"type":"null"}]},"legacy_obligation_id":{"pattern":"^sha256:[0-9a-f]{64}$","t'
    'ype":"string"},"legacy_status":{"enum":["unstarted","started_stop_unknown","fresh_applied","reused_v'
    'alidated","refused","failed","timed_out","reuse_rejected","unknown"]},"saved_completed_application":'
    '{"anyOf":[{"type":"boolean"},{"type":"null"}]},"selective_completed_changed_application_ref":{"anyOf'
    '":[{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},{"type":"null"}]},"selective_obligation_id":{'
    '"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"selective_retained_original_applied_ref":{"anyOf'
    '":[{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},{"type":"null"}]},"selective_status":{"enum":'
    '["unstarted","started_stop_unknown","fresh_applied","reused_validated","refused","failed","timed_out'
    '","reuse_rejected","unknown"]},"selective_zero_dispatch_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type'
    '":"null"}]}},"required":["claim_id","config_ref","legacy_obligation_id","selective_obligation_id","l'
    'egacy_status","selective_status","legacy_completed_changed_application_ref","selective_completed_cha'
    'nged_application_ref","selective_retained_original_applied_ref","selective_zero_dispatch_pin","saved'
    '_completed_application"],"type":"object"},"ObservationPolicy":{"additionalProperties":false,"propert'
    'ies":{"closing_validation":{"const":"fresh_native_owner_read"},"max_age_seconds":{"const":3600},"ori'
    'ginal_window_renewal":{"const":"forbidden"},"profile":{"const":"batch"},"schema_version":{"const":"o'
    'bserver-recency-policy/v1"},"witness_semantics":{"const":"observer_recency_bound_not_native_calibrat'
    'ion_witness"}},"required":["schema_version","profile","max_age_seconds","witness_semantics","closing'
    '_validation","original_window_renewal"],"type":"object"},"OwnerPass":{"additionalProperties":false,"'
    'properties":{"authority_custody_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"elapsed_sec'
    'onds":{"anyOf":[{"description":"Finite, exact Python int or float, never bool; unknown is null only '
    'where permitted.","maximum":21600,"minimum":0,"type":"number"},{"type":"null"}]},"eligibility_receip'
    't_pins":{"items":{"$ref":"#/$defs/Pin"},"maxItems":12,"minItems":0,"type":"array"},"failure":{"anyOf'
    '":[{"$ref":"#/$defs/Failure"},{"type":"null"}]},"host_qualification_pin":{"anyOf":[{"$ref":"#/$defs/'
    'Pin"},{"type":"null"}]},"journal_receipt_pins":{"items":{"$ref":"#/$defs/Pin"},"maxItems":13,"minIte'
    'ms":0,"type":"array"},"native_current_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"obser'
    'ved_utc":{"anyOf":[{"description":"Timezone-aware UTC, actual valid calendar/time; no coercion; comp'
    'are as instants.","format":"date-time","maxLength":40,"type":"string"},{"type":"null"}]},"publicatio'
    'n_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"round":{"enum":["original_0","original_1"'
    ',"current_closing"]},"semantic_identity":{"anyOf":[{"pattern":"^sha256:[0-9a-f]{64}$","type":"string'
    '"},{"type":"null"}]},"source_manifest_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"statu'
    's":{"enum":["unstarted","returned","refused","failed","unknown"]},"strict_pin":{"anyOf":[{"$ref":"#/'
    '$defs/Pin"},{"type":"null"}]}},"required":["round","status","strict_pin","publication_pin","native_c'
    'urrent_pin","host_qualification_pin","journal_receipt_pins","eligibility_receipt_pins","source_manif'
    'est_pin","authority_custody_pin","semantic_identity","observed_utc","elapsed_seconds","failure"],"ty'
    'pe":"object"},"PairSpec":{"additionalProperties":false,"properties":{"candidate_manifest_pin":{"$ref'
    '":"#/$defs/Pin"},"candidate_sha256":{"pattern":"^[0-9a-f]{64}$","type":"string"},"case_id":{"enum":['
    '"independent-claim-text","independent-claim-limitations"]},"changed_cycle":{"const":"first-change"},'
    '"clock_contract_pin":{"$ref":"#/$defs/Pin"},"expected_legacy_refresh_ids":{"items":{"enum":["observe'
    'd-capacity","effective-capacity","adequacy","observed-demand","margin","independent-note"]},"maxItem'
    's":6,"minItems":6,"type":"array"},"expected_selective_refresh_ids":{"items":{"enum":["observed-capac'
    'ity","effective-capacity","adequacy","observed-demand","margin","independent-note"]},"maxItems":6,"m'
    'inItems":0,"type":"array"},"fixture_pin":{"$ref":"#/$defs/Pin"},"frozen_case_count":{"const":99},"fr'
    'ozen_case_manifest_pin":{"$ref":"#/$defs/Pin"},"host_id":{"enum":["direct","scoped_mcp"]},"legacy_po'
    'licy":{"$ref":"#/$defs/PolicyPin"},"minimum_avoided_completed_review_applications":{"const":1},"muta'
    'tion_pin":{"$ref":"#/$defs/Pin"},"normalization_contract_id":{"pattern":"^sha256:[0-9a-f]{64}$","typ'
    'e":"string"},"normalization_contract_pin":{"$ref":"#/$defs/Pin"},"prospective_case_manifest_pin":{"$'
    'ref":"#/$defs/Pin"},"schema_version":{"const":"review-dependency-differential-pair-spec/v1"},"scope"'
    ':{"const":"local_bundled_deterministic_worker_fixture"},"selective_policy":{"$ref":"#/$defs/PolicyPi'
    'n"},"verdict_table_pin":{"$ref":"#/$defs/Pin"}},"required":["schema_version","candidate_sha256","can'
    'didate_manifest_pin","host_id","case_id","changed_cycle","fixture_pin","mutation_pin","verdict_table'
    '_pin","normalization_contract_id","clock_contract_pin","legacy_policy","selective_policy","expected_'
    'legacy_refresh_ids","expected_selective_refresh_ids","frozen_case_count","frozen_case_manifest_pin",'
    '"prospective_case_manifest_pin","scope","minimum_avoided_completed_review_applications","normalizati'
    'on_contract_pin"],"type":"object"},"PhaseCaps":{"additionalProperties":false,"properties":{"cleanup"'
    ':{"maximum":60,"minimum":1,"type":"integer"},"global_close":{"maximum":1500,"minimum":1,"type":"inte'
    'ger"},"job":{"maximum":600,"minimum":1,"type":"integer"},"mutation":{"maximum":600,"minimum":1,"type'
    '":"integer"},"owner_read":{"maximum":1200,"minimum":1,"type":"integer"},"setup":{"maximum":600,"mini'
    'mum":1,"type":"integer"}},"required":["setup","job","mutation","owner_read","global_close","cleanup"'
    '],"type":"object"},"Pin":{"additionalProperties":false,"properties":{"path":{"maxLength":8192,"patte'
    'rn":"^/","type":"string"},"raw_sha256":{"pattern":"^[0-9a-f]{64}$","type":"string"},"size_bytes":{"m'
    'aximum":16777216,"minimum":0,"type":"integer"}},"required":["path","raw_sha256","size_bytes"],"type"'
    ':"object"},"Plan":{"additionalProperties":false,"properties":{"candidate_manifest_pin":{"$ref":"#/$d'
    'efs/Pin"},"candidate_sha256":{"pattern":"^[0-9a-f]{64}$","type":"string"},"claim_schema_version":{"e'
    'num":["evidence-strict-claims/v1","evidence-strict-claims/v2"]},"control":{"const":"reserved_native_'
    'attempt_changed_basis_no_dispatch_no_application"},"elapsed_cap_seconds":{"maximum":21600,"minimum":'
    '1,"type":"integer"},"execution_policy":{"$ref":"#/$defs/ExecutionPolicy"},"execution_policy_sha256":'
    '{"pattern":"^[0-9a-f]{64}$","type":"string"},"expires_at":{"description":"Timezone-aware UTC, actual'
    ' valid calendar/time; no coercion; compare as instants.","format":"date-time","maxLength":40,"type":'
    '"string"},"frozen_at":{"description":"Timezone-aware UTC, actual valid calendar/time; no coercion; c'
    'ompare as instants.","format":"date-time","maxLength":40,"type":"string"},"group_overhead_cap_second'
    's":{"maximum":21600,"minimum":0,"type":"integer"},"host_identity_pin":{"$ref":"#/$defs/Pin"},"input_'
    'manifest_pin":{"$ref":"#/$defs/Pin"},"journal_slots":{"items":{"$ref":"#/$defs/JournalSlot"},"maxIte'
    'ms":13,"minItems":13,"type":"array"},"measured_recipe_pin":{"$ref":"#/$defs/Pin"},"mode":{"enum":["l'
    'egacy_full","selective_reuse"]},"obligation_source_pin":{"$ref":"#/$defs/Pin"},"obligations":{"items'
    '":{"$ref":"#/$defs/Obligation"},"maxItems":24,"minItems":24,"type":"array"},"observation_policy":{"$'
    'ref":"#/$defs/ObservationPolicy"},"observation_policy_sha256":{"pattern":"^[0-9a-f]{64}$","type":"st'
    'ring"},"pair_spec_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"pair_spec_pin":{"$ref":"#'
    '/$defs/Pin"},"phase_caps":{"$ref":"#/$defs/PhaseCaps"},"producer_manifest_pin":{"$ref":"#/$defs/Pin"'
    '},"protected_policy":{"$ref":"#/$defs/PolicyPin"},"review_producer_id":{"pattern":"^sha256:[0-9a-f]{'
    '64}$","type":"string"},"schema_version":{"const":"review-dependency-workload-plan/v5"},"scope":{"con'
    'st":"all_six_claims_two_questions_global_policy_sources_computation"},"source_protocol_limits_pin":{'
    '"$ref":"#/$defs/Pin"},"stdin":{"const":"closed"},"study":{"const":"full"}},"required":["schema_versi'
    'on","study","mode","pair_spec_pin","pair_spec_id","candidate_sha256","candidate_manifest_pin","host_'
    'identity_pin","input_manifest_pin","obligation_source_pin","obligations","journal_slots","protected_'
    'policy","claim_schema_version","review_producer_id","producer_manifest_pin","phase_caps","group_over'
    'head_cap_seconds","elapsed_cap_seconds","measured_recipe_pin","execution_policy","execution_policy_s'
    'ha256","observation_policy","observation_policy_sha256","source_protocol_limits_pin","frozen_at","ex'
    'pires_at","control","scope","stdin"],"type":"object"},"PolicyPin":{"additionalProperties":false,"pro'
    'perties":{"content_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"raw_pin":{"$ref":"#/$def'
    's/Pin"},"schema_version":{"maxLength":128,"minLength":1,"type":"string"}},"required":["schema_versio'
    'n","raw_pin","content_id"],"type":"object"},"Report":{"additionalProperties":false,"properties":{"ca'
    'ndidate_sha256":{"pattern":"^[0-9a-f]{64}$","type":"string"},"case_id":{"enum":["independent-claim-t'
    'ext","independent-claim-limitations"]},"cleanup_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"'
    '}]},"control":{"$ref":"#/$defs/Control"},"cost":{"$ref":"#/$defs/Cost"},"counters":{"$ref":"#/$defs/'
    'Counters"},"dispatch_snapshot_pins":{"items":{"$ref":"#/$defs/Pin"},"maxItems":6,"minItems":0,"type"'
    ':"array"},"failure":{"anyOf":[{"$ref":"#/$defs/Failure"},{"type":"null"}]},"global_closing_utc":{"an'
    'yOf":[{"description":"Timezone-aware UTC, actual valid calendar/time; no coercion; compare as instan'
    'ts.","format":"date-time","maxLength":40,"type":"string"},{"type":"null"}]},"host_id":{"enum":["dire'
    'ct","scoped_mcp"]},"journal_observations":{"items":{"$ref":"#/$defs/JournalObservation"},"maxItems":'
    '13,"minItems":13,"type":"array"},"mode":{"enum":["legacy_full","selective_reuse"]},"obligations":{"i'
    'tems":{"$ref":"#/$defs/Obligation"},"maxItems":24,"minItems":24,"type":"array"},"original_response_m'
    'anifest_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"owner_passes":{"items":{"$ref":"#/$'
    'defs/OwnerPass"},"maxItems":3,"minItems":3,"type":"array"},"pair_spec_id":{"pattern":"^sha256:[0-9a-'
    'f]{64}$","type":"string"},"plan_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"plan_pin":{'
    '"$ref":"#/$defs/Pin"},"qualification":{"const":false},"raw_journal_manifest_pin":{"anyOf":[{"$ref":"'
    '#/$defs/Pin"},{"type":"null"}]},"reference_oracle_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"nul'
    'l"}]},"resolutions":{"items":{"$ref":"#/$defs/Resolution"},"maxItems":24,"minItems":24,"type":"array'
    '"},"schema_version":{"const":"review-dependency-workload-observation/v5"},"stage_observations":{"ite'
    'ms":{"$ref":"#/$defs/Stage"},"maxItems":3,"minItems":3,"type":"array"},"study":{"const":"full"},"ter'
    'minal_outcome":{"enum":["unstarted","started_stop_unknown","completed","refused","failed","unavailab'
    'le"]}},"required":["schema_version","plan_pin","plan_id","pair_spec_id","candidate_sha256","mode","h'
    'ost_id","case_id","study","terminal_outcome","qualification","obligations","resolutions","journal_ob'
    'servations","owner_passes","stage_observations","dispatch_snapshot_pins","control","counters","cost"'
    ',"reference_oracle_pin","original_response_manifest_pin","raw_journal_manifest_pin","cleanup_pin","g'
    'lobal_closing_utc","failure"],"type":"object"},"Resolution":{"additionalProperties":false,"propertie'
    's":{"additional_completed_review_applications":{"anyOf":[{"maximum":4096,"minimum":0,"type":"integer'
    '"},{"type":"null"}]},"additional_review_dispatches":{"anyOf":[{"maximum":4096,"minimum":0,"type":"in'
    'teger"},{"type":"null"}]},"attempt_history_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"'
    'eligibility_receipt_pins":{"items":{"$ref":"#/$defs/Pin"},"maxItems":4,"minItems":0,"type":"array"},'
    '"failure":{"anyOf":[{"$ref":"#/$defs/Failure"},{"type":"null"}]},"final_action":{"enum":["none","fre'
    'sh_application","retained_review"]},"fresh_application":{"anyOf":[{"$ref":"#/$defs/ApplicationEviden'
    'ce"},{"type":"null"}]},"historical_obligation_id":{"anyOf":[{"pattern":"^sha256:[0-9a-f]{64}$","type'
    '":"string"},{"type":"null"}]},"obligation_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"p'
    'lanned_action":{"enum":["fresh_required","retained_candidate"]},"reuse_validation_elapsed_seconds":{'
    '"anyOf":[{"description":"Finite, exact Python int or float, never bool; unknown is null only where p'
    'ermitted.","maximum":21600,"minimum":0,"type":"number"},{"type":"null"}]},"review_elapsed_seconds":{'
    '"anyOf":[{"description":"Finite, exact Python int or float, never bool; unknown is null only where p'
    'ermitted.","maximum":21600,"minimum":0,"type":"number"},{"type":"null"}]},"status":{"enum":["unstart'
    'ed","started_stop_unknown","fresh_applied","reused_validated","refused","failed","timed_out","reuse_'
    'rejected","unknown"]},"zero_dispatch_receipt_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]}'
    '},"required":["obligation_id","status","planned_action","final_action","historical_obligation_id","f'
    'resh_application","eligibility_receipt_pins","zero_dispatch_receipt_pin","attempt_history_pin","revi'
    'ew_elapsed_seconds","reuse_validation_elapsed_seconds","additional_review_dispatches","additional_co'
    'mpleted_review_applications","failure"],"type":"object"},"SourceTranslation":{"additionalProperties"'
    ':false,"properties":{"coordinate":{"enum":["shared","demand","unused"]},"current_native_source_gate_'
    'pin":{"$ref":"#/$defs/Pin"},"fixture_capture_recipe_pin":{"$ref":"#/$defs/Pin"},"fixture_observed_at'
    '":{"format":"date-time","maxLength":40,"type":"string"},"fixture_record_sha256":{"pattern":"^sha256:'
    '[0-9a-f]{64}$","type":"string"},"fixture_source_id":{"enum":["shared","demand","unused"]},"mode":{"e'
    'num":["legacy_full","selective_reuse"]},"native_capture_pin":{"$ref":"#/$defs/Pin"},"native_observed'
    '_at":{"format":"date-time","maxLength":40,"type":"string"},"native_record_pin":{"$ref":"#/$defs/Pin"'
    '},"native_record_sha256":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"native_revision_id":{"'
    'pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"native_source_id":{"maxLength":256,"minLength":1,'
    '"type":"string"},"native_usage_deposit_pin":{"$ref":"#/$defs/Pin"}},"required":["mode","coordinate",'
    '"native_source_id","native_revision_id","native_record_sha256","native_observed_at","fixture_source_'
    'id","fixture_record_sha256","fixture_observed_at","native_capture_pin","native_usage_deposit_pin","n'
    'ative_record_pin","fixture_capture_recipe_pin","current_native_source_gate_pin"],"type":"object"},"S'
    'tage":{"additionalProperties":false,"properties":{"accepted_claim_ids":{"items":{"enum":["observed-c'
    'apacity","effective-capacity","adequacy","observed-demand","margin","independent-note"]},"maxItems":'
    '6,"minItems":0,"type":"array"},"accepted_original_ids":{"items":{"enum":["capacity","demand"]},"maxI'
    'tems":2,"minItems":0,"type":"array"},"failure":{"anyOf":[{"$ref":"#/$defs/Failure"},{"type":"null"}]'
    '},"native_basis_rows_pin":{"anyOf":[{"$ref":"#/$defs/Pin"},{"type":"null"}]},"native_claim_ids":{"it'
    'ems":{"enum":["observed-capacity","effective-capacity","adequacy","observed-demand","margin","indepe'
    'ndent-note"]},"maxItems":6,"minItems":0,"type":"array"},"publication_pin":{"anyOf":[{"$ref":"#/$defs'
    '/Pin"},{"type":"null"}]},"stage":{"enum":["baseline","before_refresh","after_refresh"]},"status":{"e'
    'num":["unstarted","returned","refused","failed","unknown"]},"strict_pin":{"anyOf":[{"$ref":"#/$defs/'
    'Pin"},{"type":"null"}]}},"required":["stage","status","strict_pin","publication_pin","native_basis_r'
    'ows_pin","native_claim_ids","accepted_claim_ids","accepted_original_ids","failure"],"type":"object"}'
    ',"ZeroDispatchReceipt":{"additionalProperties":false,"properties":{"cleanup_snapshot_pin":{"$ref":"#'
    '/$defs/Pin"},"closing_snapshot_pin":{"$ref":"#/$defs/Pin"},"current_journal_inventory_pin":{"$ref":"'
    '#/$defs/Pin"},"failure":{"anyOf":[{"$ref":"#/$defs/Failure"},{"type":"null"}]},"invocation_api_call_'
    'count":{"anyOf":[{"maximum":4096,"minimum":0,"type":"integer"},{"type":"null"}]},"new_completed_revi'
    'ew_application_count":{"anyOf":[{"maximum":4096,"minimum":0,"type":"integer"},{"type":"null"}]},"new'
    '_local_review_worker_birth_count":{"anyOf":[{"maximum":4096,"minimum":0,"type":"integer"},{"type":"n'
    'ull"}]},"new_review_attempt_count":{"anyOf":[{"maximum":4096,"minimum":0,"type":"integer"},{"type":"'
    'null"}]},"obligation_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"opening_snapshot_pin":'
    '{"$ref":"#/$defs/Pin"},"plan_id":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"schema_version'
    '":{"const":"research-retained-review-zero-dispatch/v1"},"scope":{"const":"new_local_bundled_review_w'
    'orker_processes_in_changed_interval"},"state":{"enum":["verified_zero","nonzero","unknown"]},"unclas'
    'sified_birth_count":{"anyOf":[{"maximum":4096,"minimum":0,"type":"integer"},{"type":"null"}]}},"requ'
    'ired":["schema_version","plan_id","obligation_id","state","scope","opening_snapshot_pin","closing_sn'
    'apshot_pin","cleanup_snapshot_pin","current_journal_inventory_pin","new_review_attempt_count","new_l'
    'ocal_review_worker_birth_count","new_completed_review_application_count","unclassified_birth_count",'
    '"invocation_api_call_count","failure"],"type":"object"}},"$id":"review-dependency-reuse-accounting-c'
    'ontract/v1","$schema":"https://json-schema.org/draft/2020-12/schema","oneOf":[{"$ref":"#/$defs/PairS'
    'pec"},{"$ref":"#/$defs/Plan"},{"$ref":"#/$defs/Report"},{"$ref":"#/$defs/EligibilityReceipt"},{"$ref'
    '":"#/$defs/DispatchSnapshot"},{"$ref":"#/$defs/ZeroDispatchReceipt"},{"$ref":"#/$defs/Differential"}'
    ',{"$ref":"#/$defs/NormalizationContract"},{"$ref":"#/$defs/NormalizationEvidence"}]}'
)

NORMALIZATION_CONTRACT = json.loads(
    '{"calculation_array_order":"original native calculations order; v1 absence preserved; v2 required in'
    'cluding empty array","capture_time_translation_rule":"generated_host_capture_retrieved_at_to_frozen_'
    'fixture_observed_at_only","claim_array_order":"original validated native manifest order; same full6 '
    'claim membership; no accepted-only subset","claim_v1_fields":["id","question_slug","qualification","'
    'text","scope","time","units","evidence","premises","derivation","limitations"],"claim_v2_fields":["i'
    'd","question_slug","qualification","text","scope","time","units","evidence","premises","derivation",'
    '"limitations","calculations"],"computation_translation_rule":"owner_verified_complete_same_definitio'
    'n_values_units_rounding_normalized_lineage","evidence_array_order":"original native evidence order; '
    'all fields and null/presence retained","excluded_identity_checks":"schema_version/mode/policy/legacy'
    ' producer_id/new review_producer_id/candidate checked separately before normalization, never ignored'
    ' for eligibility","excluded_top_level_publication_fields":["schema_version","basis_id","policy_id","'
    'producer_id","review_producer_id","evaluated_at"],"original_outcome_fields":["original_id","question'
    '_slugs","accepted","unselected_question_slugs"],"output_domain":"review-dependency-normalized-useful'
    '-content/v1","publication_fields":["assurance","verdict","questions","limitations","original_outcome'
    's","selection_complete"],"question_fields":["slug","original_id","question","status","accepted","cla'
    'ims","unresolved_claims","gaps"],"question_order":"native manifest order; original outcomes native s'
    'orted original_id order","reason_normalization":"no normalization; preserve exact reasons/order and '
    'fail unexpected routes","schema_version":"review-dependency-differential-normalization/v1","source_c'
    'oordinates":["shared","demand","unused"],"source_translation_rule":"owner_verified_frozen_fixture_ca'
    'pture_recipe","translated_calculation_fields":["result_id"],"translated_evidence_fields":["source_id'
    '","record_sha256","observed_at"],"translation_evidence_required":true,"unchanged_calculation_fields"'
    ':["pointer","expected","form","unit","rounded"],"unchanged_evidence_fields":["capture","quote","loca'
    'tion_hint","anchor"],"unknown_field_rule":"refuse, never omit; validate native input schema then exa'
    'ct listed output keys","unresolved_claim_fields":["id","qualification","reasons"],"verification_fiel'
    'd_handling":"retain exact native verification originals; require successful native mechanics/review/'
    'live gates separately, compare current outcomes; basis/signature/review IDs are not claim content"}'
)

PLAN = "review-dependency-workload-plan/v5"
PAIR_PLAN = "review-dependency-workload-plan/v6"
PAIR_RESERVATION = {"schema_version": "review-pair-closing-reservation/v1",
    "mode_close_seconds": 900, "pair_close_seconds": 600,
    "budget_source": "global_close", "pilot_fit": "unmeasured"}
# Preserve the original definition bundle before adding opt-in versions, so
# every original schema export retains its exact serialized bytes.
_ORIGINAL_SCHEMA_DEFS = copy.deepcopy(_SCHEMA["$defs"])
_SCHEMA["$defs"]["PairPlan"] = copy.deepcopy(_SCHEMA["$defs"]["Plan"])
_SCHEMA["$defs"]["PairPlan"]["properties"]["schema_version"] = {"const": PAIR_PLAN}
_SCHEMA["$defs"]["PairPlan"]["properties"]["pair_closing_reservation"] = {"const": PAIR_RESERVATION}
_SCHEMA["$defs"]["PairPlan"]["required"].append("pair_closing_reservation")
_SCHEMA["$defs"]["PairNormalizationEvidence"] = copy.deepcopy(_SCHEMA["$defs"]["NormalizationEvidence"])
_SCHEMA["$defs"]["PairNormalizationEvidence"]["properties"]["schema_version"] = {"const": "review-dependency-differential-normalization-evidence/v2"}
_SCHEMA["$defs"]["PairNormalizationEvidence"]["properties"]["current_pair_closing_pin"] = {"$ref": "#/$defs/Pin"}
_SCHEMA["$defs"]["PairNormalizationEvidence"]["required"].append("current_pair_closing_pin")
_PRE_EXTENDED_SCHEMA_DEFS = copy.deepcopy(_SCHEMA["$defs"])
EXTENDED_PLAN = "review-dependency-workload-plan/v7"
EXTENDED_REPORT = "review-dependency-workload-observation/v6"
_EXTENDED_NAMES = {name: "Extended" + name for name in
                   ("Cost", "OwnerPass", "Resolution", "PhaseCaps", "ExecutionPolicy", "Report")}


def _extended_shape(value):
    if type(value) is dict:
        result = {key: _extended_shape(item) for key, item in value.items()}
        if result.get("maximum") == 21600:
            result["maximum"] = 86400
        if "$ref" in result:
            name = result["$ref"].removeprefix("#/$defs/")
            result["$ref"] = "#/$defs/" + _EXTENDED_NAMES.get(name, name)
        return result
    if type(value) is list:
        return [_extended_shape(item) for item in value]
    return value


for _name, _extended_name in _EXTENDED_NAMES.items():
    _SCHEMA["$defs"][_extended_name] = _extended_shape(_SCHEMA["$defs"][_name])
_SCHEMA["$defs"]["ExtendedPhaseCaps"]["properties"]["job"]["maximum"] = 3600
for _key, _maximum in (("setup", 1200), ("mutation", 1200), ("owner_read", 3600), ("global_close", 10800)):
    _SCHEMA["$defs"]["ExtendedPhaseCaps"]["properties"][_key]["maximum"] = _maximum
_extended_execution = _SCHEMA["$defs"]["ExtendedExecutionPolicy"]["properties"]
_extended_execution["schema_version"] = {"const": "review-execution-budget-policy/v2"}
_extended_execution["profile"] = {"enum": ["eight-hour", "sixteen-hour", "twenty-four-hour"]}
for _key in ("authority_seconds", "credential_lifetime_seconds"):
    _extended_execution[_key] = {"enum": [28800, 57600, 86400]}
_SCHEMA["$defs"]["ExtendedReport"]["properties"]["schema_version"] = {"const": EXTENDED_REPORT}
_SCHEMA["$defs"]["FormalApplicationPolicy"] = {
    "type": "object", "additionalProperties": False,
    "properties": {"schema_version": {"const": "review-formal-application-policy/v1"},
        "service_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
        "native_worker_seconds": {"const": 600},
        "application_lease": {"type": "object", "additionalProperties": False,
            "properties": {"schema_version": {"const": "research-review-application-lease/v2"},
                "seconds": {"type": "integer", "minimum": 1, "maximum": 3600}},
            "required": ["schema_version", "seconds"]},
        "execution_profile_schema": {"const": "research-execution-profile/v17"},
        "job_schema": {"const": "research-host-job/v6"}},
    "required": ["schema_version", "service_seconds", "native_worker_seconds", "application_lease", "execution_profile_schema", "job_schema"]}
_SCHEMA["$defs"]["ExtendedPlan"] = _extended_shape(_SCHEMA["$defs"]["PairPlan"])
_SCHEMA["$defs"]["ExtendedPlan"]["properties"].update(
    schema_version={"const": EXTENDED_PLAN}, formal_application_policy={"$ref": "#/$defs/FormalApplicationPolicy"})
_SCHEMA["$defs"]["ExtendedPlan"]["required"].append("formal_application_policy")
_SCHEMA["$defs"]["ExtendedClosingReservation"] = {
    "type": "object", "additionalProperties": False,
    "properties": {"schema_version": {"const": "review-pair-closing-reservation/v2"},
        "mode_close_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
        "pair_close_seconds": {"type": "integer", "minimum": 1, "maximum": 7200},
        "budget_source": {"const": "global_close"}, "pilot_fit": {"const": "unmeasured"}},
    "required": ["schema_version", "mode_close_seconds", "pair_close_seconds", "budget_source", "pilot_fit"]}
_SCHEMA["$defs"]["ExtendedPlan"]["properties"]["pair_closing_reservation"] = {"$ref": "#/$defs/ExtendedClosingReservation"}
# Freeze all earlier export bundles before adding the explicitly selected route.
_PRE_COLLECTION_SCHEMA_DEFS = copy.deepcopy(_SCHEMA["$defs"])
COLLECTION_PLAN = "review-dependency-workload-plan/v8"
COLLECTION_REPORT = "review-dependency-workload-observation/v7"
from tools._review_journal_artifacts import POLICY as JOURNAL_COLLECTION_POLICY
from tools._review_journal_artifacts import RECEIPT as COLLECTION_JOURNAL_RECEIPT
from tools._review_journal_artifacts import selected as collection_selected

_SCHEMA["$defs"]["CollectionPlan"] = copy.deepcopy(_SCHEMA["$defs"]["ExtendedPlan"])
_SCHEMA["$defs"]["CollectionPlan"]["properties"].update(
    schema_version={"const": COLLECTION_PLAN}, journal_collection_policy={"const": JOURNAL_COLLECTION_POLICY})
_SCHEMA["$defs"]["CollectionPlan"]["required"].append("journal_collection_policy")
_SCHEMA["$defs"]["CollectionReport"] = copy.deepcopy(_SCHEMA["$defs"]["ExtendedReport"])
_SCHEMA["$defs"]["CollectionReport"]["properties"].update(
    schema_version={"const": COLLECTION_REPORT}, journal_collection_policy={"const": JOURNAL_COLLECTION_POLICY})
_SCHEMA["$defs"]["CollectionReport"]["required"].append("journal_collection_policy")
_SCHEMA["$defs"]["CollectionJournalReceipt"] = copy.deepcopy(_SCHEMA["$defs"]["JournalReceipt"])
_collection_receipt = _SCHEMA["$defs"]["CollectionJournalReceipt"]
_collection_receipt["properties"]["records_bytes"]["maximum"] = 16_777_216
_collection_receipt["properties"].update(schema_version={"const": COLLECTION_JOURNAL_RECEIPT},
    physical_nodes={"type": "integer", "minimum": 1, "maximum": 65_536},
    expanded_bytes={"type": "integer", "minimum": 2, "maximum": 16_777_216},
    artifact_kind={"enum": ["legacy_array", "paged_collection"]},
    page_pins={"type": "array", "maxItems": 4096, "items": {"$ref": "#/$defs/Pin"}})
_collection_receipt["required"].extend(("schema_version", "physical_nodes", "expanded_bytes", "artifact_kind", "page_pins"))


# Preserve the complete Collection-era export bundle before adding definitions.
_PRE_LONG_SCHEMA_DEFS = copy.deepcopy(_SCHEMA["$defs"])
LONG_PLAN = "review-dependency-workload-plan/v9"
OWNER_PLAN = "review-dependency-workload-plan/v10"
LONG_REPORT = "review-dependency-workload-observation/v8"
OWNER_REPORT = "review-dependency-workload-observation/v9"
from tools import _review_control_ceiling as ceiling_policy

_SCHEMA["$defs"]["ControlPhaseCeilingPolicy"] = {"const": ceiling_policy.POLICY}
_SCHEMA["$defs"]["LongPhaseCaps"] = copy.deepcopy(_SCHEMA["$defs"]["ExtendedPhaseCaps"])
_SCHEMA["$defs"]["LongPhaseCaps"]["properties"]["mutation"]["maximum"] = 4500
for _name, _old, _version in (("LongPlan", "CollectionPlan", LONG_PLAN), ("LongReport", "CollectionReport", LONG_REPORT)):
    _SCHEMA["$defs"][_name] = copy.deepcopy(_SCHEMA["$defs"][_old])
    _SCHEMA["$defs"][_name]["properties"].update(schema_version={"const": _version},
        phase_ceiling_policy={"$ref": "#/$defs/ControlPhaseCeilingPolicy"},
        phase_ceiling_policy_sha256={"const": ceiling_policy.binding()["phase_ceiling_policy_sha256"]})
    _SCHEMA["$defs"][_name]["required"].extend(ceiling_policy.FIELDS)
_SCHEMA["$defs"]["LongPlan"]["properties"]["phase_caps"] = {"$ref": "#/$defs/LongPhaseCaps"}

_PRE_OWNER_SCHEMA_DEFS = copy.deepcopy(_SCHEMA["$defs"])
for _key, _value in (("profile", "forty-eight-hour"), ("authority_seconds", 172800),
                     ("credential_lifetime_seconds", 172800)):
    _SCHEMA["$defs"]["ExtendedExecutionPolicy"]["properties"][_key]["enum"].append(_value)
# Extended observer policy belongs to the current owner contract only.
_SCHEMA["$defs"]["ObservationPolicy"] = {"anyOf": [
    copy.deepcopy(_SCHEMA["$defs"]["ObservationPolicy"]),
    {"additionalProperties": False, "properties": {
        "closing_validation": {"const": "fresh_native_owner_read"},
        "max_age_seconds": {"const": 7200},
        "original_window_renewal": {"const": "forbidden"},
        "profile": {"const": "batch-extended"},
        "schema_version": {"const": "observer-recency-policy/v2"},
        "witness_semantics": {"const": "observer_recency_bound_not_native_calibration_witness"}},
     "required": ["schema_version", "profile", "max_age_seconds", "witness_semantics",
                  "closing_validation", "original_window_renewal"], "type": "object"}]}
_SCHEMA["$defs"]["OwnerControlPhaseCeilingPolicy"] = {"const": ceiling_policy.OWNER_POLICY}
for _name, _old, _version in (("OwnerPlan", "LongPlan", OWNER_PLAN), ("OwnerReport", "LongReport", OWNER_REPORT)):
    _SCHEMA["$defs"][_name] = copy.deepcopy(_SCHEMA["$defs"][_old])
    _SCHEMA["$defs"][_name]["properties"].update(schema_version={"const": _version},
        phase_ceiling_policy={"$ref": "#/$defs/OwnerControlPhaseCeilingPolicy"},
        phase_ceiling_policy_sha256={"const": ceiling_policy.binding(ceiling_policy.OWNER_PROFILE)["phase_ceiling_policy_sha256"]})

_SCHEMA["$defs"]["OwnerPlan"]["properties"]["elapsed_cap_seconds"]["maximum"] = 172800
_SCHEMA["$defs"]["OwnerReport"]["properties"]["cost"] = copy.deepcopy(_SCHEMA["$defs"]["ExtendedCost"])
_SCHEMA["$defs"]["OwnerReport"]["properties"]["cost"]["properties"]["elapsed_seconds"]["anyOf"][0]["maximum"] = 172800

REPORT = "review-dependency-workload-observation/v5"
PAIR = "review-dependency-differential-pair-spec/v1"
DIFFERENTIAL = "review-dependency-native-differential/v1"
CLAIMS = ("observed-capacity", "effective-capacity", "observed-demand", "adequacy", "margin", "independent-note")
ROUNDS = ("original_0", "original_1", "current_closing")
STAGES = ("baseline", "before_refresh", "after_refresh")
STATUSES = tuple(_SCHEMA["$defs"]["Counters"]["properties"]["status_counts"]["properties"])


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def extended_closing_reservation(global_close, selected=None):
    """Freeze one bounded decomposition of the already selected close budget."""
    require(type(global_close) is int and 1500 <= global_close <= 10800, "accounting_pair_reservation_invalid")
    mode = max(1, global_close // 3)
    result = {"schema_version": "review-pair-closing-reservation/v2", "mode_close_seconds": mode,
              "pair_close_seconds": global_close - mode, "budget_source": "global_close", "pilot_fit": "unmeasured"} if selected is None else selected
    validate("ExtendedClosingReservation", result)
    require(result["mode_close_seconds"] + result["pair_close_seconds"] == global_close, "accounting_pair_reservation_invalid")
    return copy.deepcopy(result)


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode()


def identity(domain, value):
    return "sha256:" + hashlib.sha256(domain.encode() + b"\0" + canonical(value)).hexdigest()


def document_id(value):
    return identity(value["schema_version"], value)


def schema_documents():
    """Keep original export bundles byte-stable; new versions include new defs."""
    return {name: {"$ref": "#/$defs/" + name,
                   "$defs": copy.deepcopy(_ORIGINAL_SCHEMA_DEFS if name in _ORIGINAL_SCHEMA_DEFS else
                       _PRE_EXTENDED_SCHEMA_DEFS if name in _PRE_EXTENDED_SCHEMA_DEFS else
                       _PRE_COLLECTION_SCHEMA_DEFS if name in _PRE_COLLECTION_SCHEMA_DEFS else
                       _PRE_LONG_SCHEMA_DEFS if name in _PRE_LONG_SCHEMA_DEFS else
                       _PRE_OWNER_SCHEMA_DEFS if name in _PRE_OWNER_SCHEMA_DEFS else _SCHEMA["$defs"])}
            for name in _SCHEMA["$defs"]}


def _same(left, right):
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(_same(value, right[key]) for key, value in left.items())
    if type(left) is list:
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right, strict=True))
    return left == right


def _shape(value, schema):
    if "$ref" in schema:
        reference = schema["$ref"]
        require(reference.startswith("#/$defs/") and reference[8:] in _SCHEMA["$defs"], "accounting_reference_invalid")
        return _shape(value, _SCHEMA["$defs"][reference[8:]])
    for kind in ("anyOf", "oneOf"):
        if kind in schema:
            matches = 0
            for branch in schema[kind]:
                try:
                    _shape(value, branch)
                    matches += 1
                except ValueError:
                    pass
            require(matches >= 1 if kind == "anyOf" else matches == 1, "accounting_union_invalid")
    if "const" in schema:
        require(_same(value, schema["const"]), "accounting_constant_invalid")
    if "enum" in schema:
        require(any(_same(value, candidate) for candidate in schema["enum"]), "accounting_enum_invalid")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    kind = schema.get("type")
    if kind == "number":
        require(type(value) in {int, float} and math.isfinite(value), "accounting_number_invalid")
    elif kind is not None:
        require(kind in types and type(value) is types[kind], "accounting_type_invalid")
    if type(value) is dict:
        require(set(schema.get("required", [])) <= value.keys(), "accounting_required_field_missing")
        properties = schema.get("properties", {})
        extra = value.keys() - properties.keys()
        require(not extra or schema.get("additionalProperties") is not False, "accounting_unknown_field")
        for key, item in value.items():
            if key in properties:
                _shape(item, properties[key])
            elif type(schema.get("additionalProperties")) is dict:
                _shape(item, schema["additionalProperties"])
    elif type(value) is list:
        require(schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 65_536), "accounting_array_bound")
        for item in value:
            if "items" in schema:
                _shape(item, schema["items"])
    elif type(value) is str:
        value.encode("utf-8", errors="strict")
        require(schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 16_777_216), "accounting_string_bound")
        if "pattern" in schema:
            require(re.search(schema["pattern"], value) is not None, "accounting_string_pattern")
        if schema.get("format") == "date-time":
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            require(moment.tzinfo is not None and moment.utcoffset() == timedelta(0), "accounting_clock_invalid")
    elif type(value) in {int, float}:
        require(math.isfinite(value) and schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf), "accounting_number_bound")


def validate(name, value):
    """Apply closed shape, scalar, depth, collection and byte bounds."""
    if name == "Plan" and type(value) is dict and value.get("schema_version") in {LONG_PLAN, OWNER_PLAN}:
        name = "OwnerPlan" if value["schema_version"] == OWNER_PLAN else "LongPlan"
    if name == "Report" and type(value) is dict and value.get("schema_version") in {LONG_REPORT, OWNER_REPORT}:
        name = "OwnerReport" if value["schema_version"] == OWNER_REPORT else "LongReport"
    if name == "Plan" and type(value) is dict and value.get("schema_version") == COLLECTION_PLAN:
        name = "CollectionPlan"
    if name == "Report" and type(value) is dict and value.get("schema_version") == COLLECTION_REPORT:
        name = "CollectionReport"
    if name == "JournalReceipt" and type(value) is dict and value.get("schema_version") == COLLECTION_JOURNAL_RECEIPT:
        name = "CollectionJournalReceipt"
    if name == "Plan" and type(value) is dict and value.get("schema_version") == PAIR_PLAN:
        name = "PairPlan"
    if name == "Plan" and type(value) is dict and value.get("schema_version") == EXTENDED_PLAN:
        name = "ExtendedPlan"
    if name == "Report" and type(value) is dict and value.get("schema_version") == EXTENDED_REPORT:
        name = "ExtendedReport"
    if name == "NormalizationEvidence" and type(value) is dict and value.get("schema_version") == "review-dependency-differential-normalization-evidence/v2":
        name = "PairNormalizationEvidence"
    require(name in _SCHEMA["$defs"], "accounting_schema_unsupported")
    maximum = {"Report": 16_777_216, "ExtendedReport": 16_777_216, "CollectionReport": 16_777_216, "LongReport": 16_777_216, "OwnerReport": 16_777_216, "Differential": 4_194_304}.get(name, 1_048_576)
    try:
        raw = canonical(value)
    except (ValueError, TypeError, RecursionError, OverflowError) as error:
        raise ValueError("accounting_document_invalid") from error
    require(len(raw) <= maximum, "accounting_document_bound")
    pending, nodes = [(value, 1)], 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        require(depth <= 16 and nodes <= 65_536, "accounting_tree_bound")
        if type(item) is dict:
            require(len(item) <= 4096 and all(type(key) is str for key in item), "accounting_mapping_bound")
            pending.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif type(item) is list:
            require(len(item) <= 4096, "accounting_collection_bound")
            pending.extend((part, depth + 1) for part in item)
        else:
            require(item is None or type(item) in {str, int, bool, float}, "accounting_value_invalid")
            if type(item) is float:
                require(math.isfinite(item), "accounting_number_invalid")
    if name in {"Plan", "PairPlan", "ExtendedPlan", "CollectionPlan", "LongPlan"} and type(value) is dict:
        _shape(value.get("observation_policy"), _PRE_OWNER_SCHEMA_DEFS["ObservationPolicy"])
    if name in {"Plan", "PairPlan", "ExtendedPlan", "CollectionPlan", "LongPlan"} and type(value) is dict:
        _shape(value.get("execution_policy"), _PRE_OWNER_SCHEMA_DEFS["ExtendedExecutionPolicy"]
               if name in {"ExtendedPlan", "CollectionPlan", "LongPlan"} else _SCHEMA["$defs"]["ExecutionPolicy"])
    _shape(value, _SCHEMA["$defs"][name])
    if name == "NormalizedClaimDocument":
        fields = NORMALIZATION_CONTRACT["claim_v1_fields" if value["claim_schema_version"] == "evidence-strict-claims/v1" else "claim_v2_fields"]
        require(all(set(row) == set(fields) for row in value["claims"]), "accounting_normalized_claim_schema_mixed")
    if name in {"NormalizationEvidence", "PairNormalizationEvidence"}:
        require({(row["mode"], row["coordinate"]) for row in value["source_translations"]}
            == {(mode, coordinate) for mode in ("legacy_full", "selective_reuse") for coordinate in ("shared", "demand", "unused")}
            and {row["mode"] for row in value["computation_translations"]} == {"legacy_full", "selective_reuse"}, "accounting_normalization_denominator_invalid")
    return copy.deepcopy(value)


def decode(name, raw):
    require(type(raw) is bytes and len(raw) <= {"Report": 16_777_216, "ExtendedReport": 16_777_216, "CollectionReport": 16_777_216, "LongReport": 16_777_216, "OwnerReport": 16_777_216, "Differential": 4_194_304}.get(name, 1_048_576), "accounting_document_bound")

    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, "accounting_duplicate_key")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("accounting_number_invalid")

    value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=pairs, parse_constant=constant)
    return validate(name, value)


def read_pin(pin, *, roots, maximum=16_777_216):
    """Byte custody is checked under explicit parent-selected private roots."""
    validate("Pin", pin)
    path = Path(pin["path"])
    require(bool(roots) and path.is_absolute() and path.resolve(strict=True) == path
            and any(path.is_relative_to(Path(root).resolve(strict=True)) for root in roots), "accounting_pin_scope_invalid")
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size == pin["size_bytes"]
            and before.st_size <= maximum, "accounting_pin_file_invalid")
    require(hasattr(os, "O_NOFOLLOW"), "accounting_pin_platform_unsupported")
    def observed(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        require(observed(os.fstat(descriptor)) == observed(before), "accounting_pin_changed")
        chunks, size = [], 0
        while chunk := os.read(descriptor, min(65_536, maximum + 1 - size)):
            size += len(chunk)
            require(size <= maximum, "accounting_pin_file_invalid")
            chunks.append(chunk)
        raw = b"".join(chunks)
        require(observed(os.fstat(descriptor)) == observed(before), "accounting_pin_changed")
    finally:
        os.close(descriptor)
    require(path.resolve(strict=True) == path and observed(path.lstat()) == observed(before)
            and len(raw) == pin["size_bytes"] and hashlib.sha256(raw).hexdigest() == pin["raw_sha256"], "accounting_pin_changed")
    return raw


def obligations(assignments, manifest):
    """Preserve the full native fixture mapping and all twenty-four services."""
    from tools._review_dependency_workload import assignment_groups, journal_run_id
    assignment_groups(assignments)
    question_originals = {row["slug"]: row["original_id"] for row in manifest["questions"]}
    claim_originals = {row["id"]: question_originals[row["question_slug"]] for row in manifest["claims"]}
    require(set(claim_originals) == set(CLAIMS), "accounting_fixture_claims_invalid")
    result, baseline = [], {}
    for assignment in assignments:
        coordinate = {key: assignment[key] for key in ("host_id", "case_id", "cycle", "claim_id", "config_ref")}
        validate("Coordinate", coordinate)
        identifier = identity("review-dependency-service-obligation/v1", coordinate)
        key = (coordinate["claim_id"], coordinate["config_ref"])
        historical = None
        if coordinate["cycle"] == "baseline":
            require(key not in baseline, "accounting_duplicate_obligation")
            baseline[key] = identifier
        else:
            require(key in baseline, "accounting_baseline_obligation_missing")
            historical = baseline[key]
        require(assignment["original_id"] == claim_originals[coordinate["claim_id"]], "accounting_original_mapping_invalid")
        row = {"obligation_id": identifier, "coordinate": coordinate, "original_id": assignment["original_id"],
               "execution_role": assignment["execution_role"], "task_role": assignment["task_role"],
               "controller_ref": assignment["controller_ref"], "journal_key": assignment["journal_key"],
               "native_run_id": journal_run_id(assignment["journal_key"]), "required": True, "baseline_obligation_id": historical}
        result.append(validate("Obligation", row))
    require(len(result) == 24 and len({row["obligation_id"] for row in result}) == 24, "accounting_obligation_denominator_invalid")
    return result


def validate_pair(pair):
    validate("PairSpec", pair)
    require(len(set(pair["expected_legacy_refresh_ids"])) == 6 and set(pair["expected_legacy_refresh_ids"]) == set(CLAIMS)
            and len(set(pair["expected_selective_refresh_ids"])) == len(pair["expected_selective_refresh_ids"]), "accounting_refresh_inventory_invalid")
    require(pair["normalization_contract_id"] == document_id(NORMALIZATION_CONTRACT), "accounting_normalization_contract_changed")
    for mode, schema in (("legacy_policy", "evidence-strict-policy/v1"), ("selective_policy", "evidence-strict-policy/v2")):
        require(pair[mode]["schema_version"] == schema, "accounting_policy_mode_invalid")
    return pair


def validate_plan(plan, pair=None):
    validate("Plan", plan)
    ceiling_policy.validate(plan, required=plan["schema_version"] in {LONG_PLAN, OWNER_PLAN})
    if plan["schema_version"] == PAIR_PLAN:
        require(plan["pair_closing_reservation"] == PAIR_RESERVATION
                and plan["phase_caps"]["global_close"] == 1500,
                "accounting_pair_reservation_invalid")
    if plan["schema_version"] in {EXTENDED_PLAN, COLLECTION_PLAN, LONG_PLAN, OWNER_PLAN}:
        extended_closing_reservation(plan["phase_caps"]["global_close"], plan["pair_closing_reservation"])
        from tools import run_review_dependency_workload as workload
        workload.validate_formal_application_policy(plan["formal_application_policy"], plan["execution_policy"], plan["phase_caps"])
        ceilings = workload.formal_phase_ceilings(plan["observation_policy"], plan["execution_policy"], ceiling_binding=plan)
        require(all(plan["phase_caps"][name] <= maximum for name, maximum in ceilings.items()), "accounting_phase_cap_invalid")
        caps = plan["phase_caps"]
        expected_elapsed = workload.allocation_seconds(caps, 24) + 2 * caps["global_close"] + 12 * plan["group_overhead_cap_seconds"]
        require(plan["elapsed_cap_seconds"] == expected_elapsed <= plan["execution_policy"]["authority_seconds"], "accounting_allocation_invalid")
    from tools._execution_budget import validate_execution_binding, validate_execution_window
    from tools._observation_freshness import validate_observation_policy
    from tools._review_dependency_workload import digest, journal_run_id
    validate_execution_binding(plan["execution_policy"], plan)
    validate_observation_policy(plan["observation_policy"])
    validate_execution_window(plan["execution_policy"], plan["frozen_at"], plan["expires_at"])
    require(plan["execution_policy_sha256"] == digest(plan["execution_policy"]), "accounting_execution_binding_invalid")
    rows = plan["obligations"]
    coordinates = [row["coordinate"] for row in rows]
    expected = {(cycle, claim, config) for cycle in ("baseline", "first-change") for claim in CLAIMS for config in ("evidence", "critic")}
    require(len({row["obligation_id"] for row in rows}) == 24
            and {(row["cycle"], row["claim_id"], row["config_ref"]) for row in coordinates} == expected, "accounting_obligation_denominator_invalid")
    host_cases = {(row["host_id"], row["case_id"]) for row in coordinates}
    require(len(host_cases) == 1, "accounting_coordinate_binding_invalid")
    by_subject = {(row["coordinate"]["cycle"], row["coordinate"]["claim_id"], row["coordinate"]["config_ref"]): row for row in rows}
    for row in rows:
        coordinate = row["coordinate"]
        baseline = by_subject[("baseline", coordinate["claim_id"], coordinate["config_ref"])]
        require(row["obligation_id"] == identity("review-dependency-service-obligation/v1", coordinate)
                and row["journal_key"] == "/".join(coordinate[key] for key in ("host_id", "case_id", "cycle", "claim_id"))
                and row["native_run_id"] == journal_run_id(row["journal_key"])
                and row["controller_ref"] == coordinate["config_ref"] + "-controller"
                and row["task_role"] == ("critic" if coordinate["config_ref"] == "critic" else "evaluator")
                and row["baseline_obligation_id"] == (None if coordinate["cycle"] == "baseline" else baseline["obligation_id"]), "accounting_obligation_binding_invalid")
    slots = plan["journal_slots"]
    require(len({row["slot_id"] for row in slots}) == 13 and len({row["native_run_id"] for row in slots}) == 13, "accounting_journal_denominator_invalid")
    reviews = [row for row in slots if row["kind"] == "review"]
    require(len(reviews) == 12 and {(row["cycle"], row["claim_id"], row["native_run_id"]) for row in reviews}
            == {(row["coordinate"]["cycle"], row["coordinate"]["claim_id"], row["native_run_id"]) for row in rows}, "accounting_journal_binding_invalid")
    control = [row for row in slots if row["kind"] == "changed_basis_control"]
    host, case = next(iter(host_cases))
    require(len(control) == 1 and control[0]["claim_id"] is None and control[0]["cycle"] == "first-change"
            and control[0]["native_run_id"] == "control-" + hashlib.sha256((host + "/" + case).encode()).hexdigest()[:24], "accounting_control_binding_invalid")
    mode = plan["protected_policy"]
    require(mode["schema_version"] == ("evidence-strict-policy/v1" if plan["mode"] == "legacy_full" else "evidence-strict-policy/v2"), "accounting_policy_mode_invalid")
    if pair is not None:
        validate_pair(pair)
        require(document_id(pair) == plan["pair_spec_id"] and pair["candidate_sha256"] == plan["candidate_sha256"]
                and (pair["host_id"], pair["case_id"]) == (host, case)
                and plan["protected_policy"] == pair["legacy_policy" if plan["mode"] == "legacy_full" else "selective_policy"], "accounting_pair_binding_invalid")
    return plan


def initialize_report(plan, plan_pin, pair):
    validate_plan(plan, pair)
    refresh = set(pair["expected_legacy_refresh_ids"] if plan["mode"] == "legacy_full" else pair["expected_selective_refresh_ids"])
    resolutions = []
    for row in plan["obligations"]:
        retained = row["coordinate"]["cycle"] == "first-change" and row["coordinate"]["claim_id"] not in refresh
        resolutions.append({"obligation_id": row["obligation_id"], "status": "unstarted", "planned_action": "retained_candidate" if retained else "fresh_required",
            "final_action": "none", "historical_obligation_id": row["baseline_obligation_id"], "fresh_application": None,
            "eligibility_receipt_pins": [], "zero_dispatch_receipt_pin": None, "attempt_history_pin": None,
            "review_elapsed_seconds": None, "reuse_validation_elapsed_seconds": None, "additional_review_dispatches": None,
            "additional_completed_review_applications": None, "failure": None})
    host, case = (plan["obligations"][0]["coordinate"][key] for key in ("host_id", "case_id"))
    owner = {key: None for key in ("strict_pin", "publication_pin", "native_current_pin", "host_qualification_pin", "source_manifest_pin", "authority_custody_pin", "semantic_identity", "observed_utc", "elapsed_seconds", "failure")}
    report = {**ceiling_policy.validate(plan), "schema_version": ceiling_policy.version(LONG_REPORT, plan) if plan["schema_version"] in {LONG_PLAN, OWNER_PLAN} else COLLECTION_REPORT if plan["schema_version"] == COLLECTION_PLAN else EXTENDED_REPORT if plan["schema_version"] == EXTENDED_PLAN else REPORT, "plan_pin": plan_pin, "plan_id": document_id(plan), "pair_spec_id": plan["pair_spec_id"],
        "candidate_sha256": plan["candidate_sha256"], "mode": plan["mode"], "host_id": host, "case_id": case,
        "study": "full", "terminal_outcome": "unstarted", "qualification": False, "obligations": copy.deepcopy(plan["obligations"]),
        "resolutions": resolutions,
        "journal_observations": [{"slot_id": row["slot_id"], "run_id": row["native_run_id"], "state": "unstarted", "opening_receipt_pin": None,
            "round_receipt_pins": [], "closing_receipt_pin": None, "raw_journal_pins": [], "failure": None} for row in plan["journal_slots"]],
        "owner_passes": [{**copy.deepcopy(owner), "round": label, "status": "unstarted", "journal_receipt_pins": [], "eligibility_receipt_pins": []} for label in ROUNDS],
        "stage_observations": [{"stage": stage, "status": "unstarted", "strict_pin": None, "publication_pin": None, "native_basis_rows_pin": None,
            "native_claim_ids": [], "accepted_claim_ids": [], "accepted_original_ids": [], "failure": None} for stage in STAGES],
        "dispatch_snapshot_pins": [], "control": {"slot_id": next(row["slot_id"] for row in plan["journal_slots"] if row["kind"] == "changed_basis_control"),
            "status": "unstarted", "attempt_pin": None, "native_refusal_pin": None, "dispatch_count": None, "applied_count": None, "failure": None},
        "counters": {}, "cost": {"elapsed_seconds": None, "known_review_elapsed_subtotal_seconds": None, "reuse_validation_elapsed_seconds": None,
            "owner_validation_elapsed_seconds": None, "model_calls": None, "tool_calls": None, "tokens": None, "financial_cost_pin": None,
            "original_cost_report_pin": None, "coverage": "unknown", "unknown_obligation_ids": [row["obligation_id"] for row in resolutions]},
        "reference_oracle_pin": None, "original_response_manifest_pin": None, "raw_journal_manifest_pin": None,
        "cleanup_pin": None, "global_closing_utc": None, "failure": None}
    if collection_selected(plan):
        report["journal_collection_policy"] = dict(JOURNAL_COLLECTION_POLICY)
    report["counters"] = counters(report)
    validate("Report", report)
    return report


def counters(report):
    rows = report["resolutions"]
    by_id = {row["obligation_id"]: row for row in report["obligations"]}
    counts = Counter(row["status"] for row in rows)
    complete = all(row["status"] in {"fresh_applied", "reused_validated"} for row in rows)
    def count_fresh(cycle):
        selected = [row for row in rows if by_id[row["obligation_id"]]["coordinate"]["cycle"] == cycle]
        return sum(row["status"] == "fresh_applied" for row in selected) if all(row["status"] in {"fresh_applied", "reused_validated"} for row in selected) else None
    changed = [row for row in rows if by_id[row["obligation_id"]]["coordinate"]["cycle"] == "first-change"]
    return {"planned_service_obligations": 24, "planned_baseline_obligations": 12, "planned_changed_obligations": 12,
        "planned_review_journals": 12, "planned_control_journals": 1, "status_counts": {key: counts[key] for key in STATUSES},
        "fresh_completed_baseline_applications": count_fresh("baseline"), "fresh_completed_changed_applications": count_fresh("first-change"),
        "validated_reused_changed_obligations": sum(row["status"] == "reused_validated" for row in changed) if complete else None,
        "additional_local_review_dispatches_changed": sum(row["additional_review_dispatches"] for row in changed)
            if all(type(row["additional_review_dispatches"]) is int for row in changed) else None,
        "failed_or_unknown_obligation_ids": [row["obligation_id"] for row in rows if row["status"] in {"refused", "failed", "timed_out", "reuse_rejected", "unknown", "started_stop_unknown"}],
        "unstarted_obligation_ids": [row["obligation_id"] for row in rows if row["status"] == "unstarted"], "evidence_coverage": "complete" if complete else "unknown" if all(row["status"] == "unstarted" for row in rows) else "partial"}


def validate_report(report, plan):
    validate_plan(plan)
    validate("Report", report)
    ceiling_policy.validate(report, required=plan["schema_version"] in {LONG_PLAN, OWNER_PLAN})
    require(report["schema_version"] == (ceiling_policy.version(LONG_REPORT, plan) if plan["schema_version"] in {LONG_PLAN, OWNER_PLAN} else COLLECTION_REPORT if plan["schema_version"] == COLLECTION_PLAN else EXTENDED_REPORT if plan["schema_version"] == EXTENDED_PLAN else REPORT), "accounting_report_binding_invalid")
    require(report["plan_id"] == document_id(plan) and report["obligations"] == plan["obligations"]
            and report["mode"] == plan["mode"] and report["candidate_sha256"] == plan["candidate_sha256"]
            and report["pair_spec_id"] == plan["pair_spec_id"], "accounting_report_binding_invalid")
    require([row["obligation_id"] for row in report["resolutions"]] == [row["obligation_id"] for row in plan["obligations"]], "accounting_resolution_denominator_invalid")
    require([(row["slot_id"], row["run_id"]) for row in report["journal_observations"]]
            == [(row["slot_id"], row["native_run_id"]) for row in plan["journal_slots"]], "accounting_journal_denominator_invalid")
    require([row["round"] for row in report["owner_passes"]] == list(ROUNDS)
            and [row["stage"] for row in report["stage_observations"]] == list(STAGES), "accounting_owner_denominator_invalid")
    require(report["counters"] == counters(report), "accounting_counter_invalid")
    applications = set()
    for obligation, resolution in zip(plan["obligations"], report["resolutions"], strict=True):
        status = resolution["status"]
        require(resolution["historical_obligation_id"] == obligation["baseline_obligation_id"], "accounting_history_binding_invalid")
        if status == "fresh_applied":
            application = resolution["fresh_application"]
            require(application is not None and application["operation"] == "evaluate" and application["passed"] is True
                    and application["run_id"] == obligation["native_run_id"] and resolution["final_action"] == "fresh_application"
                    and application["applied_record_ref"] not in applications
                    and resolution["additional_completed_review_applications"] == 1
                    and resolution["additional_review_dispatches"] in {None, 1}, "accounting_application_invalid")
            applications.add(application["applied_record_ref"])
        elif status == "reused_validated":
            require(plan["mode"] == "selective_reuse" and obligation["coordinate"]["cycle"] == "first-change"
                    and resolution["fresh_application"] is None and resolution["final_action"] == "retained_review"
                    and len(resolution["eligibility_receipt_pins"]) == 4 and resolution["zero_dispatch_receipt_pin"] is not None
                    and resolution["additional_review_dispatches"] == 0 and resolution["additional_completed_review_applications"] == 0
                    and resolution["review_elapsed_seconds"] is None, "accounting_reuse_evidence_incomplete")
    if report["terminal_outcome"] == "completed":
        require(all(row["status"] in {"fresh_applied", "reused_validated"} and row["additional_review_dispatches"] is not None for row in report["resolutions"])
                and all(row["state"] in {"authenticated", "authenticated_empty"} and row["closing_receipt_pin"] is not None for row in report["journal_observations"])
                and all(row["status"] == "returned" and len(row["journal_receipt_pins"]) == 13
                        and all(row[key] is not None for key in ("strict_pin", "publication_pin", "native_current_pin", "host_qualification_pin", "authority_custody_pin", "source_manifest_pin")) for row in report["owner_passes"])
                and report["control"]["status"] == "reserved_refused_as_expected"
                and report["control"]["dispatch_count"] == 0 and report["control"]["applied_count"] == 0
                and report["cleanup_pin"] is not None and report["global_closing_utc"] is not None and report["failure"] is None,
                "accounting_completion_evidence_incomplete")
        require(all(set(stage["accepted_claim_ids"]) == set(CLAIMS) and set(stage["accepted_original_ids"]) == {"capacity", "demand"}
                    for stage in (report["stage_observations"][0], report["stage_observations"][2])), "accounting_useful_content_missing")
        require(all(row["status"] == "fresh_applied" for obligation, row in zip(plan["obligations"], report["resolutions"], strict=True)
                    if obligation["coordinate"]["cycle"] == "baseline" or plan["mode"] == "legacy_full"), "accounting_fresh_denominator_invalid")
    return report


class _DispatchObservation:
    """A raw receipt alone cannot reconstruct the live executor association."""

    def __init__(self, mint, document, pin, *, tracker, executor, attempts, journals):
        require(mint is _DISPATCH_MINT, "accounting_dispatch_owner_required")
        self.document = validate("DispatchSnapshot", document)
        self.pin = validate("Pin", pin)
        self.tracker, self.executor = tracker, executor
        self.attempts = copy.deepcopy(attempts)
        self.journals = copy.deepcopy(journals)
        self._journal_collection = tracker.originals.journal_collection
        self._original = self._identity()
        _dispatch_members().add(self)

    def _identity(self):
        journals = self.journals
        if self._journal_collection:
            from evidence_wiki.research_journal_collections import collection_summary
            journals = {key: collection_summary(records) for key, records in journals.items()}
        return canonical({"document": self.document, "attempts": self.attempts, "journals": journals})

    def assert_original(self):
        require(self in _dispatch_members() and self._original == self._identity(),
                "accounting_dispatch_custody_changed")


_DISPATCH_MINT = object()


def _dispatch_members():
    import weakref
    if not hasattr(_dispatch_members, "members"):
        _dispatch_members.members = weakref.WeakSet()
    return _dispatch_members.members


def dispatch_snapshot(tracker, executor, phase, original_pins):
    """Join actual executor Running/process objects to existing child custody."""
    from evidence_wiki import BoundedSubprocessExecutor
    from tools._review_dependency_workload import authenticated_journal
    plan, runtime, root = (original_pins[key] for key in ("plan", "runtime", "root"))
    validate_plan(plan)
    require(type(executor) is BoundedSubprocessExecutor and executor is runtime.execution.executor
            and tracker._review_dispatch_state is not None, "accounting_dispatch_owner_invalid")
    require(tracker.originals.journal_collection == collection_selected(plan), "accounting_journal_collection_policy_changed")
    state = tracker._review_dispatch_state
    require(state["runtime"] is runtime and state["plan_id"] == document_id(plan), "accounting_dispatch_owner_changed")
    journals, receipts = {}, []
    for slot in plan["journal_slots"]:
        records, receipt, _snapshot, _view = authenticated_journal(root, runtime, slot["native_run_id"], tracker.originals)
        require((receipt.get("schema_version") == COLLECTION_JOURNAL_RECEIPT) == collection_selected(plan), "accounting_journal_collection_policy_changed")
        validate("CollectionJournalReceipt" if collection_selected(plan) else "JournalReceipt", receipt)
        journals[slot["native_run_id"]] = records
        receipts.append(receipt)
    journal_pin = tracker.originals.append(("review-dispatch-" + phase + "-journals", receipts))
    attempts, associations, incomplete = [], {}, False
    # Native invocation takes the executor lock before the existing child lock.
    # Snapshotting follows the same order and never launches, polls or signals.
    with executor._lock, tracker.lock:
        for selected, kind in state["executors"]:
            require(type(selected) is BoundedSubprocessExecutor, "accounting_dispatch_executor_changed")
            for attempt_id, running in selected._attempts.items():
                attempt, process = running.attempt, running.process
                require(attempt_id == attempt.attempt_id, "accounting_dispatch_attempt_changed")
                document = attempt.document()
                coordinate = next((row for row in plan["obligations"] if row["native_run_id"] == document["run_id"]
                    and row["coordinate"]["claim_id"] == attempt.job.get("payload", {}).get("claim_id")
                    and row["coordinate"]["config_ref"] == attempt.config_ref), None)
                if selected is executor:
                    classification = "local_review_worker" if coordinate is not None else "unclassified"
                    attempts.append({"attempt_id": attempt_id, "obligation_id": None if coordinate is None else coordinate["obligation_id"],
                                     "config_ref": attempt.config_ref, "run_id": document["run_id"], "attempt": document})
                else:
                    classification = kind
                if process is None:
                    incomplete = True
                    continue
                require(id(process) not in associations, "accounting_dispatch_process_duplicate")
                command = tuple(selected._commands.get(attempt.config_ref, ()))
                require(command and tuple(process.args) == command, "accounting_dispatch_command_changed")
                if running.snapshot is not None:
                    selected.authenticate(copy.deepcopy(running.snapshot), attempt)
                associations[id(process)] = (process, classification, attempt_id, coordinate, command)
        births = []
        for ordinal, process in enumerate(tracker.owned, 1):
            spawn = tracker._spawn_pins.get(id(process))
            require(spawn is not None and spawn[0] is process and spawn[1] == ordinal, "accounting_dispatch_spawn_missing")
            associated = associations.get(id(process))
            if associated is None:
                classification, attempt_id, coordinate = "unclassified", None, None
                command = {"argv_sha256": hashlib.sha256(canonical(process.args)).hexdigest(), "association": "unclassified"}
            else:
                actual, classification, attempt_id, coordinate, argv = associated
                require(actual is process, "accounting_dispatch_process_changed")
                command = {"argv": list(argv), "source_sha256": hashlib.sha256(Path(argv[1]).read_bytes()).hexdigest()}
            command_pin = tracker.originals.append(("review-dispatch-" + phase + "-command-" + str(ordinal), command))
            completion = tracker._completed.get(id(process))
            if completion is not None:
                require(completion.process is process and completion.ordinal == ordinal, "accounting_dispatch_completion_changed")
            births.append({"ordinal": ordinal, "pid": process.pid, "pgid": process.pid, "kind": classification,
                "attempt_id": attempt_id, "obligation_id": None if coordinate is None else coordinate["obligation_id"],
                "config_ref": None if coordinate is None else coordinate["coordinate"]["config_ref"], "command_manifest_pin": command_pin,
                "spawn_pin": json.loads(spawn[2]), "completion_pin": None if completion is None else json.loads(completion.completion_pin)})
        require(set(associations) <= {id(process) for process in tracker.owned}, "accounting_dispatch_untracked_process")
        if any(row["kind"] == "unclassified" for row in births):
            incomplete = True
        if tracker.closed and any(row["completion_pin"] is None for row in births):
            incomplete = True
        attempt_pin = tracker.originals.append(("review-dispatch-" + phase + "-attempts", attempts))
        document = {"schema_version": "research-local-review-dispatch-custody/v1", "plan_id": state["plan_id"], "phase": phase,
            "tracker_identity": state["identity"], "executor_class": "evidence_wiki.agent_execution.BoundedSubprocessExecutor",
            "implementation_manifest_pin": plan["producer_manifest_pin"], "state": "unknown" if incomplete else "complete", "births": births,
            "attempt_inventory_pin": attempt_pin, "journal_inventory_pin": journal_pin, "guard_closed": tracker.closed,
            "observed_utc": datetime.now(timezone.utc).isoformat(), "failure": None}
        validate("DispatchSnapshot", document)
        pin = tracker.originals.append(("review-dispatch-" + phase, document))
    return _DispatchObservation(_DISPATCH_MINT, document, pin, tracker=tracker, executor=executor, attempts=attempts, journals=journals)


def zero_dispatch(plan, obligation, opening, closing, cleanup):
    """Zero requires exhaustive interval custody and authentic journal absence."""
    validate_plan(plan)
    validate("Obligation", obligation)
    observations = (opening, closing, cleanup)
    require(all(type(row) is _DispatchObservation and row in _dispatch_members() for row in observations), "accounting_dispatch_receipt_not_live")
    require(all(row.tracker is opening.tracker and row.executor is opening.executor for row in observations)
            and [row.document["phase"] for row in observations] == ["changed_opening", "current_closing", "cleanup_closed"]
            and all(row.document["plan_id"] == document_id(plan) for row in observations), "accounting_dispatch_interval_invalid")
    known = all(row.document["state"] == "complete" for row in observations) and cleanup.document["guard_closed"]
    require(any(row == obligation for row in plan["obligations"]), "accounting_obligation_not_planned")
    for row in observations:
        row.assert_original()
    target = obligation["obligation_id"]
    old_attempts = {row["attempt_id"] for row in opening.attempts}
    attempts = [row for row in cleanup.attempts if row["attempt_id"] not in old_attempts and row["obligation_id"] == target]
    old_births = {row["ordinal"] for row in opening.document["births"]}
    births = [row for row in cleanup.document["births"] if row["ordinal"] not in old_births]
    review_births = [row for row in births if row["kind"] == "local_review_worker" and row["obligation_id"] == target]
    unclassified = [row for row in births if row["kind"] == "unclassified"]
    counts = {"new_review_attempt_count": len(attempts), "new_local_review_worker_birth_count": len(review_births),
              "new_completed_review_application_count": 0, "unclassified_birth_count": len(unclassified)}
    from evidence_wiki import execution_session as native
    before_refs = {row["event_ref"] for rows in opening.journals.values() for row in rows}
    slots = {row["native_run_id"]: row for row in plan["journal_slots"]}
    attempt_count = 0
    for run_id, rows in cleanup.journals.items():
        slot = slots.get(run_id)
        require(slot is not None, "accounting_dispatch_journal_unplanned")
        after_attempts = native._attempt_rows(rows)
        before_attempts = native._attempt_rows(opening.journals[run_id])
        if slot["kind"] == "changed_basis_control":
            # The separately frozen control reserves once and must never invoke
            # or apply. It cannot consume a service obligation or claim savings.
            if len(after_attempts) != 1 or any(row["kind"] == "execution_applied" for row in rows):
                known = False
            continue
        for attempt_id, row in after_attempts.items():
            if attempt_id in before_attempts:
                require(row == before_attempts[attempt_id], "accounting_dispatch_original_attempt_changed")
                continue
            config = row["attempt"]["config_ref"]
            matched = next((item for item in plan["obligations"] if item["native_run_id"] == run_id
                and item["coordinate"]["claim_id"] == row.get("claim_id") and item["coordinate"]["config_ref"] == config), None)
            if matched is None:
                known = False
            elif matched["obligation_id"] == target:
                attempt_count += 1
        for row in rows:
            if row["event_ref"] in before_refs:
                continue
            data = row["data"]
            if row["kind"] == "execution_native" and data.get("operation") == "review_pages.host_assembly":
                arguments = data["arguments"]
                if (run_id == obligation["native_run_id"] and arguments.get("claim_id") == obligation["coordinate"]["claim_id"]
                        and arguments.get("task_role") == obligation["task_role"]):
                    attempt_count = max(1, attempt_count)
            if (row["kind"] == "execution_applied" and run_id == obligation["native_run_id"]
                    and data.get("claim_id") == obligation["coordinate"]["claim_id"] and data.get("profile_ref") == obligation["coordinate"]["config_ref"]):
                counts["new_completed_review_application_count"] += 1
    counts["new_review_attempt_count"] = max(counts["new_review_attempt_count"], attempt_count)
    require(all(run in cleanup.journals for run in (row["native_run_id"] for row in plan["journal_slots"])), "accounting_dispatch_journal_missing")
    result = {"schema_version": "research-retained-review-zero-dispatch/v1", "plan_id": document_id(plan), "obligation_id": target,
        "state": "unknown" if not known else "nonzero" if any(counts.values()) else "verified_zero",
        "scope": "new_local_bundled_review_worker_processes_in_changed_interval", "opening_snapshot_pin": opening.pin,
        "closing_snapshot_pin": closing.pin, "cleanup_snapshot_pin": cleanup.pin,
        "current_journal_inventory_pin": cleanup.document["journal_inventory_pin"],
        **(counts if known else dict.fromkeys(counts)), "invocation_api_call_count": None, "failure": None}
    return validate("ZeroDispatchReceipt", result)


def eligibility_receipt(plan, obligation, original_application, facts, round_name, originals):
    """Pin actual native owner outputs immediately; selectors never grant reuse."""
    validate_plan(plan)
    validate("Obligation", obligation)
    validate("ApplicationEvidence", original_application)
    expected = {"state", "reason", "original", "current", "strict", "publication", "usage_authority", "qualification", "journal_receipt", "custody"}
    require(type(facts) is dict and set(facts) == expected, "accounting_native_eligibility_shape_invalid")
    original, current = facts["original"], facts["current"]
    for key in ("basis_id", "semantic_material_id", "observed_workspace_basis_id", "review_producer_id", "review_id", "attempt_id"):
        require(original[key] == original_application[key], "accounting_original_application_changed")
    require(original["applied_record_ref"] == original_application["applied_record_ref"]
            and original["config_ref"] == obligation["coordinate"]["config_ref"]
            and original["task_role"] == obligation["task_role"] and original["controller_ref"] == obligation["controller_ref"],
            "accounting_original_service_changed")
    pins = {name: originals.append(("retained-" + round_name + "-" + obligation["coordinate"]["config_ref"] + "-" + name, facts[name]))
            if facts[name] is not None else None for name in ("strict", "publication", "usage_authority", "qualification", "journal_receipt", "custody")}
    result = {"schema_version": "research-retained-review-eligibility/v1", "plan_id": document_id(plan), "obligation_id": obligation["obligation_id"],
        "historical_obligation_id": obligation["baseline_obligation_id"], "claim_id": obligation["coordinate"]["claim_id"],
        "config_ref": obligation["coordinate"]["config_ref"], "task_role": obligation["task_role"], "controller_ref": obligation["controller_ref"],
        "round": round_name, "state": facts["state"], "reason": facts["reason"], "original_application": original_application,
        "original_basis_id": original["basis_id"], "current_basis_id": current["basis_id"],
        "original_semantic_material_id": original["semantic_material_id"], "current_semantic_material_id": current["semantic_material_id"],
        "original_observed_workspace_basis_id": original["observed_workspace_basis_id"], "current_workspace_basis_id": current["workspace_basis_id"],
        "original_review_producer_id": original["review_producer_id"], "current_review_producer_id": current["review_producer_id"],
        "current_selected_review_id": current["selected_review_id"], "current_strict_pin": pins["strict"], "current_publication_pin": pins["publication"],
        "current_usage_authority_pin": pins["usage_authority"], "current_qualification_pin": pins["qualification"],
        "current_journal_receipt_pin": pins["journal_receipt"], "current_custody_pin": pins["custody"],
        "observed_utc": datetime.now(timezone.utc).isoformat(), "failure": None}
    if result["state"] == "eligible":
        require(all(result[key] is not None for key in ("current_basis_id", "current_semantic_material_id", "current_workspace_basis_id", "current_review_producer_id", "current_selected_review_id"))
                and all(pin is not None for pin in pins.values())
                and original["basis_id"] == current["basis_id"] and original["semantic_material_id"] == current["semantic_material_id"]
                and original["review_producer_id"] == current["review_producer_id"], "accounting_eligibility_identity_invalid")
    return validate("EligibilityReceipt", result)


def application_evidence(facts, timing_pin, originals):
    """Capture the authenticated application chain, preserving native identities."""
    original = facts["original"]
    keys = ("attempt_id", "run_id", "operation", "application_record_ref", "applied_record_ref", "review_id", "authorship_id",
            "basis_id", "semantic_material_id", "observed_workspace_basis_id", "review_producer_id", "passed")
    result = {key: original[key] for key in keys}
    for field, name in (("envelope_pin", "envelope"), ("attempt_pin", "attempt"), ("final_observation_pin", "final_observation"),
                        ("delivery_pin", "delivery"), ("original_custody_pin", "original_custody"), ("journal_receipt_pin", "journal_receipt")):
        require(original.get(name) is not None, "accounting_original_custody_unavailable")
        result[field] = originals.append(("application-" + original["attempt_id"] + "-" + name, original[name]))
    result["timing_pin"] = timing_pin
    return validate("ApplicationEvidence", result)


def normalized_claims(native_claims, *, claim_schema_version, mode, source_translations, computation_translation):
    """Translate only the exact source/clock/result coordinates proved upstream."""
    require(claim_schema_version in {"evidence-strict-claims/v1", "evidence-strict-claims/v2"}, "accounting_claim_schema_invalid")
    sources = {row["native_source_id"]: validate("SourceTranslation", row) for row in source_translations if row["mode"] == mode}
    require(len(sources) == 3 and {row["coordinate"] for row in sources.values()} == {"shared", "demand", "unused"}, "accounting_source_translation_incomplete")
    calculation = validate("ComputationTranslation", computation_translation)
    require(calculation["mode"] == mode, "accounting_computation_translation_changed")
    fields = NORMALIZATION_CONTRACT["claim_v2_fields" if claim_schema_version.endswith("/v2") else "claim_v1_fields"]
    result = []
    for original in native_claims:
        require(set(original) == set(fields), "accounting_claim_unknown_field")
        row = copy.deepcopy(original)
        for evidence in row["evidence"]:
            translation = sources.get(evidence["source_id"])
            require(translation is not None and evidence["record_sha256"] == translation["native_record_sha256"]
                    and evidence["observed_at"] == translation["native_observed_at"], "accounting_source_translation_changed")
            evidence.update(source_id=translation["fixture_source_id"], record_sha256=translation["fixture_record_sha256"], observed_at=translation["fixture_observed_at"])
        for reference in row.get("calculations", []):
            require(reference["result_id"] == calculation["native_result_id"], "accounting_computation_translation_changed")
            reference["result_id"] = calculation["fixture_result_id"]
        result.append(row)
    require(len(result) == 6 and {row["id"] for row in result} == set(CLAIMS), "accounting_normalized_claim_denominator_invalid")
    value = {"claim_schema_version": claim_schema_version, "claims": result}
    validate("NormalizedClaimDocument", value)
    return value


def normalized_publication(publication, claims):
    """Compare the complete native answer fields with verification kept private."""
    normalized = {row["id"]: row for row in claims["claims"]}
    result = {key: copy.deepcopy(publication[key]) for key in NORMALIZATION_CONTRACT["publication_fields"]}
    for question in result["questions"]:
        require(set(question) == set(NORMALIZATION_CONTRACT["question_fields"]), "accounting_publication_unknown_field")
        rewritten = []
        for claim in question["claims"]:
            original = {key: value for key, value in claim.items() if key != "verification"}
            require(set(original) == set(normalized[claim["id"]]), "accounting_publication_claim_changed")
            rewritten.append(copy.deepcopy(normalized[claim["id"]]))
        question["claims"] = rewritten
        for unresolved in question["unresolved_claims"]:
            require(set(unresolved) == set(NORMALIZATION_CONTRACT["unresolved_claim_fields"]), "accounting_publication_unknown_field")
    for outcome in result["original_outcomes"]:
        require(set(outcome) == set(NORMALIZATION_CONTRACT["original_outcome_fields"]), "accounting_publication_unknown_field")
    return result


class _NativeNormalization:
    """Only current native owners and complete recipe checks mint comparison facts."""

    def __init__(self, _mint, *, evidence, claims, publications, stages, owners, pair, plans, reports, current_assert=None):
        require(_mint is _NORMALIZATION_MINT, "accounting_normalization_owner_required")
        require(evidence.get("schema_version") != "review-dependency-differential-normalization-evidence/v2"
                or callable(current_assert), "accounting_pair_closing_owner_required")
        self._current_assert = self._current_assert_owner = current_assert
        self.evidence = validate("NormalizationEvidence", evidence)
        self.claims, self.publications = copy.deepcopy(claims), copy.deepcopy(publications)
        self.stages, self.owners = copy.deepcopy(stages), copy.deepcopy(owners)
        self.binding = {"pair_id": document_id(pair), "plan_ids": {mode: document_id(plan) for mode, plan in plans.items()},
                        "report_ids": {mode: document_id(report) for mode, report in reports.items()}}
        self._original = canonical({"evidence": self.evidence, "claims": self.claims, "publications": self.publications, "stages": self.stages, "owners": self.owners, "binding": self.binding})
        _normalization_members().add(self)

    def assert_original(self):
        require(self._current_assert is self._current_assert_owner and self in _normalization_members() and self._original == canonical({"evidence": self.evidence, "claims": self.claims,
            "publications": self.publications, "stages": self.stages, "owners": self.owners, "binding": self.binding}), "accounting_normalization_custody_changed")

    def assert_binding(self, pair, plans, reports):
        self.assert_original()
        require(self.evidence["pair_spec_id"] == document_id(pair)
                and self.binding == {"pair_id": document_id(pair),
                    "plan_ids": {mode: document_id(plan) for mode, plan in plans.items()},
                    "report_ids": {mode: document_id(report) for mode, report in reports.items()}}
                and self.stages == {mode: report["stage_observations"] for mode, report in reports.items()}
                and self.owners == {mode: report["global_closing_utc"] for mode, report in reports.items()},
                "accounting_normalization_execution_changed")


def _normalization_members():
    import weakref
    if not hasattr(_normalization_members, "members"):
        _normalization_members.members = weakref.WeakSet()
    return _normalization_members.members


_NORMALIZATION_MINT = object()



def _read_pair_normalization_pin(plan):
    pin = plan["pair_spec_pin"]
    pair = decode("PairSpec", read_pin(pin, roots=[Path(pin["path"]).parent], maximum=1_048_576))
    return pair["normalization_contract_pin"]


def differential(pair, plans, reports, *, pair_pin, plan_pins, report_pins, originals, normalization=None, prospective_case_results_pin=None):
    """Project all six/twelve joins; missing native proof remains explicit unknown."""
    validate_pair(pair)
    if prospective_case_results_pin is not None:
        from tools._review_dependency_case_evidence import read_index
        read_index(prospective_case_results_pin, roots=[Path(prospective_case_results_pin["path"]).parent],
            candidate_sha256=pair["candidate_sha256"])
    for mode in ("legacy_full", "selective_reuse"):
        validate_plan(plans[mode], pair)
        validate_report(reports[mode], plans[mode])
        require(plans[mode]["mode"] == mode and reports[mode]["mode"] == mode, "accounting_mode_binding_invalid")
    proof = normalization if type(normalization) is _NativeNormalization and normalization in _normalization_members() else None
    if proof is not None:
        if any(plan["schema_version"] in {PAIR_PLAN, EXTENDED_PLAN, COLLECTION_PLAN, LONG_PLAN, OWNER_PLAN} for plan in plans.values()):
            require(all(plan["schema_version"] == plans["legacy_full"]["schema_version"] for plan in plans.values())
                    and proof.evidence["schema_version"] == "review-dependency-differential-normalization-evidence/v2",
                    "accounting_pair_closing_owner_required")
        proof.assert_binding(pair, plans, reports)
    normalization_pin = None if proof is None else originals.append(("normalization-evidence", proof.evidence))
    claim_joins = []
    for claim in CLAIMS:
        row = {"claim_id": claim, "legacy_before_accepted": None, "selective_before_accepted": None,
               "legacy_after_accepted": None, "selective_after_accepted": None,
               "legacy_normalized_content_id": None, "selective_normalized_content_id": None,
               "legacy_required_refresh": claim in pair["expected_legacy_refresh_ids"], "selective_required_refresh": claim in pair["expected_selective_refresh_ids"],
               "native_basis_evidence_pins": [], "normalization_evidence_pin": normalization_pin, "result": "unknown"}
        for mode, prefix in (("legacy_full", "legacy"), ("selective_reuse", "selective")):
            for stage, name in (("before_refresh", "before"), ("after_refresh", "after")):
                observation = next(item for item in reports[mode]["stage_observations"] if item["stage"] == stage)
                if proof is not None and observation["status"] == "returned" and set(observation["native_claim_ids"]) == set(CLAIMS):
                    row[prefix + "_" + name + "_accepted"] = claim in observation["accepted_claim_ids"]
                if observation["native_basis_rows_pin"] is not None:
                    row["native_basis_evidence_pins"].append(observation["native_basis_rows_pin"])
            if proof is not None:
                native_claim = next(item for item in proof.claims[mode]["claims"] if item["id"] == claim)
                row[prefix + "_normalized_content_id"] = identity("review-dependency-normalized-useful-content/v1", native_claim)
        if proof is not None:
            row["result"] = "equal_after_refresh" if row["legacy_normalized_content_id"] == row["selective_normalized_content_id"] else "mismatch"
        claim_joins.append(row)
    joins, lrefs, srefs = [], set(), set()
    for claim in CLAIMS:
        for config in ("evidence", "critic"):
            selected = {}
            for mode in ("legacy_full", "selective_reuse"):
                obligation = next(row for row in plans[mode]["obligations"] if row["coordinate"]["cycle"] == "first-change"
                                  and row["coordinate"]["claim_id"] == claim and row["coordinate"]["config_ref"] == config)
                resolution = next(row for row in reports[mode]["resolutions"] if row["obligation_id"] == obligation["obligation_id"])
                selected[mode] = (obligation, resolution)
            legacy_obligation, legacy = selected["legacy_full"]
            selective_obligation, selective = selected["selective_reuse"]
            lref = legacy["fresh_application"]["applied_record_ref"] if proof is not None and legacy["status"] == "fresh_applied" else None
            sref = selective["fresh_application"]["applied_record_ref"] if proof is not None and selective["status"] == "fresh_applied" else None
            if lref is not None:
                require(lref not in lrefs, "accounting_application_credit_duplicate")
                lrefs.add(lref)
            if sref is not None:
                require(sref not in srefs, "accounting_application_credit_duplicate")
                srefs.add(sref)
            historical = None
            if proof is not None and selective["status"] == "reused_validated":
                original = next(row for row in reports["selective_reuse"]["resolutions"] if row["obligation_id"] == selective_obligation["baseline_obligation_id"])
                historical = None if original["fresh_application"] is None else original["fresh_application"]["applied_record_ref"]
            saved = None if proof is None else lref is not None and selective["status"] == "reused_validated" and historical is not None and selective["zero_dispatch_receipt_pin"] is not None
            joins.append({"claim_id": claim, "config_ref": config, "legacy_obligation_id": legacy_obligation["obligation_id"],
                "selective_obligation_id": selective_obligation["obligation_id"], "legacy_status": "unknown" if proof is None else legacy["status"], "selective_status": "unknown" if proof is None else selective["status"],
                "legacy_completed_changed_application_ref": lref, "selective_completed_changed_application_ref": sref,
                "selective_retained_original_applied_ref": historical, "selective_zero_dispatch_pin": None if proof is None else selective["zero_dispatch_receipt_pin"], "saved_completed_application": saved})
    complete = all(report["terminal_outcome"] == "completed" for report in reports.values())
    lcount, scount = (len(lrefs), len(srefs)) if complete and proof is not None else (None, None)
    saved = None if lcount is None else lcount - scount
    outcome = "unknown"
    if proof is not None:
        outcome = ("pair_pass" if complete and lcount == 12 and saved >= 1 and sum(row["saved_completed_application"] is True for row in joins) == saved
            and all(row["result"] == "equal_after_refresh" and row["legacy_after_accepted"] is True and row["selective_after_accepted"] is True for row in claim_joins)
            else "failed")
    result = {"schema_version": DIFFERENTIAL, "pair_spec_pin": pair_pin, "pair_spec_id": document_id(pair), "candidate_sha256": pair["candidate_sha256"],
        "legacy_plan_pin": plan_pins["legacy_full"], "selective_plan_pin": plan_pins["selective_reuse"],
        "legacy_report_pin": report_pins["legacy_full"], "selective_report_pin": report_pins["selective_reuse"], "claim_joins": claim_joins,
        "changed_obligation_joins": joins, "question_original_comparison_pin": None if proof is None else originals.append(("original-question-comparison", proof.publications)),
        "publication_content_comparison_pin": None if proof is None else originals.append(("publication-comparison", proof.publications)),
        "legacy_changed_completed_applications": lcount, "selective_changed_completed_applications": scount, "avoided_completed_applications": saved,
        "accepted_useful_claims_removed": None if proof is None else 0, "all_planned_obligations_visible": True,
        "all_current_owners_validated": proof is not None, "frozen_cases_retained": 99, "prospective_case_results_pin": prospective_case_results_pin,
        "outcome": outcome, "qualification": False, "failure": None, "normalization_contract_pin": pair["normalization_contract_pin"], "normalization_evidence_pin": normalization_pin}
    result = validate("Differential", result)
    if proof is not None and proof.evidence["schema_version"] == "review-dependency-differential-normalization-evidence/v2":
        require(callable(proof._current_assert) and proof._current_assert is proof._current_assert_owner,
                "accounting_pair_closing_owner_required")
        proof._current_assert(proof.assert_original)
    return result
