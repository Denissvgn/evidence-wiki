"""Private originals retain exact bytes and fail closed on malformed custody."""

import copy
from types import SimpleNamespace

import pytest

from evidence_wiki._review_dependency_custody import _decoded, _encoded, _hint
from evidence_wiki._review_dependency_inputs import assert_current, current_inputs
from evidence_wiki.errors import EvidenceWikiError


@pytest.mark.parametrize("raw", [b"", b"\x00\xff\r\nraw\n", "Exact café\r\n".encode(), b"x" * 100000])
def test_private_original_codec_round_trips_exact_bytes_with_bounded_chunks(raw):
    encoded = _encoded(raw)
    assert _decoded(encoded) == raw
    assert all(0 < len(chunk) <= 65536 and len(chunk) % 4 == 0 for chunk in encoded["raw_base64"])
    assert all(len(chunk) == 65536 for chunk in encoded["raw_base64"][:-1])


@pytest.mark.parametrize("change", [
    lambda row: row.update(bytes=row["bytes"] + 1),
    lambda row: row.update(sha256="sha256:" + "0" * 64),
    lambda row: row.update(raw_base64=["eA==\n"]),
    lambda row: row.update(raw_base64=["eA==="]),
    lambda row: row.update(raw_base64=["eB=="]),
    lambda row: row.update(raw_base64=["eA==", "eA=="]),
    lambda row: row.update(raw_base64="eA=="),
    lambda row: row.update(extra="unadmitted"),
])
def test_private_original_codec_rejects_forgery_noncanonical_and_ambiguous_chunks(change):
    encoded = copy.deepcopy(_encoded(b"x"))
    change(encoded)
    with pytest.raises(EvidenceWikiError):
        _decoded(encoded)


def test_public_configuration_shape_cannot_mint_an_owner_holder():
    assert current_inputs() is None
    with pytest.raises(EvidenceWikiError):
        assert_current(SimpleNamespace(public_profile=lambda: {}, role_semantics=lambda: []))


def test_binding_hint_refuses_a_caller_control_directory_without_active_owner(tmp_path):
    fake = SimpleNamespace(context=SimpleNamespace(mutation_control_directory=tmp_path))
    with pytest.raises(EvidenceWikiError):
        _hint(fake, "sha256:" + "0" * 64, value={"schema_version": "evidence-review-delivery-binding/v1"})
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def original_material(tmp_path, monkeypatch):
    from evidence_wiki import _review_dependency_custody as custody
    from evidence_wiki import research_session
    from tests.test_research_journal_bounds import observer

    execution = observer()
    session = SimpleNamespace(root=tmp_path, run_id="original", binding={"run_id": "original"}, execution=execution)
    record = execution.seal_record(session, "execution_native", "first", {"original": True}, records=[])
    events = [{"event_id": "first", "data": {"request_id": "first", "record": record}}]
    state = tmp_path / "state"
    state.write_text("{}")
    proxy = SimpleNamespace(run_state_path=lambda *_: state, events_path=lambda *_: state,
        _session_state=lambda *_: {"research_session": session.binding}, _bounded_journal=lambda *_: {"events": events})
    monkeypatch.setattr(research_session, "_load", lambda *_: proxy)
    holder = SimpleNamespace(_session=session, valid=True)
    def owner_current(supplied):
        assert supplied is holder
        if not holder.valid:
            custody.refuse("REVIEW_DEPENDENCY_INPUTS_CHANGED")
    monkeypatch.setattr(custody, "assert_current", owner_current)
    envelope = {"authenticated": {"review": "original"}}
    proof = custody._Original(holder, {"claims.json": b"exact\r\n"}, {"nested": ["original"]},
        {"run_id": session.run_id}, execution.records(session), envelope)
    proof._original_binding = copy.deepcopy(session.binding)
    verified = []
    verify = execution._verify
    def authenticated(record):
        verified.append(record["request_id"])
        return verify(record)
    monkeypatch.setattr(execution, "_verify", authenticated)
    return SimpleNamespace(proof=proof, envelope=envelope, events=events, session=session,
        holder=holder, verified=verified)


def test_original_material_reads_one_fresh_authenticated_journal_and_returns_owned_values(original_material):
    case = original_material
    inputs, snapshot = case.proof.original_material(copy.deepcopy(case.envelope))
    assert case.verified == ["first"]
    assert inputs == {"claims.json": b"exact\r\n"} and snapshot == {"nested": ["original"]}
    inputs.clear()
    snapshot["nested"].clear()
    assert case.proof.original_material(case.envelope) == ({"claims.json": b"exact\r\n"}, {"nested": ["original"]})
    assert case.verified == ["first", "first"]
    case.proof.assert_current()
    assert case.verified == ["first", "first", "first"]


@pytest.mark.parametrize("changed", ["authentication", "data", "binding", "observer", "append", "holder", "envelope"])
def test_original_material_never_reuses_prior_authentication_or_changed_owner_state(original_material, changed):
    case = original_material
    case.proof.original_material(case.envelope)
    envelope = copy.deepcopy(case.envelope)
    if changed in {"authentication", "data"}:
        case.events[0]["data"]["record"][changed] = "0" * 64 if changed == "authentication" else {"changed": True}
    elif changed == "binding":
        case.session.binding = {"run_id": "different"}
    elif changed == "observer":
        case.session.execution._observer_key = b"changed-observer"
    elif changed == "append":
        first = case.events[0]["data"]["record"]
        record = case.session.execution.seal_record(case.session, "execution_native", "next", {}, records=[first])
        case.events.append({"event_id": "next", "data": {"request_id": "next", "record": record}})
    elif changed == "holder":
        case.holder.valid = False
    else:
        envelope["authenticated"]["review"] = "different"
    for _ in range(2):
        with pytest.raises(EvidenceWikiError):
            case.proof.original_material(envelope)
