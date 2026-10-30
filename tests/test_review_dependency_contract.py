"""Closed native schemas enforce semantic depth and exact scalar identity."""

import json
from copy import deepcopy

import pytest

from evidence_wiki.pack_discovery import owner


@pytest.fixture
def contract(monkeypatch):
    module = owner("_review_dependency_contract")
    # Small fixtures exercise memo behavior explicitly.
    monkeypatch.setattr(module, "_SCHEMA_CACHE_MIN_BYTES", 0)
    return module


def test_typed_logical_depth_is_independent_of_encoding_wrappers(contract):
    node = {"t": "string", "v": "leaf"}
    for _ in range(15):
        node = {"t": "mapping", "v": [["child", node]]}
    assert contract.validate_node(node) == node
    with pytest.raises(ValueError, match="bound"):
        contract.validate_node({"t": "list", "v": [node]})


def test_typed_shape_does_not_relax_unowned_json_depth(contract):
    node = {"t": "string", "v": "leaf"}
    for _ in range(15):
        node = {"t": "list", "v": [node]}
    with pytest.raises(ValueError, match="bound"):
        contract.validate_schema(node, {"type": "object"})


def test_failed_schema_branch_cannot_grant_an_independent_depth_budget(contract):
    node = {"t": "string", "v": "leaf"}
    for _ in range(12):
        node = {"t": "list", "v": [node]}
    definitions = contract.schema_documents()[contract.MATERIAL]["$defs"]
    schema = {"$defs": definitions, "anyOf": [
        {"allOf": [{"$ref": "#/$defs/node"}, {"const": None}]}, {"type": "object"}]}
    with pytest.raises(ValueError, match="bound"):
        contract.validate_schema(node, schema)


def test_unowned_version_discriminator_cannot_reset_a_depth_budget(contract):
    node = {"leaf": "value"}
    for _ in range(17):
        node = {"schema_version": "evidence-strict-authorship/v2", "child": node}
    with pytest.raises(ValueError, match="bound"):
        contract.validate_schema(node, {"type": "object"})


def test_child_schema_identity_does_not_coerce_boolean_keywords_to_integers(contract):
    value = {"schema_version": "evidence-strict-authorship/v2", "basis_id": "sha256:" + "1" * 64,
        "claim_id": "claim", "claim_schema_version": "evidence-strict-claims/v1",
        "observed_workspace_basis_id": "sha256:" + "2" * 64, "semantic_material_id": "sha256:" + "3" * 64}
    shape = contract.schema_documents()["evidence-strict-authorship/v2"]
    altered = deepcopy(shape)
    altered["additionalProperties"] = 0
    assert shape == altered
    original = value
    for _ in range(15):
        value = {"child": value}
        shape = {"type": "object", "properties": {"child": shape}}
        altered = {"type": "object", "properties": {"child": altered}}
    assert contract.validate_schema(value, shape)
    with pytest.raises(ValueError, match="bound"):
        contract.validate_schema(value, altered)
    assert original["claim_id"] == "claim"


@pytest.mark.parametrize("node", [
    {"t": "integer", "v": "01"}, {"t": "integer", "v": "-0"},
    {"t": "boolean", "v": 1}, {"t": "float", "v": "nan"},
    {"t": "float", "v": "0x0p+0"}, {"t": "date", "v": "2026-02-30"},
    {"t": "null", "v": None},
    {"t": "mapping", "v": [["b", {"t": "null"}], ["a", {"t": "null"}]]},
    {"t": "mapping", "v": [["a", {"t": "null"}], ["a", {"t": "null"}]]},
])
def test_ambiguous_typed_scalars_and_mapping_orders_refuse(contract, node):
    with pytest.raises(ValueError):
        contract.validate_node(node)


def test_schema_compound_keywords_preserve_exact_boolean_integer_distinction(contract):
    schema = {"allOf": [{"type": "object", "required": ["flag"],
        "properties": {"flag": {"const": True}}, "additionalProperties": False},
        {"if": {"properties": {"flag": {"const": True}}}, "then": {"minProperties": 1}}]}
    assert contract.validate_schema({"flag": True}, schema) == {"flag": True}
    with pytest.raises(ValueError):
        contract.validate_schema({"flag": 1}, schema)
    with pytest.raises(ValueError):
        contract.validate_schema({"flag": True, "extra": None}, schema)


def test_nested_schema_ids_use_their_own_local_reference_scope(contract):
    schema = {"type": "object", "properties": {"child": {
        "$id": "native-child", "$defs": {"value": {"const": "inner"}},
        "$ref": "#/$defs/value"}}, "$defs": {"value": {"const": "outer"}}}
    assert contract.validate_schema({"child": "inner"}, schema) == {"child": "inner"}
    with pytest.raises(ValueError):
        contract.validate_schema({"child": "outer"}, schema)
    with pytest.raises(ValueError, match="reference"):
        contract.validate_schema({}, {"$ref": "https://unowned.example/schema"})


def test_schema_documents_are_detached_from_owner_state(contract):
    documents = contract.schema_documents()
    original = deepcopy(documents)
    documents.clear()
    assert contract.schema_documents() == original


def test_snapshot_accepts_native_computation_descriptions(contract, tmp_path):
    from tests._computation_fixture import definition, graph, workspace

    declaration = definition()
    declaration["graphs"]["worksheet"] = graph()
    workspace(tmp_path, declaration)
    result = owner("_computation_runtime").evaluate(tmp_path)
    review = contract.schema_documents()["evidence-strict-review/v3"]
    schema = {"$defs": review["$defs"],
        **review["properties"]["snapshot"]["properties"]["computation"]}
    assert contract.validate_schema(result, schema) == result
    for kind in ("aggregations", "graphs"):
        invalid = deepcopy(result)
        next(iter(invalid["definition"][kind].values()))["description"] = 42
        with pytest.raises(ValueError):
            contract.validate_schema(invalid, schema)


def test_repeated_policy_validation_reuses_the_bound_schema_generation(contract, monkeypatch):
    policy = json.loads(owner("_strict_contract").canonical_bytes({
        "schema_version": "evidence-strict-policy/v2", "review_basis_mode": "claim_dependencies_v1",
        "policy_id": "policy", "revision": "1", "assurance": "artifact_checked", "claims_path": "claims.json",
        "instructions": {"AGENTS.md": "sha256:" + "a" * 64}, "human_review": False,
        "max_source_age_seconds": 3600, "max_review_age_seconds": 3600,
        "rubric": {"id": "rubric", "revision": "1", "criteria": {name: "Assess retained evidence."
            for name in ("support", "source_suitability", "scope", "time", "units", "counterevidence")}},
    }))
    original = json.loads
    parsed = []

    def observed(raw, *args, **kwargs):
        if type(raw) is str and raw.startswith('{"evidence-strict-policy/v2":'):
            parsed.append(raw)
        return original(raw, *args, **kwargs)

    monkeypatch.setattr(json, "loads", observed)
    contract._SHAPE_CACHE.clear()
    for _ in range(5):
        assert contract.validate("evidence-strict-policy/v2", policy) == policy
    assert len(parsed) == 1


@pytest.mark.parametrize("version", ["evidence-strict-claims/v1", "evidence-strict-claims/v2"])
def test_publication_preserves_explicit_claim_calculation_presence(contract, version):
    from tests.test_dependency_consumers import dependency_inputs

    document = dependency_inputs(complete=True)["current"]["canonical_release"]["publication"]
    document["claim_schema_version"] = version
    if version.endswith("v1"):
        for question in document["questions"]:
            for claim in question["claims"]:
                claim.pop("calculations")
    assert contract.validate("evidence-strict-publication/v3", document) == document
    claim = document["questions"][0]["claims"][0]
    if version.endswith("v1"):
        claim["calculations"] = []
    else:
        claim.pop("calculations")
    with pytest.raises(ValueError):
        contract.validate("evidence-strict-publication/v3", document)


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}'])
def test_native_json_rejects_duplicate_keys_and_nonfinite_values(contract, raw):
    with pytest.raises(ValueError):
        contract.loads(raw)


def test_successful_schema_memo_is_detached_and_skips_only_shape_walk(contract, monkeypatch):
    calls = []
    shape = contract._shape

    def observed(*args, **kwargs):
        calls.append(None)
        return shape(*args, **kwargs)

    monkeypatch.setattr(contract, "_shape", observed)
    schema = {"type": "object", "properties": {"flag": {"const": True}},
              "required": ["flag"], "additionalProperties": False}
    value = {"flag": True}
    first = contract.validate_schema(value, schema)
    count = len(calls)
    first["flag"] = False
    assert contract.validate_schema(deepcopy(value), deepcopy(schema)) == value
    assert len(calls) == count
    value["flag"] = False
    with pytest.raises(ValueError):
        contract.validate_schema(value, schema)
    assert len(calls) > count


@pytest.mark.parametrize("invalid", [
    {"flag": 1}, {"flag": True, "extra": None}, {1: True},
    {"flag": float("nan")}, {"flag": float("inf")},
])
def test_schema_memo_never_coerces_changed_values(contract, invalid):
    schema = {"type": "object", "properties": {"flag": {"const": True}},
              "required": ["flag"], "additionalProperties": False}
    contract.validate_schema({"flag": True}, schema)
    with pytest.raises(ValueError):
        contract.validate_schema(invalid, schema)


def test_schema_memo_tracks_schema_and_validation_generation(contract, monkeypatch):
    schema = {"type": "integer", "const": 1}
    contract.validate_schema(1, schema)
    schema["const"] = True
    with pytest.raises(ValueError):
        contract.validate_schema(1, schema)
    schema["const"] = 1

    def refuse(*args, **kwargs):
        raise ValueError("replacement validator")

    monkeypatch.setattr(contract, "_shape", refuse)
    with pytest.raises(ValueError, match="replacement validator"):
        contract.validate_schema(1, schema)


def test_schema_memo_tracks_native_documents_and_module_identity(contract, monkeypatch):
    from types import ModuleType

    native = contract._shapes()
    schema = {"type": "integer"}
    contract.validate_schema(1, schema)
    previous_keys = set(contract._SCHEMA_SUCCESSES)
    replacement = ModuleType("replacement_shapes")
    replacement.DOCUMENTS = deepcopy(native.DOCUMENTS)
    monkeypatch.setattr(contract, "_shapes", lambda: replacement)
    contract.validate_schema(1, schema)
    assert len(contract._SCHEMA_SUCCESSES) == 1
    assert previous_keys.isdisjoint(contract._SCHEMA_SUCCESSES)
    assert next(iter(contract._SCHEMA_SUCCESSES))[3] is replacement
    replacement.DOCUMENTS[contract.MATERIAL]["$id"] = "changed-generation"
    previous_keys = set(contract._SCHEMA_SUCCESSES)
    contract.validate_schema(1, schema)
    assert len(contract._SCHEMA_SUCCESSES) == 1
    assert set(contract._SCHEMA_SUCCESSES) != previous_keys


@pytest.mark.parametrize("bound", ["MAX_BYTES", "MAX_NODES", "MAX_DEPTH"])
def test_schema_memo_observes_changed_bounds(contract, monkeypatch, bound):
    value = {"child": [1]}
    contract.validate_schema(value, True)
    monkeypatch.setattr(contract, bound, 1)
    with pytest.raises(ValueError, match="bound"):
        contract.validate_schema(value, True)


def test_schema_memo_retains_typed_node_checks_and_is_bounded(contract):
    contract._SCHEMA_SUCCESSES.clear()
    schema = {"$ref": "#/$defs/node",
              "$defs": contract.schema_documents()[contract.MATERIAL]["$defs"]}
    contract.validate_schema({"t": "boolean", "v": True}, schema)
    with pytest.raises(ValueError):
        contract.validate_schema({"t": "boolean", "v": 1}, schema)
    for value in range(contract._SCHEMA_CACHE_LIMIT + 2):
        contract.validate_schema(value, {"type": "integer"})
    assert len(contract._SCHEMA_SUCCESSES) == contract._SCHEMA_CACHE_LIMIT


def test_schema_memo_rejects_native_subclasses_even_with_identical_json(contract):
    class Integer(int):
        pass

    class Mapping(dict):
        pass

    contract.validate_schema(1, {"type": "integer"})
    with pytest.raises(ValueError):
        contract.validate_schema(Integer(1), {"type": "integer"})
    contract.validate_schema({"value": 1}, {"type": "object"})
    with pytest.raises(ValueError):
        contract.validate_schema(Mapping(value=1), {"type": "object"})


def test_schema_memo_rejects_changed_version_and_cyclic_input(contract):
    schema = {"type": "object", "properties": {"schema_version": {"const": "native/v1"}},
              "required": ["schema_version"], "additionalProperties": False}
    value = {"schema_version": "native/v1"}
    contract.validate_schema(value, schema)
    value["schema_version"] = "native/v2"
    with pytest.raises(ValueError):
        contract.validate_schema(value, schema)
    value["schema_version"] = value
    with pytest.raises(ValueError, match="cycle"):
        contract.validate_schema(value, schema)


def test_schema_memo_does_not_reuse_alias_depth_allowances(contract):
    schema = {"type": "object", "properties": {
        "typed": {"$ref": "#/$defs/node"}, "other": True},
        "$defs": contract.schema_documents()[contract.MATERIAL]["$defs"]}
    node = {"t": "boolean", "v": True}
    other = node
    for _ in range(15):
        other = [other]
    aliased = {"typed": node, "other": other}
    detached = json.loads(json.dumps(aliased))
    contract._SCHEMA_SUCCESSES.clear()
    assert contract.validate_schema(aliased, schema) == aliased
    assert not contract._SCHEMA_SUCCESSES
    with pytest.raises(ValueError, match="bound"):
        contract.validate_schema(detached, schema)
    assert not contract._SCHEMA_SUCCESSES
    # The failed detached call also cannot alter the legacy alias acceptance.
    assert contract.validate_schema(aliased, schema) == aliased


def test_schema_memo_never_stores_failures(contract, monkeypatch):
    contract._SCHEMA_SUCCESSES.clear()
    calls = []
    shape = contract._shape

    def observed(*args, **kwargs):
        calls.append(None)
        return shape(*args, **kwargs)

    monkeypatch.setattr(contract, "_shape", observed)
    for _ in range(2):
        with pytest.raises(ValueError):
            contract.validate_schema(False, {"const": True})
    assert len(calls) == 2
    assert not contract._SCHEMA_SUCCESSES


def test_schema_memo_guards_lru_operations_but_not_validation(contract, monkeypatch):
    from collections import OrderedDict

    class ObservedLock:
        held = False

        def __enter__(self):
            assert not self.held
            self.held = True

        def __exit__(self, *args):
            self.held = False

    lock = ObservedLock()

    class GuardedMemo(OrderedDict):
        def __contains__(self, key):
            assert lock.held
            return super().__contains__(key)

        def __setitem__(self, key, value):
            assert lock.held
            return super().__setitem__(key, value)

        def move_to_end(self, key):
            assert lock.held
            return super().move_to_end(key)

        def popitem(self, last=True):
            assert lock.held
            return super().popitem(last=last)

    shape = contract._shape

    def observed(*args, **kwargs):
        assert not lock.held
        return shape(*args, **kwargs)

    monkeypatch.setattr(contract, "_SCHEMA_CACHE_LOCK", lock)
    monkeypatch.setattr(contract, "_SCHEMA_SUCCESSES", GuardedMemo())
    monkeypatch.setattr(contract, "_shape", observed)
    schema = {"type": "integer"}
    for value in range(contract._SCHEMA_CACHE_LIMIT + 1):
        contract.validate_schema(value, schema)
    contract.validate_schema(contract._SCHEMA_CACHE_LIMIT, schema)
    assert len(contract._SCHEMA_SUCCESSES) == contract._SCHEMA_CACHE_LIMIT


@pytest.mark.parametrize("value", [
    [None] * 33, {str(i): None for i in range(17)}, "x" * 33, ["x" * 17, "y" * 17],
])
def test_schema_memo_bounds_preprocessing_before_encoding(contract, monkeypatch, value):
    def unexpected_encoding(*args, **kwargs):
        pytest.fail("plainly oversized JSON must fail before encoding")

    monkeypatch.setattr(contract, "MAX_BYTES", 32)
    monkeypatch.setattr(contract, "canonical_bytes", unexpected_encoding)
    with pytest.raises(ValueError, match="bytes_bound"):
        contract.validate_schema(value, True)


def test_schema_memo_validates_encoded_snapshot_when_caller_mutates(contract, monkeypatch):
    contract._SCHEMA_SUCCESSES.clear()
    value = {"flag": False}
    schema = {"type": "object", "properties": {"flag": {"const": True}}}
    original_key = contract._schema_memo_key

    def mutate_after_key(raw, shape):
        key = original_key(raw, shape)
        value["flag"] = True
        return key

    monkeypatch.setattr(contract, "_schema_memo_key", mutate_after_key)
    with pytest.raises(ValueError):
        contract.validate_schema(value, schema)
    assert not contract._SCHEMA_SUCCESSES
    with pytest.raises(ValueError):
        contract.validate_schema({"flag": False}, schema)


@pytest.mark.parametrize("warm", [False, True])
def test_schema_memo_returns_validated_snapshot_when_caller_mutates(contract, monkeypatch, warm):
    contract._SCHEMA_SUCCESSES.clear()
    value = {"flag": True}
    schema = {"type": "object", "properties": {"flag": {"const": True}}}
    if warm:
        contract.validate_schema(value, schema)
    original_key = contract._schema_memo_key

    def mutate_after_key(raw, shape):
        key = original_key(raw, shape)
        value["flag"] = False
        return key

    monkeypatch.setattr(contract, "_schema_memo_key", mutate_after_key)
    assert contract.validate_schema(value, schema) == {"flag": True}
    assert value == {"flag": False}


def test_schema_memo_validates_encoded_schema_and_skips_changed_generation(contract, monkeypatch):
    contract._SCHEMA_SUCCESSES.clear()
    schema = {"const": False}
    original_key = contract._schema_memo_key

    def mutate_after_key(raw, shape):
        key = original_key(raw, shape)
        schema["const"] = True
        return key

    monkeypatch.setattr(contract, "_schema_memo_key", mutate_after_key)
    with pytest.raises(ValueError):
        contract.validate_schema(True, schema)
    assert not contract._SCHEMA_SUCCESSES
    schema["const"] = False
    assert contract.validate_schema(False, schema) is False
    assert not contract._SCHEMA_SUCCESSES


@pytest.mark.parametrize("mutate_schema", [False, True])
def test_small_schema_validation_owns_snapshots_without_memo(contract, monkeypatch, mutate_schema):
    monkeypatch.setattr(contract, "_SCHEMA_CACHE_MIN_BYTES", 4096)
    contract._SCHEMA_SUCCESSES.clear()
    value = {"flag": True}
    schema = {"type": "object", "properties": {"flag": {"const": True}}}
    original_shape = contract._shape
    calls = []

    def forbidden_key(*args):
        pytest.fail("small inputs must not construct a memo key")

    def mutate_then_validate(current, shape, root, depth=0):
        if depth == 0:
            calls.append(True)
            value["flag"] = False
            if mutate_schema:
                schema["properties"]["flag"]["const"] = False
        return original_shape(current, shape, root, depth)

    monkeypatch.setattr(contract, "_schema_memo_key", forbidden_key)
    monkeypatch.setattr(contract, "_shape", mutate_then_validate)
    for generation in range(2):
        value["flag"] = True
        schema["properties"]["flag"]["const"] = True
        if generation:
            monkeypatch.setattr(contract._shapes(), "DOCUMENTS", deepcopy(contract._shapes().DOCUMENTS))
        result = contract.validate_schema(value, schema)
        assert result == {"flag": True}
        result["flag"] = False
    assert len(calls) == 2
    assert not contract._SCHEMA_SUCCESSES


def test_uncacheable_schema_preserves_ignored_extension_behavior(contract):
    class IgnoredExtension:
        def __deepcopy__(self, memo):
            raise AssertionError("ignored schema extension must not be copied")

    contract._SCHEMA_SUCCESSES.clear()
    schema = {"const": True, "unrecognized_extension": IgnoredExtension()}
    assert contract.validate_schema(True, schema) is True
    assert not contract._SCHEMA_SUCCESSES
