"""Complete private native proof bytes stay bounded and exactly committed."""

import hashlib
import json
import shutil
from copy import deepcopy

import pytest

from tools.replay_qualification_native import (
    PART_BYTES,
    PAYLOAD_BYTES,
    _artifact_bytes,
    read_artifact,
    write_artifact,
)


def payload():
    return {"schema_version": "qualification-native-replay/v1",
        "reference_attempts": [{"attempt_id": f"config-{config}-case-{case}", "case_id": f"case-{case}",
            "config_ref": f"config-{config}", "receipt_sha256": "a" * 64, "input_sha256": "b" * 64,
            "original_input": {"values": ["3", "2"], "units": "count"}, "complete_provenance": "x" * 280}
            for config in range(4) for case in range(600)],
        "current": {"original_questions": ["Unchanged original question."]},
        "native_receipts": [{"attempt_id": "real-original", "final_accounting": True}]}


def retained(tmp_path):
    directory = tmp_path / "proof"
    value = payload()
    summary = write_artifact(value, directory)
    return directory, value, summary


def test_complete_2400_inventory_over_stdout_bound_roundtrips_in_private_record_sized_parts(tmp_path):
    directory, original, summary = retained(tmp_path)
    raw = _artifact_bytes(original)
    assert 1_048_576 < len(raw) <= PAYLOAD_BYTES
    assert len(_artifact_bytes(summary)) <= PART_BYTES
    assert read_artifact(summary, directory) == original
    manifest = json.loads((directory / "manifest.json").read_bytes())
    assert manifest["payload_bytes"] == len(raw)
    assert manifest["payload_sha256"] == hashlib.sha256(raw).hexdigest()
    assert sum(item["bytes"] for item in manifest["parts"]) == len(raw)
    assert all(0 < item["bytes"] <= PART_BYTES for item in manifest["parts"])
    assert all(path.stat().st_mode & 0o077 == 0 for path in directory.iterdir())
    assert len(read_artifact(summary, directory)["reference_attempts"]) == 2400


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "reordered", "altered", "truncated", "symlink", "public"])
def test_part_custody_and_exact_inventory_refuse_changed_or_omitted_original_bytes(tmp_path, change):
    directory, _original, summary = retained(tmp_path)
    part = directory / "part-0000.bin"
    if change == "missing":
        part.unlink()
    elif change == "extra":
        (directory / "unexpected.bin").write_bytes(b"extra")
    elif change in {"duplicate", "reordered"}:
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        if change == "duplicate":
            manifest["parts"][1] = deepcopy(manifest["parts"][0])
        else:
            manifest["parts"][0], manifest["parts"][1] = manifest["parts"][1], manifest["parts"][0]
        manifest_path.chmod(0o600)
        manifest_path.write_bytes(_artifact_bytes(manifest))
        manifest_path.chmod(0o400)
        # Even a coordinated changed-manifest commitment cannot drop inventory.
        summary["manifest"].update(bytes=manifest_path.stat().st_size,
            sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest())
    elif change == "symlink":
        copy = tmp_path / "copy.bin"
        copy.write_bytes(part.read_bytes())
        part.unlink()
        part.symlink_to(copy)
    elif change == "public":
        part.chmod(0o644)
    else:
        raw = part.read_bytes()
        part.chmod(0o600)
        part.write_bytes(raw[:-1] if change == "truncated" else b"!" + raw[1:])
        part.chmod(0o400)
    with pytest.raises((ValueError, OSError)):
        read_artifact(summary, directory)


def test_wrong_route_manifest_path_and_parent_symlink_cannot_replace_original_custody(tmp_path):
    directory, _original, summary = retained(tmp_path)
    with pytest.raises(ValueError, match="frozen route"):
        read_artifact(summary, tmp_path)
    link = tmp_path / "alias"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        read_artifact(summary, link)


def test_legacy_large_stdout_body_and_changed_original_manifest_are_not_artifact_proof(tmp_path):
    directory, original, summary = retained(tmp_path)
    with pytest.raises(ValueError, match="complete artifact commitment"):
        read_artifact(original, directory)
    summary["manifest"]["sha256"] = "f" * 64
    with pytest.raises(ValueError, match="manifest changed"):
        read_artifact(summary, directory)


def test_existing_aggregate_bound_and_no_replacement_of_previous_artifacts(tmp_path):
    with pytest.raises(ValueError, match="existing aggregate bound"):
        write_artifact({"too_large": "x" * PAYLOAD_BYTES}, tmp_path / "oversized")
    directory, original, _summary = retained(tmp_path)
    with pytest.raises(FileExistsError):
        write_artifact(original, directory)


def test_split_unicode_bytes_and_empty_result_preserve_exact_canonical_payload(tmp_path):
    value = {"question": "π" * PART_BYTES, "native_receipts": [], "reference_attempts": []}
    directory = tmp_path / "unicode"
    assert read_artifact(write_artifact(value, directory), directory) == value


def test_parent_swap_between_parts_refuses_even_byte_identical_replacement(monkeypatch, tmp_path):
    from tools import replay_qualification_native as owner

    directory, _original, summary = retained(tmp_path)
    replacement = tmp_path / "replacement"
    shutil.copytree(directory, replacement)
    read = owner._private_part
    swapped = False
    def change_parent(path, maximum, **options):
        nonlocal swapped
        raw = read(path, maximum, **options)
        if path == "part-0000.bin" and not swapped:
            swapped = True
            directory.rename(tmp_path / "original-held")
            replacement.rename(directory)
        return raw
    monkeypatch.setattr(owner, "_private_part", change_parent)
    with pytest.raises(ValueError, match="directory changed|ancestor changed"):
        read_artifact(summary, directory)


def test_journal_capture_refuses_events_changed_since_native_owner_authentication(tmp_path):
    from types import SimpleNamespace

    from tools.replay_qualification_native import journal_snapshot

    directory = tmp_path / "runs" / "business"
    directory.mkdir(parents=True)
    event = {"event_id": "evt-0001", "data": {"record": {"kind": "original", "value": "changed"}}}
    path = directory / "events.jsonl"
    path.write_text(json.dumps(event) + "\n")
    path.chmod(0o600)
    session = SimpleNamespace(root=tmp_path, run_id="business")
    with pytest.raises(ValueError, match="changed after original owner"):
        journal_snapshot(session, [{"kind": "original", "value": "authenticated", "event_ref": "evt-0001"}])


@pytest.mark.parametrize("index,field", [(0,"source_path"),(0,"state_path"),(1,"source_path"),(1,"state_path")])
def test_each_main_and_child_journal_and_state_is_reread_at_actual_closing(tmp_path, index, field):
    from tools.replay_qualification_native import verify_journal_closing

    snapshots = []
    for number in range(2):
        files = {}
        for name, key in (("events", "source"),("state", "state")):
            p = tmp_path / f"{number}-{name}.json"
            p.write_bytes(b'{"original":true}\n')
            p.chmod(0o600)
            files[key + "_path"] = str(p)
            files[key + "_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
        snapshots.append(files)
    verify_journal_closing(snapshots)
    from pathlib import Path
    changed = Path(snapshots[index][field])
    changed.write_bytes(b'{"changed":true}\n')
    with pytest.raises(ValueError, match="changed before closing"):
        verify_journal_closing(snapshots)


@pytest.mark.parametrize("changed", [False, True])
def test_child_current_and_records_share_one_owner_observation_and_close_again(monkeypatch, changed):
    from types import SimpleNamespace

    from tools import replay_qualification_native as producer

    records = [{"kind": "original", "value": "authenticated"}]
    finished = []
    observed = SimpleNamespace(finish=lambda: finished.append(True))
    owner = SimpleNamespace(_observe_current=lambda child: ({"basis_id": "original"}, observed, [], records))
    child = SimpleNamespace(run_id="child", current=lambda: pytest.fail("separate child current read"))
    runtime = SimpleNamespace(execution=SimpleNamespace(records=lambda child:
        records + [{"kind": "changed"}] if changed else records))
    monkeypatch.setattr(producer, "_native_receipts", lambda runtime, rows: ([], []))
    monkeypatch.setattr(producer, "journal_snapshot", lambda child, rows: {"records": rows})
    if changed:
        with pytest.raises(ValueError, match="additional current and journal changed"):
            producer.additional_snapshot(runtime, child, owner)
    else:
        result = producer.additional_snapshot(runtime, child, owner)
        assert result["current"] == {"basis_id": "original"}
        assert result["journal"]["records"] == records
    assert finished == [True]
