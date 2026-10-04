"""Closed inert complete review delivery shapes; codecs confer no authority."""

from copy import deepcopy

from ._pack_io import bounded, canonical
from .errors import EvidenceWikiError
from .mutation_contracts import detached
from .onboarding_contract import _matches
from .packet_pages import refuse

SCHEMAS = ("research-review-delivery/v1", "research-review-page/v1", "research-paged-review/v1",
    "research-review-packet/v1", "research-host-assembled-review/v1", "research-complete-review-delivery/v1",
    "research-review-sections/v1", "research-blinded-review-input/v1")


def schemas():
    from .calculation_contracts import schema_document as calculation_schema
    from .pack_discovery import owner

    def obj(**fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    def arr(item, maximum=512):
        return {"type": "array", "items": item, "maxItems": maximum}
    def text(maximum=4096):
        return {"type": "string", "maxLength": maximum}
    def nullable(shape):
        return {"anyOf": [shape, {"type": "null"}]}
    hashed = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    counter = {"type": "integer", "minimum": 0, "maximum": 4194304}
    identity = text(256)
    unknown = {"type": "string", "const": "not_evaluated"}
    false = {"type": "boolean", "const": False}
    metadata = {"type": "object", "properties": {**{key: text() for key in ("title", "published_at", "source_type", "url")},
        "authors": arr(text(256), 16)}, "additionalProperties": False}
    evidence = obj(source_id=identity, record_sha256=hashed, observed_at=text(65536),
        capture={"enum": ["primary", "excerpt", "summary", "unknown"]}, quote=nullable(text(65536)),
        location_hint=nullable(text(65536)), anchor=nullable(obj(pointer=text(512), expected=text(512))))
    claim_fields = dict(id=identity, question_slug=identity, kind={"enum": ["assertion", "inference"]},
        text=text(65536), scope=text(65536), time=text(65536), units=text(65536), derivation=nullable(text(65536)), limitations=arr(text(65536), 16),
        premises=arr(identity, 32), evidence=arr(evidence, 16))
    reference = calculation_schema("evidence-calculation-selection/v1")["properties"]["reference"]
    claim = {"anyOf": [obj(**claim_fields), obj(**claim_fields, calculations=arr(reference, 16))]}
    rubric = obj(id=identity, revision=identity, criteria=obj(**{key: text(65536) for key in
        ("support", "source_suitability", "scope", "time", "units", "counterevidence")}))
    question = obj(slug=identity, original_id=identity, question=text(65536))
    source = obj(source_id=identity, source_revision=nullable(hashed), record_sha256=hashed, metadata=metadata)
    source_descriptor = obj(page_id=identity, source_id=identity, sha256=hashed, required={"type": "boolean", "const": True})
    section_descriptor = obj(page_id=identity, section_id={"enum": ["computation", "mechanical-counter"]}, sha256=hashed, required={"type": "boolean", "const": True})
    descriptor = {"anyOf": [source_descriptor, section_descriptor]}
    page_common = dict(schema_version={"const": "research-review-page/v1"}, context_id=identity, context_sha256=hashed,
        page_id=identity, start=counter, end=counter, text=text(65536), semantic_approval=unknown)
    page = {"anyOf": [obj(**page_common, source_id=identity, source_revision=nullable(hashed), record_sha256=hashed, source_chars=counter),
        obj(**page_common, section_id={"enum": ["computation", "mechanical-counter"]}, section_sha256=hashed, section_chars=counter)]}
    header = obj(schema_version={"const": "research-paged-review/v1"}, context_id=identity, context_sha256=hashed,
        task_role={"enum": ["evaluator", "critic", "adjudicator"]}, basis_id=hashed, rubric=rubric, claim=claim, question=question,
        premises=arr(claim, 32), sources=arr(source, 64),
        mechanical_counter_context=obj(status={"enum": ["retained", "not_provided"]}, section_sha256=nullable(hashed),
            counter_search_completeness={"const": "not_established"}),
        computation=obj(required={"type": "boolean"}, section_sha256=nullable(hashed)), pages=arr(descriptor),
        missing_source_ids=arr(identity, 64), limitations=arr(text(128), 32), complete_evidence_delivered=false,
        semantic_approval=unknown, accepted=false)
    completeness = obj(complete={"type": "boolean"}, missing_page_ids=arr(identity), semantic_approval=unknown, accepted=false)
    packet = obj(schema_version={"const": "research-review-packet/v1"}, header=header, pages=arr(page), complete=completeness)
    host = obj(schema_version={"const": "research-host-assembled-review/v1"}, claim_id=identity,
        packet_sha256=hashed, packet=packet, worker_consumption={"const": "not_observed"}, semantic_approval=unknown, accepted=false)
    delivery = obj(schema_version={"const": "research-complete-review-delivery/v1"}, packet_sha256=hashed, packet=packet,
        delivery_scope={"const": "bounded_host_assembled_input"}, worker_comprehension={"const": "not_observed"},
        semantic_approval=unknown, accepted=false)
    plan = obj(schema_version={"const": "research-review-delivery/v1"}, mode={"const": "bounded_host_assembled"},
        page_chars={"type": "integer", "minimum": 256, "maximum": 16384},
        max_packet_bytes={"type": "integer", "minimum": 4096, "maximum": 196608},
        max_input_bytes={"type": "integer", "minimum": 4096, "maximum": 524288},
        required_sections={"const": ["sources", "computation_if_configured", "mechanical", "counter_context"]},
        worker_comprehension={"const": "not_observed"}, semantic_approval=unknown)
    diagnostic = owner("_claim_diagnostics").schema_document()["properties"]["claims"]["items"]["properties"]
    mechanical_evidence = deepcopy(diagnostic["evidence"]["items"])
    for key in ("location_hint", "anchor_pointer"):
        mechanical_evidence["properties"].pop(key)
        mechanical_evidence["required"].remove(key)
    mechanical_claim = obj(claim_id=identity, mechanical_result={"enum": ["verified", "not_established"]},
        evidence=arr(mechanical_evidence, 16), semantic_approval=unknown, accepted=false)
    counter_quote = obj(source_id=identity, record_sha256=hashed, start=counter, end=counter, quote=text(4096),
        truncated={"type": "boolean"}, grounding={"const": "exact_current_source_slice"}, semantic_approval=unknown, accepted=false)
    # The native question-plan owner validates arbitrary scientific scope values;
    # the supplement is also bounded and exactly rebound before every use.
    scope = {"type": "object", "maxProperties": 32}
    from .planning_contracts import REQUEST
    from .planning_contracts import schemas as planning_schemas
    criteria = deepcopy(planning_schemas()[REQUEST]["properties"]["decisions"]["properties"]["question_plans"]["items"]["properties"]["criteria"]["items"])
    alternative = obj(source_id=identity, record_sha256=hashed, metadata=metadata, content=text(49152))
    sections = obj(schema_version={"const": "research-review-sections/v1"}, context_sha256=hashed,
        mechanical=arr(mechanical_claim, 33), counter_material=arr(counter_quote, 32),
        counter_context=obj(status={"enum": ["not_observed", "current_native_instruction"]}, criteria=arr(criteria, 32),
            scope=scope, alternative_sources=arr(alternative, 32)), counter_search_completeness={"const": "not_established"},
        semantic_approval=unknown, accepted=false)
    context_source = obj(**source["properties"], content=text(65536))
    manifest = obj(required_source_ids=arr(identity, 64), included_source_ids=arr(identity, 64), missing_source_ids=arr(identity, 64),
        omitted_source_ids=arr(identity, 64), context_complete={"type": "boolean"}, truncated={"type": "boolean"},
        limitations=arr(text(128), 32), sections=arr(obj(section_id=identity, sha256=hashed, bytes=counter,
            included={"type": "boolean"}, required={"type": "boolean", "const": True}), 128))
    context_fields = dict(context_id=identity, task_role={"enum": ["evaluator", "critic", "adjudicator"]}, basis_id=hashed,
        rubric=rubric, claim=claim, question=question, premises=arr(claim, 32), sources=arr(context_source, 64), manifest=manifest)
    context = obj(**context_fields, schema_version={"enum": ["research-review-context/v1", "research-review-context/v2"]},
        computation=nullable(calculation_schema("research-calculation-context/v1")))
    output_contract = obj(verdicts=obj(**{key: {"const": "pass|fail|unknown"} for key in
        ("support", "source_suitability", "scope", "time", "units", "counterevidence")}), rationale=text())
    standard_input = obj(packet=context, instructions=text(65536), output_contract=output_contract, complete_review_delivery=delivery)
    examination = obj(basis_id={"type": "string", "pattern": r"^[a-f0-9]{64}$(?![\s\S])"}, alternative_sources=arr(obj(source_id=identity,
        record_sha256=hashed, content=text(49152)), 32), criteria=arr(criteria, 32), scope=scope, semantic_approval=unknown)
    counter_output = obj(schema_version={"const": "evidence-counterevidence-proposal/v1"}, basis_id=text(64), findings=text())
    standard_input["properties"]["output_contract"] = {"anyOf": [output_contract, counter_output]}
    standard_input["properties"].update(examination=examination, purpose={"const": "counterevidence"})
    standard_input["allOf"] = [{"if": {"required": ["purpose"]}, "then": {"required": ["examination"]},
        "else": {"not": {"required": ["examination"]}}}]
    values = (plan, page, header, packet, host, delivery, sections, standard_input)
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **shape} for name, shape in zip(SCHEMAS, values, strict=True)}


def schema_document(identifier):
    result = schemas().get(identifier)
    if result is None:
        refuse("packet_schema_unknown")
    return deepcopy(result)


def validate(identifier, value):
    bounded(value)
    try:
        _matches(value, schema_document(identifier))
    except (EvidenceWikiError, ValueError, TypeError, KeyError):
        refuse("packet_contract_invalid")
    if identifier == "research-blinded-review-input/v1":
        expected = {"packet", "instructions", "output_contract", "complete_review_delivery"}
        if "purpose" in value:
            expected |= {"purpose", "examination"}
        if set(value) != expected:
            refuse("packet_contract_invalid")
        from .review_context import _projection
        try:
            _projection(value["packet"])
        except (ValueError, TypeError, KeyError):
            refuse("packet_contract_invalid")
    if len(canonical(value)) > 524288:
        refuse("packet_contract_bound")
    return detached(value)
