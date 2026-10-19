"""Closed native acceptance coordinates, assertion scopes and fixture plan."""

from __future__ import annotations

import copy
import re

FAMILIES = {
    "baseline": "unchanged-baseline",
    "claim": "independent-claim-text independent-claim-scope independent-claim-time independent-claim-units independent-claim-limitations premise-transitive-edit cross-question-premise-edit derivation-edit quote-edit-with-same-source calculation-reference-edit premise-link-removal claim-mechanical-material claim-counterevidence-material",
    "question": "question-capacity-body question-capacity-metadata question-demand-body question-demand-metadata question-record-text",
    "source": "shared-source-raw shared-source-normalized unrelated-source-raw source-provenance source-ancestry",
    "global": "full-requirements unrelated-question-coverage policy rubric instructions authority-content authority-key-revocation qualification-profile producer-identity computation-output computation-lineage computation-definition computation-clock unclassified-file",
    "membership": "claim-addition claim-deletion question-addition source-addition source-deletion unclassified-file-addition unclassified-file-deletion",
    "usage_time": "unused-source-permission-revoked unused-source-expired source-effective-interval-not-started source-effective-interval-ended review-max-age-expired time-regression",
    "authentication": "review-signature-failure authorship-signature-failure controller-separation-failure current-role-failure current-qualification-failure",
    "live_output_gates": "release-eligibility-failure export-eligibility-failure delivery-eligibility-failure live-grounding-failure live-coverage-failure live-computation-failure",
    "history": "recorded-review-revocation newest-fail-blocks-older-pass revoked-newest-fail-never-resurrects-pass newest-unknown-blocks-older-pass revoked-newest-unknown-never-resurrects-pass untrusted-user-timestamp-cannot-win",
    "ingress": "incomplete-manifest missing-complete-packet forged-basis-binding forged-selective-signature unsupported-basis-schema unknown-field duplicate-coordinate duplicate-json-key nonfinite-json-number transport-binding-forged",
    "equivalence_provenance": "validated-transport-only-change historical-workspace-provenance-difference",
    "invalid_graph": "question-deletion premise-cycle missing-premise-target duplicate-claim-id",
    "bounds": "transitive-premise-bound source-record-bound dependency-entry-bound dependency-basis-byte-bound complete-packet-byte-bound worker-input-byte-bound stricter-host-packet-bound claim-inventory-bound",
    "races": "workspace-mid-read-change authority-mid-read-change",
    "migration": "mode-caller-flag-cannot-switch legacy-envelope-not-convertible selective-envelope-current-runtime-refusal protected-policy-enable-selective protected-policy-rollback",
}

CASE_IDS = tuple(case for values in FAMILIES.values() for case in values.split())
SERVICE_CASES = frozenset(
    {
        "qualification-profile",
        "current-qualification-failure",
        "producer-identity",
        "mode-caller-flag-cannot-switch",
        "legacy-envelope-not-convertible",
        "selective-envelope-current-runtime-refusal",
        "protected-policy-enable-selective",
        "protected-policy-rollback",
    }
)

_FAMILY = {case: family for family, values in FAMILIES.items() for case in values.split()}

_FAILURE_CLOSED = frozenset(
    "claim-mechanical-material unrelated-question-coverage question-deletion source-effective-interval-not-started controller-separation-failure current-role-failure current-qualification-failure release-eligibility-failure export-eligibility-failure delivery-eligibility-failure live-grounding-failure live-coverage-failure live-computation-failure recorded-review-revocation revoked-newest-fail-never-resurrects-pass revoked-newest-unknown-never-resurrects-pass premise-cycle missing-premise-target duplicate-claim-id".split()
)

_INGRESS_REJECTION = frozenset(
    "review-signature-failure authorship-signature-failure untrusted-user-timestamp-cannot-win incomplete-manifest missing-complete-packet forged-basis-binding forged-selective-signature unsupported-basis-schema unknown-field duplicate-coordinate duplicate-json-key nonfinite-json-number transport-binding-forged mode-caller-flag-cannot-switch legacy-envelope-not-convertible selective-envelope-current-runtime-refusal".split()
)

INITIAL_CASES = (
    "unchanged-baseline",
    "independent-claim-text",
    "independent-claim-limitations",
    "premise-transitive-edit",
    "historical-workspace-provenance-difference",
)

CLAIM_IDS = frozenset(
    {"observed-capacity", "effective-capacity", "observed-demand", "adequacy", "margin", "independent-note"}
)

_CLAIM_CHANGES = {
    "independent-claim-text": (
        "independent-note",
        ("text",),
        "The retained service note documents scheduled maintenance.",
    ),
    "independent-claim-scope": ("independent-note", ("scope",), "Retained service note only."),
    "independent-claim-time": ("independent-note", ("time",), "Observed on 2026-10-12 at noon."),
    "independent-claim-units": ("independent-note", ("units",), "none"),
    "independent-claim-limitations": ("independent-note", ("limitations",), ["Synthetic retained service note only."]),
    "premise-transitive-edit": ("observed-capacity", ("text",), "The retained capacity record states six units."),
    "cross-question-premise-edit": ("observed-demand", ("scope",), "Retained demand record only."),
    "derivation-edit": (
        "effective-capacity",
        ("derivation",),
        "Use the retained capacity as effective capacity under the stated scope.",
    ),
    "quote-edit-with-same-source": ("independent-note", ("evidence", 0, "quote"), "scheduled maintenance"),
    "calculation-reference-edit": ("margin", ("calculations", 0, "expected"), "3"),
    "premise-link-removal": ("adequacy", ("premises",), ["effective-capacity"]),
    "historical-workspace-provenance-difference": (
        "independent-note",
        ("text",),
        "The retained service note documents scheduled maintenance.",
    ),
}

_LIMITATIONS = {
    "claim-counterevidence-material": "A real unsupported critic/semantic input refusal can satisfy the declared limited no-acceptance route; finding-only local-set comparison remains unavailable. Never inject findings.",
    "stricter-host-packet-bound": "Frozen cap4095 rejects configuration; supported cap4096 is a separate packet-bound companion.",
    "worker-input-byte-bound": "Raw whitespace violates canonical owned input before the intended input-byte branch. Record exact earlier refusal; a reduced supported input cap is a separate companion.",
    "dependency-basis-byte-bound": "Residual content is committed by path/digest; content expansion alone does not grow dependency basis. Measure actual basis bytes and retain unavailable branch or earlier refusal.",
    "transitive-premise-bound": "Frozen oversized graph is distinct from exact32-total-supported/33-total-refused closure companions.",
    "source-record-bound": "Attempted65-source recipe may meet the current32-source owner limit first; no later branch credit.",
    "computation-definition": "Literal exact-rational is unsupported arithmetic shape; a supported precision/rounding change is a separate companion.",
    "selective-envelope-current-runtime-refusal": "Legacy-policy rejection and selective-policy positive admission are separate mode observations; candidate v3 is not globally unsupported.",
}


def _require(condition, reason):
    if not condition:
        raise AssertionError(reason)


def case_plan():
    """Freeze synthetic context-coordinate decisions before any worker output."""
    return {
        "schema_version": "native-review-case-plan/v1",
        "default_verdict": "pass",
        "rules": [
            {"config_ref": config, "context_id_prefix": prefix, "verdict": verdict}
            for config in ("evidence", "critic")
            for prefix, verdict in (("context-0", "fail"), ("context-1", "unknown"), ("context-2", "pass"))
        ],
    }


def validate_case_plan(value):
    _require(value == case_plan() and type(value) is dict, "native_case_plan_invalid")
    return copy.deepcopy(value)


def case_manifest():
    """Keep every supported case coordinate visible without importing reports."""
    _require(len(CASE_IDS) == 99 and len(set(CASE_IDS)) == 99, "case_denominator_changed")
    rows = []
    for case_id in CASE_IDS:
        family = _FAMILY[case_id]
        scope = "native_per_claim_evaluation"
        if case_id in _INGRESS_REJECTION:
            scope = "native_ingress_rejection"
        elif family == "bounds":
            scope = "native_bound_refusal"
        elif family == "races":
            scope = "native_custody_race_refusal"
        elif case_id in _FAILURE_CLOSED:
            scope = "native_failure_closed"
        rows.append(
            {
                "case_id": case_id,
                "family": family,
                "native_assertion_scope": scope,
                "status": "not_executed",
                "assertions": [],
                "evidence": [],
                "set_comparison": "conditional_on_preserved_semantic_inputs_and_required_native_surfaces",
                "earlier_owner_refusal": None,
                "unavailable_branches": [],
                "limitation": _LIMITATIONS.get(case_id),
                "qualification": False,
            }
        )
    return rows


def _accepted(strict):
    return {row["claim"]["id"] for row in strict["claims"] if row["accepted"]}


def _basis(strict):
    return {row["claim"]["id"]: row.get("claim_basis_id", strict["basis_id"]) for row in strict["claims"]}


def _dependants(manifest, identifier):
    affected = {identifier}
    while True:
        expanded = affected | {row["id"] for row in manifest["claims"] if set(row["premises"]) & affected}
        if expanded == affected:
            return affected
        affected = expanded


def _reason(error):
    reason = getattr(error, "details", {}).get("reason")
    if reason is None and isinstance(error, ValueError):
        reason = str(error)
    return reason if type(reason) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", reason) else None
