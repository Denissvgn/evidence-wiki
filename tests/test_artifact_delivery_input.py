"""Private publication input cannot be borrowed from data or replayed dispatch."""

import copy

import pytest

from evidence_wiki import artifact_delivery_input as custody
from evidence_wiki.errors import UsageError
from evidence_wiki.resolution_operations import (
    _input_type,
    _OwnedArtifactInput,
    _OwnedReadabilityInput,
    _OwnedResolution,
)


def test_publication_requires_actual_fresh_native_dispatch_before_any_owner_read(monkeypatch):
    monkeypatch.setattr(custody.native, "retained", lambda *a: pytest.fail("Unowned scope read the journal."))
    with pytest.raises(UsageError) as caught:
        with custody.publication_scope(None, "publication", {}, None):
            pytest.fail("Unowned scope yielded.")
    assert caught.value.details["reason"] == "publication_dispatch_not_fresh"


def test_dispatch_is_single_use_and_closes_even_on_a_refusal(monkeypatch):
    monkeypatch.setattr(custody.native, "retained", lambda *a: pytest.fail("Invalid session read the journal."))
    with custody.effect_dispatch(None, "publication", {}, None):
        lease = custody._DISPATCH.get()
        with pytest.raises(UsageError) as first:
            with custody.publication_scope(None, "publication", {}, None):
                pytest.fail("Invalid session yielded.")
        assert first.value.details["reason"] == "publication_native_scope_required"
        assert lease.consumed
        with pytest.raises(UsageError) as second:
            with custody.publication_scope(None, "publication", {}, None):
                pytest.fail("Consumed dispatch yielded.")
        assert second.value.details["reason"] == "publication_dispatch_not_fresh"
    assert not lease.active and custody._DISPATCH.get() is None


def test_nested_dispatch_and_untyped_admission_refuse_without_current_evidence(monkeypatch):
    monkeypatch.setattr(custody.native, "retained", lambda *a: pytest.fail("Invalid admission read the journal."))
    with custody.effect_dispatch(None, "publication", {}, None), pytest.raises(UsageError):
        with custody.effect_dispatch(None, "publication", {}, None):
            pytest.fail("Nested native dispatch yielded.")
    with pytest.raises(UsageError):
        custody.constraints(None, None, None, None, {"accepted": True})
    assert _input_type(None) is _OwnedResolution
    with pytest.raises(UsageError):
        _input_type({"current": True})


def test_owned_publication_shape_is_distinct_and_has_no_public_document():
    assert _OwnedArtifactInput is not _OwnedReadabilityInput
    owned = _OwnedArtifactInput(None, {}, "basis", "proof", 0, _input_admission=None)
    with pytest.raises(UsageError) as caught:
        owned.document()
    assert caught.value.details["reason"] == "private_publication_input_is_not_current_resolution"


def test_admission_cannot_be_copied_or_used_after_its_actual_scope(monkeypatch):
    monkeypatch.setattr(custody.native, "retained", lambda *a: [])
    admission = custody._PublicationAdmission(None, "publication", {}, None)
    for clone in (copy.copy, copy.deepcopy):
        with pytest.raises(UsageError):
            clone(admission)
    with pytest.raises(UsageError) as caught:
        admission.held(None)
    assert caught.value.details["reason"] == "publication_custody_not_held"


def test_unowned_publication_cannot_create_even_a_temporary_file(tmp_path):
    from evidence_wiki._filesystem import os
    from evidence_wiki.source_delivery import _publish

    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        pytest.skip("Native descriptor publication is unavailable.")
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(UsageError):
            _publish(descriptor, "brief.md", b"Unapproved content.", custody={"accepted": True})
    finally:
        os.close(descriptor)
    assert list(tmp_path.iterdir()) == []


def test_no_replace_publication_preserves_even_identical_existing_bytes(tmp_path):
    from evidence_wiki._filesystem import os
    from evidence_wiki.source_delivery import _publish

    target = tmp_path / "brief.md"
    raw = b"# Evidence brief\n"
    target.write_bytes(raw)
    before = target.stat()
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(FileExistsError):
            _publish(descriptor, "brief.md", raw)
    finally:
        os.close(descriptor)
    assert target.read_bytes() == raw and target.stat().st_ino == before.st_ino
    assert list(tmp_path.iterdir()) == [target]


def test_publication_checks_named_parent_against_retained_native_directory(tmp_path):
    from types import SimpleNamespace

    from evidence_wiki._filesystem import os
    from evidence_wiki.artifact_publication import _PublicationWitness
    from evidence_wiki.host_context import HostContext

    root = tmp_path.resolve()
    parent = root / "runs/decision/deliveries"
    parent.mkdir(parents=True)
    info = root.stat()
    writer = object.__new__(_PublicationWitness)
    writer.session = SimpleNamespace(root=root, context=HostContext([root], [], "host", []),
        binding={"workspace_binding": {"device": str(info.st_dev), "inode": str(info.st_ino)}})
    writer.path = "runs/decision/deliveries/brief.md"
    writer.directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        writer.check_parent()
        parent.rename(parent.with_name("retained"))
        parent.mkdir()
        with pytest.raises(UsageError) as changed:
            writer.check_parent()
        assert changed.value.details["reason"] == "publication_parent_changed"
    finally:
        os.close(writer.directory)


def test_history_checks_cannot_publish_metadata_after_permission_withdrawal(monkeypatch):
    from types import SimpleNamespace

    from evidence_wiki import artifact_delivery_status as history
    from evidence_wiki.errors import ConfigError

    revoked = []
    artifact = {"artifact_id": "sha256:" + "a" * 64, "artifact_sha256": "sha256:" + "a" * 64,
        "bytes": 19, "media_type": "text/markdown; charset=utf-8", "basis_id": "sha256:" + "b" * 64,
        "check_scope": "current_native_brief", "authorized": False}
    intent = {"arguments": {"proposal": {"artifact_id": artifact["artifact_id"],
        "artifact_sha256": artifact["artifact_sha256"], "artifact_bytes": 19, "destination_ref": "approved"}}}
    def prepare():
        if revoked:
            raise ConfigError("SOURCE_DENIED", "Current source use denied.")
        return {"artifact": artifact}
    selected = SimpleNamespace(_check=lambda *_a: None, prepare_delivery=prepare)
    monkeypatch.setattr(history, "_selected", lambda *_a: ("delivery", [], intent))
    monkeypatch.setattr(history, "_receipt", lambda *_a: {"status": "committed"})
    def closing_history(_session):
        revoked.append(True)
        return []
    monkeypatch.setattr(history.native, "retained", closing_history)
    result = history.status(selected, request_id="delivery")
    assert result["status"] == "unavailable" and result["current_eligibility"] == "unavailable"
    assert all(result[name] is None for name in ("artifact", "destination_ref", "receipt_ref"))
