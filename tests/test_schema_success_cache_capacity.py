"""Bounded schema success memo keeps validation generations and returned values isolated."""
from copy import deepcopy

import pytest

from evidence_wiki.pack_discovery import owner


@pytest.fixture
def contract(monkeypatch):
    module = owner('_review_dependency_contract')
    monkeypatch.setattr(module, '_SCHEMA_CACHE_MIN_BYTES', 0)
    module._SCHEMA_SUCCESSES.clear()
    yield module
    module._SCHEMA_SUCCESSES.clear()

def payload_bytes(contract):
    return sum(len(part) for key in contract._SCHEMA_SUCCESSES for part in key[:3])

def test_count_cap_and_reuse_above_eight(contract, monkeypatch):
    calls = []
    original = contract._shape
    def observed(*args, **kwargs):
        calls.append(None)
        return original(*args, **kwargs)
    monkeypatch.setattr(contract, '_shape', observed)
    schema = {'type': 'integer'}
    for value in range(64):
        assert contract.validate_schema(value, schema) == value
    before = len(calls)
    for value in range(64):
        assert contract.validate_schema(value, schema) == value
    assert len(calls) == before
    contract.validate_schema(64, schema)
    assert len(contract._SCHEMA_SUCCESSES) == 64
    assert all(key[0] != b'0\n' for key in contract._SCHEMA_SUCCESSES)
    assert payload_bytes(contract) <= contract._SCHEMA_CACHE_KEY_BYTES

def test_byte_cap_evicts_lru_and_replacement_does_not_double_count(contract, monkeypatch):
    schema = {'type': 'integer'}
    raw, _ = contract._exact_json_bytes(0)
    key = contract._schema_memo_key(raw, schema)
    cost = sum(len(part) for part in key[:3]) + contract._SCHEMA_CACHE_ENTRY_CHARGE
    monkeypatch.setattr(contract, '_SCHEMA_CACHE_KEY_BYTES', 2 * cost)
    for value in (0, 1, 0, 2):
        contract.validate_schema(value, schema)
    assert [key[0] for key in contract._SCHEMA_SUCCESSES] == [b'0\n', b'2\n']
    assert payload_bytes(contract) + 2 * contract._SCHEMA_CACHE_ENTRY_CHARGE == 2 * cost
    monkeypatch.setattr(contract, '_SCHEMA_CACHE_KEY_BYTES', cost - 1)
    assert contract.validate_schema(3, schema) == 3
    assert not contract._SCHEMA_SUCCESSES

@pytest.mark.parametrize('warm', [False, True])
def test_return_and_caller_nested_mutation_are_isolated(contract, warm):
    schema = {'type': 'array', 'items': {'type': 'array', 'items': {'type': 'integer'}}}
    value = [[1]]
    if warm:
        contract.validate_schema(value, schema)
    returned = contract.validate_schema(value, schema)
    returned[0].append(2)
    assert value == [[1]]
    value[0].append(3)
    assert returned == [[1, 2]]
    assert contract.validate_schema([[1]], deepcopy(schema)) == [[1]]

def test_miss_return_remains_isolated_from_replaced_helper_capture(contract, monkeypatch):
    captured = []
    original = contract._shape
    def observed(value, schema, root, depth=0):
        if depth == 0:
            captured.append(value)
        return original(value, schema, root, depth)
    monkeypatch.setattr(contract, '_shape', observed)
    returned = contract.validate_schema([[1]], {'type': 'array'})
    captured[0][0].append(2)
    assert returned == [[1]]

def test_schema_variants_share_epoch_but_documents_switch_and_restore_clear(contract, monkeypatch):
    from types import ModuleType
    native = ModuleType('public_epoch_shapes')
    native.DOCUMENTS = {'public': {'type': 'integer'}}
    monkeypatch.setattr(contract, '_shapes', lambda: native)
    for number in range(12):
        contract.validate_schema(number, {'type': 'integer', 'title': str(number)})
    assert len(contract._SCHEMA_SUCCESSES) == 12
    prior = tuple(contract._SCHEMA_SUCCESSES)
    native.DOCUMENTS['public']['title'] = 'changed'
    contract.validate_schema(99, {'type': 'integer'})
    assert len(contract._SCHEMA_SUCCESSES) == 1
    changed = tuple(contract._SCHEMA_SUCCESSES)
    del native.DOCUMENTS['public']['title']
    contract.validate_schema(0, {'type': 'integer', 'title': '0'})
    assert len(contract._SCHEMA_SUCCESSES) == 1
    assert tuple(contract._SCHEMA_SUCCESSES)[0] == prior[0]
    assert tuple(contract._SCHEMA_SUCCESSES)[0][2:] != changed[0][2:]

@pytest.mark.parametrize('helper', ['_shape', '_matches', '_equal', '_bounded', '_node', '_require', '_shape_documents', '_native_node_choices'])
def test_helper_epoch_change_cannot_reuse_prior_success(contract, monkeypatch, helper):
    contract.validate_schema(1, {'type': 'integer'})
    old = next(iter(contract._SCHEMA_SUCCESSES))
    original = getattr(contract, helper)
    def forwarding(*args, **kwargs):
        return original(*args, **kwargs)
    monkeypatch.setattr(contract, helper, forwarding)
    contract.validate_schema(2, {'type': 'integer'})
    assert len(contract._SCHEMA_SUCCESSES) == 1
    assert next(iter(contract._SCHEMA_SUCCESSES))[2:] != old[2:]
    with pytest.raises(ValueError):
        contract.validate_schema(True, {'type': 'integer'})
    assert len(contract._SCHEMA_SUCCESSES) == 1

def test_late_old_epoch_insertion_clears_new_epoch_without_false_hit(contract, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, local
    state = local()
    entered, release = Event(), Event()
    original_key, original_shape = contract._schema_memo_key, contract._shape
    def key(raw, schema):
        current = original_key(raw, schema)
        # Public synthetic documents; each thread holds a stable epoch across
        # its before/after check. This models out-of-order validated completion.
        documents = b'{"public_epoch":1}' if state.epoch == 1 else b'{"public_epoch":2}'
        return current[:2] + (documents,) + current[3:]
    def shape(value, schema, root, depth=0):
        if depth == 0 and state.epoch == 1:
            entered.set()
            assert release.wait(5)
        return original_shape(value, schema, root, depth)
    monkeypatch.setattr(contract, '_schema_memo_key', key)
    monkeypatch.setattr(contract, '_shape', shape)
    def validate(epoch):
        state.epoch = epoch
        return contract.validate_schema(epoch, {'type': 'integer'})
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(validate, 1)
        try:
            assert entered.wait(5)
            assert validate(2) == 2
            assert next(iter(contract._SCHEMA_SUCCESSES))[2] == b'{"public_epoch":2}'
        finally:
            release.set()
        assert first.result(timeout=5) == 1
    assert len(contract._SCHEMA_SUCCESSES) == 1
    assert next(iter(contract._SCHEMA_SUCCESSES))[2] == b'{"public_epoch":1}'
    assert validate(2) == 2
    assert len(contract._SCHEMA_SUCCESSES) == 1
    assert next(iter(contract._SCHEMA_SUCCESSES))[2] == b'{"public_epoch":2}'

def test_canonical_bytes_identity_switch_and_restore_invalidates(contract, monkeypatch):
    schema, value = {'type': 'integer'}, 1
    contract.validate_schema(value, schema)
    original_key = next(iter(contract._SCHEMA_SUCCESSES))
    original = contract.canonical_bytes
    def replacement(*args, **kwargs):
        return original(*args, **kwargs)
    monkeypatch.setattr(contract, 'canonical_bytes', replacement)
    assert contract.validate_schema(value, schema) == value
    assert len(contract._SCHEMA_SUCCESSES) == 1
    assert next(iter(contract._SCHEMA_SUCCESSES))[2:] != original_key[2:]
    monkeypatch.setattr(contract, 'canonical_bytes', original)
    assert contract.validate_schema(value, schema) == value
    assert tuple(contract._SCHEMA_SUCCESSES) == (original_key,)

@pytest.mark.parametrize('bound', ['MAX_BYTES', 'MAX_NODES', 'MAX_DEPTH'])
def test_bound_generation_switch_and_restore_invalidates_same_input(contract, monkeypatch, bound):
    schema, value = {'type': 'array'}, [1, 2]
    original = getattr(contract, bound)
    contract.validate_schema(value, schema)
    old_key = next(iter(contract._SCHEMA_SUCCESSES))
    monkeypatch.setattr(contract, bound, original + 1)
    assert contract.validate_schema(value, schema) == value
    assert len(contract._SCHEMA_SUCCESSES) == 1
    assert next(iter(contract._SCHEMA_SUCCESSES))[2:] != old_key[2:]
    monkeypatch.setattr(contract, bound, original)
    assert contract.validate_schema(value, schema) == value
    assert tuple(contract._SCHEMA_SUCCESSES) == (old_key,)
    # Failed preprocessing need not clear old entries, but must never hit them.
    monkeypatch.setattr(contract, bound, 1)
    with pytest.raises(ValueError, match='bound'):
        contract.validate_schema(value, schema)

@pytest.mark.parametrize('replace', [False, True])
def test_external_clear_or_ordered_dict_replacement_has_no_stale_epoch_state(contract, monkeypatch, replace):
    from collections import OrderedDict
    contract.validate_schema(1, {'type': 'integer'})
    old_key = next(iter(contract._SCHEMA_SUCCESSES))
    contract._SCHEMA_SUCCESSES.clear()
    if replace:
        monkeypatch.setattr(contract, '_SCHEMA_SUCCESSES', OrderedDict())
    assert contract.validate_schema(1, {'type': 'integer'}) == 1
    assert tuple(contract._SCHEMA_SUCCESSES) == (old_key,)
    contract.validate_schema(2, {'type': 'integer'})
    assert len(contract._SCHEMA_SUCCESSES) == 2
    assert (payload_bytes(contract)
            + len(contract._SCHEMA_SUCCESSES) * contract._SCHEMA_CACHE_ENTRY_CHARGE
            <= contract._SCHEMA_CACHE_KEY_BYTES)

def test_same_json_alias_transition_uses_ordinary_validation_and_retains_alias_copy(contract, monkeypatch):
    calls = []
    original = contract._shape
    def observed(*args, **kwargs):
        calls.append(None)
        return original(*args, **kwargs)
    monkeypatch.setattr(contract, '_shape', observed)
    schema = {'type': 'array', 'items': {'type': 'array', 'items': {'type': 'integer'}}}
    contract.validate_schema([[1], [1]], schema)
    previous_calls = len(calls)
    previous_keys = tuple(contract._SCHEMA_SUCCESSES)
    shared = [1]
    aliased = [shared, shared]
    returned = contract.validate_schema(aliased, schema)
    assert len(calls) > previous_calls
    assert tuple(contract._SCHEMA_SUCCESSES) == previous_keys
    assert returned[0] is returned[1]
    assert returned[0] is not shared
    returned[0].append(2)
    assert aliased == [[1], [1]]
    assert contract.validate_schema([[1], [1]], schema) == [[1], [1]]

@pytest.mark.parametrize('mutation', ['cycle', 'bool', 'subclass'])
def test_same_input_changed_to_cycle_or_coercible_type_cannot_reuse_success(contract, mutation):
    value = {'n': 1, 'children': []}
    schema = {'type': 'object', 'properties': {'n': {'type': 'integer'}, 'children': {'type': 'array'}}, 'required': ['n', 'children']}
    contract.validate_schema(value, schema)
    old_keys = tuple(contract._SCHEMA_SUCCESSES)
    if mutation == 'cycle':
        value['children'].append(value)
    elif mutation == 'bool':
        value['n'] = True
    else:
        class IntegerSubclass(int):
            pass
        value['n'] = IntegerSubclass(1)
    with pytest.raises(ValueError):
        contract.validate_schema(value, schema)
    assert tuple(contract._SCHEMA_SUCCESSES) == old_keys
