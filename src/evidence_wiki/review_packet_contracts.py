"""Closed inert complete review delivery shapes; codecs confer no authority."""

from copy import deepcopy

from ._pack_io import bounded, canonical
from ._review_dependency_json import detached
from .errors import EvidenceWikiError
from .onboarding_contract import _matches
from .packet_pages import refuse

SCHEMAS = ("research-review-delivery/v1", "research-review-page/v1", "research-paged-review/v1",
    "research-review-packet/v1", "research-host-assembled-review/v1", "research-complete-review-delivery/v1",
    "research-review-sections/v1", "research-blinded-review-input/v1")
COMPLETE_SCHEMAS = tuple(name.replace("/v1", "/v2") for name in SCHEMAS if name != "research-review-page/v1")
DEPENDENCY_SCHEMAS = tuple(name.replace("/v1", "/v3") for name in SCHEMAS if name != "research-review-page/v1")


def schemas(native_computation=None, native_diagnostics=None, native_dependency=None):
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
    def questions(item):
        return {**arr(item, 32), "minItems": 1}
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
    reference = calculation_schema("evidence-calculation-selection/v1", native_computation)["properties"]["reference"]
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
    if native_diagnostics is None:
        native_diagnostics = owner("_claim_diagnostics").schema_document()
    diagnostic = native_diagnostics["properties"]["claims"]["items"]["properties"]
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
    from .planning_contracts import question_criteria_schema
    criteria = question_criteria_schema()
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
        computation=nullable(calculation_schema("research-calculation-context/v1", native_computation)))
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
    result = {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **shape} for name, shape in zip(SCHEMAS, values, strict=True)}
    question_complete = obj(**question["properties"], metadata=nullable({"type": "object"}), body=text(65536))
    complete_claim_fields = {**claim_fields, "qualification": {"enum": ["attributed", "supported", "inference", "contested", "insufficient_evidence"]}}
    claim_complete = {"anyOf": [obj(**complete_claim_fields), obj(**complete_claim_fields, calculations=arr(reference, 16))]}
    complete_context_fields = {**context_fields, "claim": claim_complete, "premises": arr(claim_complete, 32)}
    context_complete = obj(**complete_context_fields, schema_version={"const": "research-review-context/v3"},
        questions=questions(question_complete), computation=nullable(calculation_schema("research-calculation-context/v1", native_computation)))
    header_complete = deepcopy(header)
    header_complete["properties"].update(schema_version={"const": "research-paged-review/v2"}, questions=questions(question_complete),
        claim=claim_complete, premises=arr(claim_complete, 32))
    header_complete["required"].append("questions")
    packet_complete = obj(**packet["properties"])
    packet_complete["properties"].update(schema_version={"const": "research-review-packet/v2"}, header=header_complete)
    host_complete = deepcopy(host)
    host_complete["properties"].update(schema_version={"const": "research-host-assembled-review/v2"}, packet=packet_complete)
    delivery_complete = deepcopy(delivery)
    delivery_complete["properties"].update(schema_version={"const": "research-complete-review-delivery/v2"}, packet=packet_complete)
    plan_complete = deepcopy(plan)
    plan_complete["properties"].update(schema_version={"const": "research-review-delivery/v2"},
        context_version={"const": "research-review-context/v3"})
    plan_complete["required"].append("context_version")
    sections_complete = deepcopy(sections)
    sections_complete["properties"].update(schema_version={"const": "research-review-sections/v2"},
        counter_context=obj(status={"const": "current_native_instruction"}, questions=questions(obj(question_slug=identity,
            claim_ids=arr(identity, 32), criteria=arr(criteria, 32), scope=scope, alternative_sources=arr(alternative, 32)))))
    input_complete = deepcopy(standard_input)
    input_complete["properties"].update(packet=context_complete, complete_review_delivery=delivery_complete)
    complete_values = (plan_complete, header_complete, packet_complete, host_complete, delivery_complete, sections_complete, input_complete)
    result.update({name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **shape}
        for name, shape in zip(COMPLETE_SCHEMAS, complete_values, strict=True)})
    if native_dependency is None:
        native_dependency = owner("_review_dependency_contract").schema_documents()
    from ._review_dependency_wire import schema as wire_schema
    wire_material = wire_schema()
    wire_material["properties"]["kind"] = {"const": "blinded_material"}
    material_schema = {"oneOf": [native_dependency["research-dependency-review-material/v1"], wire_material]}
    dependency_context = deepcopy(context_complete)
    dependency_question = deepcopy(question_complete)
    dependency_question["properties"]["metadata"] = {"$ref": "#/$defs/node"}
    dependency_context["properties"].update(schema_version={"const": "research-review-context/v4"},
        questions=questions(dependency_question),
        dependency_material={"$ref": "#/$defs/dependency_material"})
    dependency_context["required"].append("dependency_material")
    computation_definitions = {}
    dependency_context["properties"]["computation"] = _shared_computation(
        dependency_context["properties"]["computation"], computation_definitions)
    dependency_header = deepcopy(header_complete)
    dependency_header["properties"]["schema_version"] = {"const": "research-paged-review/v3"}
    dependency_header["properties"]["questions"] = questions(dependency_question)
    for shape in (dependency_context, dependency_header):
        shape["properties"].update(claim={"$ref": "#/$defs/claim"},
            premises=arr({"$ref": "#/$defs/claim"}, 32), rubric={"$ref": "#/$defs/rubric"},
            question={"$ref": "#/$defs/question_record"}, questions=questions({"$ref": "#/$defs/question_context"}))
    dependency_packet = deepcopy(packet_complete)
    dependency_packet["properties"].update(schema_version={"const": "research-review-packet/v3"}, header=dependency_header)
    dependency_host = deepcopy(host_complete)
    dependency_host["properties"].update(schema_version={"const": "research-host-assembled-review/v3"}, packet=dependency_packet)
    dependency_delivery = deepcopy(delivery_complete)
    dependency_delivery["properties"].update(schema_version={"const": "research-complete-review-delivery/v3"}, packet=dependency_packet)
    dependency_plan = deepcopy(plan_complete)
    dependency_plan["properties"].update(schema_version={"const": "research-review-delivery/v3"},
        context_version={"const": "research-review-context/v4"})
    dependency_sections = deepcopy(sections_complete)
    dependency_sections["properties"].update(schema_version={"const": "research-review-sections/v3"},
        mechanical=arr(deepcopy(native_diagnostics["properties"]["claims"]["items"]), 32))
    dependency_input = deepcopy(input_complete)
    dependency_input["properties"].update(packet=dependency_context, complete_review_delivery=dependency_delivery)
    dependency_values = (dependency_plan, dependency_header, dependency_packet, dependency_host,
        dependency_delivery, dependency_sections, dependency_input)
    dependency_definitions = {"dependency_material": deepcopy(material_schema),
        "node": deepcopy(native_dependency["research-dependency-review-material/v1"]["$defs"]["node"]), "claim": claim_complete,
        "rubric": rubric, "question_record": question, "question_context": dependency_question,
        **computation_definitions}
    for name, shape in zip(DEPENDENCY_SCHEMAS, dependency_values, strict=True):
        result[name] = {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": _used_definitions(shape, dependency_definitions), **shape}
    return result


def _shared_computation(shape, definitions):
    """Intern exact repeated calculation shapes within their existing scope."""
    maps = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas", "dependencies"}
    arrays = {"allOf", "anyOf", "oneOf", "prefixItems"}
    singles = {"additionalProperties", "unevaluatedProperties", "propertyNames", "contains", "not",
        "if", "then", "else", "additionalItems", "unevaluatedItems", "contentSchema"}

    def children(value, visit):
        result = {}
        for name, child in value.items():
            if name in maps and type(child) is dict:
                result[name] = {key: visit(item) for key, item in child.items()}
            elif (name in arrays or name == "items") and type(child) is list:
                result[name] = [visit(item) for item in child]
            elif name in singles or name == "items":
                result[name] = visit(child)
            else:
                result[name] = deepcopy(child)
        return result

    counts, values = {}, {}

    def collect(value):
        if type(value) is not dict or "$id" in value:
            return value
        key = canonical(value)
        if len(key) >= 64 and any(name in value for name in ("type", "anyOf", "oneOf", "allOf", "enum", "const")):
            counts[key] = counts.get(key, 0) + 1
            values[key] = value
        children(value, collect)
        return value

    collect(shape)
    names = {key: "calculation_" + str(index + 1) for index, key in enumerate(sorted(
        (key for key, count in counts.items() if len(key) * (count - 1) > 40 * count + 24),
        key=lambda key: (-len(key), key)))}

    def replace(value, retained=None):
        if type(value) is not dict or "$id" in value:
            return deepcopy(value)
        key = canonical(value)
        if key in names and key != retained:
            return {"$ref": "#/$defs/" + names[key]}
        return children(value, replace)

    definitions.update({name: replace(values[key], key) for key, name in names.items()})
    return replace(shape)


def _used_definitions(shape, definitions):
    """Keep local definitions reachable without crossing a native $id scope."""
    used, pending = set(), [shape]
    while pending:
        value = pending.pop()
        if type(value) is dict:
            if "$id" in value:
                continue
            reference = value.get("$ref")
            if type(reference) is str and reference.startswith("#/$defs/"):
                name = reference[len("#/$defs/"):].split("/", 1)[0]
                if name not in used:
                    used.add(name)
                    pending.append(definitions[name])
            pending.extend(value.values())
        elif type(value) is list:
            pending.extend(value)
    return {name: deepcopy(definitions[name]) for name in sorted(used)}


def schema_document(identifier, native_computation=None, native_diagnostics=None, native_dependency=None):
    result = schemas(native_computation, native_diagnostics, native_dependency).get(identifier)
    if result is None:
        refuse("packet_schema_unknown")
    return deepcopy(result)


def validate(identifier, value):
    if identifier not in DEPENDENCY_SCHEMAS:
        bounded(value)
    try:
        shape = schema_document(identifier)
        if identifier in DEPENDENCY_SCHEMAS:
            from .pack_discovery import owner
            owner("_review_dependency_contract").validate_schema(value, shape)
        else:
            _matches(value, shape)
    except (EvidenceWikiError, ValueError, TypeError, KeyError):
        refuse("packet_contract_invalid")
    if identifier in {"research-blinded-review-input/v1", "research-blinded-review-input/v2", "research-blinded-review-input/v3"}:
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
