"""Inert formula interpretation cannot override the deterministic owner result."""

from evidence_wiki._pack_io import bounded
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import detached, digest
from evidence_wiki.pack_discovery import owner

PROPOSAL = "evidence-calculation-proposal/v1"
SELECTION = "evidence-calculation-selection/v1"


def schema_document(identifier=PROPOSAL):
    """Publish the closed inert interpretation proposal; execution stays native."""
    if identifier == "research-calculation-context/v1":
        from .calculation_context import schema_document as context_schema
        return context_schema()
    if identifier == SELECTION:
        contract = owner("_computation_contract").schemas()["evidence-computation-result/v1"]["properties"]
        def obj(**fields):
            return {"type":"object", "properties":fields, "required":list(fields), "additionalProperties":False}
        hashed = contract["result_id"]
        text = lambda maximum: {"type":"string", "minLength":1, "maxLength":maximum}
        fields = dict(schema_version={"const":SELECTION}, reference=obj(result_id=hashed, pointer=text(512),
            expected=text(4096), form={"enum":["value", "formatted"]}, unit=text(128), rounded={"type":"boolean"}),
            binding=obj(definition_id=hashed, input_id=hashed, engine_id=hashed, clock=contract["clock"],
                arithmetic=contract["definition"]["properties"]["arithmetic"], invariants=contract["invariants"],
                source_bindings=contract["sources"], value_lineage={"type":"array", "items":text(128), "maxItems":4096}),
            interpretation=obj(formula=obj(graph_id=text(128), output_id=text(128), expression=text(4096), unit=text(128)),
                assumptions={"type":"array", "items":text(1024), "minItems":1, "maxItems":16, "uniqueItems":True},
                rationale=text(4096), status={"const":"independent_current_verifier_required"}),
            proposal_sha256=hashed, accepted={"const":False}, write_authority={"const":"not_established"})
        return {"$schema":"https://json-schema.org/draft/2020-12/schema", **obj(**fields)}
    if identifier != PROPOSAL:
        refuse("calculation_schema_unknown")
    text = lambda maximum: {"type":"string", "minLength":1, "maxLength":maximum}
    hashed = {"type":"string", "pattern":r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    fields = {"schema_version":{"const":PROPOSAL}, "result_id":hashed, "definition_id":hashed, "pointer":text(512),
        "form":{"enum":["value", "formatted"]}, "expression":text(4096), "unit":text(128),
        "as_of":{"anyOf":[text(40), {"type":"null"}]}, "assumptions":{"type":"array", "items":text(1024),
            "minItems":1, "maxItems":16, "uniqueItems":True}, "rationale":text(4096)}
    return {"$schema":"https://json-schema.org/draft/2020-12/schema", "type":"object", "properties":fields,
        "required":list(fields), "additionalProperties":False}


def refuse(reason):
    raise UsageError("CALCULATION_PROPOSAL_REFUSED", "Calculation proposal refused.", details={"reason":reason})


def checked_result(result):
    """Validate only wire identity; callers must reproduce through the native owner."""
    try:
        value = detached(result)
        owner("_computation_contract").validate_result(value)
        return value
    except (ValueError, TypeError, KeyError):
        refuse("owner_result_invalid")


def formula(result, pointer):
    """Expose the configured expression attached to an exact graph output."""
    if type(pointer) is not str:
        refuse("output_pointer_invalid")
    parts = pointer.split("/")
    if len(parts) != 5 or parts[0] or parts[1] != "graphs" or parts[3] != "outputs":
        refuse("configured_graph_output_required")
    graph_id, output = parts[2], parts[4]
    graph = result["definition"]["graphs"].get(graph_id)
    if graph is None or output not in graph["output_mapping"]:
        refuse("configured_graph_output_required")
    node = graph["nodes"][graph["output_mapping"][output]]
    return {"graph_id":graph_id, "output_id":output, "expression":node["expr"], "unit":node["unit"]}


def choose(result, proposal):
    """Check configured formulas/units; no proposed value is ever trusted."""
    result = checked_result(result)
    bounded(proposal)
    fields = {"schema_version", "result_id", "definition_id", "pointer", "form", "expression", "unit",
        "as_of", "assumptions", "rationale"}
    if type(proposal) is not dict or set(proposal) != fields or proposal["schema_version"] != PROPOSAL:
        refuse("proposal_shape_invalid")
    if proposal["result_id"] != result["result_id"] or proposal["definition_id"] != result["definition_id"]:
        refuse("result_or_definition_changed")
    if result["status"] != "passed" or any(not row["passed"] or not row["complete"] for row in result["invariants"]):
        refuse("invariants_not_passed")
    configured = formula(result, proposal["pointer"])
    if proposal["expression"] != configured["expression"] or proposal["unit"] != configured["unit"]:
        refuse("configured_formula_or_unit_changed")
    if type(proposal["form"]) is not str or proposal["form"] not in {"value", "formatted"} or proposal["as_of"] != result["clock"]["as_of"]:
        refuse("form_or_time_assumption_changed")
    assumptions = proposal["assumptions"]
    if (type(assumptions) is not list or not 1 <= len(assumptions) <= 16
            or any(type(item) is not str or not 0 < len(item) <= 1024 for item in assumptions)
            or len(set(assumptions)) != len(assumptions)
            or type(proposal["rationale"]) is not str or not 0 < len(proposal["rationale"]) <= 4096):
        refuse("interpretation_assumptions_invalid")
    resolved = owner("_structured_view").resolve_pointer(result, proposal["pointer"])
    scalar = resolved.value if resolved.ok else None
    if (type(scalar) is not dict or scalar.get("type") != "decimal" or scalar.get("unit") != proposal["unit"]
            or type(scalar.get(proposal["form"])) is not str):
        refuse("numeric_value_unknown")
    reference = {"result_id":result["result_id"], "pointer":proposal["pointer"], "expected":scalar[proposal["form"]],
        "form":proposal["form"], "unit":scalar["unit"], "rounded":scalar["rounded"]}
    return {"schema_version":SELECTION, "reference":reference,
        "binding":{"definition_id":result["definition_id"], "input_id":result["input_id"], "engine_id":result["engine_id"],
            "clock":result["clock"], "arithmetic":result["definition"]["arithmetic"], "invariants":result["invariants"],
            "source_bindings":result["sources"], "value_lineage":list(scalar["lineage"])},
        "interpretation":{"formula":configured, "assumptions":list(assumptions), "rationale":proposal["rationale"],
            "status":"independent_current_verifier_required"},
        "proposal_sha256":digest(proposal), "accepted":False, "write_authority":"not_established"}


def require_selected_references(proposal, selections, question_slugs):
    """An explicit owner-selected quantitative intent must survive generation.

    The canonical workflow supplies authenticated selections and scoped question
    slugs. This inert comparison grants no arithmetic or publication authority.
    Source observations and premises can retain an empty calculation list.
    """
    if not selections:
        return
    references = [reference for claim in proposal["inventory"]["claims"]
        if claim["question_slug"] in question_slugs and claim["qualification"] == "inference"
        for reference in claim.get("calculations", [])]
    if any(selection["reference"] not in references for selection in selections):
        refuse("selected_calculation_reference_missing")


def require_source_premises(proposal, premises):
    """Generation cannot erase or rewrite its retained direct source premises."""
    claims = proposal["inventory"]["claims"]
    if any(premise not in claims for premise in premises):
        refuse("calculation_source_premise_changed")
