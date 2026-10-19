"""Canonical resource export must precede complete installed asset discovery."""

import json
from pathlib import Path

import pytest

from evidence_wiki import calculation_contracts, pack_discovery, repair_progress, resources, review_packet_contracts
from evidence_wiki._agent_catalog import CALCULATION_SCHEMAS, REPAIR_SCHEMAS, REVIEW_PACKET_SCHEMAS, resource_paths
from evidence_wiki._script_host import load_packaged_script
from evidence_wiki.calculation_context import schema_document as calculation_context_schema
from evidence_wiki.planning_contracts import question_criteria_schema
from tests._script_loader import load_module

ROOT = Path(__file__).resolve().parents[1]


def unavailable():
    raise RuntimeError("installed assets unavailable")


def test_resource_exports_bootstrap_without_installed_discovery(monkeypatch):
    exporter = load_module("resource_export_bootstrap", ROOT / "tools/sync_agent_resources.py")
    monkeypatch.setattr(pack_discovery, "shared_assets_root", unavailable)
    monkeypatch.setattr(resources, "assets_root", unavailable)

    output = exporter.exports()

    paths = resource_paths()
    for identifier in (*CALCULATION_SCHEMAS, *REPAIR_SCHEMAS, *REVIEW_PACKET_SCHEMAS):
        assert paths[identifier] in output
        assert isinstance(json.loads(output[paths[identifier]]), dict)


@pytest.mark.parametrize("schema", [
    lambda: calculation_contracts.schema_document(calculation_contracts.SELECTION),
    calculation_context_schema,
    lambda: repair_progress.schema_document(repair_progress.PROGRESS),
    lambda: review_packet_contracts.schema_document("research-review-packet/v2"),
    lambda: review_packet_contracts.schema_document("research-review-packet/v3"),
])
def test_runtime_schema_defaults_retain_installed_discovery(monkeypatch, schema):
    monkeypatch.setattr(pack_discovery, "shared_assets_root", unavailable)
    with pytest.raises(RuntimeError, match="installed assets unavailable"):
        schema()


def test_explicit_native_schemas_preserve_default_shapes(monkeypatch):
    owners = {stem: load_packaged_script(ROOT, stem) for stem in
        ("_computation_contract", "_strict_contract", "_claim_diagnostics", "_review_dependency_contract")}
    monkeypatch.setattr(calculation_contracts, "owner", owners.__getitem__)
    monkeypatch.setattr(repair_progress, "owner", owners.__getitem__)
    monkeypatch.setattr(pack_discovery, "owner", owners.__getitem__)
    computation = owners["_computation_contract"].schemas()
    strict = owners["_strict_contract"].schema_documents()
    diagnostics = owners["_claim_diagnostics"].schema_document()

    for identifier in CALCULATION_SCHEMAS:
        assert calculation_contracts.schema_document(identifier, computation) == calculation_contracts.schema_document(identifier)
    assert repair_progress.schemas(strict) == repair_progress.schemas()
    assert review_packet_contracts.schemas(computation, diagnostics, strict) == review_packet_contracts.schemas()
    retained = json.loads((ROOT / resource_paths()["research-review-sections/v1"]).read_bytes())
    assert question_criteria_schema() == retained["properties"]["counter_context"]["properties"]["criteria"]["items"]


def test_dependency_exports_fit_existing_bound_and_resolve_local_references():
    schemas = review_packet_contracts.schemas()

    def visit(value, scope):
        if type(value) is dict:
            if "$id" in value:
                scope = value
            reference = value.get("$ref")
            if reference is not None:
                assert reference.startswith("#/")
                target = scope
                for component in reference[2:].split("/"):
                    target = target[component.replace("~1", "/").replace("~0", "~")]
            for child in value.values():
                visit(child, scope)
        elif type(value) is list:
            for child in value:
                visit(child, scope)

    for identifier in review_packet_contracts.DEPENDENCY_SCHEMAS:
        schema = schemas[identifier]
        raw = (json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        assert len(raw) <= 65536
        visit(schema, schema)
