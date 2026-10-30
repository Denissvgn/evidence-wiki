"""Immutable pending leases, automatic unknown fallbacks and no invented effect."""

from __future__ import annotations

import copy
import json

import pytest

from evidence_wiki import pending_work_contracts as pending
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.workflow_contracts import WorkflowContractError
from tests.test_workflow_contracts import DEADLINE, ISSUED, NOW, job

BINDING = "sha256:" + "1" * 64


def receipt(*, observed=NOW, run_deadline=DEADLINE, basis=None):
    return pending.create(basis or job(), run_id="run", binding_id=BINDING, request_id="request",
                          run_deadline_utc=run_deadline, observed_utc=observed)


def test_job_hash_exact_original_and_smaller_call_timeout_are_preserved():
    basis = job()
    value = receipt(basis=basis)
    assert basis["timeout_seconds"] == 30 and value["job_sha256"] == digest(basis)
    assert value["effective_deadline_utc"] == "2026-01-01T00:01:00+00:00"
    assert value["state"] == "reserved" and value["reservation_occupied"] and not value["terminal"]
    assert not value["authorized"] and not value["evidence_accepted"]
    changed = copy.deepcopy(basis)
    changed["original_text"] += " "
    changed["payload"]["question"] = changed["original_text"]
    assert receipt(basis=changed)["job_sha256"] != value["job_sha256"]


def test_canonical_roundtrip_detaches_schema_and_utc_values():
    value = receipt()
    raw = pending.encode_document(pending.PENDING, value)
    assert pending.decode_document(pending.PENDING, raw) == value
    assert pending.encode_document(pending.PENDING, pending.decode_document(pending.PENDING, raw)) == raw
    detached = pending.validate_document(pending.PENDING, value)
    detached["run_id"] = "other"
    assert value["run_id"] == "run"
    schema = pending.schema_document(pending.PENDING)
    schema["properties"].clear()
    assert pending.schema_document(pending.PENDING)["properties"]
    lexical = copy.deepcopy(value)
    lexical["issued_utc"] = ISSUED
    assert pending.validate_document(pending.PENDING, lexical) == value


@pytest.mark.parametrize("run_deadline,expected", [
    ("2026-01-01T00:00:40Z", "2026-01-01T00:00:40+00:00"),
    ("2026-01-01T00:05:00Z", "2026-01-01T00:01:00+00:00"),
])
def test_effective_deadline_is_exact_minimum_never_renewed(run_deadline, expected):
    value = receipt(run_deadline=run_deadline)
    observed = pending.observe(value, observed_utc="2026-01-01T00:00:15Z")
    cancelled = pending.request_cancellation(observed, observed_utc="2026-01-01T00:00:20Z")
    fallback = pending.fallback(cancelled, observed_utc="2026-01-01T00:00:21Z", reason="cancellation_unconfirmed")
    assert all(row["effective_deadline_utc"] == expected for row in (value, observed, cancelled, fallback))
    assert all(row["job_sha256"] == value["job_sha256"] for row in (observed, cancelled, fallback))
    assert all(row["issued_utc"] == value["issued_utc"] for row in (observed, cancelled, fallback))


@pytest.mark.parametrize("state", ["reserved", "cancellation_requested"])
def test_deadline_boundary_expires_without_inventing_external_completion(state):
    value = receipt()
    if state == "cancellation_requested":
        value = pending.request_cancellation(value, observed_utc="2026-01-01T00:00:10Z")
    result = pending.observe(value, observed_utc=DEADLINE)
    assert result["state"] == "expired" and result["terminal"] and result["reservation_occupied"]
    assert result["fallback_reason"] == "deadline_expired" and result["completion_ref"] is None
    assert pending.request_cancellation(result, observed_utc=DEADLINE) == result
    assert pending.fallback(result, observed_utc=DEADLINE, reason="executor_unavailable") == result


def test_historical_expired_job_is_represented_and_before_issue_clock_refuses():
    result = receipt(observed="2026-01-01T00:02:00Z")
    assert result["state"] == "expired" and result["reservation_occupied"]
    with pytest.raises(WorkflowContractError) as error:
        receipt(observed="2025-12-31T23:59:59Z")
    assert error.value.error_code == "PENDING_WORK_CLOCK_REGRESSED"


@pytest.mark.parametrize("operation", ["observe", "request_cancellation", "fallback"])
def test_backward_owner_clock_refuses_without_mutating_receipt(operation):
    value = receipt()
    before = copy.deepcopy(value)
    arguments = {"observed_utc": "2026-01-01T00:00:04Z"}
    if operation == "fallback":
        arguments["reason"] = "executor_unavailable"
    with pytest.raises(WorkflowContractError) as error:
        getattr(pending, operation)(value, **arguments)
    assert error.value.error_code == "PENDING_WORK_CLOCK_REGRESSED"
    assert value == before


@pytest.mark.parametrize("reason", pending.FALLBACK_REASONS)
def test_automatic_unknown_fallback_is_terminal_and_keeps_reservation(reason):
    value = receipt()
    result = pending.fallback(value, observed_utc=NOW, reason=reason)
    assert result["state"] == "unknown" and result["fallback_reason"] == reason
    assert result["terminal"] and result["reservation_occupied"] and result["completion_ref"] is None
    after = pending.observe(result, observed_utc="2026-01-01T00:02:00Z")
    assert after["state"] == "unknown" and after["fallback_reason"] == reason and after["reservation_occupied"]
    assert pending.request_cancellation(after, observed_utc=after["last_observed_utc"]) == after


def test_cancellation_request_is_not_a_cancel_acknowledgement_and_is_idempotent():
    value = receipt()
    requested = pending.request_cancellation(value, observed_utc="2026-01-01T00:00:10Z")
    assert requested["state"] == "cancellation_requested" and not requested["terminal"]
    assert requested["cancellation_requested_utc"] == "2026-01-01T00:00:10+00:00"
    assert requested["completion_ref"] is None and requested["reservation_occupied"]
    assert pending.request_cancellation(requested, observed_utc="2026-01-01T00:00:10Z") == requested
    later = pending.request_cancellation(requested, observed_utc="2026-01-01T00:00:20Z")
    assert later["cancellation_requested_utc"] == requested["cancellation_requested_utc"]


@pytest.mark.parametrize("state", ["completed", "cancelled"])
def test_codec_can_describe_inert_future_observations_but_helpers_create_no_authority(state):
    value = receipt()
    value.update(state=state, terminal=True, reservation_occupied=False, completion_ref="sha256:" + "2" * 64)
    if state == "cancelled":
        value["cancellation_requested_utc"] = NOW
    described = pending.validate_document(pending.PENDING, value)
    observed = pending.observe(described, observed_utc="2026-01-01T00:02:00Z")
    assert observed["state"] == state and observed["completion_ref"] == described["completion_ref"]
    assert not observed["authorized"] and not observed["evidence_accepted"]
    assert pending.fallback(observed, observed_utc=observed["last_observed_utc"], reason="executor_unavailable") == observed


@pytest.mark.parametrize("kind", ["extension", "earlier_min", "occupied", "terminal", "unknown_reason", "unobserved_complete", "cancel_without_request", "late_cancel", "future_cancel", "unknown_field", "authority", "bare_hash"])
def test_forged_state_and_lease_fields_refuse(kind):
    value = receipt()
    if kind == "extension":
        value["effective_deadline_utc"] = "2026-01-01T00:01:01Z"
    elif kind == "earlier_min":
        value["effective_deadline_utc"] = "2026-01-01T00:00:59Z"
    elif kind == "occupied":
        value["reservation_occupied"] = False
    elif kind == "terminal":
        value["terminal"] = True
    elif kind == "unknown_reason":
        value.update(state="unknown", terminal=True, fallback_reason="approved")
    elif kind == "unobserved_complete":
        value.update(state="completed", terminal=True, reservation_occupied=False)
    elif kind == "cancel_without_request":
        value.update(state="cancelled", terminal=True, reservation_occupied=False, completion_ref="sha256:" + "2" * 64)
    elif kind in {"late_cancel", "future_cancel"}:
        value.update(state="cancellation_requested", cancellation_requested_utc=DEADLINE if kind == "late_cancel" else "2026-01-01T00:00:10Z")
    elif kind == "unknown_field":
        value["executor"] = "worker-selected"
    elif kind == "authority":
        value["authorized"] = True
    else:
        value["job_sha256"] = value["job_sha256"][7:]
    with pytest.raises(WorkflowContractError):
        pending.validate_document(pending.PENDING, value)


@pytest.mark.parametrize("clock", ["2026-01-01T00:00:05", "2026-01-01T01:00:05+01:00", "2026-01-01T00:00:60Z", "2026-02-30T00:00:05Z", "2026-01-01 00:00:05Z", "2026-01-01T00:00:05.1234567Z", None, True])
def test_noncanonical_ambiguous_and_invalid_clocks_refuse(clock):
    with pytest.raises(WorkflowContractError):
        pending.observe(receipt(), observed_utc=clock)


@pytest.mark.parametrize("name", [None, [], {}, True, "research-pending-work/v2"])
def test_unsupported_schema_names_refuse_safely(name):
    with pytest.raises(WorkflowContractError) as error:
        pending.schema_document(name)
    assert error.value.error_code == "PENDING_WORK_VERSION_UNSUPPORTED"


@pytest.mark.parametrize("reason", [None, [], "completed", "cancelled", "approved", "deadline_expired"])
def test_fallback_cannot_assert_completion_cancellation_or_arbitrary_reason(reason):
    with pytest.raises(WorkflowContractError) as error:
        pending.fallback(receipt(), observed_utc=NOW, reason=reason)
    assert error.value.error_code == "PENDING_WORK_REASON_INVALID"


def test_malformed_duplicate_oversized_and_cyclic_inputs_refuse():
    raw = pending.encode_document(pending.PENDING, receipt())
    duplicate = b'{"state":"reserved",' + raw[1:]
    for encoded in (duplicate, b"[]", b"{", b"{\"state\":NaN}", b"x" * (pending.MAX_BYTES + 1), b"\xff"):
        with pytest.raises(WorkflowContractError):
            pending.decode_document(pending.PENDING, encoded)
    cyclic = receipt()
    cyclic["unexpected"] = cyclic
    with pytest.raises(WorkflowContractError):
        pending.validate_document(pending.PENDING, cyclic)
    assert json.loads(raw)["completion_ref"] is None


def test_create_reuses_canonical_job_timeout_validation():
    value = job()
    value["timeout_seconds"] = 61
    with pytest.raises(WorkflowContractError):
        receipt(basis=value)


def test_shared_transition_preserves_exact_reservation_and_terminal_history():
    previous = receipt()
    candidate = pending.fallback(previous, observed_utc="2026-01-01T00:00:10Z", reason="executor_unavailable")
    assert pending.validate_transition(previous, candidate) == candidate
    assert pending.validate_transition(candidate, candidate) == candidate
    copied = pending.validate_transition(previous, candidate)
    copied["run_id"] = "caller-mutation"
    assert candidate["run_id"] == "run"
    changed = pending.observe(candidate, observed_utc="2026-01-01T00:00:11Z")
    with pytest.raises(WorkflowContractError) as error:
        pending.validate_transition(candidate, changed)
    assert error.value.error_code == "PENDING_WORK_TRANSITION_INVALID"


@pytest.mark.parametrize("field", ["run_id", "binding_id", "request_id", "job_id", "job_sha256", "issued_utc", "job_deadline_utc", "run_deadline_utc"])
def test_shared_transition_refuses_edited_immutable_identity_or_lease(field):
    previous = receipt()
    changed = pending.fallback(previous, observed_utc="2026-01-01T00:00:10Z", reason="executor_unavailable")
    if field in {"binding_id", "job_sha256"}:
        changed[field] = "sha256:" + "2" * 64
    elif field in {"run_id", "request_id", "job_id"}:
        changed[field] = "other"
    elif field == "issued_utc":
        changed[field] = "2025-12-31T23:59:59Z"
    elif field == "job_deadline_utc":
        changed[field] = changed["effective_deadline_utc"] = "2026-01-01T00:00:59Z"
    else:
        changed[field] = "2026-01-01T00:01:01Z"
    with pytest.raises(WorkflowContractError):
        pending.validate_transition(previous, changed)


@pytest.mark.parametrize("state", ["reserved", "cancellation_requested", "cancelled", "completed"])
def test_shared_initial_profile_does_not_admit_unconfirmed_or_refunded_external_completion(state):
    previous = receipt()
    candidate = copy.deepcopy(previous)
    if state == "cancellation_requested":
        candidate = pending.request_cancellation(candidate, observed_utc=NOW)
    elif state in {"cancelled", "completed"}:
        candidate.update(state=state, terminal=True, reservation_occupied=False, completion_ref="sha256:" + "2" * 64)
        if state == "cancelled":
            candidate["cancellation_requested_utc"] = NOW
    with pytest.raises(WorkflowContractError) as error:
        pending.validate_transition(previous, candidate)
    assert error.value.error_code == "PENDING_WORK_TRANSITION_INVALID"


def test_shared_transition_refuses_regressed_observation_even_when_inert_candidate_is_valid():
    previous = pending.observe(receipt(), observed_utc="2026-01-01T00:00:10Z")
    candidate = pending.fallback(receipt(), observed_utc="2026-01-01T00:00:09Z", reason="executor_unavailable")
    with pytest.raises(WorkflowContractError) as error:
        pending.validate_transition(previous, candidate)
    assert error.value.error_code == "PENDING_WORK_TRANSITION_INVALID"
