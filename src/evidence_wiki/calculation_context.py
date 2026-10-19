"""Bounded deterministic calculation projection for independent interpretation."""

from evidence_wiki._pack_io import bounded, canonical
from evidence_wiki.mutation_contracts import detached, digest

from .calculation_contracts import checked_result, refuse

CONTEXT = "research-calculation-context/v1"


def project(result, source_aliases):
    """Retain arithmetic and scientific definitions while masking source names."""
    result = checked_result(result)
    if type(source_aliases) is not dict or not {row["source_id"] for row in result["sources"]} <= source_aliases.keys():
        refuse("calculation_source_context_missing")
    sources = [{"source_id":source_aliases[row["source_id"]], "record_sha256":row["record_sha256"],
        "structured_sha256":row["structured_sha256"], "source_revision":row["source_revision"]} for row in result["sources"]]
    lineage = {}
    for identifier, row in result["lineage"].items():
        if "source_id" in row:
            lineage[identifier] = {"kind":row["kind"], "source_id":source_aliases[row["source_id"]],
                "record_sha256":row["record_sha256"], "structured_sha256":row["structured_sha256"], "pointer":row["pointer"]}
        else:
            lineage[identifier] = detached(row)
    definition = detached(result["definition"])
    # A selector path is an owner locator, not independent model evidence. Its
    # binding survives in definition_id; aliases expose the retained source set.
    for item in definition["aggregations"].values():
        item["selector"]["source_glob"] = "owner-selected-sources"
        item["output_target"] = None
    for item in definition["invariants"].values():
        if item["selector"] is not None:
            item["selector"]["source_glob"] = "owner-selected-sources"
    for item in definition["graphs"].values():
        item["output_page"] = None
    projection = {"schema_version":CONTEXT, "result_id":result["result_id"], "definition_id":result["definition_id"],
        "input_id":result["input_id"], "engine_id":result["engine_id"], "clock":result["clock"], "status":result["status"],
        "definition":definition, "graphs":result["graphs"], "aggregations":result["aggregations"],
        "invariants":result["invariants"], "findings":result["findings"], "lineage":lineage,
        "sources":sources, "semantic_approval":"not_evaluated", "accepted":False}
    bounded(projection)
    if len(canonical(projection)) > 196_608:
        refuse("calculation_context_bound")
    return detached(projection)


def check_projection(projection, result, source_aliases):
    """The authenticated parent binds the exact current native projection."""
    if project(result, source_aliases) != projection:
        refuse("calculation_context_changed")
    return digest(projection)


def schema_document(native=None):
    """Publish the complete redacted decisive packet shape."""
    if native is None:
        from .pack_discovery import owner
        native = owner("_computation_contract").schemas()
    native = native["evidence-computation-result/v1"]["properties"]
    hashed = native["result_id"]
    source = {"type":"object", "properties":{"source_id":{"type":"string", "pattern":r"^source-[1-9][0-9]?$"},
        "record_sha256":hashed, "structured_sha256":hashed, "source_revision":{"anyOf":[hashed, {"type":"null"}]}},
        "required":["source_id", "record_sha256", "structured_sha256", "source_revision"], "additionalProperties":False}
    fields = {key:native[key] for key in ("result_id", "definition_id", "input_id", "engine_id", "clock",
        "status", "definition", "graphs", "aggregations", "invariants", "findings")}
    fields.update(schema_version={"const":CONTEXT}, sources={"type":"array", "items":source, "maxItems":64},
        lineage={"type":"object", "maxProperties":4096, "additionalProperties":{"anyOf":[
            {"type":"object", "properties":{**{key:shape for key, shape in source["properties"].items() if key != "source_revision"},
                "kind":{"enum":["record", "field"]}, "pointer":{"type":"string", "maxLength":512}},
                "required":["kind", "source_id", "record_sha256", "structured_sha256", "pointer"], "additionalProperties":False},
            {"type":"object", "properties":{"kind":{"enum":["constant", "table", "rule"]}, "definition_id":hashed,
                "pointer":{"type":"string", "minLength":1, "maxLength":512}},
                "required":["kind", "definition_id", "pointer"], "additionalProperties":False}]}},
        semantic_approval={"const":"not_evaluated"}, accepted={"const":False})
    return {"$schema":"https://json-schema.org/draft/2020-12/schema", "type":"object", "properties":fields,
        "required":list(fields), "additionalProperties":False}


def validate_context(value):
    """Check retained inert wire shape; the parent must rebind exact owner inputs."""
    fields = {"schema_version", "result_id", "definition_id", "input_id", "engine_id", "clock", "status",
        "definition", "graphs", "aggregations", "invariants", "findings", "lineage", "sources", "semantic_approval", "accepted"}
    bounded(value)
    if (type(value) is not dict or set(value) != fields or value["schema_version"] != CONTEXT
            or value["accepted"] is not False or value["semantic_approval"] != "not_evaluated"
            or type(value["status"]) is not str or value["status"] not in {"passed", "failed"} or type(value["sources"]) is not list or len(value["sources"]) > 64):
        refuse("calculation_context_invalid")
    import re
    for field in ("result_id", "definition_id", "input_id", "engine_id"):
        if type(value[field]) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", value[field]) is None:
            refuse("calculation_context_invalid")
    from .pack_discovery import owner
    contract = owner("_computation_contract")
    schema = contract.schemas()[contract.RESULT_SCHEMA]["properties"]
    try:
        for field in ("definition", "clock", "graphs", "aggregations", "invariants", "findings"):
            contract.validate_shape(value[field], schema[field])
    except (ValueError, TypeError, KeyError):
        refuse("calculation_context_invalid")
    if type(value["lineage"]) is not dict or len(value["lineage"]) > 4096:
        refuse("calculation_context_invalid")
    aliases = set()
    source_rows = {}
    for row in value["sources"]:
        if (type(row) is not dict or set(row) != {"source_id", "record_sha256", "structured_sha256", "source_revision"}
                or type(row["source_id"]) is not str or re.fullmatch(r"source-[1-9][0-9]?", row["source_id"]) is None):
            refuse("calculation_context_invalid")
        if row["source_id"] in aliases:
            refuse("calculation_context_invalid")
        aliases.add(row["source_id"])
        source_rows[row["source_id"]] = row
        for field in ("record_sha256", "structured_sha256", "source_revision"):
            if field == "source_revision" and row[field] is None:
                continue
            if type(row[field]) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", row[field]) is None:
                refuse("calculation_context_invalid")
    for identifier, row in value["lineage"].items():
        if type(identifier) is not str or type(row) is not dict:
            refuse("calculation_context_invalid")
        if "source_id" in row:
            if (set(row) != {"kind", "source_id", "record_sha256", "structured_sha256", "pointer"}
                    or type(row["kind"]) is not str or row["kind"] not in {"record", "field"}
                    or type(row["source_id"]) is not str or row["source_id"] not in aliases):
                refuse("calculation_context_invalid")
            for field in ("record_sha256", "structured_sha256"):
                if type(row[field]) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", row[field]) is None:
                    refuse("calculation_context_invalid")
            if any(row[field] != source_rows[row["source_id"]][field] for field in ("record_sha256", "structured_sha256")):
                refuse("calculation_context_invalid")
            try:
                contract.pointer(row["pointer"])
            except (ValueError, TypeError):
                refuse("calculation_context_invalid")
        elif (set(row) != {"kind", "definition_id", "pointer"} or type(row["kind"]) is not str or row["kind"] not in {"constant", "table", "rule"}
                or row["definition_id"] != value["definition_id"]):
            refuse("calculation_context_invalid")
        elif type(row["pointer"]) is not str or not 0 < len(row["pointer"]) <= 512:
            refuse("calculation_context_invalid")
    def check_lineage(item):
        if type(item) is dict:
            if "lineage" in item and (type(item["lineage"]) is not list or not set(item["lineage"]) <= value["lineage"].keys()):
                refuse("calculation_context_invalid")
            for part in item.values():
                check_lineage(part)
        elif type(item) is list:
            for part in item:
                check_lineage(part)
    for field in ("graphs", "aggregations", "findings", "clock"):
        check_lineage(value[field])
    if len(canonical(value)) > 196_608:
        refuse("calculation_context_bound")
    return value


def validate_references(references, context):
    from evidence_wiki.pack_discovery import owner
    validate_context(context)
    if type(references) is not list or len(references) > 16:
        refuse("calculation_reference_invalid")
    for reference in references:
        if (type(reference) is not dict or set(reference) != {"result_id", "pointer", "expected", "form", "unit", "rounded"}
                or reference["result_id"] != context["result_id"] or type(reference["form"]) is not str
                or reference["form"] not in {"value", "formatted"}
                or type(reference["pointer"]) is not str or not 0 < len(reference["pointer"]) <= 512
                or type(reference["expected"]) is not str or not 0 < len(reference["expected"]) <= 4096
                or type(reference["unit"]) is not str or not 0 < len(reference["unit"]) <= 128
                or type(reference["rounded"]) is not bool):
            refuse("calculation_reference_invalid")
        resolved = owner("_structured_view").resolve_pointer(context, reference["pointer"])
        value = resolved.value if resolved.ok else None
        if (type(value) is not dict or value.get("type") != "decimal" or value.get(reference["form"]) != reference["expected"]
                or value.get("unit") != reference["unit"] or value.get("rounded") != reference["rounded"]):
            refuse("calculation_reference_invalid")
