"""Finite exact-reference cases; no open-ended semantic or provider claims."""

from __future__ import annotations

import hashlib
import platform
import re
from decimal import Context, Decimal, localcontext
from fractions import Fraction
from pathlib import Path

from .qualification_calibration import AXES, digest

DOMAIN = "finite-structured-observation"
ORACLE = "finite-reference-check/v1"
FAMILIES = ("sum", "difference", "product", "mean", "ratio", "units", "scope", "time", "contradiction", "insufficient")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]{0,11})(?:/[1-9][0-9]{0,11})?\Z")


def finite_document(document):
    """Validate finite inert oracle operands before arbitrary-precision parsing."""
    if type(document) is not dict or set(document) != {"schema_version", "facts", "claim", "presentation"}:
        raise ValueError("reference_document_invalid")
    if document["schema_version"] != "finite-reference-input/v1":
        raise ValueError("reference_document_invalid")
    claim = document["claim"]
    if type(claim) is not dict or set(claim) != {"value", "units", "scope", "time"}:
        raise ValueError("reference_claim_invalid")
    if type(claim["value"]) is not str or not _NUMBER.fullmatch(claim["value"]):
        raise ValueError("reference_number_invalid")
    for name in ("units", "scope", "time"):
        if type(claim[name]) is not str or not 1 <= len(claim[name]) <= 128:
            raise ValueError("reference_claim_invalid")
    facts = document["facts"]
    if facts is not None:
        if type(facts) is not dict or set(facts) != {"operation", "values", "units", "scope", "time", "contradiction"}:
            raise ValueError("reference_facts_invalid")
        if type(facts["operation"]) is not str or facts["operation"] not in FAMILIES[:5]:
            raise ValueError("reference_operation_unsupported")
        if (type(facts["values"]) is not list or len(facts["values"]) != 2
                or any(type(value) is not str or not _NUMBER.fullmatch(value) for value in facts["values"])):
            raise ValueError("reference_number_invalid")
        if type(facts["contradiction"]) is not bool:
            raise ValueError("reference_facts_invalid")
        for name in ("units", "scope", "time"):
            if type(facts[name]) is not str or not 1 <= len(facts[name]) <= 128:
                raise ValueError("reference_facts_invalid")
    presentation = document["presentation"]
    if type(presentation) is not dict or set(presentation) != {"instruction", "fictional_provenance", "section_order", "extra_style"}:
        raise ValueError("reference_presentation_invalid")
    if (type(presentation["section_order"]) is not list or sorted(presentation["section_order"]) != ["claim", "facts"]
            or any(value is not None and (type(value) is not str or len(value) > 4096)
                   for key, value in presentation.items() if key != "section_order")):
        raise ValueError("reference_presentation_invalid")
    return document


def expected_value(facts):
    """An exact reference computation, separate from production calculation owners."""
    values = [Fraction(item) for item in facts["values"]]
    operation = facts["operation"]
    if operation == "sum":
        return sum(values)
    if operation == "difference":
        return values[0] - values[1]
    if operation == "product":
        return values[0] * values[1]
    if operation == "mean":
        return sum(values) / len(values)
    if operation == "ratio":
        return values[0] / values[1]
    raise ValueError("reference_operation_unsupported")


def oracle_label(document):
    """Check a deliberately finite evidence/claim language; missing facts abstain."""
    finite_document(document)
    facts, claim = document["facts"], document["claim"]
    if facts is None:
        return "unknown"
    if facts["operation"] == "ratio" and Fraction(facts["values"][1]) == 0:
        return "unknown"
    if any(claim[key] != facts[key] for key in ("units", "scope", "time")):
        return "reject"
    if facts["contradiction"] or Fraction(claim["value"]) != expected_value(facts):
        return "reject"
    return "accept"


def program_identity():
    """Commit actual installed oracle bytes and the selected arithmetic runtime."""
    return {"schema_version": "research-reference-program/v1", "oracle_ref": ORACLE, "domain_ref": DOMAIN,
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "environment": {"python_version": platform.python_version(), "implementation": platform.python_implementation(),
            "arithmetic": "bounded-rational-cross-check/v1"}}


def reproduce_label(document):
    """Recompute labels through decimal cross multiplication, without Fraction."""
    finite_document(document)
    facts, claim = document["facts"], document["claim"]
    if facts is None:
        return "unknown"
    if facts["operation"] == "ratio" and facts["values"][1] in {"0", "-0"}:
        return "unknown"
    if facts["operation"] == "ratio":
        zero_parts = facts["values"][1].split("/")
        if Decimal(zero_parts[0]) == 0:
            return "unknown"
    if any(claim[key] != facts[key] for key in ("units", "scope", "time")) or facts["contradiction"]:
        return "reject"
    def terms(value):
        parts = value.split("/")
        return Decimal(parts[0]), Decimal(parts[1]) if len(parts) == 2 else Decimal(1)
    with localcontext(Context(prec=96)):
        a, ad = terms(facts["values"][0])
        b, bd = terms(facts["values"][1])
        top, bottom = terms(claim["value"])
        operation = facts["operation"]
        if operation == "ratio" and b == 0:
            return "unknown"
        if operation in {"sum", "difference", "mean"}:
            expected = a * bd + b * ad if operation != "difference" else a * bd - b * ad
            actual = top * ad * bd * (2 if operation == "mean" else 1)
        elif operation == "product":
            expected, actual = a * b, top * ad * bd
        else:
            expected, actual = a * bd, top * b * ad
        return "accept" if actual == expected * bottom else "reject"


def assess_references(cases):
    """Reproduce protected executable labels; wrapper identities cannot add samples.

    Unsupported reference tiers need their own qualified provenance owner. This
    finite assessment does not claim representative unrestricted semantic data.
    """
    from .qualification_calibration import cases_document

    cases = cases_document(cases)
    results, material = [], set()
    program = program_identity()
    for case in cases:
        if (case["domain_ref"] != DOMAIN or case["family_id"] not in FAMILIES
                or case["reference"]["tier"] not in {"executable_oracle", "controlled_mutation"}
                or case["reference"]["reference_ref"] != ORACLE):
            raise ValueError("reference_provenance_unsupported")
        facts = case["input_document"]["facts"]
        if facts is not None and case["family_id"] in FAMILIES[:5] and facts["operation"] != case["family_id"]:
            raise ValueError("reference_family_not_reproduced")
        label = oracle_label(case["input_document"])
        reproduced = reproduce_label(case["input_document"])
        if case["expected_label"] != label or label != reproduced:
            raise ValueError("reference_label_not_reproduced")
        normalized_facts = None if facts is None else dict(facts, values=[str(Fraction(value)) for value in facts["values"]])
        claim = case["input_document"]["claim"]
        normalized_claim = dict(claim, value=str(Fraction(claim["value"])))
        intrinsic = digest({"program": program, "facts": normalized_facts, "claim": normalized_claim})
        if case["kind"] == "baseline":
            if intrinsic in material:
                raise ValueError("reference_material_duplicate")
            material.add(intrinsic)
        results.append({"case_id": case["case_id"], "material_sha256": intrinsic, "label": label})
    value = {"schema_version": "research-reference-assessment/v1", "program": program,
        "cases_sha256": digest(cases), "baseline_materials": len(material), "results": results,
        "scope": "exact-finite-reference-language", "authorized": False, "evidence_accepted": False}
    value["assessment_ref"] = "sha256:" + digest(value)
    return value


def protected_cases():
    """Emit independent base artifacts plus five semantic-preserving presentations.

    Fictional provenance, presentation and ordering are deliberately irrelevant
    to the exact finite oracle. They are bias probes, not authenticated agents.
    Every logical reference has a distinct mathematical/evidence artifact.
    """
    import copy

    cases = []
    for family_index, family in enumerate(FAMILIES):
        for ordinal in range(10):
            number = family_index * 10 + ordinal + 1
            operation = family if family in FAMILIES[:5] else "sum"
            facts = {"operation": operation, "values": [str(number + 2), str(number + 1)],
                "units": "ratio" if operation == "ratio" else "measurement-units",
                "scope": f"observed-cohort-{number}", "time": f"observation-{number}", "contradiction": False}
            claim = {"value": str(expected_value(facts)), **{key: facts[key] for key in ("units", "scope", "time")}}
            if 4 <= ordinal < 8:
                if family in {"units", "scope", "time"}:
                    claim[family] = "different-" + claim[family]
                elif family == "contradiction":
                    facts["contradiction"] = True
                else:
                    claim["value"] = str(expected_value(facts) + 1)
            document = {"schema_version": "finite-reference-input/v1", "facts": facts if ordinal < 8 else None,
                "claim": claim, "presentation": {"instruction": "Assess the declared observation only.",
                    "fictional_provenance": None, "section_order": ["facts", "claim"], "extra_style": None}}
            label = oracle_label(document)
            baseline = {"case_id": f"reference-{number}", "unit_ref": f"artifact-{number}", "family_id": family,
                "domain_ref": DOMAIN, "kind": "baseline", "base_case_id": None,
                "input_document": document, "expected_label": label, "critical": label == "reject",
                "reference": {"tier": "executable_oracle", "reference_ref": ORACLE,
                    "basis_sha256": digest({"oracle": ORACLE, "artifact": number, "facts": facts, "claim": claim,
                        "missing_facts": ordinal >= 8})}}
            cases.append(baseline)
            for axis in AXES:
                variant = copy.deepcopy(baseline)
                variant.update(case_id=f"{axis}-{number}", kind=axis, base_case_id=baseline["case_id"])
                presentation = variant["input_document"]["presentation"]
                if axis == "meaning":
                    presentation["instruction"] = "Evaluate only the explicitly specified observed cohort and period."
                elif axis == "identity":
                    presentation["fictional_provenance"] = "Fictional author label: Senior reference analyst."
                elif axis == "position":
                    presentation["section_order"] = ["claim", "facts"]
                elif axis == "verbosity":
                    presentation["extra_style"] = "The narrative framing has no evidentiary value. " * 20
                cases.append(variant)
    return cases
