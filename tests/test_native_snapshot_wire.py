"""Constructed native snapshots survive canonical transport without aliases."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from evidence_wiki._review_dependency_wire import pack, unpack
from evidence_wiki.pack_discovery import owner
from tests.test_native_review_signing import _prepared
from tests.test_strict_evidence import CORE


@pytest.mark.parametrize("configured", [False, True])
def test_constructed_snapshot_roundtrips_native_wire(tmp_path, configured):
    prepared = _prepared()
    original = prepared["snapshot"]
    material = deepcopy(original["semantic_material"])
    computation = None
    if configured:
        from tests._computation_fixture import definition, graph, workspace
        declaration = definition()
        declaration["graphs"]["worksheet"] = graph()
        workspace(tmp_path / "computation", declaration)
        computation = owner("_computation_runtime").evaluate(tmp_path / "computation")
        owner("_computation_contract").validate_result(computation)
    files = {row["path"]: b"---\ntype: question\nquestion: Sample?\nstatus: open\n---\nExact body.\r\n"
        for row in material["questions"]}
    snapshot = CORE._dependency_snapshot(tmp_path, {}, original["policy"], SimpleNamespace(files=files),
        {"questions": [row["manifest_question"] for row in material["questions"]]},
        material, {"selected": {}}, computation)
    native = owner("_review_dependency_contract")
    assert native._exact_json_bytes(snapshot)[1]
    raw = native.canonical_bytes(snapshot)
    wire = pack(snapshot, "native_snapshot")
    restored = unpack(wire, "native_snapshot")
    assert restored == snapshot
    assert native.canonical_bytes(restored) == raw
    assert restored["computation"] == computation
    assert snapshot["claim"] is not snapshot["semantic_material"]["claims"][0]
    assert native._exact_json_bytes(restored)[1]
