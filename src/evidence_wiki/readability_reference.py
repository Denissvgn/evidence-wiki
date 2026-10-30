"""Finite scalar references do not qualify unrestricted language quality."""

import hashlib
import platform
from copy import deepcopy
from pathlib import Path

from .errors import EvidenceWikiError
from .mutation_contracts import canonical, digest
from .readability_contracts import OUTPUT, field_manifest, output_document, refuse

DOMAIN = "finite-brief-preservation"
ORACLE = "readability-field-reference/v1"
REFERENCE = "readability-field-reference-input/v1"
FAMILIES = ("qualification", "scope", "time", "units", "limitation", "evidence", "inference", "unresolved", "meaning", "manifest")
DESCRIPTOR = {"schema_version": "research-readability-domain/v1", "domain_ref": DOMAIN,
    "purpose": "readability", "reference_input_schema": REFERENCE, "output_schema": OUTPUT,
    "scope": "exact_printable_ascii_scalar_presentation_and_unresolved_field_preservation",
    "general_language_quality": "not_qualified", "scientific_acceptance": "native_owners_only"}


def program_identity():
    return {"schema_version": "research-readability-reference-program/v1", "oracle_ref": ORACLE,
        "domain_ref": DOMAIN, "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python_version": platform.python_version(), "implementation": platform.python_implementation()}


def reference_document(value):
    """Compact calibration language; actual native jobs require complete context."""
    from ._pack_io import bounded
    bounded(value)
    if type(value) is not dict or set(value) != {"purpose", "packet"} or value["purpose"] != "readability_calibration":
        refuse("reference_input_invalid")
    packet = value["packet"]
    if (type(packet) is not dict or set(packet) != {"schema_version", "context_id", "task_role", "payload", "field_manifest", "instructions"}
            or packet["schema_version"] != REFERENCE or packet["task_role"] != "critic"
            or type(packet["context_id"]) is not str or not 1 <= len(packet["context_id"]) <= 96
            or type(packet["instructions"]) is not str or not 1 <= len(packet["instructions"]) <= 4096):
        refuse("reference_input_invalid")
    part = packet["payload"]
    if type(part) is not dict or set(part) != {"family", "subject", "value", "displayed"} or part["family"] not in FAMILIES:
        refuse("reference_input_invalid")
    if type(part["subject"]) is not str or not 1 <= len(part["subject"]) <= 256:
        refuse("reference_input_invalid")
    if part["value"] is not None and (type(part["value"]) is not str or not 1 <= len(part["value"]) <= 256):
        refuse("reference_input_invalid")
    if part["displayed"] is not None and (type(part["displayed"]) is not str or len(part["displayed"]) > 256):
        refuse("reference_input_invalid")
    if packet["field_manifest"] != field_manifest(part):
        refuse("reference_manifest_invalid")
    return value


def expected_issues(document):
    try:
        packet = reference_document(document)["packet"]
    except (EvidenceWikiError, ValueError):
        return None
    part = packet["payload"]
    if part["value"] is None:
        return None
    if part["value"] == part["displayed"]:
        return []
    code = {"qualification": "qualification_hidden", "scope": "scope_misleading", "time": "scope_misleading",
        "units": "scope_misleading", "limitation": "limitation_hidden", "evidence": "evidence_ambiguous",
        "inference": "inference_as_finding", "unresolved": "unresolved_outcome_hidden",
        "meaning": "meaning_unclear", "manifest": "meaning_unclear"}[part["family"]]
    return [{"code": code, "pointer": "/value", "field_sha256": digest(part["value"])}]


def oracle_label(document):
    issues = expected_issues(document)
    return "unknown" if issues is None else "reject" if issues else "accept"


def score_output(document, output):
    value = output_document(output, document["packet"])
    expected = expected_issues(document)
    if expected is None:
        if value["state"] != "abstain":
            refuse("reference_requires_abstention")
        return "unknown"
    if value["state"] == "abstain":
        return "unknown"
    if not value["issues"]:
        return "accept"
    return "reject" if value["issues"] == expected else "unknown"


def assess_references(cases):
    from .qualification_calibration import cases_document
    cases = cases_document(cases)
    materials, rows = set(), []
    for case in cases:
        if (case["domain_ref"] != DOMAIN or case["family_id"] not in FAMILIES
                or case["reference"]["tier"] not in {"executable_oracle", "controlled_mutation"}
                or case["reference"]["reference_ref"] != ORACLE):
            raise ValueError("readability_reference_unsupported")
        label = oracle_label(case["input_document"])
        part = case["input_document"]["packet"]["payload"]
        if part["family"] != case["family_id"] or case["expected_label"] != label:
            raise ValueError("readability_reference_label_not_reproduced")
        intrinsic = digest({"payload": part, "label": label})
        if case["kind"] == "baseline":
            if intrinsic in materials or case["reference"]["basis_sha256"] != intrinsic.removeprefix("sha256:"):
                raise ValueError("readability_reference_material_duplicate_or_changed")
            materials.add(intrinsic)
        rows.append({"case_id": case["case_id"], "input_sha256": digest(case["input_document"]),
            "label": label, "issues": expected_issues(case["input_document"]),
            "material_sha256": intrinsic})
    value = {"schema_version": "research-readability-reference-assessment/v1", "program": program_identity(),
        "cases_sha256": hashlib.sha256(canonical(cases)).hexdigest(), "baseline_materials": len(materials),
        "results": rows, "scope": DESCRIPTOR["scope"], "authorized": False, "evidence_accepted": False}
    value["assessment_ref"] = digest(value)
    return value


def protected_cases():
    """Distinct field facts plus invariant control/presentation probes."""
    from .qualification_calibration import AXES
    cases = []
    for index in range(100):
        family = FAMILIES[index % len(FAMILIES)]
        scalar = {"qualification": "supported", "scope": "sample-" + str(index), "time": "time-" + str(index),
            "units": "count", "limitation": "Controlled single sample " + str(index), "evidence": "Observed count " + str(index + 7),
            "inference": "inference", "unresolved": "abstain", "meaning": "Measured count " + str(index + 7),
            "manifest": "Measurement " + str(index)}[family]
        label = "accept" if index % 3 == 0 else "reject" if index % 3 == 1 else "unknown"
        payload = {"family": family, "subject": "Count=" + str(index + 7) + ";scope=sample-" + str(index) + ";time=time-" + str(index),
            "value": None if label == "unknown" else scalar, "displayed": scalar if label != "reject" else None}
        packet = {"schema_version": REFERENCE, "context_id": "reference-" + str(index), "task_role": "critic",
            "payload": payload, "field_manifest": field_manifest(payload),
            "instructions": "Check declared field and display. Return exact field issues or abstain on invalid context."}
        document = {"purpose": "readability_calibration", "packet": packet}
        intrinsic = digest({"payload": payload, "label": label})
        base = {"case_id": "case-" + str(index), "unit_ref": "unit-" + str(index), "family_id": family,
            "domain_ref": DOMAIN, "kind": "baseline", "base_case_id": None, "input_document": document,
            "expected_label": label, "critical": label == "reject", "reference": {"tier": "controlled_mutation",
                "reference_ref": ORACLE, "basis_sha256": intrinsic.removeprefix("sha256:")}}
        cases.append(base)
        for axis in AXES:
            variant = deepcopy(base)
            variant.update(case_id=base["case_id"] + "-" + axis, kind=axis, base_case_id=base["case_id"])
            if axis == "position":
                variant["input_document"]["packet"]["payload"] = dict(reversed(list(payload.items())))
            if axis != "repeat":
                variant["input_document"]["packet"]["instructions"] += {
                    "meaning": " Preserve exact associations.", "identity": " Fictional narrator is irrelevant.",
                    "position": " Display order is irrelevant.", "verbosity": " Decorative verbosity adds no evidence."}[axis]
            cases.append(variant)
    return cases


def observe_domain(brief):
    from .brief_contracts import BRIEF, checked
    from .brief_projection import substance
    unknown = {"state": "unknown", "domain_ref": DOMAIN, "semantic_basis_id": None, "reason": "unsupported_complete_brief"}
    try:
        value = checked(BRIEF, brief)
        if value["global_blockers"] or not value["accounting_complete"] or not any(row["eligible_questions"] for row in value["original_outcomes"]):
            return unknown
        pending = [substance(value)]
        while pending:
            part = pending.pop()
            if type(part) is dict:
                pending.extend(part.values())
            elif type(part) is list:
                pending.extend(part)
            elif type(part) is str and (len(part) > 4096 or any(not 32 <= ord(c) <= 126 for c in part)):
                return unknown
        field_manifest({"brief": substance(value)})
        return {"state": "observed", "domain_ref": DOMAIN, "semantic_basis_id": digest(substance(value)),
            "descriptor_sha256": digest(DESCRIPTOR), "reason": "exact_permitted_printable_field_extent"}
    except (EvidenceWikiError, ValueError):
        return unknown
