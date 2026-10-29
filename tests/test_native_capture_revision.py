"""Actual native custody, immutable pair replay and closed pilot capture cases."""

import hashlib
import io
import json
from types import SimpleNamespace

import pytest
import yaml

from evidence_wiki import mutation_capture_recovery as recovery
from evidence_wiki import mutation_operations as mutations
from evidence_wiki import source_capture_contracts as codec
from evidence_wiki import source_capture_operations as capture
from evidence_wiki._filesystem import os
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_store import MutationStore
from evidence_wiki.pack_discovery import owner
from evidence_wiki.source_capture_inputs import SCHEMA
from tests._execution_fixture import authenticate, identifier
from tests._usage_fixture import UsageFixture
from tests.test_mutation_operations import GRANTS
from tests.test_mutation_operations import roots as roots


def metadata(format="plain_text", **changes):
    return {"schema_version": SCHEMA, "capture_id": "capture", "tool_id": "host", "tool_version": "1",
        "origin_url": "https://example.org/original", "title": "Original capture", "retrieved_at": "2026-10-01T12:00:00Z",
        "capture_method": "connector_export", "content_format": format, "content_kind": "primary", "completeness": "complete",
        "completeness_note": "Host declaration only.", "rights": {"status": "allowed", "license": None, "terms_url": None, "note": "No authority supplied."},
        "scope": {"question_id": "sample"}, "request_id": None, "parents": [],
        "sanitization": {"required": False, "approved_revision_id": None, "receipt_ref": None}} | changes


def test_public_metadata_input_schema_matches_builders_without_claiming_content_identity():
    from evidence_wiki.onboarding_contract import _matches
    schema = codec.schemas()[SCHEMA]
    _matches(metadata(), schema)
    assert "content_sha256" not in schema["properties"] and "content_bytes" not in schema["properties"]
    with pytest.raises(EvidenceWikiError):
        _matches({**metadata(), "content_sha256": "sha256:" + "a" * 64}, schema)
    assert codec.validate(codec.from_bytes(b"Original", metadata()))["schema_version"] == codec.DELIVERY


@pytest.fixture
def selected(roots):
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd or os.link not in os.supports_dir_fd:
        pytest.skip("Actual native capture custody is unavailable on this platform.")
    root, control = roots
    return HostContext([root], GRANTS | {capture.GRANT, capture.REPAIR_GRANT}, "host", [root], mutation_control_directory=control)


@pytest.mark.parametrize("format,raw,suffix", [
    ("plain_text", b"Original exact observation.\r\n", "txt"),
    ("markdown", b"# Original\n\nExact observed value.\n", "md"),
    ("json", b'{"rows":[{"value":3,"unit":"ratio"}]}', "json"),
    ("csv", b"value,unit\n3,ratio\n", "csv"),
])
def test_native_original_and_provenance_are_owned_before_returned_revision(roots, selected, format, raw, suffix):
    root, _ = roots
    delivery = codec.from_bytes(raw, metadata(format))
    path = "raw/web/captured." + suffix
    with selected.activate():
        request = mutations.prepare_request(root, request_id="capture", operation=capture.OPERATION, arguments={"path": path, "delivery": delivery})
        receipt = mutations.submit(root, request)
        assert receipt["status"] == "committed", receipt
        assert (root / path).read_bytes() == raw
        sidecar = json.loads((root / (path + ".provenance.yml")).read_bytes())
        assert sidecar == capture.provenance(delivery)
        assert receipt["result"]["artifact_revision"] == owner("_record_artifacts").closure_identity({path: raw, path + ".provenance.yml": (root / (path + ".provenance.yml")).read_bytes()})
        assert not receipt["result"]["research_ready"] and not receipt["result"]["evidence_accepted"]
        assert mutations.submit(root, request) == receipt
        record = {"kind": "host_capture", "raw_paths": [path], "provenance": sidecar}
        inspected = owner("_host_capture").inspect_record(root, record)
        assert inspected["profile"] == delivery["capture"]


def request_capture(root, delivery, *, request_id="capture", path="raw/web/captured.txt"):
    return mutations.prepare_request(root, request_id=request_id, operation=capture.OPERATION, arguments={"path": path, "delivery": delivery})


@pytest.mark.parametrize("after", [1, 2])
def test_interrupted_owned_pair_replays_then_explicitly_repairs_from_sealed_bytes(roots, selected, monkeypatch, after):
    root, _ = roots
    raw = b"Exact original."
    delivery = codec.from_bytes(raw, metadata())
    writer = owner("_usage_materialization")
    actual = writer.publish_file
    calls = 0
    def interrupted(*args, **kwargs):
        nonlocal calls
        result = actual(*args, **kwargs)
        calls += 1
        if calls == after:
            raise OSError("Controlled publication interruption")
        return result
    with selected.activate():
        request = request_capture(root, delivery)
        with monkeypatch.context() as scoped:
            scoped.setattr(writer, "publish_file", interrupted)
            receipt = mutations.submit(root, request)
        assert receipt["status"] in {"partial", "commit_unknown"}
        assert mutations.submit(root, request) == receipt
        result = recovery.resume(root, "capture")
        assert result["status"] == "reconciled", result
        assert not result["completion_proved"] and result["desired_state_proved"]
        assert result["owner_observation"]["artifact_revision"] is not None
        assert recovery.resume(root, "capture") == result
        assert mutations.submit(root, request) == receipt
        assert (root / "raw/web/captured.txt").read_bytes() == raw


def test_same_bytes_different_provenance_have_distinct_immutable_revisions(roots, selected):
    root, _ = roots
    first = codec.from_bytes(b"Same bytes", metadata())
    second = codec.from_bytes(b"Same bytes", metadata(origin_url="https://example.org/another"))
    with selected.activate():
        before = mutations.submit(root, request_capture(root, first))
        with pytest.raises(EvidenceWikiError):
            mutations.submit(root, request_capture(root, second, request_id="conflict"))
        after = mutations.submit(root, request_capture(root, second, request_id="separate", path="raw/web/another.txt"))
        assert before["result"]["artifact_revision"] != after["result"]["artifact_revision"]
        assert (root / "raw/web/captured.txt").read_bytes() == b"Same bytes"


def test_approved_path_is_observed_before_stage_and_never_reread_after_owned_stage(roots, selected, monkeypatch):
    root, _ = roots
    path = root / "original.txt"
    raw = b"Approved exact original."
    path.write_bytes(raw)
    with selected.activate():
        delivery = codec.from_path(selected, root=root, relative="original.txt", supplied=metadata(capture_method="host_approved_path"),
            expected_content_sha256="sha256:" + hashlib.sha256(raw).hexdigest(), expected_bytes=len(raw))
        request = request_capture(root, delivery)
        receipt = mutations.submit(root, request)
        path.unlink()
        assert mutations.submit(root, request) == receipt
        assert (root / "raw/web/captured.txt").read_bytes() == raw


def test_valid_local_pdf_original_has_actual_native_capture_revision(roots, selected):
    pypdf = pytest.importorskip("pypdf", reason="Optional local PDF capability unavailable; no installation requested.")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    stream = io.BytesIO()
    writer.write(stream)
    raw = stream.getvalue()
    assert len(pypdf.PdfReader(io.BytesIO(raw)).pages) == 1
    root, _ = roots
    with selected.activate():
        delivery = codec.from_bytes(raw, metadata("pdf"))
        receipt = mutations.submit(root, request_capture(root, delivery, path="raw/web/captured.pdf"))
        assert receipt["status"] == "committed", receipt
        assert (root / "raw/web/captured.pdf").read_bytes() == raw
        assert receipt["result"]["extraction"] == "not_run" and not receipt["result"]["research_ready"]


@pytest.mark.parametrize("changed", ["missing-grant", "orphan-original", "symlink"])
def test_unsafe_or_unowned_capture_refuses_without_relabeling_existing_bytes(roots, selected, changed):
    root, control = roots
    delivery = codec.from_bytes(b"New original", metadata())
    target = root / "raw/web/captured.txt"
    if changed == "missing-grant":
        selected = HostContext([root], GRANTS, "host", [root], mutation_control_directory=control)
    elif changed == "orphan-original":
        target.write_bytes(b"New original")
    else:
        target.symlink_to(root / "raw/web/source.html")
    before = dict(owner("_evidence_revision").capture_workspace(root).files) if changed != "symlink" else None
    with selected.activate(), pytest.raises((EvidenceWikiError, ValueError)):
        request = request_capture(root, delivery)
        mutations.submit(root, request)
    if before is not None:
        assert dict(owner("_evidence_revision").capture_workspace(root).files) == before
    else:
        assert target.is_symlink() and not (root / "raw/web/captured.txt.provenance.yml").exists()


@pytest.mark.parametrize("wrong", [False, True], ids=["native-signed", "invalid-scrub"])
def test_protected_capture_reuses_actual_signed_usage_deposit_and_never_writes_raw_pair(roots, tmp_path, monkeypatch, wrong):
    root, control = roots
    authority_dir = tmp_path / "authority"
    authority_dir.mkdir()
    authority = UsageFixture(authority_dir, monkeypatch)
    authority.root = root
    authority.binding = identifier("evidence-host-workspace/v1", {"root": str(root)})
    config = yaml.safe_load((root / "research.yml").read_bytes())
    config.update(authority.config)
    (root / "research.yml").write_text(yaml.safe_dump(config))
    authority.config = config
    usage = owner("_evidence_usage")
    authority.transact(usage, "initialize")
    path, raw = "raw/web/protected.txt", b"Host-sanitized original observation."
    supplied = metadata(sanitization={"required": True, "approved_revision_id": None, "receipt_ref": None})
    files = capture.protected_artifacts(raw, supplied, path=path)
    revision = owner("_record_artifacts").closure_identity(files)
    body, _ = authority.source(owner("source_inventory").stable_id(path))
    body["source_revision"] = revision
    grant = body["grant"]["payload"]
    grant["source_revision"] = revision
    body["grant"] = authenticate(grant, "owner", "usage")
    scrub = body["scrub"]["payload"]
    scrub["sanitized_revision"] = revision
    body["scrub"] = authenticate(scrub, "runner", "scrubber")
    if wrong:
        body["scrub"]["authentication"]["signature"] = "invalid"
    envelope = authority.command("deposit", body, request_id="capture-deposit")
    supplied["sanitization"].update(approved_revision_id=revision, receipt_ref="capture-deposit")
    delivery = codec.from_bytes(raw, supplied)
    selected = HostContext([root], GRANTS | {capture.GRANT, "evidence_usage.run_transact"}, "host", [root],
        mutation_control_directory=control, authority_file=authority.policy_path, state_directory=authority.host)
    with selected.activate():
        if wrong:
            with pytest.raises(EvidenceWikiError):
                capture.deposit_protected(root, delivery, envelope, path=path)
        else:
            result = capture.deposit_protected(root, delivery, envelope, path=path)
            assert result["status"] == "protected_deposited" and result["artifact_revision"] == revision
            assert not result["research_ready"] and not result["evidence_accepted"]
            assert capture.deposit_protected(root, delivery, envelope, path=path) == result
    with usage.current_view(root, config) as view:
        assert len(view.state.revisions) == (0 if wrong else 1)
        if not wrong:
            assert view.state.revisions[revision]["files"] == files
    assert not (root / path).exists() and not (root / (path + ".provenance.yml")).exists()


@pytest.mark.parametrize("change", ["none", "changed", "symlink"], ids=["observed", "stale", "link"])
def test_large_approved_structured_original_and_preparation_races(roots, tmp_path, change):
    root, control = roots
    approved = tmp_path / "approved"
    approved.mkdir()
    original = approved / "original.csv"
    raw = (",".join("field" + str(index) for index in range(100)) + "\n" + (",".join(["1000"] * 100) + "\n") * 150).encode()
    assert len(raw) > codec.INLINE_BYTES
    original.write_bytes(raw)
    selected = HostContext([root, approved], GRANTS | {capture.GRANT}, "host", [root], mutation_control_directory=control)
    with selected.activate():
        delivery = codec.from_path(selected, root=approved, relative="original.csv", supplied=metadata("csv", capture_method="host_approved_path"),
            expected_content_sha256="sha256:" + hashlib.sha256(raw).hexdigest(), expected_bytes=len(raw))
        request = request_capture(root, delivery, path="raw/web/large.csv")
        if change == "changed":
            original.write_bytes(raw.replace(b"1000", b"2000"))
        elif change == "symlink":
            original.unlink()
            original.symlink_to(root / "raw/web/source.html")
        if change == "none":
            receipt = mutations.submit(root, request)
            assert receipt["status"] == "committed", receipt
            assert (root / "raw/web/large.csv").read_bytes() == raw
        else:
            with pytest.raises(EvidenceWikiError):
                mutations.submit(root, request)
            assert not (root / "raw/web/large.csv").exists()
            assert not (root / "raw/web/large.csv.provenance.yml").exists()


def test_new_publisher_needs_actual_sealed_request_not_a_caller_plan_or_duck_store(roots, selected):
    root, control = roots
    delivery = codec.from_bytes(b"Original", metadata())
    with selected.activate(), MutationStore(root, control).workspace_locked():
        for supplied in (None, SimpleNamespace(_active=True, workspace_root=root, control_root=control)):
            with pytest.raises(EvidenceWikiError) as error:
                capture.deliver(root, delivery, path="raw/web/captured.txt", _store=supplied, _basis={"invented": True})
            assert error.value.error_code == "MUTATION_CAPTURE_STAGED_GATE_REQUIRED"
    assert not (root / "raw/web/captured.txt").exists()


def test_real_inventory_observes_new_capture_without_claiming_normalization_or_acceptance(roots, selected):
    root, _ = roots
    with selected.activate():
        delivery = codec.from_bytes(b'{"value":3,"unit":"ratio"}', metadata("json", completeness="partial"))
        receipt = mutations.submit(root, request_capture(root, delivery, path="raw/web/partial.json"))
        assert receipt["status"] == "committed"
        request = mutations.prepare_request(root, request_id="inventory", operation="sources.inventory", arguments={})
        result = mutations.submit(root, request)
        assert result["status"] == "committed", result
    records = [json.loads(line) for line in (root / "sources/manifest.jsonl").read_text().splitlines()]
    record = next(row for row in records if row.get("raw_paths") == ["raw/web/partial.json"])
    assert record["kind"] == "host_capture"
    assert record["provenance"]["host_capture"]["completeness"] == "partial"
    assert record["evidence_usable"] is False
    assert "host_capture_format_normalization_not_run" in record["unusable_evidence_reasons"]
