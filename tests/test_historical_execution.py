"""Accepted input clocks stay separate from later computation and evaluation."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from evidence_wiki import verify_snapshot
from evidence_wiki.errors import SourceError
from tests._execution_fixture import binding, canonical, closure, identifier, independently_recalculate
from tests._historical_fixture import HistoricalFixture, late_signature
from tests._snapshot_fixture import SnapshotFixture
from tests._temporal_fixture import clock_claim


@pytest.fixture
def host(tmp_path, monkeypatch):
    return HistoricalFixture(tmp_path, monkeypatch)


@pytest.mark.parametrize("mode", ["historical-audit", "historical-available"])
def test_laboratory_history_live_and_offline_share_input_cutoff(host, mode):
    body, files = host.history(mode=mode)
    with patch("subprocess.run", side_effect=AssertionError("inert records")), patch("socket.create_connection", side_effect=AssertionError("offline")):
        report, assessment = host.assessment(body, files)
        assert assessment["eligible"], assessment
        assert assessment["historical_inputs"]["qualified_source_cutoffs"] == 1
        data, preparation, command, _result = host.export(host.selection(body))
        assert json.loads(data)["schema_version"] == "evidence-snapshot/v3"
        assert verify_snapshot(data, trust_policy_bytes=host.policy_path.read_bytes())["valid"]
        assert host.workspace.snapshots.check(data)["current_use"] == "authorized"
        assert not host.workspace.snapshots.export(host.selection(body), registration_request_id=command["payload"]["request_id"])["created"]
    assert [record["outcome"] for record in report["records"]] == ["failed", "inconclusive", "passed"]
    originals = {path.removeprefix("evidence/"): raw for path, raw in files.items() if path.startswith("evidence/")}
    for record in report["records"]:
        if record["record_type"] == "observation":
            assert independently_recalculate(originals, record["payload"])[2] == record["outcome"]
    assert preparation["manifest"]["selection"]["schema_version"] == "evidence-snapshot-selection/v1"
    request = host.request("execution:lab", mode=mode, cutoff="2026-09-10T05:00:00Z")
    selected = host.evaluate(request)
    assert selected["result"]["complete"], selected
    temporal = {**host.selection(body), "schema_version": "evidence-snapshot-selection/v2",
                "temporal": {key: request[key] for key in ("mode", "cutoff", "checkpoint")}}
    historical, _, _, _ = host.export(temporal)
    assert verify_snapshot(historical, trust_policy_bytes=host.policy_path.read_bytes())["valid"]


@pytest.mark.parametrize("change,reason", [
    ({"published_at": clock_claim("2026-09-10T02:00:00Z"), "available_at": clock_claim("2026-09-10T02:00:00Z")}, "temporal_future_publication"),
    ({"measurement": {"start": clock_claim("2026-09-10T00:00:00Z"), "end": clock_claim("2026-09-10T02:00:00Z")}}, "temporal_measurement_incomplete_at_cutoff"),
    ({"expires_at": clock_claim("2026-09-10T01:00:00Z")}, "temporal_expired_at_cutoff"),
])
def test_later_result_clock_never_qualifies_future_or_expired_inputs(host, change, reason):
    body, files = host.history(parent_times=change, available=False)
    report, assessment = host.assessment(body, files)
    assert report["valid"] and not assessment["eligible"] and assessment["reason"] == reason
    assert not any(item["positive_eligible"] for item in assessment["receipts"])
    before = (host.host / "evidence-state.json").read_bytes()
    with pytest.raises(SourceError):
        host.workspace.snapshots.prepare(host.selection(body))
    assert (host.host / "evidence-state.json").read_bytes() == before


def test_all_input_ancestors_are_qualified_even_when_not_named_as_inputs(host):
    ancestor, _ = host.captured("lab:unreleased", published="2026-09-10T02:00:00Z", available="2026-09-10T02:00:00Z")
    body, files = host.history(parent_parents=[ancestor["source_revision"]])
    _report, assessment = host.assessment(body, files)
    assert not assessment["eligible"] and assessment["reason"] == "temporal_future_publication"


def test_historical_available_requires_accepted_independent_proof(host):
    body, files = host.history(mode="historical-available", available=False)
    _report, assessment = host.assessment(body, files)
    assert not assessment["eligible"]
    assert assessment["reason"] == "execution_independent_availability_missing"


@pytest.mark.parametrize("input_mode", ["historical-audit", "historical-available"])
def test_temporal_export_cannot_borrow_input_proof_from_a_later_checkpoint(host, input_mode):
    body, _files = host.history(mode=input_mode, available=False)
    checkpoint = host.checkpoint
    selection = {**host.selection(body), "schema_version": "evidence-snapshot-selection/v2",
                 "temporal": {"mode": "historical-audit", "cutoff": "2026-09-10T05:00:00Z", "checkpoint": checkpoint}}
    host.set_time("2026-09-10T06:00:00Z")
    host.availability(host.parent, host.parent_files)
    current, _, _, _ = host.export(host.selection(body))
    assert verify_snapshot(current, trust_policy_bytes=host.policy_path.read_bytes())["valid"]
    assert json.loads(current)["manifest"]["execution_inputs"]["availability"][host.parent["source_revision"]] is not None
    before = (host.host / "evidence-state.json").read_bytes()
    if input_mode == "historical-available":
        with pytest.raises(SourceError) as caught:
            host.workspace.snapshots.prepare(selection)
        assert caught.value.details["reason"] == "snapshot_no_eligible_examples"
        assert (host.host / "evidence-state.json").read_bytes() == before
    else:
        historical, _, _, _ = host.export(selection)
        assert json.loads(historical)["manifest"]["execution_inputs"]["availability"][host.parent["source_revision"]] is None
        assert verify_snapshot(historical, trust_policy_bytes=host.policy_path.read_bytes())["valid"]


@pytest.mark.parametrize("checkpoint_time,valid", [("2026-09-10T05:00:00Z", False), ("2026-09-10T06:00:00Z", True)])
def test_offline_historical_input_proof_is_bounded_by_checkpoint_not_capture(host, checkpoint_time, valid):
    body, _files = host.history(mode="historical-available", available=False)
    checkpoint = host.checkpoint
    host.set_time("2026-09-10T06:00:00Z")
    host.availability(host.parent, host.parent_files)
    data, _, _, _ = host.export(host.selection(body))
    bundle = json.loads(data)
    manifest = bundle["manifest"]
    manifest["selection"].update(schema_version="evidence-snapshot-selection/v2", temporal={
        "mode": "historical-audit", "cutoff": "2026-09-10T05:00:00Z",
        "checkpoint": manifest["captured_state"]["checkpoint"] if valid else checkpoint})
    manifest["temporal"] = {"checkpoint_observed_at": checkpoint_time,
                            "availability": {source["source_revision"]: None for source in manifest["sources"]}}
    bundle["snapshot_id"] = identifier(manifest["schema_version"], manifest)
    bundle["registration"]["payload"]["body"]["node_id"] = bundle["snapshot_id"]
    bundle["registration"] = signature_at(bundle["registration"]["payload"], "owner", "usage", "2026-09-10T06:00:00Z")
    result = verify_snapshot(canonical(bundle), trust_policy_bytes=host.policy_path.read_bytes())
    assert result["valid"] is valid, result
    if not valid:
        assert result["reason"] == "snapshot_availability_observation_invalid"


@pytest.mark.parametrize("change", ["cutoff", "context"])
def test_historical_generation_shape_rejects_time_travel_and_unbound_context(host, change):
    def edit(files, record):
        payload = record["records"][-1]["payload"]
        if change == "cutoff":
            payload["temporal"]["cutoff"] = "2026-09-10T06:00:00Z"
        else:
            files["context.txt"] = b"Historical model context.\n"
            record["artifacts"].append({"path": "context.txt", **binding(files["context.txt"]), "role": "context"})
            payload["model"]["context"] = {"path": "context.txt", "content_hash": binding(files["context.txt"])["content_hash"]}
    body, files = host.history(edit=edit)
    execution = host.workspace._script("_execution_evidence")
    prefix = json.loads(files["source-record.json"])["evidence_root"]
    reason = "execution_input_cutoff_after_start" if change == "cutoff" else "execution_historical_context_input_required"
    with pytest.raises(ValueError, match=reason):
        execution.validate_closure(body["source_id"], {path[len(prefix) + 1:]: raw for path, raw in files.items() if path.startswith(prefix + "/")})


def test_offline_input_proofs_are_bound_and_cannot_be_removed_or_forged(host):
    body, _files = host.history(mode="historical-available")
    data, _, _, _ = host.export(host.selection(body))
    for mutation in ("control", "missing", "signature", "replay"):
        bundle = json.loads(data)
        proofs = bundle["manifest"]["execution_inputs"]["availability"]
        if mutation == "missing":
            del proofs[host.parent["source_revision"]]
        elif mutation == "signature":
            proofs[host.parent["source_revision"]]["body"]["receipt"]["authentication"]["signature"] = "0" * 64
        elif mutation == "replay":
            proofs[host.parent["source_revision"]] = proofs[body["source_revision"]]
        bundle["snapshot_id"] = identifier(bundle["manifest"]["schema_version"], bundle["manifest"])
        bundle["registration"]["payload"]["body"]["node_id"] = bundle["snapshot_id"]
        bundle["registration"] = late_signature(bundle["registration"]["payload"], "owner", "usage")
        result = verify_snapshot(canonical(bundle), trust_policy_bytes=host.policy_path.read_bytes())
        assert result["valid"] is (mutation == "control"), result
    # Downgrading the envelope cannot bypass the historical input contract.
    bundle = json.loads(data)
    bundle["schema_version"] = "evidence-snapshot/v1"
    assert not verify_snapshot(canonical(bundle), trust_policy_bytes=host.policy_path.read_bytes())["valid"]


def test_revocation_and_missing_host_context_cannot_promote_historical_pass(host):
    body, files = host.history()
    report, assessment = host.assessment(body, files)
    assert assessment["eligible"]
    execution = host.workspace._script("_execution_evidence")
    legacy_config = {key: value for key, value in host.config.items() if key != "evidence_usage"}
    assert not execution.assess_verification(report, host.root, legacy_config, host.clock.value)["eligible"]
    host.transact(host.module, "revoke", {"scope": "revision", "source_id": body["source_id"], "source_revision": body["source_revision"], "reason": "withdrawn"})
    assert not host.assessment(body, files)[1]["eligible"]


@pytest.mark.parametrize("cutoff,eligible", [("2026-09-10T06:00:00Z", True), ("2026-09-10T04:30:00Z", False)])
def test_nested_run_requires_result_to_exist_at_consumers_input_cutoff(host, cutoff, eligible):
    first, first_files = host.history()
    host.parent, host.parent_files = first, first_files
    host.set_time("2026-09-10T08:00:00Z")

    def edit(files, record):
        files["inputs.txt"] = first_files["evidence/result.txt"]
        for member in record["artifacts"]:
            if member["path"] == "inputs.txt":
                member.update(binding(files["inputs.txt"]))
        for envelope in record["records"] + record["receipts"]:
            for field in ("started_at", "finished_at"):
                original = datetime.fromisoformat(envelope["payload"][field].replace("Z", "+00:00"))
                envelope["payload"][field] = (original + timedelta(hours=7)).isoformat()
        for envelope in record["records"]:
            payload = envelope["payload"]
            payload["inputs"][0].update(source_id=first["source_id"],
                artifact={"path": "inputs.txt", "content_hash": binding(files["inputs.txt"])["content_hash"]})
            payload["workspace"]["content_hash"] = closure({"inputs.txt": files["inputs.txt"]})
            payload["temporal"] = {"mode": "historical-audit", "cutoff": cutoff, "limitations": []}

    def sign(payload, principal, role):
        # Use the same independently authenticated payload with a later issuance.
        return signature_at(payload, principal, role, "2026-09-10T08:00:00Z")

    body, files = SnapshotFixture.add_execution(host, source_id="execution:downstream", edit=edit, sign=sign)
    assessment = host.assessment(body, files)[1]
    assert assessment["eligible"] is eligible, assessment
    if eligible:
        assert assessment["historical_inputs"]["qualified_source_cutoffs"] == 3
        raw, _, _, _ = host.export(host.selection(body))
        assert verify_snapshot(raw, trust_policy_bytes=host.policy_path.read_bytes())["valid"]
    else:
        assert assessment["reason"] == "temporal_future_publication"


def signature_at(payload, principal, role, issued, expires="2027-01-01T00:00:00Z"):
    import hashlib
    import hmac

    from tests._execution_fixture import KEYS

    result = late_signature(payload, principal, role)
    auth = result["authentication"]
    auth.update(issued_at=issued, expires_at=expires)
    del auth["signature"]
    auth["signature"] = hmac.new(bytes.fromhex(KEYS[principal]), b"evidence-attestation/v1\0" +
        canonical({"payload": payload, "authentication": auth}), hashlib.sha256).hexdigest()
    return result


def test_execution_authority_expiry_is_rechecked_after_input_qualification(host, monkeypatch):
    def sign(payload, principal, role):
        return signature_at(payload, principal, role, "2026-09-10T05:00:00Z", "2026-09-10T05:00:01Z")
    body, files = host.history(sign=sign)
    initial = host.assessment(body, files)[1]
    assert initial["eligible"], initial
    module = host.workspace._script("_execution_temporal")
    original = module.execution_input_context

    def finish(*args, **kwargs):
        result = original(*args, **kwargs)
        host.set_time("2026-09-10T05:00:01Z")
        return result
    monkeypatch.setattr(module, "execution_input_context", finish)
    assessment = host.assessment(body, files)[1]
    assert not assessment["eligible"] and assessment["reason"] == "execution_historical_authority_expired"
