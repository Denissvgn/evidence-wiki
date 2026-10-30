"""Owned child codecs preserve bytes and never exempt arbitrary deep JSON."""

import json

import pytest

from evidence_wiki._review_dependency_json import encoded
from evidence_wiki.agent_execution import ExecutionError, _json
from evidence_wiki.pack_discovery import owner


def _authorship():
    return {"schema_version": "evidence-strict-authorship/v2", "basis_id": "sha256:" + "1" * 64,
        "claim_id": "claim", "claim_schema_version": "evidence-strict-claims/v1",
        "observed_workspace_basis_id": "sha256:" + "2" * 64, "semantic_material_id": "sha256:" + "3" * 64}


def test_owned_and_legacy_json_bytes_keep_exact_existing_no_lf_spelling():
    for value in (_authorship(), {"schema_version": "ordinary/v1", "label": "café", "value": -0.0}):
        expected = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        assert _json(value) == expected and not _json(value).endswith(b"\n")
    assert encoded({"schema_version": "ordinary/v1"}) is None


def test_malformed_known_version_cannot_gain_a_depth_exemption():
    document = _authorship()
    document["extra"] = "unadmitted"
    with pytest.raises(ExecutionError):
        _json(document)


def test_unknown_version_and_unowned_child_keep_ordinary_depth_limit():
    deep = "leaf"
    for _ in range(18):
        deep = {"opaque": deep}
    for document in ({"schema_version": "research-review-context/v99", "opaque": deep},
            {"unowned": {"schema_version": "evidence-strict-authorship/v2", "opaque": deep}},
            {"t": "mapping", "v": deep}):
        with pytest.raises(ExecutionError):
            _json(document)


def test_cycle_refuses_without_silently_cutting_the_child():
    document = _authorship()
    document["claim_id"] = document
    with pytest.raises(ExecutionError):
        _json(document)


def test_wire_discriminator_alone_never_grants_an_owned_child_budget():
    document = {"schema_version": "research-review-native-bytes/v1", "kind": "blinded_material", "opaque": [None] * 16384}
    assert encoded(document) is None
    with pytest.raises(ExecutionError):
        _json(document)


def test_tagged_semantic_depth_has_its_own_declared_schema_budget():
    native = owner("_review_dependency_contract")
    shapes = native.schema_documents()["research-dependency-review-material/v1"]["$defs"]
    schema = {"$defs": shapes, "$ref": "#/$defs/node"}
    node = {"t": "string", "v": "leaf"}
    for _ in range(17):
        node = {"t": "list", "v": [node]}
    with pytest.raises(ValueError):
        native.validate_schema(node, schema)


def _large_basis():
    return {"schema_version": "evidence-review-dependency-basis/v1",
        "review_basis_mode": "claim_dependencies_v1", "basis_id": "sha256:" + "1" * 64,
        "target_claim_id": "claim", "claim_schema_version": "evidence-strict-claims/v1",
        "required_review_role": "evaluator", "computation_mode": "none", "premise_closure": [],
        "question_slugs": ["question"], "semantic_material_id": "sha256:" + "2" * 64,
        "dependencies": [{"scope": "global", "kind": "workspace_material", "coordinate": "file-" + str(index),
            "content_id": "sha256:" + "3" * 64} for index in range(3300)],
        "coverage": {"classification_complete": True, "reviewer_material_complete": True,
            "truncated": False, "missing_coordinates": [], "unclassified_coordinates": []}}


def test_execution_accepts_native_child_above_ordinary_node_limit_with_exact_bytes():
    from evidence_wiki._review_dependency_json import detached

    basis = _large_basis()
    # Each dependency has five value nodes; its independently valid native
    # representation exceeds the ordinary execution document allowance.
    assert len(basis["dependencies"]) * 5 > 16384
    raw = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert _json(basis) == raw
    assert detached(basis) == json.loads(raw)
    basis["dependencies"][0]["scope"] = "unrecognized"
    with pytest.raises(ExecutionError):
        _json(basis)


def test_native_wrapper_keeps_shared_aggregate_and_byte_limits():
    basis = _large_basis()
    wrapper = {"observation": None, "error_code": None, "result": basis,
        "ordinary": [[None] * 4096 for _ in range(10)]}
    with pytest.raises(ValueError, match="aggregate_bound"):
        encoded(wrapper)
    with pytest.raises(ExecutionError):
        _json(wrapper)
    wrapper["ordinary"] = ["x" * 1000] * 800
    with pytest.raises(ExecutionError):
        _json(wrapper)
    for ordinary in ({"items": [None] * 16384}, {"result": basis}):
        with pytest.raises(ExecutionError):
            _json(ordinary)
