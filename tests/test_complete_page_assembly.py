"""Real packet codecs and native replay retain closing custody boundaries."""

import copy
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_session as native
from evidence_wiki import review_page_operations as pages
from evidence_wiki import review_page_session as assembly
from evidence_wiki._pack_io import canonical
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.operation_control import OperationControl
from evidence_wiki.packet_pages import PacketPages
from evidence_wiki.research_session import ResearchSession
from evidence_wiki.review_delivery import ReviewDelivery
from tests.test_complete_review_delivery import complete_case as _complete_case_fixture

complete_case = _complete_case_fixture


@pytest.fixture
def custody(tmp_path, monkeypatch, complete_case):
    # This fixture owns page collection/receipt ordering, with synthetic execution
    # seams. Genuine dependency input custody is exercised by native sessions.
    monkeypatch.setattr("evidence_wiki._review_dependency_inputs.bind_session", lambda _: nullcontext())
    values, _context, diagnostics, instructions = complete_case
    material = {"schema_version": "research-complete-review-material/v1", "basis_id": values["basis_id"],
        "native_snapshot_sha256": digest(values["owner_snapshot"]),
        "claim_id": values["owner_snapshot"]["claim"]["id"],
        "claims": [claim for instruction in instructions for claim in instruction["claims"]],
        "questions": [{"slug": instruction["question_slug"], "original_id": instruction["question_id"],
            "question": instruction["scope"]["method"], "metadata": None, "body": "Retained original question."}
            for instruction in instructions]}
    state = SimpleNamespace(values=values, material=material, diagnostics=diagnostics,
        instructions=instructions, active=False, snapshots=[], records=[], receipt_hook=None,
        closing_failure=False, terminal=False)
    host = SimpleNamespace(require_grant=lambda _: None, require_path=lambda path: path)

    @contextmanager
    def snapshot(root, claim_id, **kwargs):
        assert not state.active
        state.active = True
        state.snapshots.append(kwargs)
        try:
            prepared = {"snapshot": values["owner_snapshot"], "rubric": values["rubric"],
                "result": {"basis_id": values["basis_id"]}}
            yield prepared, values["sources"], diagnostics, state.instructions, {row["source_id"] for row in values["sources"]}
            if kwargs.get("_closing_time") and state.closing_failure:
                pages.refuse("current_owner_changed")
        finally:
            state.active = False

    def append(_session, kind, request_id, data):
        assert not state.active, "A native journal write entered source custody."
        state.records.append({"kind": kind, "request_id": request_id, "data": copy.deepcopy(data)})
        if kind == "execution_review_page_delivery" and state.receipt_hook is not None:
            state.receipt_hook()

    monkeypatch.setattr(pages, "snapshot", snapshot)
    monkeypatch.setattr(pages, "current_context", lambda: host)
    monkeypatch.setattr(pages, "current_review_material", lambda *_: state.material)
    monkeypatch.setattr(native, "gate", lambda *_: nullcontext())
    monkeypatch.setattr(native, "_begin", lambda _: {"state": {"current": "finished" if state.terminal else "running"}})
    monkeypatch.setattr(native, "owner", lambda _: SimpleNamespace(TERMINAL_STATES={"finished"}))
    monkeypatch.setattr(native, "retained", lambda _: copy.deepcopy(state.records))
    monkeypatch.setattr(native, "require_machine_compatible", lambda _: None)
    monkeypatch.setattr(native, "current", lambda _: {"execution": {"source_bindings": [
        {"source_id": row["source_id"]} for row in values["sources"]]}})
    monkeypatch.setattr(native, "append", append)
    monkeypatch.setattr(native, "_limits", lambda _: {})
    monkeypatch.setattr(native.accounting, "projection", lambda *_args, **_kwargs: {
        "usage_unknown": False, "overrun": False, "remaining": {"tool_calls": 100}})
    monkeypatch.setattr(native, "deadline", lambda _: datetime.now(timezone.utc) + timedelta(seconds=60))
    selected = object.__new__(ResearchSession)
    selected._root, selected._context = tmp_path, host
    selected._binding_raw = canonical({"run": "packet-assembly"})
    selected._check = lambda *_args, **_kwargs: None
    selected._execution = SimpleNamespace(_observer_key=b"x" * 32,
        profile=SimpleNamespace(review_delivery=ReviewDelivery(context_version="complete"),
            currency="USD", pricing_ref="test", cleanup_timeout_seconds=1),
        ledger=lambda *_: {}, native_observation=lambda _session, identifier, **_kwargs: {"attempt_id": identifier})
    state.session = selected
    state.arguments = {"claim_id": values["owner_snapshot"]["claim"]["id"],
        "context_id": "context-" + "a" * 32, "_order_seed": b"a" * 32,
        "source_ids": [row["source_id"] for row in values["sources"]], "context_version": "complete", "page_chars": 256}
    return state


def delivered(state):
    return [row for row in state.records if row["kind"] == "execution_review_page_delivery"]


def assemble(state, request_id="packet"):
    return assembly.assembled(state.session, request_id=request_id,
        claim_id=state.arguments["claim_id"], page_chars=256)


def test_seeded_collector_matches_every_public_packet_byte(custody):
    packet = pages._collect_packet(custody.session.root, **custody.arguments)
    assert len(custody.snapshots) == 1 and custody.snapshots[0]["_closing_time"]
    handle = pages.prepare(custody.session.root, **custody.arguments)
    header = handle.header()
    collected = [handle.page(row["page_id"]) for row in header["pages"]]
    expected = {"schema_version": "research-review-packet/v2", "header": header,
        "pages": collected, "complete": handle.complete(collected)}
    assert canonical(packet) == canonical(expected)
    assert digest(packet) == digest(expected)


def test_complete_assembly_closes_collection_before_receipts_and_replays_without_collection(custody, monkeypatch):
    collections = []
    collect = assembly._collect_packet

    def observed(*args, **kwargs):
        value = collect(*args, **kwargs)
        assert not custody.active
        collections.append(value)
        return value

    monkeypatch.setattr(assembly, "_collect_packet", observed)
    custody.receipt_hook = lambda: collections or pytest.fail("Receipt preceded the closed packet.")
    value = assemble(custody)
    assert len(collections) == 1
    assert len(custody.snapshots) == 2  # collection and final retained-packet check
    assert all(row["_closing_time"] for row in custody.snapshots)
    assert len(delivered(custody)) == len(value["packet"]["pages"])
    for receipt, page in zip(delivered(custody), value["packet"]["pages"], strict=True):
        assert receipt["data"] == {"claim_id": custody.arguments["claim_id"],
            "header_sha256": digest(value["packet"]["header"]), "page_id": page["page_id"],
            "page_sha256": digest(page), "delivery_scope": "host_assembly", "worker_consumption": "not_observed"}
        assert receipt["request_id"] == native.child_id("packet", "page." + page["page_id"])
    records = copy.deepcopy(custody.records)
    snapshots = len(custody.snapshots)
    custody.terminal = True
    assert assemble(custody) == value
    assert len(custody.snapshots) == snapshots + 1
    assert custody.snapshots[-1]["_closing_time"]
    assert len(collections) == 1 and custody.records == records
    assert not value["accepted"] and value["worker_consumption"] == "not_observed"


@pytest.mark.parametrize("changed,reason", [
    ("header", "historical_packet_not_current"),
    ("complete", "historical_decisive_pages_not_current"),
])
def test_retained_packet_recheck_preserves_historical_refusals(custody, monkeypatch, changed, reason):
    assemble(custody)
    records = copy.deepcopy(custody.records)
    actual = native._native_call

    def changed_return(*args, **kwargs):
        value = copy.deepcopy(actual(*args, **kwargs))
        if changed == "header":
            value["packet"]["header"]["context_id"] = "context-" + "b" * 32
        else:
            value["packet"]["complete"]["complete"] = False
        return value

    monkeypatch.setattr(native, "_native_call", changed_return)
    before = len(custody.snapshots)
    with pytest.raises(EvidenceWikiError) as error:
        assemble(custody)
    assert error.value.details["reason"] == reason
    assert len(custody.snapshots) == before + 1
    assert custody.records == records


@pytest.mark.parametrize("failure", ["snapshot", "page", "closing"])
def test_final_packet_recheck_keeps_snapshot_pages_and_closing_custody(custody, monkeypatch, failure):
    assemble(custody)
    records = copy.deepcopy(custody.records)
    checked = []
    actual_check = pages.ProtectedPages._check_snapshot

    def check(handle, *args):
        assert custody.active
        checked.append(handle)
        actual_check(handle, *args)
        if failure == "snapshot":
            pages.refuse("source_input_changed")

    monkeypatch.setattr(pages.ProtectedPages, "_check_snapshot", check)
    if failure == "closing":
        custody.closing_failure = True
    elif failure == "page":
        actual = native._native_call

        def changed_return(*args, **kwargs):
            value = copy.deepcopy(actual(*args, **kwargs))
            value["packet"]["pages"][0]["text"] += " Changed retained bytes."
            return value

        monkeypatch.setattr(native, "_native_call", changed_return)
    before = len(custody.snapshots)
    with pytest.raises(EvidenceWikiError):
        assemble(custody)
    assert len(checked) == 1 and len(custody.snapshots) == before + 1
    assert custody.snapshots[-1]["_closing_time"]
    assert not custody.active and custody.records == records


@pytest.mark.parametrize("failure", ["closing", "bound", "terminal"])
def test_collection_and_terminal_failure_emit_no_page_receipts(custody, failure):
    custody.closing_failure = failure == "closing"
    custody.terminal = failure == "terminal"
    if failure == "bound":
        custody.session.execution.profile.review_delivery = ReviewDelivery(context_version="complete", max_packet_bytes=4096)
    with pytest.raises(EvidenceWikiError):
        assemble(custody)
    assert not delivered(custody)
    assert not any(row["kind"] == "execution_attempt" for row in custody.records)
    assert not any(row["kind"] == "execution_native_observation" and row["data"]["error_code"] is None
        for row in custody.records)


def test_receipt_source_mutation_is_refused_by_unchanged_public_recheck(custody):
    def mutate():
        custody.values["sources"][0]["content"] += " Changed retained conditions."
        custody.receipt_hook = None

    custody.receipt_hook = mutate
    with pytest.raises(EvidenceWikiError):
        assemble(custody)
    assert delivered(custody)
    assert not any(row["kind"] == "execution_attempt" for row in custody.records)


def test_interrupted_receipt_history_remains_unobserved_on_replay(custody):
    def interrupt():
        raise KeyboardInterrupt

    custody.receipt_hook = interrupt
    with pytest.raises(KeyboardInterrupt):
        assemble(custody)
    assert len(delivered(custody)) == 1
    assert not any(row["kind"] == "execution_native_observation" for row in custody.records)
    records = copy.deepcopy(custody.records)
    snapshots = len(custody.snapshots)
    with pytest.raises(EvidenceWikiError) as error:
        assemble(custody)
    assert error.value.error_code == "RESEARCH_NATIVE_EFFECT_UNOBSERVED"
    assert custody.records == records and len(custody.snapshots) == snapshots


@pytest.mark.parametrize("boundary", ["before_packet_collection", "before_required_page", "after_required_page",
    "after_packet_collection", "before_required_page_receipt", "after_required_page_receipt"])
def test_original_control_stops_collection_without_success_or_receipts(custody, monkeypatch, boundary):
    original = OperationControl.checkpoint
    calls = []

    def expire(control, name):
        calls.append(name)
        if name == boundary:
            control._native._end = 0
        original(control, name)

    monkeypatch.setattr(OperationControl, "checkpoint", expire)
    with pytest.raises(EvidenceWikiError) as error:
        assemble(custody)
    assert error.value.error_code == "OPERATION_DEADLINE_EXCEEDED"
    assert boundary in calls
    assert len(delivered(custody)) == (1 if boundary == "after_required_page_receipt" else 0)
    assert not any(row["kind"] == "execution_native_observation" and row["data"]["error_code"] is None
        for row in custody.records)


def test_collector_refuses_effectful_cancellation_control_before_custody(custody):
    effects = []
    control = OperationControl(deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat(),
        timeout_seconds=10, cancel_requested=lambda: effects.append("callback") or False)
    try:
        with pytest.raises(EvidenceWikiError) as error:
            pages._collect_packet(custody.session.root, **custody.arguments, _control=control)
        assert error.value.details["reason"] == "packet_operation_control_invalid"
        assert not custody.snapshots and not effects
    finally:
        control.close()


def test_every_immutable_page_has_original_control_checkpoints(custody, monkeypatch):
    events = []
    original_checkpoint, original_page = OperationControl.checkpoint, PacketPages.page

    def checkpoint(control, name):
        events.append(name)
        original_checkpoint(control, name)

    def page(handle, identifier):
        events.append(("page", identifier))
        return original_page(handle, identifier)

    monkeypatch.setattr(OperationControl, "checkpoint", checkpoint)
    monkeypatch.setattr(PacketPages, "page", page)
    control = OperationControl(deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat(), timeout_seconds=10)
    try:
        packet = pages._collect_packet(custody.session.root, **custody.arguments, _control=control)
    finally:
        control.close()
    # Completeness re-reads retained immutable pages after the first checked collection.
    start = 0
    for row in packet["pages"]:
        start = events.index("before_required_page", start)
        assert events[start:start + 3] == ["before_required_page", ("page", row["page_id"]), "after_required_page"]
        start += 3
    assert events[0] == "before_packet_collection" and events[-1] == "after_packet_collection"


def test_terminal_transition_during_receipts_retains_only_partial_history(custody):
    def finish():
        custody.terminal = True

    custody.receipt_hook = finish
    with pytest.raises(EvidenceWikiError) as error:
        assemble(custody)
    assert error.value.details["reason"] == "packet_run_terminal"
    assert len(delivered(custody)) == 1
    assert not any(row["kind"] == "execution_native_observation" and row["data"]["error_code"] is None
        for row in custody.records)


def test_legacy_assembly_preserves_individual_owner_reads_and_receipt_order(custody, monkeypatch):
    subject = custody.values["owner_snapshot"]["claim"]
    subject.update(qualification="supported", premises=[], derivation=None)
    custody.values["owner_snapshot"]["premises"] = []
    custody.diagnostics["claims"][:] = custody.diagnostics["claims"][:1]
    custody.diagnostics["claims"][0].update(qualification="supported", premises=[])
    custody.instructions = custody.instructions[0]
    custody.instructions.pop("basis_id")
    custody.instructions["basis_id"] = digest(custody.instructions)
    custody.session.execution.profile.review_delivery = ReviewDelivery()
    monkeypatch.setattr(assembly, "_collect_packet", lambda *_args, **_kwargs: pytest.fail("Legacy assembly used bulk collection."))
    reads_at_receipt = []
    custody.receipt_hook = lambda: reads_at_receipt.append(len(custody.snapshots))
    value = assemble(custody)
    assert value["schema_version"] == "research-host-assembled-review/v1"
    assert value["packet"]["schema_version"] == "research-review-packet/v1"
    assert len(custody.snapshots) == len(value["packet"]["pages"]) + 6
    assert reads_at_receipt == list(range(3, 3 + len(value["packet"]["pages"])))
    assert not any(row.get("_closing_time") for row in custody.snapshots)
    assert "context_version" not in custody.records[0]["data"]["arguments"]
