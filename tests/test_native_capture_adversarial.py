"""Independent real staged publisher and protected-history attacks."""

import json
import os
from pathlib import Path

import pytest

from evidence_wiki import mutation_operations as mutations
from evidence_wiki import source_capture_contracts as codec
from evidence_wiki import source_capture_operations as capture
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_store import MutationStore
from evidence_wiki.pack_discovery import owner
from tests.test_mutation_operations import GRANTS
from tests.test_mutation_operations import roots as roots
from tests.test_native_capture_revision import metadata, request_capture
from tests.test_native_capture_revision import selected as selected


def test_readonly_historical_store_cannot_mutate_a_partial_capture(roots, selected, monkeypatch):
    root, control = roots
    delivery = codec.from_bytes(b"Original", metadata())
    writer = owner("_usage_materialization")
    actual = writer.publish_file
    calls = 0
    def interrupted(*args, **kwargs):
        nonlocal calls
        result = actual(*args, **kwargs)
        calls += 1
        if calls == 1:
            raise OSError("Controlled provenance-only interruption")
        return result
    with selected.activate():
        request = request_capture(root, delivery)
        with monkeypatch.context() as scoped:
            scoped.setattr(writer, "publish_file", interrupted)
            receipt = mutations.submit(root, request)
        assert receipt["status"] in {"partial", "commit_unknown"}
        original = root / "raw/web/captured.txt"
        assert not original.exists()
        assert (root / "raw/web/captured.txt.provenance.yml").exists()
        with MutationStore(root, control).historical_locked("capture") as store:
            state = store.read()
            with pytest.raises(EvidenceWikiError):
                capture.deliver(root, delivery, path="raw/web/captured.txt", _store=store, _basis=state["owner_basis"], _recovery=True)
        assert not original.exists()


def test_projected_capture_count_refuses_before_any_owned_file_is_written(roots, selected, monkeypatch):
    root, _ = roots
    revision = owner("_evidence_revision")
    existing = revision.capture_workspace(root)
    package_count = len(revision.capture_workspace(Path(capture.__file__).parent).files)
    maximum = max(package_count + 32, len(existing.files) + 32)
    for index in range(maximum - len(existing.files) - 1):
        (root / ("fixture-" + str(index))).write_bytes(b"Bounded retained input")
    # The package generation remains fully observable. One new file would fit,
    # but the immutable original/provenance pair exceeds the actual tree quota.
    monkeypatch.setattr(revision, "MAX_FILES", maximum)
    delivery = codec.from_bytes(b"Original", metadata())
    with selected.activate():
        request = request_capture(root, delivery)
        try:
            mutations.submit(root, request)
        except EvidenceWikiError:
            pass
    assert not (root / "raw/web/captured.txt").exists()
    assert not (root / "raw/web/captured.txt.provenance.yml").exists()


def test_current_codecs_do_not_silently_relabel_v2_as_legacy_bytes(roots, selected):
    from evidence_wiki.source_contracts import DELIVERY, decode
    delivery = codec.from_bytes(b"{}", metadata("json"))
    with pytest.raises(EvidenceWikiError):
        decode(DELIVERY, json.dumps(delivery).encode())


@pytest.mark.parametrize("relative", ["../outside", "nested//original", "/original", "./original"])
def test_approved_descriptor_traversal_is_closed_before_owner_read(relative):
    delivery = codec.from_bytes(b"Original", metadata())
    delivery["input"] = {"kind": "approved_path", "root": "/approved", "root_identity": {"device": "1", "inode": "1"}, "relative": relative}
    with pytest.raises((EvidenceWikiError, ValueError)):
        codec.validate(delivery)


def test_declared_deep_output_cannot_exceed_native_tree_depth_after_writing(roots, selected):
    root, _ = roots
    maximum = owner("_evidence_revision").MAX_DEPTH
    path = "raw/web/" + "/".join(["level"] * maximum) + "/captured.txt"
    delivery = codec.from_bytes(b"Original", metadata())
    with selected.activate():
        try:
            request = request_capture(root, delivery, path=path)
            mutations.submit(root, request)
        except EvidenceWikiError:
            pass
    assert not (root / path).exists()
    assert not (root / (path + ".provenance.yml")).exists()


@pytest.mark.parametrize("change", ["unknown-parent", "expired-grant", "changed-bytes", "wrong-command-ref", "denied-retrieval"])
def test_protected_capture_actual_owner_binds_closure_ancestry_and_current_usage(roots, tmp_path, monkeypatch, change):
    import yaml

    from evidence_wiki.host_context import HostContext
    from tests._execution_fixture import authenticate, identifier
    from tests._usage_fixture import UsageFixture
    from tests.test_mutation_operations import GRANTS
    root, control = roots
    directory = tmp_path / "authority"
    directory.mkdir()
    authority = UsageFixture(directory, monkeypatch)
    authority.root = root
    authority.binding = identifier("evidence-host-workspace/v1", {"root": str(root)})
    config = yaml.safe_load((root / "research.yml").read_bytes())
    config.update(authority.config)
    (root / "research.yml").write_text(yaml.safe_dump(config))
    authority.config = config
    usage = owner("_evidence_usage")
    authority.transact(usage, "initialize")
    path, raw = "raw/web/protected.txt", b"Original sanitized observation."
    supplied = metadata(sanitization={"required": True, "approved_revision_id": None, "receipt_ref": None})
    if change == "unknown-parent":
        supplied["parents"] = ["sha256:" + "d" * 64]
    files = capture.protected_artifacts(raw, supplied, path=path)
    revision = owner("_record_artifacts").closure_identity(files)
    body, _ = authority.source(owner("source_inventory").stable_id(path))
    body["source_revision"] = revision
    grant = body["grant"]["payload"]
    grant["source_revision"] = revision
    if change == "expired-grant":
        grant["expires_at"] = "2026-01-01T00:00:00Z"
    if change == "denied-retrieval":
        grant["permissions"]["retrieval"] = False
    body["grant"] = authenticate(grant, "owner", "usage")
    scrub = body["scrub"]["payload"]
    scrub["sanitized_revision"] = revision
    body["scrub"] = authenticate(scrub, "runner", "scrubber")
    envelope = authority.command("deposit", body, request_id="protected")
    supplied["sanitization"].update(approved_revision_id=revision, receipt_ref="wrong" if change == "wrong-command-ref" else "protected")
    delivery = codec.from_bytes(b"Changed sanitized observation." if change == "changed-bytes" else raw, supplied)
    context = HostContext([root], GRANTS | {capture.GRANT, "evidence_usage.run_transact"}, "host", [root],
        mutation_control_directory=control, authority_file=authority.policy_path, state_directory=authority.host)
    with context.activate():
        if change == "denied-retrieval":
            result = capture.deposit_protected(root, delivery, envelope, path=path)
            assert result["artifact_revision"] == revision and not result["research_ready"]
        else:
            with pytest.raises(EvidenceWikiError):
                capture.deposit_protected(root, delivery, envelope, path=path)
    with usage.current_view(root, config) as view:
        if change == "denied-retrieval":
            check = view.check(revision, uses=["retrieval"], purpose="research", consumer="evidence-wiki")
            assert not check["eligible"]
        else:
            assert not view.state.revisions
    assert not (root / path).exists() and not (root / (path + ".provenance.yml")).exists()


def test_capture_respects_actual_inventory_raw_lock(roots):
    root, control = roots
    directory = root / "raw/.locks"
    directory.mkdir(mode=0o700)
    path = directory / "acquisition.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_EXCL, 0o600)
    context = HostContext([root], GRANTS | {capture.GRANT}, "host", [root], mutation_control_directory=control)
    delivery = codec.from_bytes(b"Original observation", metadata())
    locks = owner("_workspace_locks")
    try:
        with context.activate():
            request = mutations.prepare_request(root, request_id="capture", operation=capture.OPERATION,
                arguments={"path": "raw/web/captured.txt", "delivery": delivery})
            with locks.descriptor_lock(descriptor, timeout_seconds=0):
                with pytest.raises(EvidenceWikiError) as failure:
                    mutations.submit(root, request)
                assert failure.value.error_code == "MUTATION_CAPTURE_ACQUISITION_CONTENDED"
                assert failure.value.recoverable is True
                assert not (root / "raw/web/captured.txt").exists()
                assert not (root / "raw/web/captured.txt.provenance.yml").exists()
    finally:
        os.close(descriptor)
