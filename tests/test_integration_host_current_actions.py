"""Retained host work stays separate from authenticated native eligibility."""

import copy
import json
from hashlib import sha256
from pathlib import Path

import pytest

from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.research_command_contracts import RESULT
from evidence_wiki.research_command_operations import refusal
from tests.test_repeated_integration_host import owner_fixture
from tools import _integration_improvement_owners as owners
from tools._release_decision import canonical, digest


def action_document(run_id="baseline", *, state="returned", result=True):
    return {"schema_version": "finite-host-current-action/v1", "caller_type": "trusted_reference_host",
            "operation": "refresh_current", "run_id": run_id, "state": state, "result": result,
            "failure": None if state == "returned" else {"kind": "unknown", "error_code": None, "reason": None},
            "allowance": {"deadline_utc": "2030-01-01T12:00:20Z", "reserved_seconds": 20.0, "remaining_seconds": 19.5},
            "elapsed_seconds": 0.5}


def retain(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)
    return {"path": str(path), "raw_sha256": sha256(raw).hexdigest(), "size_bytes": len(raw)}


def response_fixture(tmp_path, *, route="direct", retry=False):
    row = {"run_id": "baseline", "projection": {"accepted": False, "integration_success": True}, "originals": {}}
    documents = {"result": {"outcome": "rejected"}, "brief": {"verdict": "reject"}, "accounting": {}}
    for name, document in documents.items():
        row["originals"][name] = retain(tmp_path / "observations" / ("baseline-" + name + ".json"), canonical(document))
    actions = [action_document()]
    if retry:
        second = action_document()
        second["allowance"].update(reserved_seconds=19.0, remaining_seconds=18.5)
        actions.append(second)
    row["host_actions"] = [retain(tmp_path / "observations" / ("baseline-host-current-" + str(index) + ".json"),
                                  canonical(document)) for index, document in enumerate(actions, 1)]
    envelopes = []
    if retry:
        envelope = refusal("brief", EvidenceWikiError("RESEARCH_RESOLUTION_REFUSED", "refused",
            details={"reason": "resolution_current_inputs_changed"}))
        if route == "direct":
            envelope = {name: envelope[name] for name in ("status", "result", "error")}
        envelopes.append(envelope)
    envelope = {"status": "returned", "result": documents["brief"], "error": None}
    if route != "direct":
        envelope.update(schema_version=RESULT, operation="brief", terminal=True, exit_code=0,
                        authorized=False, evidence_accepted=False)
    envelopes.append(envelope)
    calls = []
    for index, envelope in enumerate(envelopes, 1):
        raw = canonical(envelope)
        original = retain(tmp_path / "observations" / ("call-" + str(index) + ".json"), raw)
        call = {"operation": "brief", "arguments": {"run_id": "baseline"}, "state": envelope["status"],
                "result_sha256": digest(envelope), "original_envelope": original,
                "envelope_raw_sha256": original["raw_sha256"], "envelope_size_bytes": original["size_bytes"]}
        if envelope["status"] == "refused":
            call.update(error_code="RESEARCH_RESOLUTION_REFUSED", reason="resolution_current_inputs_changed")
        calls.append(call)
    return {"cycles": [row], "calls": calls, "failure": None}, actions, documents


@pytest.mark.parametrize("route", ["direct", "current_caller", "scoped_mcp"])
@pytest.mark.parametrize("retry", [False, True])
def test_host_actions_leave_route_envelopes_and_rejected_projection_intact(tmp_path, route, retry):
    report, _, documents = response_fixture(tmp_path, route=route, retry=retry)
    before = copy.deepcopy(report)
    assert owners._responses(report, tmp_path, route) == [documents]
    assert report == before and report["cycles"][0]["projection"]["accepted"] is False


def test_absent_host_actions_are_readable_legacy_evidence(tmp_path):
    report, _, documents = response_fixture(tmp_path)
    report["cycles"][0].pop("host_actions")
    report["calls"] = []
    assert owners._responses(report, tmp_path, "direct") == [documents]


@pytest.mark.parametrize("field,value", [
    ("schema_version", "other"), ("caller_type", "external"), ("operation", "qualify"),
    ("run_id", "parent"), ("state", "started"), ("state", []), ("result", 1), ("result", None),
    ("failure", {}), ("elapsed_seconds", True), ("elapsed_seconds", -1), ("elapsed_seconds", float("inf")),
    ("elapsed_seconds", float("nan")), ("elapsed_seconds", "0.5"), ("extra", "diagnostic"),
])
def test_action_grammar_rejects_unreviewed_fields_and_values(field, value):
    document = action_document()
    document[field] = value
    with pytest.raises(ValueError, match="^integration_original_response_changed$"):
        owners._host_action_document(document, "baseline")


@pytest.mark.parametrize("field,value", [
    ("deadline_utc", "2030-01-01T12:00:20+01:00"), ("deadline_utc", "2030-02-30T12:00:20Z"),
    ("reserved_seconds", True), ("reserved_seconds", 0), ("reserved_seconds", -1),
    ("reserved_seconds", float("nan")), ("reserved_seconds", "20"),
    ("remaining_seconds", False), ("remaining_seconds", -1), ("remaining_seconds", 20), ("extra", 0),
])
def test_action_allowance_is_finite_and_exact(field, value):
    document = action_document()
    document["allowance"][field] = value
    with pytest.raises(ValueError, match="^integration_original_response_changed$"):
        owners._host_action_document(document, "baseline")


def test_exhausted_allowance_is_valid_host_observation():
    document = action_document()
    document.update(elapsed_seconds=21)
    document["allowance"].update(reserved_seconds=20, remaining_seconds=0)
    owners._host_action_document(document, "baseline")


@pytest.mark.parametrize("state", ["refused", "failed"])
@pytest.mark.parametrize("field,value", [
    ("kind", "ArbitraryClass"), ("kind", []), ("error_code", "PRIVATE_CODE"),
    ("reason", "private_reason"), ("extra", "private diagnostic"),
])
def test_action_failure_uses_only_reviewed_tokens(state, field, value):
    document = action_document(state=state, result=None)
    document["failure"][field] = value
    with pytest.raises(ValueError, match="^integration_original_response_changed$"):
        owners._host_action_document(document, "baseline")


@pytest.mark.parametrize("state,result", [("returned", False), ("refused", None), ("failed", None)])
def test_failed_producer_preserves_host_action_original_without_positive_qualification(tmp_path, state, result):
    report, _, _ = response_fixture(tmp_path)
    report.update(failure={"kind": "ValueError"}, calls=[])
    row = report["cycles"][0]
    row["projection"].update(accepted=False, integration_success=False)
    document = action_document(state=state, result=result)
    original = row["host_actions"][0]
    row["host_actions"][0] = retain(Path(original["path"]), canonical(document))
    raw = Path(original["path"]).read_bytes()
    owners._responses(report, tmp_path, "direct")
    assert Path(original["path"]).read_bytes() == raw
    row["projection"]["accepted"] = True
    with pytest.raises(ValueError, match="^integration_original_response_changed$"):
        owners._responses(report, tmp_path, "direct")


@pytest.mark.parametrize("mutation", ["missing_brief", "extra_brief", "wrong_run", "retry_reason", "renewed_deadline",
                                      "renewed_budget", "duplicate", "unknown_index", "empty", "non_list"])
def test_action_inventory_and_brief_retry_are_closed(tmp_path, mutation):
    report, actions, _ = response_fixture(tmp_path, retry=True)
    row = report["cycles"][0]
    if mutation == "missing_brief":
        report["calls"] = []
    elif mutation == "extra_brief":
        report["calls"].append(report["calls"][-1])
    elif mutation == "wrong_run":
        report["calls"][-1]["arguments"]["run_id"] = "parent"
    elif mutation == "retry_reason":
        report["calls"][0]["reason"] = "qualification_unknown"
    elif mutation in {"renewed_deadline", "renewed_budget"}:
        if mutation == "renewed_deadline":
            actions[1]["allowance"]["deadline_utc"] = "2030-01-01T12:00:21Z"
        else:
            actions[1]["allowance"].update(reserved_seconds=20.0, remaining_seconds=19.5)
        row["host_actions"][1] = retain(Path(row["host_actions"][1]["path"]), canonical(actions[1]))
    elif mutation == "duplicate":
        row["host_actions"][1] = row["host_actions"][0]
    elif mutation == "unknown_index":
        row["host_actions"].append(row["host_actions"][0])
    elif mutation == "empty":
        row["host_actions"] = []
    else:
        row["host_actions"] = {}
    with pytest.raises(ValueError, match="^integration_original_response_changed$"):
        owners._responses(report, tmp_path, "direct")


@pytest.mark.parametrize("mutation", ["bytes", "hash", "size", "bool_size", "traversal", "origin", "extra", "symlink",
                                      "malformed", "duplicate_json", "nonfinite_json", "oversize"])
def test_host_action_retention_authenticates_fixed_bounded_private_original(tmp_path, mutation):
    report, _, _ = response_fixture(tmp_path)
    original = report["cycles"][0]["host_actions"][0]
    path = Path(original["path"])
    if mutation == "bytes":
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "hash":
        original["raw_sha256"] = "0" * 64
    elif mutation in {"size", "bool_size"}:
        original["size_bytes"] = True if mutation == "bool_size" else original["size_bytes"] + 1
    elif mutation == "traversal":
        original["path"] = str(path.parent / ".." / "observations" / path.name)
    elif mutation == "origin":
        original["path"] = str(path.with_name("change_1-host-current-1.json"))
    elif mutation == "extra":
        original["origin"] = "trusted"
    elif mutation == "symlink":
        target = path.with_name("alias.json")
        path.rename(target)
        path.symlink_to(target)
    else:
        raw = {"malformed": b"{", "duplicate_json": b'{"state":"returned","state":"failed"}',
               "nonfinite_json": b'{"elapsed_seconds":NaN}', "oversize": b" " * 4_194_305}[mutation]
        original.update(retain(path, raw))
    with pytest.raises(ValueError, match="^integration_original_response_changed$"):
        owners._responses(report, tmp_path, "direct")


@pytest.mark.parametrize("native_mutation", [None, "probe", "acceptance", "domain", "binding", "closing"])
def test_returned_host_work_cannot_replace_current_native_proof(monkeypatch, tmp_path, native_mutation):
    data = owner_fixture(monkeypatch, tmp_path)
    directory = Path(data.selection["path"]).parent
    response, actions, _ = response_fixture(tmp_path / "responses")
    row = data.report["cycles"][0]
    action_path = directory / "observations" / "baseline-host-current-1.json"
    raw = canonical(actions[0])
    data.files[action_path] = raw
    row["host_actions"] = [{"path": str(action_path), "raw_sha256": sha256(raw).hexdigest(), "size_bytes": len(raw)}]
    call = response["calls"][0]
    brief_path = Path(row["originals"]["brief"]["path"])
    envelope = {"status": "returned", "result": json.loads(data.files[brief_path]), "error": None}
    raw = canonical(envelope)
    call_path = directory / "observations" / "call-1.json"
    data.files[call_path] = raw
    call.update(result_sha256=digest(envelope), envelope_raw_sha256=sha256(raw).hexdigest(), envelope_size_bytes=len(raw),
                original_envelope={"path": str(call_path), "raw_sha256": sha256(raw).hexdigest(), "size_bytes": len(raw)})
    data.report["calls"] = [call]
    if native_mutation == "probe":
        data.proof["journal"]["events"] = []
    elif native_mutation == "acceptance":
        data.proof["current"]["accounting"]["accepted_count"] = 0
    elif native_mutation == "domain":
        data.proof["current"]["canonical_release"]["research_complete"] = False
    elif native_mutation == "binding":
        data.proof["host_inputs"] = {"changed": True}
    elif native_mutation == "closing":
        def reject_closing(_):
            raise ValueError("changed")
        monkeypatch.setattr(owners, "verify_journal_closing", reject_closing)
    data.retain()
    monkeypatch.setattr(owners, "_host_files", lambda *args: {})
    if native_mutation is None:
        evidence = data.readers()[0].read()
        assert evidence.observed_at == data.proof["read_finished_utc"]
        assert evidence.expires_at == "2030-01-01T12:00:30+00:00"
        assert len(data.calls) == 1
        return
    with pytest.raises(ValueError, match="integration_(original_current_probe_missing|current_native_unavailable|"
                                         "current_binding_changed|original_history_changed)"):
        data.readers()[0].read()
    assert len(data.calls) == 1
