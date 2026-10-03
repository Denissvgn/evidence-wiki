"""Intent-bound immutable staging, quota accounting and interrupted publication."""

from __future__ import annotations

import copy
import stat

import pytest

from evidence_wiki import mutation_store as module
from evidence_wiki.mutation_store import MutationStore, MutationStoreError
from tests.test_mutation_store import intent, request_directory
from tests.test_mutation_store import roots as roots


def test_staged_bytes_exceed_metadata_bound_and_replay_exactly(roots):
    root, control = roots
    value = intent(root)
    raw = b"output" * 200_000
    with MutationStore(root, control, value).locked() as store:
        store.prepare(value)
        descriptor = store.stage_blob("rendered-output", raw)
        assert descriptor["bytes"] > module.MAX_RECORD_BYTES
        assert store.stage_blob("rendered-output", raw) == descriptor
        assert store.read_stage(descriptor) == raw
        assert store.staged("rendered-output") == descriptor
        with pytest.raises(MutationStoreError) as error:
            store.stage_blob("rendered-output", b"changed")
        assert error.value.error_code == "MUTATION_REQUEST_CONFLICT"
    with MutationStore(root, control, value).locked() as store:
        assert store.read_stage(descriptor) == raw
    assert not (root / "rendered-output").exists()


@pytest.mark.parametrize("field,replacement", [
    ("name", "../escape"), ("name", "other"), ("bytes", True), ("bytes", -1),
    ("sha256", "sha256:" + "1" * 64), ("request_id", "other"),
    ("input_sha256", "sha256:" + "2" * 64), ("artifact_id", "sha256:" + "3" * 64),
])
def test_descriptor_substitution_refuses(roots, field, replacement):
    root, control = roots
    value = intent(root)
    with MutationStore(root, control, value).locked() as store:
        store.prepare(value)
        descriptor = store.stage_blob("output", b"exact")
        changed = copy.deepcopy(descriptor)
        changed[field] = replacement
        with pytest.raises(MutationStoreError):
            store.read_stage(changed)
        assert store.read_stage(descriptor) == b"exact"


def test_staged_descriptor_cannot_cross_request(roots):
    root, control = roots
    first, second = intent(root), intent(root, "second")
    with MutationStore(root, control, first).locked() as store:
        store.prepare(first)
        descriptor = store.stage_blob("output", b"exact")
    with MutationStore(root, control, second).locked() as store:
        store.prepare(second)
        with pytest.raises(MutationStoreError):
            store.read_stage(descriptor)


@pytest.mark.parametrize("alias", ["payload_link", "metadata_link", "payload_hardlink", "tampered"])
def test_staged_custody_and_content_changes_refuse(roots, tmp_path, alias):
    root, control = roots
    value = intent(root)
    with MutationStore(root, control, value).locked() as store:
        store.prepare(value)
        descriptor = store.stage_blob("output", b"exact")
    leaf = request_directory(control, root) / (".blob-" + descriptor["artifact_id"][7:])
    target = leaf.with_name(leaf.name + ".json") if alias == "metadata_link" else leaf
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-marker")
    outside.chmod(0o600)
    if alias == "tampered":
        target.write_bytes(b"other")
    else:
        target.unlink()
        if alias == "payload_hardlink":
            target.hardlink_to(outside)
        else:
            target.symlink_to(outside)
    with pytest.raises(MutationStoreError), MutationStore(root, control, value).locked() as store:
        store.read_stage(descriptor)
    assert outside.read_bytes() == b"outside-marker"


def test_staging_requires_intent_and_bounded_known_names(roots):
    root, control = roots
    with MutationStore(root, control).locked("request") as store:
        with pytest.raises(MutationStoreError):
            store.stage_blob("output", b"exact")
        store.prepare(intent(root))
        for name in ("../outside", "", "a/b", 3):
            with pytest.raises(MutationStoreError):
                store.stage_blob(name, b"exact")
        with pytest.raises(MutationStoreError):
            store.stage_blob("oversized", b"x" * (module.MAX_STAGE_BLOB_BYTES + 1))


def test_slot_and_aggregate_quotas_refuse_before_publication(roots, monkeypatch):
    root, control = roots
    value = intent(root)
    with MutationStore(root, control, value).locked() as store:
        store.prepare(value)
        monkeypatch.setattr(module, "MAX_STAGE_BLOBS", 2)
        store.stage_blob("first", b"a")
        store.stage_blob("second", b"b")
        before = set(module.os.listdir(store._request))
        with pytest.raises(MutationStoreError):
            store.stage_blob("third", b"c")
        assert set(module.os.listdir(store._request)) == before
        monkeypatch.setattr(module, "MAX_STAGE_BLOBS", 4096)
        monkeypatch.setattr(module, "MAX_STAGE_BYTES", sum(store._stage_sizes.values()))
        with pytest.raises(MutationStoreError):
            store.stage_blob("aggregate", b"c")
        assert set(module.os.listdir(store._request)) == before


def test_retained_temporaries_are_private_bounded_and_count_toward_quota(roots, monkeypatch):
    root, control = roots
    value = intent(root)
    with MutationStore(root, control, value).locked() as store:
        store.prepare(value)
    directory = request_directory(control, root)
    temporary = directory / (".mutation-" + "1" * 32)
    temporary.write_bytes(b"retained staging bytes")
    temporary.chmod(0o600)
    monkeypatch.setattr(module, "MAX_STAGE_BYTES", temporary.stat().st_size - 1)
    with pytest.raises(MutationStoreError) as error, MutationStore(root, control, value).locked():
        pass
    assert error.value.details == {"reason": "stage_aggregate_bound"}
    assert temporary.read_bytes() == b"retained staging bytes"
    monkeypatch.setattr(module, "MAX_STAGE_BYTES", 256 * 1024 * 1024)
    temporary.chmod(0o644)
    with pytest.raises(MutationStoreError), MutationStore(root, control, value).locked():
        pass


@pytest.mark.parametrize("failure_at", [1, 2])
def test_directory_fsync_interruption_never_returns_unproven_descriptor(roots, monkeypatch, failure_at):
    root, control = roots
    value = intent(root)
    with MutationStore(root, control, value).locked() as store:
        store.prepare(value)
        original = module.os.fsync
        calls = 0

        def interrupted(descriptor):
            nonlocal calls
            if descriptor == store._request and stat.S_ISDIR(module.os.fstat(descriptor).st_mode):
                calls += 1
                if calls == failure_at:
                    raise OSError("synthetic directory publication interruption")
            return original(descriptor)

        monkeypatch.setattr(module.os, "fsync", interrupted)
        with pytest.raises(MutationStoreError) as error:
            store.stage_blob("clock", b"frozen-clock")
        assert error.value.error_code == "MUTATION_COMMIT_UNKNOWN"
        monkeypatch.setattr(module.os, "fsync", original)
        if failure_at == 1:
            with pytest.raises(MutationStoreError):
                store.staged("clock")
        else:
            descriptor = store.staged("clock")
            assert store.read_stage(descriptor) == b"frozen-clock"

        def still_uncertain(descriptor):
            if descriptor == store._request:
                raise OSError("synthetic directory uncertainty")
            return original(descriptor)

        monkeypatch.setattr(module.os, "fsync", still_uncertain)
        with pytest.raises(MutationStoreError) as error:
            store.stage_blob("clock", b"frozen-clock")
        assert error.value.error_code == "MUTATION_COMMIT_UNKNOWN"
        monkeypatch.setattr(module.os, "fsync", original)
        descriptor = store.stage_blob("clock", b"frozen-clock")
        assert store.staged("clock") == descriptor
    assert (root / "input.yml").read_bytes() == b"schema_version: '1.0'\nquestions: []\n"
