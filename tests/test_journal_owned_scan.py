"""Owned scan bytes preserve independent canonical and authentication checks."""
from __future__ import annotations

import copy
import hashlib
import hmac
from types import SimpleNamespace

import pytest

from evidence_wiki import research_journal_contracts as codec
from evidence_wiki import research_session
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.mutation_contracts import _serialized
from tests.test_research_journal_bounds import observer
from tests.test_review_native_journal_wire import _owned_data


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("context_version", [None, "legacy", "complete", "dependencies"])
def test_scan_rechecks_owned_records_and_authentication(tmp_path, monkeypatch, large, context_version):
    execution = observer()
    execution._profile = SimpleNamespace(review_delivery=None if context_version is None
        else SimpleNamespace(context_version=context_version))
    session = SimpleNamespace(root=tmp_path, run_id="scan", binding={"run_id": "scan"})
    original = _owned_data() if large else {"nested": ["original"]}
    if large:
        original["padding"].append("x" * 60000)
    first = execution.seal_record(session, "execution_native", "one", original, records=[])
    second = execution.seal_record(session, "execution_native", "two", {}, records=[first])
    first = codec.storage_record(first)
    events = [{"event_id": str(i), "data": {"request_id": row["request_id"], "record": row}}
              for i, row in enumerate((first, second))]
    state = tmp_path / "state"
    state.write_text("{}")
    proxy = SimpleNamespace(run_state_path=lambda *_: state, events_path=lambda *_: state,
        _session_state=lambda *_: {"research_session": session.binding},
        _bounded_journal=lambda *_: {"events": events})
    monkeypatch.setattr(research_session, "_load", lambda *_: proxy)
    original_hmac = hmac.new
    hmac_calls = []
    def fresh_hmac(*args, **kwargs):
        hmac_calls.append(args[0])
        return original_hmac(*args, **kwargs)
    monkeypatch.setattr(hmac, "new", fresh_hmac)
    original_verify = execution._verify
    calls = []
    def verify(record):
        calls.append(record["request_id"])
        return original_verify(record)
    monkeypatch.setattr(execution, "_verify", verify)
    first_read = execution.records(session)
    assert calls == ["one", "two"]
    first_read[0]["data"]["padding" if large else "nested"][0] = "caller mutation"
    assert execution.records(session)[0]["data"] == original
    assert calls == ["one", "two", "one", "two"]
    assert hmac_calls == [execution._observer_key] * 4
    for index, field, value in ((1, "previous_record_ref", "sha256:" + "0" * 64),
            (0, "authentication", "0" * 64), (0, "data", {"changed": True})):
        saved = copy.deepcopy(events)
        events[index]["data"]["record"][field] = value
        for _ in range(2):
            with pytest.raises(EvidenceWikiError):
                execution.records(session)
        events[:] = saved
    execution._observer_key = b"different-observer"
    with pytest.raises(EvidenceWikiError):
        execution.records(session)


@pytest.mark.parametrize("size", [262143, 262144, 262145])
@pytest.mark.parametrize("authentication", [None, "a" * 64])
def test_each_header_threshold_and_owned_logical_independence(size, authentication):
    record = {"schema_version": codec.JOURNAL, "kind": "execution_native", "binding_id": "binding",
        "request_id": "record", "sequence": 0, "previous_record_ref": None,
        "data": _owned_data(), "authentication": authentication}
    record["data"]["padding"][-1] = "x" * (size - len(_serialized(record)))
    assert len(_serialized(record)) == size
    raw, logical = codec._canonical_owned_record(record)
    assert raw == codec.canonical_record(record)
    assert logical == codec.detached_record(record)
    assert len(raw) <= 262144
    logical["data"]["padding"][0] = "caller mutation"
    assert codec._canonical_owned_record(record)[0] == raw
    for header in ({"authentication": None}, {"previous_record_ref": "sha256:" + "f" * 64}):
        variant = {**record, **header}
        assert codec._canonical_owned_record(variant) == (codec.canonical_record(variant), codec.detached_record(variant))


@pytest.fixture
def codec_memo(monkeypatch):
    from collections import OrderedDict

    memo = codec._RecordCodecMemo
    monkeypatch.setattr(memo, "entries", OrderedDict())
    monkeypatch.setattr(memo, "generation", None)
    monkeypatch.setattr(memo, "retained_bytes", 0)
    monkeypatch.setattr(memo, "MIN_BYTES", 0)
    return memo


def memo_record(data=None):
    return {"schema_version": codec.JOURNAL, "kind": "execution_native", "binding_id": "binding",
        "request_id": "record", "sequence": 0, "previous_record_ref": None,
        "data": _owned_data() if data is None else data, "authentication": None}


@pytest.mark.parametrize("context_version", [None, "legacy", "complete", "dependencies"])
def test_signing_uses_full_codec_for_every_delivery_profile(monkeypatch, context_version):
    execution = observer()
    execution._profile = SimpleNamespace(review_delivery=None if context_version is None
        else SimpleNamespace(context_version=context_version))
    record = memo_record()
    expected = codec.canonical_record(record)
    expected_signature = hmac.new(execution._observer_key, expected, "sha256").hexdigest()

    def no_selective_key(_value):
        raise AssertionError("Signing must retain the full codec path")

    monkeypatch.setattr(codec, "_record_codec_key", no_selective_key)
    signed = execution.sign(record)
    assert signed["authentication"] == expected_signature
    execution._verify(signed)
    signed["data"]["result"]["claim_id"] = "changed"
    with pytest.raises(EvidenceWikiError):
        execution._verify(signed)


def test_uncached_codec_preserves_transport_bounds_and_owned_values(monkeypatch):
    record = memo_record()
    expected = codec._canonical_owned_record(record)
    monkeypatch.setattr(codec, "_record_codec_key", lambda _: pytest.fail("Unexpected memo lookup"))
    actual = codec._canonical_owned_record(record, use_memo=False)
    assert actual == expected
    actual[1]["data"]["padding"].clear()
    assert codec._canonical_owned_record(record, use_memo=False) == expected
    with pytest.raises(EvidenceWikiError):
        codec._canonical_owned_record(record, expanded_limit=1, use_memo=False)
    malformed = {**record, "unowned": True}
    with pytest.raises(EvidenceWikiError):
        codec._canonical_owned_record(malformed, use_memo=False)


@pytest.mark.parametrize("large", [False, True])
def test_record_digest_preserves_canonical_bytes_without_schema_key_work(monkeypatch, large):
    record = memo_record() if large else memo_record({"value": "original"})
    expected = "sha256:" + hashlib.sha256(codec.canonical_record(record)).hexdigest()
    monkeypatch.setattr(codec, "_record_codec_key", lambda _: pytest.fail("Digest unexpectedly built a memo key"))
    assert codec.digest_record(record) == expected
    with pytest.raises(EvidenceWikiError):
        codec.digest_record({**record, "foreign": True})


def test_pure_record_codec_reuses_only_success_and_returns_fresh_values(codec_memo, monkeypatch):
    original = codec.detached_record
    calls = []

    def observed(value):
        calls.append(None)
        return original(value)

    monkeypatch.setattr(codec, "detached_record", observed)
    record = memo_record()
    expected, first = codec._canonical_owned_record(record)
    first["data"]["result"]["claim_id"] = "caller mutation"
    for _ in range(2):
        encoded, logical = codec._canonical_owned_record(copy.deepcopy(record))
        assert encoded == expected and logical == record
        logical["data"]["padding"].clear()
    assert calls == [None]
    assert codec.canonical_record(record) == expected
    assert calls == [None]


@pytest.mark.parametrize("change", ["claim", "type", "field", "wire"])
def test_record_memo_cannot_accept_changed_or_malformed_bytes(codec_memo, change):
    from evidence_wiki._review_dependency_wire import pack
    from evidence_wiki.mutation_contracts import MutationError

    record = memo_record()
    if change == "wire":
        record["data"] = pack(record["data"], "journal_data")
    original = codec.canonical_record(record)
    if change == "claim":
        record["data"]["result"]["claim_id"] = "different-claim"
        assert codec.canonical_record(record) != original
        return
    if change == "type":
        record["data"]["result"]["claim_id"] = True
    elif change == "field":
        record["extra"] = "unowned"
    else:
        record["data"]["encoded_chunks"][0] = "!!!!"
    for _ in range(2):
        with pytest.raises(MutationError):
            codec.canonical_record(record)
    assert len(codec_memo.entries) == 1


@pytest.mark.parametrize("change", ["subclass", "cycle", "alias"])
def test_record_memo_preserves_exact_types_cycles_and_alias_semantics(codec_memo, monkeypatch, change):
    from evidence_wiki.mutation_contracts import MutationError

    record = memo_record({"left": {"flag": True}, "right": {"flag": True}})
    expected = codec.canonical_record(record)
    if change == "subclass":
        class Mapping(dict):
            pass
        with pytest.raises(MutationError):
            codec.canonical_record(Mapping(record))
    elif change == "cycle":
        record["data"]["left"] = record
        with pytest.raises(MutationError):
            codec.canonical_record(record)
    else:
        record["data"]["right"] = record["data"]["left"]
        assert codec._record_codec_key(record) is None
        calls = []
        original = codec.detached_record

        def observed(value):
            calls.append(None)
            return original(value)

        monkeypatch.setattr(codec, "detached_record", observed)
        assert codec.canonical_record(record) == expected
        assert calls == [None]


@pytest.mark.parametrize("change", ["schema", "validator", "bound"])
def test_record_memo_tracks_live_native_schema_validator_and_bounds(codec_memo, monkeypatch, change):
    from evidence_wiki.mutation_contracts import MutationError
    from evidence_wiki.pack_discovery import owner

    record = memo_record()
    codec.canonical_record(record)
    native = owner("_review_dependency_contract")
    if change == "schema":
        properties = native._shapes().DOCUMENTS["evidence-strict-authorship/v2"]["properties"]
        monkeypatch.setitem(properties, "claim_id", {"const": "different claim"})
    elif change == "validator":
        def refuse(*args, **kwargs):
            raise ValueError("replacement validator")
        monkeypatch.setattr(native, "_shape", refuse)
    else:
        monkeypatch.setattr(native, "MAX_NODES", 1)
    for _ in range(2):
        with pytest.raises(MutationError):
            codec.canonical_record(record)


def test_record_memo_tracks_mutable_family_schema_contents(codec_memo, monkeypatch):
    from evidence_wiki import _review_dependency_json as native_json

    record = memo_record()
    codec.canonical_record(record)
    generation = codec_memo.generation
    schema = native_json._family_schema("research-blinded-review-input/v3")
    monkeypatch.setitem(schema, "description", "new schema generation")
    codec.canonical_record(record)
    assert codec_memo.generation != generation
    assert len(codec_memo.entries) == 1


def test_record_memo_does_not_store_a_result_after_midcall_generation_change(codec_memo, monkeypatch):
    original = codec.detached_record

    def changing(value):
        result = original(value)
        monkeypatch.setattr(codec, "JOURNAL", "changed generation")
        return result

    monkeypatch.setattr(codec, "detached_record", changing)
    codec.canonical_record(memo_record())
    assert not codec_memo.entries


def test_record_memo_bounds_entry_count_and_retained_frame_bytes(codec_memo, monkeypatch):
    monkeypatch.setattr(codec_memo, "ENTRY_LIMIT", 2)
    for index in range(4):
        record = memo_record({"value": index})
        codec.canonical_record(record)
        assert len(codec_memo.entries) <= 2
        assert codec_memo.retained_bytes <= codec_memo.BYTE_LIMIT
    assert len(codec_memo.entries) == 2
    sample = memo_record({"value": 0, "padding": "x" * 60000})
    generation_charge = codec._record_codec_key(sample)[1]
    entry_size = len(_serialized(sample)) * 3 + codec_memo.ENTRY_OVERHEAD
    monkeypatch.setattr(codec_memo, "BYTE_LIMIT", generation_charge + entry_size + 10000)
    for index in range(3):
        codec.canonical_record(memo_record({"value": index, "padding": "x" * 60000}))
        assert len(codec_memo.entries) <= 1
        assert codec_memo.retained_bytes <= codec_memo.BYTE_LIMIT
    assert len(codec_memo.entries) == 1
    monkeypatch.setattr(codec_memo, "GENERATION_BYTE_LIMIT", 1)
    codec.canonical_record(memo_record())
    assert not codec_memo.entries and codec_memo.retained_bytes == 0


def test_successful_codec_reuse_never_reuses_journal_authority(codec_memo, tmp_path, monkeypatch):
    execution = observer()
    session = SimpleNamespace(root=tmp_path, run_id="scan", binding={"run_id": "scan"})
    first = execution.seal_record(session, "execution_native", "one", {"value": "first"}, records=[])
    events = [{"event_id": "event-one", "data": {"request_id": "one", "record": first}}]
    state = tmp_path / "state"
    state.write_text("{}")
    binding = {"research_session": session.binding}
    reads = []

    def fresh_events(*args):
        reads.append(None)
        return {"events": events}

    proxy = SimpleNamespace(run_state_path=lambda *_: state, events_path=lambda *_: state,
        _session_state=lambda *_: binding, _bounded_journal=fresh_events)
    monkeypatch.setattr(research_session, "_load", lambda *_: proxy)
    verify = execution._verify
    checked = []

    def fresh_signature(record):
        checked.append(record["request_id"])
        return verify(record)

    monkeypatch.setattr(execution, "_verify", fresh_signature)
    assert len(execution.records(session)) == 1
    assert len(execution.records(session)) == 1
    assert reads == [None, None] and checked == ["one", "one"]
    second = execution.seal_record(session, "execution_native", "two", {}, records=[first])
    events.append({"event_id": "event-two", "data": {"request_id": "two", "record": second}})
    assert len(execution.records(session)) == 2
    assert checked[-2:] == ["one", "two"]
    binding["research_session"] = {"changed": "binding"}
    with pytest.raises(EvidenceWikiError):
        execution.records(session)
    binding["research_session"] = session.binding
    execution._observer_key = b"replaced-observer"
    with pytest.raises(EvidenceWikiError):
        execution.records(session)


@pytest.fixture
def schema_codec(codec_memo, monkeypatch):
    from evidence_wiki.pack_discovery import owner

    monkeypatch.setattr(codec_memo, "schema_backend", None)
    return owner("_review_dependency_contract")


def assert_native_schema_preimage(native):
    backend = codec._codec_schema_backend(native)
    actual = native._schema_memo_key(b"", {})
    assert codec._codec_native_schema_key(native, backend) == actual
    key = codec._record_codec_key(memo_record({"padding": "x" * 5000}))
    assert key is not None and key[0][0] == actual[2:]
    return key[0]


def test_schema_snapshots_preserve_all_native_and_family_documents(schema_codec):
    from evidence_wiki import _review_dependency_json as native_json

    native = schema_codec
    assert codec._codec_schema_backend(native) is not None
    documents = list(native.schema_documents().items())
    documents += [(name, native_json._family_schema(name)) for name in sorted(native_json._FAMILY)]
    for name, value in documents:
        assert codec._codec_schema_bytes(value, native) == native._exact_json_bytes(value)[0], name
    actual = native._schema_memo_key(b"", {})
    assert len(actual) == 17
    assert_native_schema_preimage(native)


@pytest.mark.parametrize("name", ["_shape", "_matches", "_equal", "_bounded", "_node",
    "_require", "canonical_bytes", "_shape_documents", "_native_node_choices",
    "MAX_BYTES", "MAX_NODES", "MAX_DEPTH", "_NATIVE_NODE_FINGERPRINT"])
def test_schema_generation_keeps_each_native_helper_bound_and_fingerprint(schema_codec, monkeypatch, name):
    native = schema_codec
    before = assert_native_schema_preimage(native)
    original = getattr(native, name)
    if callable(original):
        def delegated(*args, **kwargs):
            return original(*args, **kwargs)
        monkeypatch.setattr(native, name, delegated)
    else:
        monkeypatch.setattr(native, name, original - 1 if type(original) is int else "changed-node-generation")
    assert assert_native_schema_preimage(native) != before


def test_schema_generation_keeps_shape_module_and_mutable_schema_contents(schema_codec, monkeypatch):
    from types import ModuleType

    from evidence_wiki import _review_dependency_json as native_json

    native = schema_codec
    before = assert_native_schema_preimage(native)
    replacement = ModuleType("replacement_shapes")
    replacement.DOCUMENTS = copy.deepcopy(native._shapes().DOCUMENTS)
    monkeypatch.setattr(native, "_shapes", lambda: replacement)
    changed_module = assert_native_schema_preimage(native)
    assert changed_module != before
    replacement.DOCUMENTS["evidence-strict-authorship/v2"]["properties"]["claim_id"]["description"] = "changed"
    changed_document = assert_native_schema_preimage(native)
    assert changed_document != changed_module
    family = native_json._family_schema("research-blinded-review-input/v3")
    monkeypatch.setitem(family, "description", "changed-family")
    assert assert_native_schema_preimage(native) != changed_document


def test_unknown_native_schema_key_extensions_keep_full_fallback_preimage(schema_codec, monkeypatch):
    native = schema_codec
    original = native._schema_memo_key
    marker = ["first"]

    def expanded(raw, schema):
        return original(raw, schema) + (marker[0],)

    monkeypatch.setattr(native, "_schema_memo_key", expanded)
    assert codec._codec_schema_backend(native) is None
    first = assert_native_schema_preimage(native)
    marker[0] = "second"
    second = assert_native_schema_preimage(native)
    assert second != first and second[0][-1] == "second"


@pytest.mark.parametrize("kind", ["string-subclass", "tuple-subclass", "syntax", "permission", "oversized"])
def test_schema_backend_falls_back_for_nonexact_defaults_or_unreadable_source(schema_codec, codec_memo, monkeypatch, kind):
    from io import BytesIO
    from pathlib import Path

    native = schema_codec
    assert codec._codec_schema_backend(native) is not None
    if kind in {"string-subclass", "tuple-subclass"}:
        class Text(str):
            pass

        class Defaults(tuple):
            pass

        defaults = (Text("review_dependency_contract_invalid"),) if kind == "string-subclass" else Defaults(native._require.__defaults__)
        monkeypatch.setattr(native._require, "__defaults__", defaults)
    else:
        monkeypatch.setattr(codec_memo, "schema_backend", None)
        actual_open = Path.open

        def supplied(path, *args, **kwargs):
            if str(path) == native.__file__:
                if kind == "permission":
                    raise PermissionError("inert source-read stub")
                return BytesIO(b"def invalid(" if kind == "syntax" else b"x" * 129)
            return actual_open(path, *args, **kwargs)

        if kind == "oversized":
            monkeypatch.setattr(codec_memo, "GENERATION_BYTE_LIMIT", 128)
        monkeypatch.setattr(Path, "open", supplied)
    assert codec._codec_schema_backend(native) is None
    assert codec._codec_native_schema_key(native, None) == native._schema_memo_key(b"", {})


@pytest.mark.parametrize("kind", ["mapping", "list", "integer", "boolean-integer", "nonfinite", "unicode", "alias", "cycle", "alias-expansion"])
def test_schema_snapshot_preserves_exact_types_aliases_cycles_and_expanded_bounds(schema_codec, monkeypatch, kind):
    native = schema_codec

    class Mapping(dict):
        pass

    class Array(list):
        pass

    class Integer(int):
        pass

    shared = {"flag": True}
    values = {"mapping": Mapping(flag=True), "list": Array([1]), "integer": Integer(1),
        "boolean-integer": [False, 0, True, 1, -0.0], "nonfinite": float("nan"),
        "unicode": {"café": "東京"}, "alias": [shared, shared], "cycle": []}
    values["cycle"].append(values["cycle"])
    if kind == "alias-expansion":
        value = {"leaf": "value"}
        for _ in range(10):
            value = [value, value]
        monkeypatch.setattr(native, "MAX_BYTES", 128)
    else:
        value = values[kind]
    try:
        expected = native._exact_json_bytes(value)[0]
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        with pytest.raises((ValueError, TypeError, UnicodeError, RecursionError, OverflowError)):
            codec._codec_schema_bytes(value, native)
    else:
        assert codec._codec_schema_bytes(value, native) == expected


def test_record_generation_snapshots_inplace_validator_code(schema_codec, monkeypatch):
    native = schema_codec
    before = assert_native_schema_preimage(native)

    def changed(*args, **kwargs):
        raise ValueError("changed shape code")

    monkeypatch.setattr(native._shape, "__code__", changed.__code__)
    assert assert_native_schema_preimage(native) != before
    # This observes the outer record generation only. Native child-validation
    # success memos retain their own separately defined invalidation contract.


def test_schema_fast_path_keeps_both_record_key_checks_and_unchanged_limits(schema_codec, codec_memo, monkeypatch):
    record = memo_record({"padding": "x" * 5000})
    key = codec._record_codec_key
    calls = []

    def counted(value):
        calls.append(None)
        return key(value)

    monkeypatch.setattr(codec, "_record_codec_key", counted)
    first, logical = codec._canonical_owned_record(record)
    assert len(calls) == 2
    logical["data"]["padding"] = "caller mutation"
    second, current = codec._canonical_owned_record(record)
    assert len(calls) == 4 and first == second and current == record
    assert codec_memo.ENTRY_LIMIT == 64 and codec_memo.BYTE_LIMIT == 16_777_216
    assert codec_memo.GENERATION_BYTE_LIMIT == 2_097_152
    assert codec_memo.retained_bytes <= codec_memo.BYTE_LIMIT
