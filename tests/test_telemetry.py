"""Exact retained measurements, declared spans and isolated host verification."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from decimal import ROUND_UP, Inexact, localcontext

import pytest

from evidence_wiki import telemetry
from evidence_wiki._filesystem import os as native_os
from evidence_wiki._pack_io import canonical
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.research_session import ResearchSession, current
from evidence_wiki.telemetry_contracts import EVENT, JOURNAL, VERIFICATION, validate_document
from evidence_wiki.workflow_contracts import JOB, document_sha256
from tests.test_research_session import GRANTS as SESSION_GRANTS
from tests.test_research_session import host as session_host
from tests.test_research_session import policy
from tests.test_research_session import root as _session_root


def metric(identifier="cost1", *, value="0.125", provenance="measured", currency="USD", name="cost"):
    return {"logical_id": identifier, "name": name, "value": value,
        "unit": "currency" if name == "cost" else "count", "currency": currency if name == "cost" else None,
        "pricing_ref": {"id": "provider-price-v1", "as_of": "2026-10-01T00:00:00Z"} if name == "cost" else None,
        "provenance": provenance, "source_ref": "provider-meter", "trust_realm": "local_artifact"}


def event(identifier="event1", *, measurements=None, span=None):
    return {"schema_version": EVENT, "logical_id": identifier, "run_id": "run", "agent_id": "caller",
        "request_id": None, "job_id": None, "job_sha256": None, "attempt_id": "attempt1",
        "kind": "span" if span is not None else "attempt", "occurred_at": "2026-10-01T00:00:00Z",
        "role": "host_worker", "controller_ref": "worker", "policy_id": None, "basis_id": None,
        "input_refs": [], "output_refs": [], "model": {"id": "synthetic-model", "version": "1"},
        "prompt": {"id": "bounded-prompt", "version": "1"}, "summary": {"text": "Retained bounded observation.", "evidence_refs": []},
        "decision": None, "metrics": [metric()] if measurements is None else measurements,
        "span": span, "attempt_status": "completed", "authorized": False, "evidence_accepted": False}


def journal(events):
    rows = [{"schema_version": "1.0", "event_id": "evt-" + str(index), "run_id": "run", "agent_id": "caller",
        "occurred_at": "2026-10-01T00:00:00Z", "event_type": "custom.research.telemetry", "from_state": None,
        "to_state": None, "message": "Inert telemetry recorded.", "data": {"logical_id": value["logical_id"], "event": value}}
        for index, value in enumerate(events)]
    return {"schema_version": JOURNAL, "run_id": "run", "events": rows, "sha256": telemetry._sha(rows),
        "bytes": len(canonical(rows)), "count": len(rows), "limits": {"max_bytes": 16777216,
            "max_event_bytes": 1048576, "max_events": 4096, "max_lines": 8192, "max_depth": 32},
        "authorized": False, "evidence_accepted": False}


@pytest.fixture
def session_root(tmp_path):
    return _session_root.__wrapped__(tmp_path)


@pytest.fixture
def selected(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    control = tmp_path / "control"
    native_os.mkdir(control, mode=0o700)
    grants = {"telemetry.record", "telemetry.report", "run_controller.run_telemetry_record", "run_controller.run_telemetry_events",
        "telemetry_delivery.deliver", "telemetry_delivery.inspect", "telemetry_delivery.retry"}
    return root, HostContext([root], grants, "caller", [root], mutation_control_directory=control)


def folded(selected, monkeypatch, events, verifier=None):
    monkeypatch.setattr(telemetry, "call_seam", lambda *args, **kwargs: journal(events))
    with selected[1].activate():
        return telemetry.report(selected[0], run_id="run", verifier=verifier)


def test_exact_cost_subtotals_ignore_hostile_decimal_context_and_preserve_unknown_total(selected, monkeypatch):
    amount = "999999999999999.999999999"
    entries = [event("first", measurements=[metric("one", value=amount)]), event("second", measurements=[metric("two", value=amount)])]
    with localcontext() as context:
        context.prec = 1
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        value = folded(selected, monkeypatch, entries)
    assert value["metrics"][0]["known_subtotal"] == "1999999999999999.999999998"
    assert value["metrics"][0]["total"] is None
    assert value["observed_human_interventions"] is None
    assert not value["coverage"]["whole_execution_authenticated"]


def test_exact_metric_duplicates_do_not_double_count_and_conflicts_refuse(selected, monkeypatch):
    first, second = event("first"), event("second")
    value = folded(selected, monkeypatch, [first, second])
    assert value["metrics"][0]["known_subtotal"] == "0.125" and value["metrics"][0]["observations"] == 1
    second["metrics"][0]["value"] = "0.126"
    with pytest.raises(EvidenceWikiError) as error:
        folded(selected, monkeypatch, [first, second])
    assert error.value.error_code == "TELEMETRY_METRIC_CONFLICT"


def test_provenance_currency_and_pricing_are_separate_unknown_groups(selected, monkeypatch):
    rows = [event("usd", measurements=[metric("usd", value="1")]),
        event("eur", measurements=[metric("eur", value="2", currency="EUR")]),
        event("estimate", measurements=[metric("estimate", value="3", provenance="estimated")])]
    rows[2]["metrics"][0]["trust_realm"] = "caller_estimate"
    unknown = event("unknown", measurements=[metric("unknown", value=None, provenance="unavailable", currency=None)])
    unknown["metrics"][0].update(pricing_ref=None, trust_realm="unavailable")
    value = folded(selected, monkeypatch, rows + [unknown])
    assert len(value["metrics"]) == 4
    assert sorted(row["known_subtotal"] for row in value["metrics"] if row["known_subtotal"] is not None) == ["1", "2", "3"]
    assert all(row["total"] is None for row in value["metrics"])
    assert next(row for row in value["metrics"] if row["provenance"] == "unavailable")["unknown"] == 1


def test_overlap_and_incomplete_spans_preserve_interval_sum_union_and_unknown_runtime(selected, monkeypatch):
    def span(start, end):
        return {"kind": "active", "started_at": f"2026-10-01T00:00:0{start}Z",
            "ended_at": f"2026-10-01T00:00:0{end}Z" if end is not None else None, "clock_ref": "declared-utc"}
    value = folded(selected, monkeypatch, [event("a", measurements=[], span=span(0, 5)),
        event("b", measurements=[], span=span(3, 6)), event("c", measurements=[], span=span(7, None))])
    row = value["spans"][0]
    assert row["interval_sum_milliseconds"] == "8000" and row["union_milliseconds"] == "6000"
    assert row["overlap"] and row["incomplete"] == 1 and row["total_milliseconds"] is None
    assert row["duration_basis"] == "declared_utc" and not row["authenticated"]


def verifier(*, mutation=None):
    def verify(request):
        value = {"schema_version": VERIFICATION, "event_sha256": request["event_sha256"], "workspace_binding": request["workspace_binding"],
            "run_id": request["event"]["run_id"], "agent_id": request["event"]["agent_id"], "controller_ref": "worker",
            "metric_ids": [row["logical_id"] for row in request["event"]["metrics"]],
            "verified_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(), "authenticated": True, "scope": "event_measurements"}
        if mutation:
            mutation(value)
        return value
    return telemetry.MeasurementVerifier("worker", verify)


@pytest.mark.parametrize("field", ["event_sha256", "workspace_binding", "run_id", "agent_id", "controller_ref", "metric_ids", "expires_at"])
def test_host_verification_requires_exact_current_event_scope_and_metric_set(selected, monkeypatch, field):
    def change(value):
        if field == "workspace_binding":
            value[field]["inode"] = "unrelated"
        elif field == "metric_ids":
            value[field] = []
        elif field == "event_sha256":
            value[field] = "sha256:" + "1" * 64
        elif field == "expires_at":
            value[field] = "2020-01-01T00:00:00Z"
        else:
            value[field] = "other"
    value = folded(selected, monkeypatch, [event()], verifier(mutation=change))
    assert not value["metrics"][0]["authenticated"] and value["metrics"][0]["known_subtotal"] == "0.125"


def test_verified_value_and_declared_completed_attempt_never_create_verified_total(selected, monkeypatch):
    value = folded(selected, monkeypatch, [event()], verifier())
    assert value["metrics"][0]["authenticated"] and value["metrics"][0]["total"] is None
    assert value["observed_human_interventions"] is None
    assert not value["coverage"]["whole_execution_authenticated"]
    trace = value["trace"][0]
    assert trace["event_sha256"] == telemetry._sha(trace["event"])
    assert trace["outer_event_sha256"] != trace["event_sha256"]


def test_worker_authentication_json_and_untrusted_verifier_objects_refuse(selected, monkeypatch):
    value = event()
    value["authenticated"] = True
    with pytest.raises(EvidenceWikiError):
        validate_document(EVENT, value)
    with selected[1].activate(), pytest.raises(EvidenceWikiError):
        telemetry.report(selected[0], run_id="run", verifier={"controller_ref": "worker", "authenticated": True})


def test_projection_redacts_secret_like_summary_and_private_refs_with_separate_commitments(selected, monkeypatch):
    value = event()
    value["summary"]["text"] = "Bearer synthetic-private-value password=synthetic-hidden"
    value["metrics"][0]["source_ref"] = "/private/host/secret-reference"
    original = folded(selected, monkeypatch, [value])
    projection = telemetry.project(original)
    raw = canonical(projection)
    assert b"synthetic-private-value" not in raw and b"synthetic-hidden" not in raw and b"/private/host" not in raw
    assert projection["redaction_count"] == 3 and projection["original_report_sha256"] == telemetry._sha(original)
    assert projection["report_sha256"] == telemetry._sha(projection["report"])
    assert projection["measurement_authentication"] == "not_reverified" and projection["report_is_declared"]
    assert not projection["authorized"] and not projection["evidence_accepted"]


def test_canonical_journal_real_decision_and_logical_event_replay(session_root, tmp_path):
    grants = SESSION_GRANTS | {"telemetry.record", "telemetry.report", "run_controller.run_telemetry_record", "run_controller.run_telemetry_events"}
    with session_host(session_root, tmp_path, grants=grants).activate():
        session = ResearchSession(session_root, policy(), run_id="run")
        result = session.run_until_terminal(request_id="step", expected_basis_id=current(session_root, policy())["basis_id"], max_steps=1)
        retained = telemetry.record(session_root, run_id="run", agent_id="caller", event=event())
        before = (session_root / "runs/run/events.jsonl").read_bytes()
        assert telemetry.record(session_root, run_id="run", agent_id="caller", event=event()) == retained
        assert (session_root / "runs/run/events.jsonl").read_bytes() == before
        report = telemetry.report(session_root, run_id="run")
        assert report["decisions"][0]["decision"] == result["decision"]
        assert report["metrics"][0]["known_subtotal"] == "0.125" and not report["evidence_accepted"]


@pytest.mark.parametrize("actor", ["other", None])
def test_direct_record_rejects_actor_relabel_before_gate_metadata(selected, actor):
    before = list(selected[1].mutation_control_directory.iterdir())
    with selected[1].activate(), pytest.raises(EvidenceWikiError):
        telemetry.record(selected[0], run_id="run", agent_id=actor, event=event())
    assert list(selected[1].mutation_control_directory.iterdir()) == before


def test_canonical_reserved_job_and_exact_event_job_binding_are_reconstructed(session_root, tmp_path):
    grants = SESSION_GRANTS | {"telemetry.record", "telemetry.report", "run_controller.run_telemetry_record", "run_controller.run_telemetry_events"}
    with session_host(session_root, tmp_path, grants=grants).activate():
        session = ResearchSession(session_root, policy(paid=True), run_id="run")
        original = session.current()["accounting"]["original_outcomes"][0]["original_text"]
        job = session.host_job(request_id="job", operation="generate", payload={"question": original})
        missing = telemetry.report(session_root, run_id="run")
        assert missing["coverage"]["missing_job_ids"] == ["job"]
        linked = event()
        linked.update(request_id="job", job_id="job", job_sha256=document_sha256(JOB, job),
            policy_id=job["policy_id"], basis_id=job["basis_id"], role=job["role"], controller_ref=job["controller_ref"])
        linked["prompt"]["version"] = job["prompt_version"]
        telemetry.record(session_root, run_id="run", agent_id="caller", event=linked)
        exact = telemetry.report(session_root, run_id="run")
        assert exact["coverage"]["expected_job_ids"] == ["job"] and exact["coverage"]["missing_job_ids"] == []
        assert exact["metrics"][0]["total"] is None


@pytest.mark.parametrize("exit_at", ["owner", "renderer"])
def test_direct_report_contains_process_exit_without_unsafe_payload_conversion(selected, monkeypatch, exit_at):
    class Payload:
        def __str__(self):
            pytest.fail("payload string conversion")
    def exited(*args, **kwargs):
        raise SystemExit(Payload())
    if exit_at == "owner":
        monkeypatch.setattr(telemetry, "call_seam", exited)
    else:
        class Refusal(Exception):
            error_code = "SYNTHETIC_REFUSED"
            def to_envelope(self):
                raise SystemExit(Payload())
            def __str__(self):
                raise SystemExit(Payload())
        def owner_refusal(*args, **kwargs):
            raise Refusal()
        monkeypatch.setattr(telemetry, "call_seam", owner_refusal)
    with selected[1].activate(), pytest.raises(EvidenceWikiError) as error:
        telemetry.report(selected[0], run_id="run")
    assert error.value.error_code == "LIBRARY_PROCESS_EXIT"


def test_direct_report_preserves_programming_type_error(selected, monkeypatch):
    def invalid(*args, **kwargs):
        raise TypeError("synthetic programming error")
    monkeypatch.setattr(telemetry, "call_seam", invalid)
    with selected[1].activate(), pytest.raises(TypeError, match="synthetic programming error"):
        telemetry.report(selected[0], run_id="run")


@pytest.mark.parametrize("field", ["request_id", "job_id", "job_sha256", "input_refs", "output_refs", "role", "model", "prompt", "controller_ref", "policy_id", "basis_id", "decision"])
def test_logical_meter_cannot_be_collapsed_across_changed_work_identity(field):
    first = event("first")
    second = copy.deepcopy(first)
    second["logical_id"] = "second"
    if field in {"input_refs", "output_refs"}:
        second[field] = ["sha256:" + "1" * 64]
    elif field in {"policy_id", "basis_id"}:
        second[field] = "sha256:" + "1" * 64
    elif field in {"job_id", "job_sha256"}:
        second.update(job_id="other-job", job_sha256="1" * 64)
    elif field in {"model", "prompt"}:
        second[field]["version"] = "other-version"
    elif field == "decision":
        second[field] = {"decision_ref": "other-decision", "outcome": "stop", "reason": "different work"}
    else:
        second[field] = "other"
    first, second = validate_document(EVENT, first), validate_document(EVENT, second)
    with pytest.raises(EvidenceWikiError) as error:
        telemetry._metrics([("first", first, False), ("second", second, False)], {})
    assert error.value.error_code == "TELEMETRY_METRIC_CONFLICT"


def test_logical_meter_status_clock_and_summary_updates_retain_one_exact_observation(selected, monkeypatch):
    first, second = event("first"), event("second")
    first["attempt_status"] = "started"
    second["occurred_at"] = "2026-10-01T00:00:01Z"
    second["summary"]["text"] = "Later declared completion observation."
    result = folded(selected, monkeypatch, [first, second])
    assert result["metrics"][0]["known_subtotal"] == "0.125" and result["metrics"][0]["observations"] == 1


def test_same_decision_ref_narrative_update_does_not_change_cost_identity(selected, monkeypatch):
    first, second = event("first"), event("second")
    first["decision"] = {"decision_ref": "retained-decision", "outcome": "pending", "reason": "Prior observation."}
    second["decision"] = {"decision_ref": "retained-decision", "outcome": "observed", "reason": "Later narrative."}
    result = folded(selected, monkeypatch, [first, second])
    assert result["metrics"][0]["known_subtotal"] == "0.125" and result["metrics"][0]["observations"] == 1
