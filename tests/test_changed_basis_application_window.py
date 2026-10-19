"""Changed-basis controls retain one application window through public refusal."""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_session as native
from evidence_wiki import review_application_lease as leases
from evidence_wiki import review_packet_execution
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.errors import EvidenceWikiError
from tools import _review_dependency_workload as workload


@pytest.mark.parametrize("version", [None, leases.LEASE_V1, leases.LEASE_V2])
@pytest.mark.parametrize("failure", [None, "assembly", "mutation", "advance"])
def test_changed_basis_control_owns_original_application_window(monkeypatch, tmp_path, version, failure):
    now = datetime.now(timezone.utc)
    authority_end = now + timedelta(seconds=90)
    selected = None if version is None else leases.ReviewApplicationLease(
        120, (now + timedelta(seconds=180)).isoformat(), schema_version=version)
    profile = SimpleNamespace(review_application_lease=selected, sha256="a" * 64)
    execution = SimpleNamespace(profile=profile)
    events, frozen = [], []

    def observe(label):
        events.append(label)
        if selected is None:
            with pytest.raises(ExecutionError, match="review_application_window_required"):
                leases.issuance(session)
        else:
            fields = leases.issuance(session)
            frozen.append(fields)
            assert datetime.fromisoformat(fields["application_deadline_utc"]) == authority_end

    def assemble(**kwargs):
        observe("assembly")
        assert kwargs == {"request_id": "changed-basis-control-packet", "claim_id": "observed-capacity",
            "task_role": "evaluator", "page_chars": 16384}
        if failure == "assembly":
            raise ValueError("assembly failed")

    def advance(**kwargs):
        observe("advance")
        assert kwargs == {"request_id": "changed-basis-control-advance", "expected_basis_id": "basis"}
        raise EvidenceWikiError("OTHER" if failure == "advance" else "RESEARCH_BASIS_CHANGED", "stale basis")

    session = SimpleNamespace(execution=execution, assemble_review_packet=assemble, advance=advance)
    runtime = SimpleNamespace(execution=execution, context=SimpleNamespace(activate=nullcontext),
        policy=SimpleNamespace(to_dict=lambda: {"scope": {"question_ids": ["capacity"]}}))
    workspace = SimpleNamespace(research=SimpleNamespace(session=lambda *args, **kwargs: nullcontext(session)))
    monkeypatch.setattr(workload, "Workspace", SimpleNamespace(open=lambda root: nullcontext(workspace)))
    monkeypatch.setattr(native, "gate", lambda selected_session: nullcontext())
    monkeypatch.setattr(native, "_begin", lambda selected_session: events.append("begin"))
    monkeypatch.setattr(native, "_prepare", lambda selected_session, original: events.append("prepare"))
    monkeypatch.setattr(native, "deadline", lambda selected_session: authority_end)
    monkeypatch.setattr(native, "_external", lambda *args: pytest.fail("control dispatched a worker"))
    attempt = SimpleNamespace(attempt_id="attempt", job={"basis_id": "basis"},
        document=lambda: {"attempt": True}, worker_document=lambda: {"worker": True})

    def reserve(selected_session, **kwargs):
        assert selected_session is session
        observe("reserve")
        assert kwargs == {"request_id": "changed-basis-control", "assembly_request_id": "changed-basis-control-packet",
            "original_id": "capacity", "claim_id": "observed-capacity", "config_ref": "evidence"}
        return attempt

    monkeypatch.setattr(review_packet_execution, "reserve_evaluator", reserve)

    def mutate():
        observe("mutation")
        if failure == "mutation":
            raise ValueError("mutation failed")

    originals = []
    if failure is None:
        result = workload.changed_basis_control(tmp_path, runtime, "control", originals, mutate)
        assert result == {"attempt_id": "attempt", "reserved_basis_id": "basis",
            "native_error_code": "RESEARCH_BASIS_CHANGED", "native_reason": "BASIS_CHANGED", "applied": False}
        assert [label for label, _ in originals] == ["changed-basis-attempt", "changed-basis-worker", "changed-basis-control"]
    else:
        with pytest.raises(EvidenceWikiError if failure == "advance" else ValueError):
            workload.changed_basis_control(tmp_path, runtime, "control", originals, mutate)
    expected = ["begin", "prepare", "assembly"]
    if failure != "assembly":
        expected += ["reserve", "mutation"]
        if failure != "mutation":
            expected += ["advance"]
    assert events == expected
    if frozen:
        assert all(fields == frozen[0] for fields in frozen)
    with pytest.raises(ExecutionError, match="review_application_window_required"):
        leases.issuance(session)
