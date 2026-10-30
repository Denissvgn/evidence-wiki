"""Usage gates across authenticated persistence, rollback, and dependency changes."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evidence_wiki._filesystem import os
from tests._execution_fixture import authenticate, canonical, identifier
from tests._script_loader import load_isolated_module
from tests._usage_fixture import UsageFixture, require_host_storage

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template" / "scripts"


@pytest.fixture
def usage(tmp_path, monkeypatch):
    fixture = UsageFixture(tmp_path, monkeypatch)
    module = load_isolated_module("usage_contract", SCRIPTS / "_evidence_usage.py")
    return module, fixture


def check(module, fixture, revision, uses=None, **kwargs):
    with module.current_view(fixture.root, fixture.config) as view:
        return view.check(revision, uses=uses or ["training", "export"],
                          purpose=kwargs.get("purpose", "training-snapshot"), consumer=kwargs.get("consumer", "evidence-wiki"))


def state_bytes(fixture):
    return (fixture.host / "evidence-state.json").read_bytes()


def test_separate_permissions_and_exact_originals(usage):
    module, fixture = usage
    fixture.transact(module, "initialize")
    body, files = fixture.source(training=False)
    fixture.transact(module, "deposit", body, files)
    assert check(module, fixture, body["source_revision"], ["retrieval"], purpose="research")["eligible"]
    assert not check(module, fixture, body["source_revision"])["eligible"]
    with module.current_view(fixture.root, fixture.config) as view:
        assert view.state.revisions[body["source_revision"]]["files"] == files
    assert not list(fixture.root.iterdir())


def test_grant_expiring_during_read_cannot_return_an_eligible_result(usage, monkeypatch):
    module, fixture = usage
    started = datetime.now(timezone.utc)
    deadline = started + timedelta(hours=1)

    class Clock:
        value = started

        @classmethod
        def now(cls, zone):
            return cls.value

    monkeypatch.setattr(module, "datetime", Clock)
    fixture.transact(module, "initialize")
    body, files = fixture.source()
    body["grant"]["payload"]["expires_at"] = deadline.isoformat()
    body["grant"] = authenticate(body["grant"]["payload"], "owner", "usage")
    fixture.transact(module, "deposit", body, files)

    with pytest.raises(ValueError, match="usage_outside_validity"):
        with module.current_view(fixture.root, fixture.config) as view:
            assert view.check(body["source_revision"], uses=["training"],
                              purpose="training-snapshot", consumer="evidence-wiki")["eligible"]
            Clock.value = deadline


def test_materialization_rechecks_authority_before_rename(tmp_path):
    require_host_storage()
    module = load_isolated_module("usage_publication_boundary", SCRIPTS / "_usage_materialization.py")
    root = tmp_path / "workspace"
    root.mkdir()

    def expired():
        raise module.EvidenceInvalid("usage_outside_validity")

    with pytest.raises(ValueError, match="usage_outside_validity"):
        module.publish_file(root, "sources/normalized/sample.md", b"approved bytes", None,
                            before_publish=expired)
    assert not list(root.rglob("*.md"))
    assert not list(root.rglob(".normalized-*"))


@pytest.mark.parametrize("changed", ["raw", "normalized", "scrub", "grant", "command", "policy-revision", "unknown-owner", "usage-boolean", "future", "expiry", "retention"])
def test_unauthorized_bytes_refused_before_first_persistence(usage, changed):
    module, fixture = usage
    body, files = fixture.source()
    envelope = fixture.command("deposit", body)
    if changed == "raw":
        files["source-record.json"] += b" "
    elif changed == "normalized":
        files["normalized.md"] = b"PROTECTED_UNSCRUBBED_SENTINEL"
    elif changed in {"scrub", "grant"}:
        envelope["payload"]["body"][changed]["authentication"]["signature"] = "0" * 64
        envelope = authenticate(envelope["payload"], "owner", "usage")
    elif changed == "command":
        envelope["authentication"]["signature"] = "0" * 64
    elif changed == "policy-revision":
        fixture.policy["policy_revision"] = "2"
        fixture.save_policy()
    elif changed == "unknown-owner":
        fixture.policy["principals"]["owner"]["roles"] = ["assessment"]
        fixture.save_policy()
    else:
        grant = envelope["payload"]["body"]["grant"]["payload"]
        if changed == "usage-boolean":
            grant["permissions"]["training"] = 1
        elif changed == "future":
            grant["not_before"] = "2026-12-31T00:00:00Z"
        elif changed == "expiry":
            grant["expires_at"] = "2026-09-09T00:00:00Z"
        else:
            grant["retention"] = "retain-forever"
        envelope["payload"]["body"]["grant"] = authenticate(grant, "owner", "usage")
        envelope = authenticate(envelope["payload"], "owner", "usage")
    with pytest.raises(ValueError):
        module.transact(fixture.root, fixture.config, envelope, files)
    assert {path.name for path in fixture.host.iterdir()} == {"authority.json"}
    assert not list(fixture.root.iterdir())


def test_revocation_survives_workspace_rollback_and_reauthorization(usage):
    module, fixture = usage
    fixture.transact(module, "initialize")
    body, files = fixture.source()
    fixture.transact(module, "deposit", body, files)
    fixture.revoke(module, body)
    fixture.transact(module, "authorize", body)
    (fixture.root / "old-record.json").write_bytes(canonical(body))
    verdict = check(module, fixture, body["source_revision"])
    assert verdict["reasons"] == ["source_revision_revoked"]
    assert len(json.loads(state_bytes(fixture))["events"]) == 4


@pytest.mark.parametrize("whole", [False, True])
def test_revision_and_whole_source_revocation(usage, whole):
    module, fixture = usage
    fixture.transact(module, "initialize")
    first, files = fixture.source()
    fixture.transact(module, "deposit", first, files)
    fixture.revoke(module, first, whole=whole)
    corrected, files = fixture.source(normalized=b"A separately sanitized correction.\n")
    fixture.transact(module, "deposit", corrected, files)
    assert not check(module, fixture, first["source_revision"])["eligible"]
    assert check(module, fixture, corrected["source_revision"])["eligible"] is (not whole)


def test_all_ancestors_restrict_derived_and_downstream_nodes(usage):
    module, fixture = usage
    fixture.transact(module, "initialize")
    first, files = fixture.source(export=False)
    fixture.transact(module, "deposit", first, files)
    derived, files = fixture.source("lab:derived", parents=[first["source_revision"]])
    fixture.transact(module, "deposit", derived, files)
    assert not check(module, fixture, derived["source_revision"])["eligible"]
    previous = derived["source_revision"]
    for kind in ("snapshot", "dataset", "adapter", "model"):
        node = identifier("host-reference/v1", {"kind": kind, "parent": previous})
        fixture.transact(module, "register", {"kind": kind, "node_id": node, "parents": [previous]})
        previous = node
    assert not check(module, fixture, previous)["eligible"]
    with module.current_view(fixture.root, fixture.config) as view:
        full = view.lineage(first["source_revision"])
        assert full["complete"] and len(full["nodes"]) == 6
        assert not view.lineage(first["source_revision"], limit=2)["complete"]
        assert not view.lineage("sha256:" + "0" * 64)["found"]


def test_idempotency_and_stale_checkpoint_preserve_state(usage):
    module, fixture = usage
    initial = fixture.command("initialize")
    receipt = module.transact(fixture.root, fixture.config, initial)
    before = state_bytes(fixture)
    assert module.transact(fixture.root, fixture.config, initial) == receipt
    assert state_bytes(fixture) == before
    body, files = fixture.source()
    stale = fixture.command("deposit", body)
    with pytest.raises(ValueError, match="usage_checkpoint_changed"):
        module.transact(fixture.root, fixture.config, stale, files)
    assert state_bytes(fixture) == before
    conflict = copy.deepcopy(initial["payload"])
    conflict["action"], conflict["body"] = "deposit", body
    with pytest.raises(ValueError, match="usage_request_conflict"):
        module.transact(fixture.root, fixture.config, authenticate(conflict, "owner", "usage"), files)
    assert state_bytes(fixture) == before


def test_current_policy_key_revocation_blocks_previously_accepted_grant(usage):
    module, fixture = usage
    fixture.transact(module, "initialize")
    body, files = fixture.source()
    fixture.transact(module, "deposit", body, files)
    assert check(module, fixture, body["source_revision"])["eligible"]
    fixture.policy["revoked_keys"] = ["runner-key"]
    fixture.save_policy()
    assert check(module, fixture, body["source_revision"])["reasons"] == ["authentication_revoked"]


def test_purpose_consumer_and_export_are_independent(usage):
    module, fixture = usage
    fixture.transact(module, "initialize")
    body, files = fixture.source(export=False)
    fixture.transact(module, "deposit", body, files)
    assert check(module, fixture, body["source_revision"], ["training"])["eligible"]
    assert not check(module, fixture, body["source_revision"])["eligible"]
    assert not check(module, fixture, body["source_revision"], ["training"], purpose="undeclared")["eligible"]
    assert not check(module, fixture, body["source_revision"], ["training"], consumer="untrusted")["eligible"]


def test_shared_reader_blocks_revocation_without_mixed_checkpoint(usage):
    module, fixture = usage
    fixture.transact(module, "initialize")
    body, files = fixture.source()
    fixture.transact(module, "deposit", body, files)
    before = state_bytes(fixture)
    with module.current_view(fixture.root, fixture.config) as view:
        with pytest.raises(ValueError, match="host_state_lock_unavailable"):
            fixture.revoke(module, body)
        assert view.check(body["source_revision"], uses=["retrieval"], purpose="research", consumer="evidence-wiki")["eligible"]
        assert state_bytes(fixture) == before
    fixture.revoke(module, body)
    assert not check(module, fixture, body["source_revision"])["eligible"]


def test_clock_rollback_cannot_append(usage, monkeypatch):
    module, fixture = usage
    fixture.transact(module, "initialize")
    before = state_bytes(fixture)

    class Earlier(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 10, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(module, "datetime", Earlier)
    body, files = fixture.source()
    with pytest.raises(ValueError, match="host_state_clock_regressed"):
        fixture.transact(module, "deposit", body, files)
    assert state_bytes(fixture) == before


@pytest.mark.parametrize("unsafe", ["workspace", "public-directory", "symlink", "state-symlink", "state-hardlink", "state-fifo", "public-state", "public-lock"])
def test_unsafe_host_paths_fail_closed(usage, monkeypatch, unsafe):
    module, fixture = usage
    fixture.transact(module, "initialize")
    state = fixture.host / "evidence-state.json"
    if unsafe == "workspace":
        monkeypatch.setenv("EVIDENCE_WIKI_STATE_DIR", str(fixture.root))
    elif unsafe == "public-directory":
        os.chmod(fixture.host, 0o755)
    elif unsafe == "symlink":
        alias = fixture.host.parent / "alias"
        alias.symlink_to(fixture.host, target_is_directory=True)
        monkeypatch.setenv("EVIDENCE_WIKI_STATE_DIR", str(alias))
    elif unsafe == "state-hardlink":
        os.link(state, fixture.host / "linked")
    elif unsafe == "state-fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("This host has no filesystem FIFO object; native reparse/device refusals are covered separately.")
        state.unlink()
        os.mkfifo(state, 0o600)
    elif unsafe == "state-symlink":
        state.rename(fixture.host / "saved")
        state.symlink_to(fixture.host / "saved")
    elif unsafe == "public-state":
        os.chmod(state, 0o644)
    else:
        os.chmod((fixture.host / "evidence-state.lock"), 0o644)
    with pytest.raises(ValueError):
        with module.current_view(fixture.root, fixture.config):
            pytest.fail("unsafe state accepted")


def test_host_policy_changed_during_read_refuses(usage):
    module, fixture = usage
    fixture.transact(module, "initialize")
    with pytest.raises(ValueError, match="host_trust_changed"):
        with module.current_view(fixture.root, fixture.config):
            fixture.policy["revoked_keys"] = ["owner-key"]
            fixture.save_policy()


def test_unavailable_reads_do_not_initialize_state(usage):
    module, fixture = usage
    with pytest.raises(ValueError):
        with module.current_view(fixture.root, fixture.config):
            pytest.fail("uninitialized state accepted")
    assert {path.name for path in fixture.host.iterdir()} == {"authority.json"}


def test_state_parser_has_a_separate_budget_for_each_event(usage):
    module, _fixture = usage
    document = {"schema_version": module.STATE_SCHEMA, "state_id": "sample",
                "workspace_binding": "sample", "events": [[0] * 23000 for _ in range(12)]}
    encoded = json.dumps(document).encode()
    with pytest.raises(ValueError, match="invalid_evidence_json"):
        module.json_document(encoded)
    assert module._usage_state_document(encoded) == document
    # Canonical serialization sorts events ahead of the scalar headers.
    assert module._usage_state_document(json.dumps(document, sort_keys=True).encode()) == document


def test_repeated_state_events_replay_through_existing_authority(usage):
    module, fixture = usage
    fixture.transact(module, "initialize")
    body, files = fixture.source()
    fixture.transact(module, "deposit", body, files)
    for _ in range(11):
        fixture.transact(module, "authorize", body)
    restored = module.UsageState(fixture.root, "laboratory", state_bytes(fixture))
    assert len(restored.events) == 13
    assert restored.checkpoint == fixture.checkpoint
    assert check(module, fixture, body["source_revision"])["eligible"]


def test_state_parser_bounds_each_event_and_header(usage):
    module, _fixture = usage
    base = {"schema_version": module.STATE_SCHEMA, "state_id": "sample",
            "workspace_binding": "sample", "events": []}
    for key, value in [("events", [[0] * 250000]), ("state_id", [0] * 250000)]:
        with pytest.raises(ValueError, match="invalid_evidence_json"):
            module._usage_state_document(json.dumps({**base, key: value}).encode())
    nested = 0
    for _ in range(62):
        nested = [nested]
    module._usage_state_document(json.dumps({**base, "events": [nested]}).encode())
    with pytest.raises(ValueError, match="invalid_evidence_json"):
        module._usage_state_document(json.dumps({**base, "events": [[nested]]}).encode())


def test_state_parser_enforces_byte_and_event_bounds_before_decoding(usage, monkeypatch):
    module, _fixture = usage
    encoded = b'{"events":[],"schema_version":"s","state_id":"i","workspace_binding":"b"}'
    monkeypatch.setattr(module, "MAX_STATE_BYTES", len(encoded))
    assert module._usage_state_document(encoded)["events"] == []
    with pytest.raises(ValueError, match="invalid_evidence_json"):
        module._usage_state_document(encoded + b" ")
    monkeypatch.setattr(module, "MAX_STATE_BYTES", 4096)
    monkeypatch.setattr(module, "MAX_EVENTS", 2)
    encoded = encoded.replace(b'"events":[]', b'"events":[0,0]')
    assert module._usage_state_document(encoded)["events"] == [0, 0]
    # The extra event is refused before attempting to decode its invalid JSON.
    with pytest.raises(ValueError, match="invalid_evidence_json"):
        module._usage_state_document(encoded.replace(b'[0,0]', b'[0,0,invalid]'))


@pytest.mark.parametrize("encoded", [
    b'{"schema_version":"s","state_id":"i","workspace_binding":"b","events":[],"events":[]}',
    b'{"schema_version":"s","state_id":"i","workspace_binding":"b","events":[{"a":0,"a":1}]}',
    b'{"schema_version":"s","state_id":"i","workspace_binding":"b","events":[NaN]}',
    b'{"schema_version":"s","state_id":"i","workspace_binding":"b","events":[Infinity]}',
    b'{"schema_version":"s","state_id":"i","workspace_binding":"b","events":[-Infinity]}',
    b'{"schema_version":"s","state_id":"i","workspace_binding":"b","events":["\xff"]}',
    b'{"events":[],}', b'{"events":[0,]}', b'{"events":[0 1]}',
    b'{"events":[]', b'[]', b'{}',
    b'{"events":[],"schema_version":"s","state_id":"i","workspace_binding":"b"} false',
])
def test_state_parser_refuses_malformed_documents(usage, encoded):
    module, _fixture = usage
    with pytest.raises(ValueError, match="invalid_evidence_json"):
        module._usage_state_document(encoded)



def _state_value_node_count(value):
    pending = [value]
    count = 0
    while pending:
        item = pending.pop()
        count += 1
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return count


def test_state_writer_refuses_unreadable_initial_event(usage, monkeypatch):
    module, fixture = usage
    monkeypatch.setattr(module, "_MAX_STATE_VALUE_NODES", 1)
    with pytest.raises(ValueError, match="invalid_evidence_json"):
        fixture.transact(module, "initialize")
    assert not (fixture.host / "evidence-state.json").exists()


def test_state_writer_refusal_preserves_readable_previous_generation(usage, monkeypatch):
    module, fixture = usage
    fixture.transact(module, "initialize")
    before = state_bytes(fixture)
    event = json.loads(before)["events"][0]
    monkeypatch.setattr(module, "_MAX_STATE_VALUE_NODES", _state_value_node_count(event))
    body, files = fixture.source()
    with pytest.raises(ValueError, match="invalid_evidence_json"):
        fixture.transact(module, "deposit", body, files)
    assert state_bytes(fixture) == before
    restored = module.UsageState(fixture.root, "laboratory", before)
    assert restored.checkpoint == fixture.checkpoint
    assert len(restored.events) == 1


def test_state_event_structure_budget_matches_reader(usage, monkeypatch):
    module, fixture = usage
    fixture.transact(module, "initialize")
    encoded = state_bytes(fixture)
    event = json.loads(encoded)["events"][0]
    count = _state_value_node_count(event)
    monkeypatch.setattr(module, "_MAX_STATE_VALUE_NODES", count)
    module._bound_usage_state_value(event, 2)
    assert module._usage_state_document(encoded)["events"] == [event]
    monkeypatch.setattr(module, "_MAX_STATE_VALUE_NODES", count - 1)
    with pytest.raises(ValueError, match="invalid_evidence_json"):
        module._bound_usage_state_value(event, 2)
    with pytest.raises(ValueError, match="invalid_evidence_json"):
        module._usage_state_document(encoded)
    monkeypatch.setattr(module, "_MAX_STATE_VALUE_NODES", 250_000)
    nested = 0
    for _ in range(62):
        nested = [nested]
    module._bound_usage_state_value(nested, 2)
    with pytest.raises(ValueError, match="invalid_evidence_json"):
        module._bound_usage_state_value([nested], 2)
