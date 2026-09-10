"""Frozen-byte integrity, independent authority, and atomic current-use decisions."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from evidence_wiki import verify_snapshot
from evidence_wiki.errors import SourceError
from tests._execution_fixture import KEYS, authenticate, binding, canonical, identifier
from tests._snapshot_fixture import SnapshotFixture


@pytest.fixture
def host(tmp_path, monkeypatch):
    return SnapshotFixture(tmp_path, monkeypatch)


def seal(bundle):
    bundle["snapshot_id"] = identifier("evidence-snapshot-manifest/v1", bundle["manifest"])
    payload = bundle["registration"]["payload"]
    payload["body"]["node_id"] = bundle["snapshot_id"]
    payload["body"]["parents"] = [item["source_revision"] for item in bundle["manifest"]["examples"]]
    bundle["registration"] = authenticate(payload, "owner", "usage")
    return canonical(bundle)


def rewrite_execution(bundle, change):
    """Rebind the host's container so the independent execution checks must run."""
    manifest = bundle["manifest"]
    old_revision = manifest["examples"][0]["source_revision"]
    source = next(item for item in manifest["sources"] if item["source_revision"] == old_revision)
    path = source["descriptor"]["evidence_root"] + "/execution-record.json"
    old_blob = source["files"][path]["content_hash"]
    record = json.loads(base64.b64decode(bundle["blobs"][old_blob]))
    change(record)
    data = canonical(record)
    source["files"][path] = binding(data)
    bundle["blobs"][binding(data)["content_hash"]] = base64.b64encode(data).decode()
    del bundle["blobs"][old_blob]
    revision = identifier("evidence-artifact-closure/v1", source["files"])
    source["source_revision"] = revision
    source["grant"]["payload"]["source_revision"] = revision
    source["grant"] = authenticate(source["grant"]["payload"], "owner", "usage")
    source["scrub"]["payload"]["sanitized_revision"] = revision
    source["scrub"] = authenticate(source["scrub"]["payload"], "runner", "scrubber")
    for node in manifest["lineage"]:
        if node["node_id"] == old_revision:
            node["node_id"] = revision
    manifest["lineage"].sort(key=lambda item: item["node_id"])
    manifest["sources"].sort(key=lambda item: item["source_revision"])
    manifest["examples"][0]["source_revision"] = revision
    manifest["examples"][0]["receipt_id"] = record["selected_receipt_id"]
    manifest["selection"]["source_revisions"] = [revision]
    return seal(bundle)


def test_deterministic_order_retry_and_complete_history(host):
    first, originals = host.add_execution()
    second, _ = host.add_execution(source_id="execution:second")
    request = host.selection(second, first)
    preparation = host.workspace.snapshots.prepare(request)
    reordered = {**request, "source_revisions": list(reversed(request["source_revisions"]))}
    assert host.workspace.snapshots.prepare(reordered) == preparation
    data, preparation, command, result = host.export(request)
    repeated = host.workspace.snapshots.export(reordered, registration_request_id=command["payload"]["request_id"])
    assert not repeated["created"] and repeated["content_hash"] == result["content_hash"]
    assert (host.root / repeated["path"]).read_bytes() == data
    bundle = json.loads(data)
    assert verify_snapshot(data, trust_policy_bytes=host.policy_path.read_bytes())["valid"]
    assert host.workspace.snapshots.check(data)["current_use"] == "authorized"
    assert preparation["manifest"]["coverage"]["source_count"] == 3
    assert all(item["held_out_role"] == "held-out" for item in bundle["manifest"]["examples"])
    for raw in originals.values():
        assert base64.b64decode(bundle["blobs"][binding(raw)["content_hash"]]) == raw
    assert not any(secret.encode() in data for secret in KEYS.values())
    assert result["path"].endswith(preparation["snapshot_id"][7:] + ".json")


@pytest.mark.parametrize("denial", ["retrieval-only", "export", "ancestor", "normalized", "policy", "unknown"])
def test_selection_excludes_unknown_and_denied_inputs(host, denial):
    allowed, _ = host.add_execution(source_id="execution:allowed")
    values = {"source_id": "execution:denied"}
    if denial == "retrieval-only":
        values["training"] = False
    elif denial == "export":
        values["export"] = False
    elif denial in {"normalized", "policy"}:
        claim = "training_eligible: false" if denial == "normalized" else "usage_policy: laboratory"
        values["normalized"] = ("---\nsource_id: execution:denied\n" + claim + "\n---\nData\n").encode()
    elif denial == "ancestor":
        parent, files = host.source("lab:denied", export=False)
        host.transact(host.module, "deposit", parent, files)
        values["extra_parents"] = [parent["source_revision"]]
    denied, _ = host.add_execution(**values)
    if denial == "unknown":
        denied = {"source_revision": "sha256:" + "a" * 64}
    data, preparation, _, _ = host.export(host.selection(allowed, denied))
    assert preparation["manifest"]["coverage"]["excluded_count"] == 1
    assert [item["source_revision"] for item in preparation["manifest"]["examples"]] == [allowed["source_revision"]]
    assert verify_snapshot(data, trust_policy_bytes=host.policy_path.read_bytes())["valid"]
    with pytest.raises(SourceError) as exc:
        host.workspace.snapshots.prepare(host.selection(denied))
    assert exc.value.details["reason"] == "snapshot_no_eligible_examples"


def test_negative_opt_in_retains_authentic_failed_outcome(host):
    positive, _ = host.add_execution()
    negative, _ = host.add_execution(source_id="execution:failed", outcome="failed")
    request = host.selection(positive, negative)
    assert host.workspace.snapshots.prepare(request)["manifest"]["exclusions"] == [
        {"source_revision": negative["source_revision"], "reason": "snapshot_negative_not_requested"}]
    data, preparation, _, _ = host.export(host.selection(positive, negative, negatives=True))
    assert {(item["label"], item["outcome"]) for item in preparation["manifest"]["examples"]} == {
        ("positive", "passed"), ("negative", "failed")}
    assert verify_snapshot(data, trust_policy_bytes=host.policy_path.read_bytes())["valid"]


@pytest.mark.parametrize("corruption", ["blob", "manifest", "signature", "scope", "counts", "held-out", "lineage", "qualification", "coverage"])
def test_independent_verifier_detects_rebound_tampering(host, corruption):
    body, _ = host.add_execution()
    data, _, _, _ = host.export(host.selection(body))
    bundle = json.loads(data)
    if corruption == "blob":
        key = next(iter(bundle["blobs"]))
        bundle["blobs"][key] = base64.b64encode(b"changed bytes").decode()
    elif corruption == "manifest":
        bundle["manifest"]["exporter"]["version"] = "unrecognized"
    elif corruption == "signature":
        bundle["registration"]["authentication"]["signature"] = "0" * 64
    elif corruption in {"scope", "counts"}:
        def change(record):
            payload = record["receipts"][-1]["payload"]
            if corruption == "scope":
                payload["scope"]["suite_revision"] = "different-suite"
            else:
                payload["counts"]["passed"] += 1
            record["receipts"][-1] = authenticate(payload, "evaluator", "evaluator")
            record["selected_receipt_id"] = identifier("evidence-verification-receipt/v1", payload)
        data = rewrite_execution(bundle, change)
        report = verify_snapshot(data, trust_policy_bytes=host.policy_path.read_bytes())
        assert not report["valid"]
        assert report["reason"] == ("snapshot_receipt_scope_mismatch" if corruption == "scope" else "snapshot_assertion_counts_invalid")
        return
    elif corruption == "held-out":
        bundle["manifest"]["examples"][0]["held_out_role"] = "training"
    elif corruption == "lineage":
        bundle["manifest"]["lineage"][0]["parents"] = []
        bundle["manifest"]["lineage"][0]["depth"] = 9
    elif corruption == "qualification":
        bundle["manifest"]["qualifications"] = [{"worker_authentication": "verified"}]
    else:
        bundle["manifest"]["coverage"]["included_count"] = True
    encoded = canonical(bundle) if corruption == "signature" else seal(bundle)
    assert not verify_snapshot(encoded, trust_policy_bytes=host.policy_path.read_bytes())["valid"]


def test_authentic_host_cannot_supply_evaluator_signature(host):
    body, _ = host.add_execution()
    data, _, _, _ = host.export(host.selection(body))
    def change(record):
        record["receipts"][-1]["payload"]["assertions"][0]["actual"] = "invented result"
    rebound = rewrite_execution(json.loads(data), change)
    report = verify_snapshot(rebound, trust_policy_bytes=host.policy_path.read_bytes())
    assert not report["valid"] and report["reason"] == "snapshot_signature_mismatch"


def test_revocation_preserves_historical_bytes_and_denies_current_use(host):
    body, _ = host.add_execution()
    data, _, command, result = host.export(host.selection(body))
    policy = host.policy_path.read_bytes()
    host.revoke(host.module, host.parent)
    assert verify_snapshot(data, trust_policy_bytes=policy)["valid"]
    assert host.workspace.snapshots.check(data)["current_use"] == "denied"
    with pytest.raises(SourceError):
        host.workspace.snapshots.export(host.selection(body), registration_request_id=command["payload"]["request_id"])
    assert (host.root / result["path"]).read_bytes() == data
    lineage = host.workspace.usage.lineage(host.parent["source_revision"])
    assert result["snapshot_id"] in canonical(lineage).decode()


def test_external_policy_change_refuses_current_use_but_old_authority_can_verify_history(host):
    body, _ = host.add_execution()
    data, _, _, _ = host.export(host.selection(body))
    old_policy = host.policy_path.read_bytes()
    host.policy["revoked_keys"].append("evaluator-key")
    host.save_policy()
    assert verify_snapshot(data, trust_policy_bytes=old_policy)["valid"]
    assert not verify_snapshot(data, trust_policy_bytes=host.policy_path.read_bytes())["valid"]
    with pytest.raises(SourceError):
        host.workspace.snapshots.check(data)


def test_registration_is_required_and_stale_preparation_cannot_be_registered(host):
    body, _ = host.add_execution()
    request = host.selection(body)
    preparation = host.workspace.snapshots.prepare(request)
    with pytest.raises(SourceError):
        host.workspace.snapshots.export(request, registration_request_id="not-registered")
    command = host.command("register", preparation["registration"]["body"])
    other, files = host.source("lab:later")
    host.transact(host.module, "deposit", other, files)
    with pytest.raises(SourceError):
        host.workspace.usage.transact(command)
    assert not (host.root / "exports").exists()


@pytest.mark.parametrize("hazard", ["symlink-parent", "symlink-leaf", "hardlink-leaf", "different-content"])
def test_export_refuses_unsafe_or_conflicting_destinations(host, tmp_path, hazard):
    body, _ = host.add_execution()
    request = host.selection(body)
    preparation, command = host.register(request)
    output = host.root / "exports/evidence-snapshots" / (preparation["snapshot_id"][7:] + ".json")
    protected = tmp_path / "outside"
    protected.mkdir()
    existing = protected / "data.json"
    existing.write_bytes(b"preserve existing bytes")
    if hazard == "symlink-parent":
        (host.root / "exports").symlink_to(protected, target_is_directory=True)
    else:
        output.parent.mkdir(parents=True)
        if hazard == "symlink-leaf":
            output.symlink_to(existing)
        elif hazard == "hardlink-leaf":
            os.link(existing, output)
        else:
            output.write_bytes(b"different content")
    with pytest.raises(SourceError):
        host.workspace.snapshots.export(request, registration_request_id=command["payload"]["request_id"])
    assert existing.read_bytes() == b"preserve existing bytes"


def test_independent_verifier_works_without_originating_workspace(host, monkeypatch):
    body, _ = host.add_execution()
    data, _, _, _ = host.export(host.selection(body))
    policy = host.policy_path.read_bytes()
    host.root.rename(host.root.with_name("origin-moved"))
    host.policy_path.unlink()
    monkeypatch.delenv("EVIDENCE_WIKI_AUTHORITY_FILE")
    monkeypatch.delenv("EVIDENCE_WIKI_STATE_DIR")
    report = verify_snapshot(data, trust_policy_bytes=policy)
    assert report["valid"] and report["current_use"] == "not_evaluated"
    assert not verify_snapshot(data, trust_policy_bytes=canonical(json.loads(data)["manifest"]["policy"]["public_trust"]))["valid"]


def test_api_and_real_cli_roundtrip(host):
    body, _ = host.add_execution()
    request = host.selection(body)
    command = [sys.executable, "-m", "evidence_wiki.cli", "snapshot"]
    prepared = subprocess.run([*command, "prepare", "--target", str(host.root)], input=canonical(request), capture_output=True, timeout=60)
    assert prepared.returncode == 0, prepared.stderr
    assert json.loads(prepared.stdout) == host.workspace.snapshots.prepare(request)
    data, _, registration, result = host.export(request)
    repeated = subprocess.run([*command, "export", "--target", str(host.root), "--registration-request-id", registration["payload"]["request_id"]],
                              input=canonical(request), capture_output=True, timeout=60)
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["content_hash"] == result["content_hash"]
    verified = subprocess.run([*command, "verify", "--trust-policy", str(host.policy_path)], input=data, capture_output=True, timeout=60)
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout) == verify_snapshot(data, trust_policy_bytes=host.policy_path.read_bytes())
    invalid = subprocess.run([*command, "verify", "--trust-policy", str(host.policy_path)], input=b"{}", capture_output=True, timeout=60)
    assert invalid.returncode == 1 and not json.loads(invalid.stdout)["valid"]
    unused = subprocess.run([*command, "verify", "--target", str(host.root), "--trust-policy", str(host.policy_path)],
                            input=data, capture_output=True, timeout=60)
    assert unused.returncode == 2


@pytest.mark.parametrize("data", [b'{"a":1,"a":2}', b'{"x":NaN}', b"\xff", b"[]", b'{"x":' + b"[" * 100 + b"0" + b"]" * 100 + b"}"])
def test_offline_transport_is_strict_and_bounded(host, data):
    assert not verify_snapshot(data, trust_policy_bytes=host.policy_path.read_bytes())["valid"]


@pytest.mark.parametrize("packet_name", ["native-v1.json", "native-v2-current.json", "native-v2-stale.json", "native-v2-bounded.json"])
def test_snapshot_preserves_native_packet_limits_without_authority_promotion(host, packet_name):
    raw = (Path(__file__).parent / "fixtures/codebase-intake/native-packets" / packet_name).read_bytes()

    def add_packet(files, record):
        files["context.json"] = raw
        record["artifacts"].append({"path": "context.json", **binding(raw), "role": "context-packet"})
        record["records"][-1]["payload"]["model"]["context"] = {"path": "context.json", "content_hash": binding(raw)["content_hash"]}

    body, _ = host.add_execution(edit=add_packet)
    data, preparation, _, _ = host.export(host.selection(body))
    qualification = preparation["manifest"]["qualifications"][0]
    assert qualification["worker_authentication"] == "not_established"
    assert qualification["live_reconciliation"] == "not_evaluated"
    assert qualification["original"] == binding(raw)
    assert "content" not in qualification["qualifications"]["response"]
    assert verify_snapshot(data, trust_policy_bytes=host.policy_path.read_bytes())["valid"]
    bundle = json.loads(data)
    bundle["manifest"]["qualifications"][0]["live_reconciliation"] = "current"
    assert not verify_snapshot(seal(bundle), trust_policy_bytes=host.policy_path.read_bytes())["valid"]


@pytest.mark.parametrize("corruption", ["missing-log", "changed-log", "unbound-input", "history-signature"])
def test_original_references_and_historical_authority_are_mandatory(host, corruption):
    body, files = host.add_execution()
    changed = copy.deepcopy(files)
    path = json.loads(files["source-record.json"])["evidence_root"] + "/execution-record.json"
    record = json.loads(changed[path])
    log = record["records"][-1]["payload"]["logs"][0]["path"]
    if corruption == "missing-log":
        del changed[json.loads(files["source-record.json"])["evidence_root"] + "/" + log]
    elif corruption == "changed-log":
        changed[json.loads(files["source-record.json"])["evidence_root"] + "/" + log] = b"invented output\n"
    elif corruption == "unbound-input":
        payload = record["records"][-1]["payload"]
        payload["inputs"][0]["source_id"] = "lab:unrelated"
        record["records"][-1] = authenticate(payload, "runner", "generator")
        record["selected_record_id"] = identifier("evidence-execution-record/v1", payload)
        receipt = record["receipts"][-1]["payload"]
        receipt["target_record_id"] = record["selected_record_id"]
        record["receipts"][-1] = authenticate(receipt, "evaluator", "evaluator")
        record["selected_receipt_id"] = identifier("evidence-verification-receipt/v1", receipt)
    else:
        record["records"][0]["authentication"]["signature"] = "0" * 64
    changed[path] = canonical(record)
    replaced = host.deposit_changed(body, changed)
    with pytest.raises(SourceError):
        host.workspace.snapshots.prepare(host.selection(replaced, negatives=True))


@pytest.mark.parametrize("boundary", ["before-rename", "after-rename", "policy-change", "receipt-expiry"])
def test_publication_failure_is_atomic_and_retry_reconciles_existing_bytes(host, monkeypatch, boundary):
    body, files = host.add_execution()
    if boundary == "receipt-expiry":
        path = json.loads(files["source-record.json"])["evidence_root"] + "/execution-record.json"
        record = json.loads(files[path])
        envelope = record["receipts"][-1]
        envelope["authentication"]["expires_at"] = "2026-09-11T00:00:00Z"
        auth = {key: value for key, value in envelope["authentication"].items() if key != "signature"}
        envelope["authentication"]["signature"] = hmac.new(bytes.fromhex(KEYS["evaluator"]),
            b"evidence-attestation/v1\0" + canonical({"payload": envelope["payload"], "authentication": auth}), hashlib.sha256).hexdigest()
        files[path] = canonical(record)
        body = host.deposit_changed(body, files)
    request = host.selection(body)
    preparation, registration = host.register(request)
    output = host.root / "exports/evidence-snapshots" / (preparation["snapshot_id"][7:] + ".json")
    exporter = host.workspace._script("evidence_snapshots").export.__globals__
    original = exporter["publish_file"]
    original_policy = host.policy_path.read_bytes()

    class Clock:
        value = datetime(2026, 9, 10, 23, 59, 59, tzinfo=timezone.utc)

        @classmethod
        def now(cls, zone):
            return cls.value

    def interrupted(root, relative, data, expected, *, before_publish):
        if boundary == "before-rename":
            def failed():
                before_publish()
                raise OSError("interrupted before publication")
            return original(root, relative, data, expected, before_publish=failed)
        if boundary == "after-rename":
            original(root, relative, data, expected, before_publish=before_publish)
            raise OSError("ambiguous after publication")
        def changed():
            if boundary == "policy-change":
                host.policy["revoked_keys"].append("evaluator-key")
                host.save_policy()
            else:
                Clock.value = datetime(2026, 9, 11, tzinfo=timezone.utc)
            before_publish()
        return original(root, relative, data, expected, before_publish=changed)

    if boundary == "receipt-expiry":
        monkeypatch.setitem(exporter["UsageView"].revalidate.__globals__, "datetime", Clock)
    monkeypatch.setitem(exporter, "publish_file", interrupted)
    with pytest.raises(SourceError):
        host.workspace.snapshots.export(request, registration_request_id=registration["payload"]["request_id"])
    assert output.exists() == (boundary == "after-rename")
    assert not list(output.parent.glob(".normalized-*"))
    if boundary in {"before-rename", "after-rename"}:
        monkeypatch.setitem(exporter, "publish_file", original)
        retried = host.workspace.snapshots.export(request, registration_request_id=registration["payload"]["request_id"])
        assert retried["created"] == (boundary == "before-rename")
        assert verify_snapshot(output.read_bytes(), trust_policy_bytes=original_policy)["valid"]


def test_new_source_revision_preserves_existing_export_and_changes_preparation_identity(host):
    body, files = host.add_execution()
    data, preparation, registration, result = host.export(host.selection(body))
    files["normalized.md"] += b"\nAdditional source annotation.\n"
    changed = host.deposit_changed(body, files)
    updated = host.workspace.snapshots.prepare(host.selection(changed))
    assert updated["snapshot_id"] != preparation["snapshot_id"]
    assert (host.root / result["path"]).read_bytes() == data
    retried = host.workspace.snapshots.export(host.selection(body), registration_request_id=registration["payload"]["request_id"])
    assert not retried["created"] and retried["content_hash"] == result["content_hash"]


@pytest.mark.parametrize("hazard", ["symlink", "hardlink", "public", "oversized"])
def test_offline_cli_refuses_unsafe_trust_files(host, tmp_path, hazard):
    policy = tmp_path / "trust.json"
    if hazard == "symlink":
        policy.symlink_to(host.policy_path)
    elif hazard == "hardlink":
        os.link(host.policy_path, policy)
    else:
        policy.write_bytes(host.policy_path.read_bytes() if hazard == "public" else b"x" * (1024 * 1024 + 1))
        policy.chmod(0o644 if hazard == "public" else 0o600)
    result = subprocess.run([sys.executable, "-m", "evidence_wiki.cli", "snapshot", "verify", "--trust-policy", str(policy)],
                            input=b"{}", capture_output=True, timeout=60)
    assert result.returncode == 2 and b"EVIDENCE_SNAPSHOT_REFUSED" in result.stdout + result.stderr


@pytest.mark.parametrize("selection_change", ["empty", "duplicate", "too-many", "unknown-field", "nonboolean"])
def test_invalid_selection_refused_before_any_publication(host, selection_change):
    body, _ = host.add_execution()
    request = host.selection(body)
    if selection_change == "empty":
        request["source_revisions"] = []
    elif selection_change == "duplicate":
        request["source_revisions"] *= 2
    elif selection_change == "too-many":
        request["source_revisions"] = ["sha256:" + f"{i:064x}" for i in range(33)]
    elif selection_change == "unknown-field":
        request["skip_verification"] = True
    else:
        request["include_negative_examples"] = 1
    with pytest.raises(SourceError):
        host.workspace.snapshots.prepare(request)
    assert not (host.root / "exports").exists()


@pytest.mark.parametrize("mutation", ["source", "revocation"])
def test_export_lock_prevents_concurrent_host_mutation(host, monkeypatch, mutation):
    body, _ = host.add_execution()
    request = host.selection(body)
    _preparation, registration = host.register(request)
    exporter = host.workspace._script("evidence_snapshots").export.__globals__
    original = exporter["publish_file"]
    attempted = []

    def concurrent(root, relative, data, expected, *, before_publish):
        if mutation == "source":
            source, files = host.source("lab:concurrent")
            command = host.command("deposit", source)
        else:
            files = None
            command = host.command("revoke", {"source_id": host.parent["source_id"], "source_revision": host.parent["source_revision"],
                                               "scope": "revision", "reason": "owner-withdrawal"})
        with pytest.raises(SourceError) as exc:
            host.workspace.usage.transact(command, artifacts=files)
        assert exc.value.details["reason"] == "host_state_lock_unavailable"
        attempted.append(command)
        return original(root, relative, data, expected, before_publish=before_publish)

    monkeypatch.setitem(exporter, "publish_file", concurrent)
    result = host.workspace.snapshots.export(request, registration_request_id=registration["payload"]["request_id"])
    assert len(attempted) == 1
    raw = (host.root / result["path"]).read_bytes()
    assert host.workspace.snapshots.check(raw)["eligible"]
    if mutation == "revocation":
        host.workspace.usage.transact(attempted[0])
        assert not host.workspace.snapshots.check(raw)["eligible"]
        assert (host.root / result["path"]).read_bytes() == raw
