"""Narrow canonical context classification and semantic-basis drift oracles."""

import copy
import json

import pytest

from evidence_wiki.finite_domain import DESCRIPTOR, DOMAIN, SOURCE, observe_domain
from evidence_wiki.qualification_calibration import digest
from evidence_wiki.reference_oracles import protected_cases


def inputs():
    reference = protected_cases()[0]
    body = reference["input_document"]
    facts, claim = body["facts"], body["claim"]
    text = DESCRIPTOR["claim_grammar"].format(operation=facts["operation"], **claim)
    source = {"source_id": "native-source", "record_sha256": "sha256:" + "a" * 64,
        "metadata": {"source_id": "native-source", "evidence_usable": True, "title": "Retained finite observation"},
        "content": json.dumps({"schema_version": SOURCE, "facts": facts}),
        "source_binding": {"source_id": "native-source", "extent_ref": "/workspace/raw/observation.json", "closure_id": "sha256:" + "b" * 64}}
    strict = {"id": "candidate", "question_slug": "q", "qualification": "supported", "text": text,
        "scope": claim["scope"], "time": claim["time"], "units": claim["units"], "premises": [], "derivation": None,
        "limitations": [], "evidence": [{"source_id": "native-source", "record_sha256": source["record_sha256"],
            "observed_at": "2026-10-01T00:00:00Z", "capture": "primary", "quote": None, "location_hint": None, "anchor": None}]}
    return {"originals": [{"original_id": "original", "original_text": "Assess the exact declared finite observation.",
        "questions": [{"slug": "q", "question_id": "original"}], "missing_question_ids": [], "accepted": False}],
        "strict_claims": [strict], "rubric": {"id": "finite-observation", "revision": "1", "criteria": dict.fromkeys(
            ("support", "source_suitability", "scope", "time", "units", "counterevidence"), "Assess exact finite context.")},
        "sources": [source]}


def test_exact_native_finite_context_is_classified_without_conferring_acceptance():
    value = inputs()
    result = observe_domain(**value)
    assert result["state"] == "observed" and result["domain_ref"] == DOMAIN
    assert result["semantic_basis_id"].startswith("sha256:")
    assert result["descriptor_sha256"] == "sha256:" + digest(DESCRIPTOR)
    assert result["authorized"] is False and result["evidence_accepted"] is False
    assert observe_domain(**value) == result


@pytest.mark.parametrize("field", ["original_text", "claim", "rubric", "source_content", "source_closure", "source_extent", "record_hash", "source_metadata"])
def test_semantic_inputs_and_full_source_bindings_change_semantic_basis(field):
    value = inputs()
    before = observe_domain(**value)["semantic_basis_id"]
    if field == "original_text":
        value["originals"][0]["original_text"] = "Assess the exact declared finite observation for observed-cohort-1."
    elif field == "claim":
        value["strict_claims"][0]["text"] = value["strict_claims"][0]["text"].replace("value=5", "value=6")
    elif field == "rubric":
        value["rubric"]["criteria"]["support"] += " Include exact units."
    elif field == "source_content":
        value["sources"][0]["content"] += "\n"
    elif field == "source_metadata":
        value["sources"][0]["metadata"]["title"] = "Changed source title"
    elif field == "source_closure":
        value["sources"][0]["source_binding"]["closure_id"] = "sha256:" + "c" * 64
    elif field == "source_extent":
        value["sources"][0]["source_binding"]["extent_ref"] = "/workspace/raw/other.json"
    else:
        value["sources"][0]["record_sha256"] = "sha256:" + "d" * 64
        value["strict_claims"][0]["evidence"][0]["record_sha256"] = "sha256:" + "d" * 64
    after = observe_domain(**value)
    assert after["state"] == "observed"
    assert after["semantic_basis_id"] != before


def normalized_carrier(payload):
    return ("# Exact retained observation\n\n## Citation Metadata\n\n- URL: https://example.org/observation\n\n"
        "## Abstract\n\nNone extracted.\n\n## Outline\n\nNone extracted.\n\n"
        "## Extracted Text\n\n" + payload + "\n\n## Figures and Tables\n\n- None recorded.\n\n"
        "## Links\n\n- None recorded.\n\n## Raw Source Paths\n\n- `raw/observation.json`\n\n"
        "## Parse Warnings\n\n- None recorded.\n")


def test_native_normalized_carrier_keeps_whole_material_in_qualified_basis():
    value = inputs()
    plain = observe_domain(**value)
    value["sources"][0]["content"] = normalized_carrier(value["sources"][0]["content"])
    normalized = observe_domain(**value)
    assert normalized["state"] == "observed"
    assert normalized["semantic_basis_id"] != plain["semantic_basis_id"]
    value["sources"][0]["content"] = value["sources"][0]["content"].replace("example.org/observation", "example.org/another")
    changed = observe_domain(**value)
    assert changed["state"] == "observed" and changed["semantic_basis_id"] != normalized["semantic_basis_id"]


def test_exact_native_host_custody_boilerplate_is_not_additional_factual_evidence():
    value = inputs()
    body = normalized_carrier(value["sources"][0]["content"])
    body = body.replace("## Abstract\n\nNone extracted.",
        "## Abstract\n\nHost-provided text with explicit capture qualifications.")
    body = body.replace("## Parse Warnings\n\n- None recorded.",
        "## Parse Warnings\n\n- Host origin, scope, completeness and rights are caller declarations; captured bytes are checked independently.")
    value["sources"][0]["content"] = body
    assert observe_domain(**value)["state"] == "observed"


@pytest.mark.parametrize("change", ["abstract", "outline", "media", "warning", "duplicate", "extra", "preface", "trailing", "fenced"])
def test_normalized_carrier_cannot_hide_additional_or_ambiguous_facts(change):
    value = inputs()
    payload = value["sources"][0]["content"]
    body = normalized_carrier(payload)
    replacements = {
        "abstract": ("## Abstract\n\nNone extracted.", "## Abstract\n\nThe measured value was negated."),
        "outline": ("## Outline\n\nNone extracted.", "## Outline\n\n- An opposing finding"),
        "media": ("## Figures and Tables\n\n- None recorded.", "## Figures and Tables\n\n- Table: conflicting value"),
        "warning": ("## Parse Warnings\n\n- None recorded.", "## Parse Warnings\n\n- Missing operands"),
        "duplicate": ("## Extracted Text", "## Extracted Text\n\n## Extracted Text"),
        "extra": ("## Extracted Text", "## Contrary Evidence\n\nConflicting result.\n\n## Extracted Text"),
        "preface": ("# Exact retained observation", "An unsupported premise.\n\n# Exact retained observation"),
        "trailing": (payload, payload + "\nThe opposite interpretation is correct."),
        "fenced": (payload, "```json\n" + payload + "\n```"),
    }
    before, after = replacements[change]
    value["sources"][0]["content"] = body.replace(before, after, 1)
    assert observe_domain(**value)["state"] == "unknown"


def test_question_native_status_and_run_bookkeeping_do_not_change_semantic_basis():
    value = inputs()
    before = observe_domain(**value)
    original = value["originals"][0]
    original.update(accepted=True, status="answered", outcome_ref="native-resolution", run_id="another-run")
    original["questions"][0].update(status="answered", verified=True, answered_by="native-owner", updated="later")
    assert observe_domain(**value) == before


@pytest.mark.parametrize("change", ["no_sources", "no_claims", "prose", "label_only", "unknown_descriptor", "wrong_grammar",
    "summary", "unbound_hash", "inference", "calculation", "unsupported_rubric", "duplicate_sources", "duplicate_claims",
    "source_scope_extra", "source_schema_extra", "json_duplicate", "no_originals", "unmapped_question", "zero_ratio", "missing_facts", "unrelated_original"])
def test_unsupported_context_or_arbitrary_domain_labels_remain_unknown(change):
    value = inputs()
    if change == "no_sources":
        value["sources"] = []
    elif change == "no_claims":
        value["strict_claims"] = []
    elif change in {"prose", "label_only"}:
        value["sources"][0]["content"] = "The value might be valid because a paper describes it."
        if change == "label_only":
            value["sources"][0]["metadata"].update(domain_pack="finite-structured-observation", domain_ref=DOMAIN)
    elif change == "unknown_descriptor":
        value["descriptor"] = {"domain_ref": DOMAIN}
    elif change == "wrong_grammar":
        value["strict_claims"][0]["text"] = "The observation probably generalizes to all cohorts."
    elif change == "summary":
        value["strict_claims"][0]["evidence"][0]["capture"] = "summary"
    elif change == "unbound_hash":
        value["strict_claims"][0]["evidence"][0]["record_sha256"] = "sha256:" + "f" * 64
    elif change == "inference":
        value["strict_claims"][0].update(qualification="inference", premises=["another-claim"], derivation="Extend findings.")
    elif change == "calculation":
        value["strict_claims"][0]["calculations"] = [{"result_id": "unqualified-result"}]
    elif change == "unsupported_rubric":
        value["rubric"]["criteria"] = {"support": "Always agree."}
    elif change == "duplicate_sources":
        value["sources"].append(copy.deepcopy(value["sources"][0]))
    elif change == "duplicate_claims":
        value["strict_claims"].append(copy.deepcopy(value["strict_claims"][0]))
    elif change == "source_scope_extra":
        extra = copy.deepcopy(value["sources"][0])
        extra["source_id"] = extra["source_binding"]["source_id"] = "unrelated-source"
        value["sources"].append(extra)
    elif change == "source_schema_extra":
        body = json.loads(value["sources"][0]["content"])
        body["label"] = "accept"
        value["sources"][0]["content"] = json.dumps(body)
    elif change == "json_duplicate":
        value["sources"][0]["content"] = '{"schema_version":"' + SOURCE + '","facts":null,"facts":{}}'
    elif change == "no_originals":
        value["originals"] = []
    elif change == "unrelated_original":
        value["originals"][0]["original_text"] = "Does this medical treatment cure disease?"
    elif change == "unmapped_question":
        value["strict_claims"][0]["question_slug"] = "outside"
    elif change == "zero_ratio":
        body = json.loads(value["sources"][0]["content"])
        body["facts"].update(operation="ratio", values=["1", "0"])
        value["sources"][0]["content"] = json.dumps(body)
    else:
        value["sources"][0]["content"] = json.dumps({"schema_version": SOURCE, "facts": None})
    result = observe_domain(**value)
    assert result["state"] == "unknown" and result["domain_ref"] is None
    assert result["semantic_basis_id"] is None
    assert result["authorized"] is False and result["evidence_accepted"] is False


def test_wrong_finite_claim_is_classifiable_but_not_accepted_or_scored():
    value = inputs()
    value["strict_claims"][0]["text"] = value["strict_claims"][0]["text"].replace("value=5", "value=999")
    value["strict_claims"][0]["units"] = "wrong-units"
    value["strict_claims"][0]["text"] = value["strict_claims"][0]["text"].replace("units=measurement-units", "units=wrong-units")
    result = observe_domain(**value)
    assert result["state"] == "observed"
    assert "verdict" not in result and "qualified" not in result
    assert result["evidence_accepted"] is False
