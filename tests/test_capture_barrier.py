"""Actual shared raw barrier, complete-pair visibility and namespace custody."""

import json

import pytest
import yaml

from evidence_wiki import mutation_operations as mutations
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.pack_discovery import owner
from tests.test_native_capture_revision import codec, metadata, request_capture
from tests.test_native_capture_revision import roots as roots
from tests.test_native_capture_revision import selected as selected


def test_held_actual_raw_barrier_refuses_capture_before_either_output(roots, selected):
    root, _ = roots
    locks = owner("_workspace_locks")
    with selected.activate(), locks.workspace_lock(root / "raw/.locks/acquisition.lock", timeout_seconds=0):
        request = request_capture(root, codec.from_bytes(b"Original", metadata()))
        with pytest.raises(EvidenceWikiError) as refused:
            mutations.submit(root, request)
        assert refused.value.error_code == "MUTATION_CAPTURE_ACQUISITION_CONTENDED"
    assert not (root / "raw/web/captured.txt").exists()
    assert not (root / "raw/web/captured.txt.provenance.yml").exists()


def test_native_inventory_cannot_observe_active_sidecar_only_prefix(roots, selected, monkeypatch):
    root, _ = roots
    writer, inventory, locks = owner("_usage_materialization"), owner("source_inventory"), owner("_workspace_locks")
    actual = writer.publish_file
    prefixes = []
    def zero_wait(path, **kwargs):
        return locks.workspace_lock(path, timeout_seconds=0, **kwargs)
    def publication(*args, **kwargs):
        result = actual(*args, **kwargs)
        if args[1] == "raw/web/captured.txt.provenance.yml":
            assert not (root / "raw/web/captured.txt").exists()
            with pytest.raises(locks.LockUnavailableError):
                inventory.build_records(root, yaml.safe_load((root / "research.yml").read_bytes()), {})
            prefixes.append("sidecar-only-hidden")
        return result
    with selected.activate():
        request = request_capture(root, codec.from_bytes(b"Original", metadata()))
        with monkeypatch.context() as scoped:
            scoped.setattr(inventory, "workspace_lock", zero_wait)
            scoped.setattr(writer, "publish_file", publication)
            receipt = mutations.submit(root, request)
        assert receipt["status"] == "committed", receipt
        records, _, _ = inventory.build_records(root, yaml.safe_load((root / "research.yml").read_bytes()), {})
    assert prefixes == ["sidecar-only-hidden"]
    record = next(row for row in records if row.get("raw_paths") == ["raw/web/captured.txt"])
    assert record["kind"] == "host_capture" and record["provenance"]["host_capture"]["schema_version"] == codec.CAPTURE


@pytest.mark.parametrize("namespace", ["file", "directory"])
def test_namespace_replacement_after_sidecar_blocks_original_publication(roots, selected, monkeypatch, namespace):
    root, _ = roots
    writer = owner("_usage_materialization")
    actual = writer.publish_file
    changed = []
    def publication(*args, **kwargs):
        result = actual(*args, **kwargs)
        if args[1] == "raw/web/captured.txt.provenance.yml":
            target = root / "raw/.locks" / "acquisition.lock" if namespace == "file" else root / "raw/.locks"
            saved = target.with_name(target.name + "-held")
            target.rename(saved)
            if namespace == "file":
                target.write_bytes(b"")
            else:
                target.mkdir(mode=0o700)
            changed.append((target, saved))
        return result
    with selected.activate():
        request = request_capture(root, codec.from_bytes(b"Original", metadata()))
        with monkeypatch.context() as scoped:
            scoped.setattr(writer, "publish_file", publication)
            receipt = mutations.submit(root, request)
    try:
        assert receipt["status"] in {"partial", "commit_unknown"}, receipt
        assert not (root / "raw/web/captured.txt").exists()
        assert (root / "raw/web/captured.txt.provenance.yml").exists()
        assert receipt["error"]["error_code"] == "MUTATION_CAPTURE_BARRIER_CHANGED"
    finally:
        for target, saved in changed:
            target.unlink() if namespace == "file" else target.rmdir()
            saved.rename(target)


def test_interrupted_sidecar_only_pair_is_not_an_ordinary_source_after_unlock(roots, selected, monkeypatch):
    root, _ = roots
    writer = owner("_usage_materialization")
    actual = writer.publish_file
    def publication(*args, **kwargs):
        result = actual(*args, **kwargs)
        if args[1] == "raw/web/captured.txt.provenance.yml":
            raise OSError("Controlled interruption after sidecar publication")
        return result
    with selected.activate():
        request = request_capture(root, codec.from_bytes(b"Original", metadata()))
        with monkeypatch.context() as scoped:
            scoped.setattr(writer, "publish_file", publication)
            receipt = mutations.submit(root, request)
        assert receipt["status"] in {"partial", "commit_unknown"}
        records, _, _ = owner("source_inventory").build_records(root, yaml.safe_load((root / "research.yml").read_bytes()), {})
    assert not any("raw/web/captured.txt" in row.get("raw_paths", []) for row in records)
    assert json.loads((root / "raw/web/captured.txt.provenance.yml").read_bytes())["host_capture"]["schema_version"] == codec.CAPTURE
