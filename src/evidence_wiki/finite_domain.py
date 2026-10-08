"""Pure classification of exact finite observation context; never acceptance."""

from __future__ import annotations

import json
import re
from fractions import Fraction

from .qualification_calibration import digest, plain

DOMAIN = "finite-structured-observation"
OBSERVATION = "research-reference-domain-observation/v1"
SOURCE = "finite-structured-observation/v1"
DESCRIPTOR = {"schema_version": "research-reference-domain-descriptor/v1", "domain_ref": DOMAIN,
    "source_schema": SOURCE, "classifier_version": "finite-observation-grammar/v2",
    "source_container": "plain JSON or exact normalized sections with finite JSON Extracted Text and no additional factual sections",
    "operations": ["sum", "difference", "product", "mean", "ratio"],
    "original_grammar": "Assess the exact declared finite observation[ for {scope}[ at {time}]].",
    "values_per_source": 2, "scope": "exact-declared-cohort-and-observation-label",
    "claim_grammar": "Finite observation: {operation}; value={value}; units={units}; scope={scope}; time={time}.",
    "limitations": ["Only exact finite declarations are classifiable; free prose and computational premise graphs are unsupported.",
        "Domain classification is not an evidence verdict, source authorization or semantic release."]}
_CLAIM_FIELDS = {"id", "question_slug", "qualification", "text", "scope", "time", "units", "evidence", "premises", "derivation", "limitations"}
_EVIDENCE_FIELDS = {"source_id", "record_sha256", "observed_at", "capture", "quote", "location_hint", "anchor"}
_RUBRIC = {"support", "source_suitability", "scope", "time", "units", "counterevidence"}
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")
_HASH = re.compile(r"sha256:[a-f0-9]{64}\Z")
_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]{0,11})\Z")
_RATIONAL = re.compile(r"-?(?:0|[1-9][0-9]{0,11})(?:/[1-9][0-9]{0,11})?\Z")
_QUESTION = re.compile(r"Assess the exact declared finite observation(?: for ([A-Za-z0-9][A-Za-z0-9._:@/-]{0,127})(?: at ([A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}))?)?\.\Z")
_TEXT = re.compile(r"Finite observation: (sum|difference|product|mean|ratio); value=(-?(?:0|[1-9][0-9]{0,11})(?:/[1-9][0-9]{0,11})?); units=([^;]+); scope=([^;]+); time=([^;]+)\.\Z")


def _require(condition):
    if not condition:
        raise ValueError("finite_domain_unsupported")


def _ref(value):
    _require(type(value) is str and _REF.fullmatch(value) is not None)
    return value


def _hash(value):
    _require(type(value) is str and _HASH.fullmatch(value) is not None)
    return value


def _originals(values):
    _require(type(values) is list and 1 <= len(values) <= 100)
    result, ids, slugs = [], set(), set()
    for row in values:
        _require(type(row) is dict)
        identifier = _ref(row["original_id"])
        _require(identifier not in ids and type(row["original_text"]) is str and 1 <= len(row["original_text"]) <= 16384)
        _require(_QUESTION.fullmatch(row["original_text"]) is not None)
        ids.add(identifier)
        _require(type(row["questions"]) is list and 1 <= len(row["questions"]) <= 100)
        questions = []
        for question in row["questions"]:
            _require(type(question) is dict)
            slug, question_id = _ref(question["slug"]), _ref(question["question_id"])
            _require(slug not in slugs)
            slugs.add(slug)
            questions.append({"slug": slug, "question_id": question_id})
        _require(row.get("missing_question_ids", []) == [])
        result.append({"original_id": identifier, "original_text": row["original_text"], "questions": questions})
    return result, slugs


def finite_content(content):
    """Read the finite payload without discarding ambiguous carrier material.

    Native callers validate the normalized record, source closure and current
    permissions. This pure parser accepts only the exact section carrier and
    refuses additional factual text, duplicate headings, warnings or media.
    Complete carrier bytes remain part of the observed semantic basis.
    """
    _require(type(content) is str and len(content.encode()) <= 65536)
    if content.lstrip().startswith("{"):
        return content
    names = ("Citation Metadata", "Abstract", "Outline", "Extracted Text",
             "Figures and Tables", "Links", "Raw Source Paths", "Parse Warnings")
    headings = list(re.finditer(r"^##[ \t]+([^\r\n]+?)[ \t]*$", content, re.MULTILINE))
    _require(tuple(match.group(1) for match in headings) == names)
    _require(re.fullmatch(r"[ \r\n]*# [^\r\n]{1,4096}[ \r\n]*", content[:headings[0].start()]) is not None)
    sections = {name: content[match.end():headings[index + 1].start() if index + 1 < len(headings) else len(content)].strip()
                for index, (name, match) in enumerate(zip(names, headings, strict=True))}
    empty = {"", "None extracted.", "- None extracted.", "None.", "- None.",
             "- None recorded.", "- No outline extracted.", "- No warnings."}
    _require(sections["Abstract"] in empty | {"Host-provided text with explicit capture qualifications."})
    _require(all(sections[name] in empty for name in ("Outline", "Figures and Tables")))
    _require(sections["Parse Warnings"] in empty | {
        "- Host origin, scope, completeness and rights are caller declarations; captured bytes are checked independently."})
    return sections["Extracted Text"]


def _facts(source):
    content = source["content"]
    _require(type(content) is str and len(content.encode()) <= 65536)
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result)
            result[key] = value
        return result
    document = json.loads(finite_content(content), object_pairs_hook=pairs)
    _require(type(document) is dict and set(document) == {"schema_version", "facts"} and document["schema_version"] == SOURCE)
    facts = document["facts"]
    _require(type(facts) is dict and set(facts) == {"operation", "values", "units", "scope", "time", "contradiction"})
    _require(type(facts["operation"]) is str and facts["operation"] in DESCRIPTOR["operations"])
    _require(type(facts["values"]) is list and len(facts["values"]) == 2
        and all(type(value) is str and _INTEGER.fullmatch(value) is not None for value in facts["values"]))
    _require(facts["operation"] != "ratio" or Fraction(facts["values"][1]) != 0)
    for key in ("units", "scope", "time"):
        _ref(facts[key])
    _require(type(facts["contradiction"]) is bool)
    return facts


def observe_domain(*, originals, strict_claims, rubric, sources, descriptor=None):
    """Classify native-owner inputs, producing an inert semantic context digest.

    `originals` are canonical original_outcomes (status/run fields are ignored).
    `strict_claims` are complete native strict claim objects, without review rows.
    `sources` are the complete current authorized packets from `_sources`, each
    retaining source_binding(source_id, extent_ref, closure_id), record hash,
    exact normalized content and metadata. The caller owns authority/currentness;
    this pure function cannot authenticate arbitrary supplied dictionaries.
    Empty, missing, malformed or unsupported context returns unknown, never a
    positive verdict. Classification permits wrong finite claims so independent
    evaluators can reject them; it does not perform semantic acceptance.
    """
    unknown = {"schema_version": OBSERVATION, "state": "unknown", "reason": "unsupported_finite_context",
        "domain_ref": None, "semantic_basis_id": None, "descriptor_sha256": None,
        "authorized": False, "evidence_accepted": False}
    try:
        supplied = plain({"originals": originals, "strict_claims": strict_claims, "rubric": rubric,
            "sources": sources, "descriptor": DESCRIPTOR if descriptor is None else descriptor})
        _require(supplied["descriptor"] == DESCRIPTOR)
        retained_originals, slugs = _originals(supplied["originals"])
        retained_rubric = supplied["rubric"]
        _require(type(retained_rubric) is dict and set(retained_rubric) == {"id", "revision", "criteria"}
            and type(retained_rubric["criteria"]) is dict and set(retained_rubric["criteria"]) == _RUBRIC)
        _ref(retained_rubric["id"])
        _ref(retained_rubric["revision"])
        _require(all(type(value) is str and 1 <= len(value) <= 16384 for value in retained_rubric["criteria"].values()))
        retained_sources, facts_by_id = supplied["sources"], {}
        _require(type(retained_sources) is list and 1 <= len(retained_sources) <= 64)
        for source in retained_sources:
            _require(type(source) is dict and set(source) == {"source_id", "record_sha256", "metadata", "content", "source_binding"})
            identifier = _ref(source["source_id"])
            _require(identifier not in facts_by_id and type(source["metadata"]) is dict)
            _hash(source["record_sha256"])
            binding = source["source_binding"]
            _require(type(binding) is dict and set(binding) == {"source_id", "extent_ref", "closure_id"}
                and binding["source_id"] == identifier and type(binding["extent_ref"]) is str and 1 <= len(binding["extent_ref"]) <= 4096)
            _hash(binding["closure_id"])
            _require(source["metadata"].get("evidence_usable") is not False)
            facts_by_id[identifier] = _facts(source)
        retained_claims, ids, referenced = supplied["strict_claims"], set(), set()
        _require(type(retained_claims) is list and 1 <= len(retained_claims) <= 100)
        for claim in retained_claims:
            _require(type(claim) is dict and set(claim) in (_CLAIM_FIELDS, _CLAIM_FIELDS | {"calculations"}))
            identifier = _ref(claim["id"])
            _require(identifier not in ids and _ref(claim["question_slug"]) in slugs)
            ids.add(identifier)
            _require(claim["qualification"] in {"attributed", "supported", "contested", "insufficient_evidence"}
                and claim["premises"] == [] and claim["derivation"] is None and not claim.get("calculations"))
            _require(type(claim["limitations"]) is list and len(claim["limitations"]) <= 16
                and all(type(item) is str and 1 <= len(item) <= 16384 for item in claim["limitations"]))
            _require(type(claim["text"]) is str)
            parsed = _TEXT.fullmatch(claim["text"])
            _require(parsed is not None)
            operation, value, units, scope, time = parsed.groups()
            _require(_RATIONAL.fullmatch(value) is not None and all(_REF.fullmatch(item) is not None for item in (units, scope, time)))
            _require((claim["units"], claim["scope"], claim["time"]) == (units, scope, time))
            evidence = claim["evidence"]
            _require(type(evidence) is list and len(evidence) == 1)
            item = evidence[0]
            _require(type(item) is dict and set(item) == _EVIDENCE_FIELDS)
            source_id = _ref(item["source_id"])
            _require(source_id in facts_by_id and item["capture"] == "primary" and item["anchor"] is None)
            source = next(row for row in retained_sources if row["source_id"] == source_id)
            _require(item["record_sha256"] == source["record_sha256"] and operation == facts_by_id[source_id]["operation"])
            _require(type(item["observed_at"]) is str and 1 <= len(item["observed_at"]) <= 128
                and (item["quote"] is None or type(item["quote"]) is str and 1 <= len(item["quote"]) <= 65536)
                and (item["location_hint"] is None or type(item["location_hint"]) is str and 1 <= len(item["location_hint"]) <= 512))
            referenced.add(source_id)
        _require(referenced == set(facts_by_id))
        basis = {"descriptor": supplied["descriptor"], "originals": retained_originals, "strict_claims": retained_claims,
            "rubric": retained_rubric, "authorized_sources": retained_sources}
        return {**unknown, "state": "observed", "reason": "exact_finite_context", "domain_ref": DOMAIN,
            "semantic_basis_id": "sha256:" + digest(basis), "descriptor_sha256": "sha256:" + digest(DESCRIPTOR)}
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, RecursionError, OverflowError):
        return unknown
