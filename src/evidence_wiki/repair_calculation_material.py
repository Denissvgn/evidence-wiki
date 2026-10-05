"""Scientific calculation change detection over exact native result documents."""

from evidence_wiki.calculation_context import project
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.execution_disagreement import _distinct, _fail
from evidence_wiki.mutation_contracts import detached, digest
from evidence_wiki.pack_discovery import owner


def calculation_material(result, source_material, sources):
    """Retain arithmetic/time/units/invariants, excluding artifact bookkeeping.

    The caller must obtain this result from the current native computation owner.
    This projection checks shape and source correlation; it is not reproduction,
    source permission, engine authentication or semantic acceptance.
    """
    supplied = {item["source_id"]: item for item in sources}
    aliases = {identifier: "source-" + str(index + 1) for index, identifier in enumerate(sorted(source_material))}
    try:
        projected = project(result, aliases)
    except (EvidenceWikiError, ValueError, KeyError, TypeError):
        _fail("material_calculation_invalid")
    for row in result["sources"]:
        if row["source_id"] not in supplied or row["record_sha256"] != supplied[row["source_id"]]["record_sha256"]:
            _fail("calculation_source_changed")
    lineage = {}
    for identifier, row in result["lineage"].items():
        descriptor = {"kind": row["kind"], "pointer": row["pointer"]}
        if "source_id" in row:
            if row["source_id"] not in source_material:
                _fail("calculation_source_missing")
            descriptor["source_material"] = digest(source_material[row["source_id"]])
        lineage[identifier] = digest(descriptor)
    def scientific(value):
        if type(value) is list:
            return [scientific(item) for item in value]
        if type(value) is not dict:
            return value
        transformed = {}
        for key, part in value.items():
            if key == "finding_id" and "invariant_id" in value and "message" in value:
                continue
            if key in {"lineage", "record_ids"} and type(part) is list:
                if any(identifier not in lineage for identifier in part):
                    _fail("calculation_lineage_unknown")
                transformed[key] = sorted(lineage[identifier] for identifier in part)
            elif key == "record_id" and type(part) is str:
                if part not in lineage:
                    _fail("calculation_lineage_unknown")
                transformed[key] = lineage[part]
            else:
                transformed[key] = scientific(part)
        return transformed
    result_material = {key: scientific(projected[key]) for key in
        ("clock", "status", "graphs", "aggregations", "invariants", "findings")}
    result_material["definition"] = detached(projected["definition"])
    result_material["source_material"] = _distinct([source_material[row["source_id"]] for row in result["sources"]])
    result_material["lineage"] = sorted(set(lineage.values()))
    return detached(result_material)


def reference_material(references, result):
    """Typed references remain mechanical evidence even when their value is wrong."""
    shape = owner("_strict_contract").schema_documents()["evidence-strict-claims/v2"]["properties"]["claims"]["items"]["properties"]["calculations"]
    try:
        owner("_strict_contract").validate_shape(references, shape)
    except (ValueError, KeyError, TypeError):
        _fail("material_calculation_reference_invalid")
    # Current result identity/currency and selected tuples are enforced by the
    # native service/admission and current full reviews, not by these hashes.
    return _distinct([{key: value for key, value in reference.items() if key != "result_id"}
                      for reference in references])
