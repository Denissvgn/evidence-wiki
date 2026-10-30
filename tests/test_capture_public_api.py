"""Actual core capture identity, explicit repair and typed input boundaries."""

import hashlib
import io

import pytest

from evidence_wiki import Workspace, contract
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.onboarding_contract import _matches
from evidence_wiki.pack_discovery import owner
from evidence_wiki.source_contracts import schema_document
from tests.test_mutation_operations import GRANTS
from tests.test_mutation_operations import roots as roots
from tests.test_native_capture_revision import metadata


@pytest.fixture
def selected(roots):
    root, control = roots
    return HostContext([root], GRANTS | {"source_capture_operations.deliver",
        "source_capture_operations.resume", "source_capture_contracts.from_path"},
        "host", [root], mutation_control_directory=control)


@pytest.mark.parametrize("format,content,suffix", [
    ("plain_text", b"Observed exact measurement.\r\n", "txt"),
    ("markdown", b"# Observed\n\nExact measurement.\n", "md"),
    ("json", b'{"value":3,"unit":"ratio"}', "json"),
    ("csv", b"value,unit\n3,ratio\n", "csv"),
])
def test_public_capture_retains_original_and_exposes_checked_unaccepted_revision(roots, selected, format, content, suffix):
    root, _ = roots
    with selected.activate(), Workspace.open(root) as workspace:
        supplied = metadata(format)
        _matches(supplied, schema_document(supplied["schema_version"]))
        delivery = workspace.sources.capture_bytes_document(content, supplied)
        _matches(delivery, schema_document(delivery["schema_version"]))
        request = workspace.sources.prepare_capture(request_id="capture", path="raw/web/input." + suffix, delivery=delivery)
        receipt = workspace.sources.capture_revision(request)
        assert receipt["status"] == "committed", receipt
        result = receipt["result"]
        _matches(result, schema_document(result["schema_version"]))
        assert (root / result["path"]).read_bytes() == content
        assert result["content_sha256"] == "sha256:" + hashlib.sha256(content).hexdigest()
        assert result["artifact_revision"] and not result["research_ready"]
        assert result["extraction"] == "not_run" and not result["request_fulfilled"]
        assert not result["evidence_accepted"] and not result["authorized"]
        assert workspace.sources.capture_revision(request) == receipt
        assert workspace.sources.resume_capture("capture") == receipt
    advertised = contract()["source_usability"]["revision_capture"]
    assert format in advertised["formats"] and advertised["operation"] == "sources.capture_revision"


def test_public_approved_path_captures_exact_bytes_and_replay_does_not_reacquire(roots, selected):
    root, _ = roots
    content = b"Original permitted local bytes."
    original = root / "original.txt"
    original.write_bytes(content)
    with selected.activate(), Workspace.open(root) as workspace:
        delivery = workspace.sources.capture_path_document(root=root, relative="original.txt",
            metadata=metadata(capture_method="host_approved_path"),
            expected_content_sha256="sha256:" + hashlib.sha256(content).hexdigest(), expected_bytes=len(content))
        request = workspace.sources.prepare_capture(request_id="capture", path="raw/web/captured.txt", delivery=delivery)
        receipt = workspace.sources.capture_revision(request)
        assert receipt["status"] == "committed", receipt
        original.unlink()
        assert workspace.sources.capture_revision(request) == receipt
        assert (root / "raw/web/captured.txt").read_bytes() == content


def test_public_pdf_retains_a_readable_original_without_claiming_extraction(roots, selected):
    pypdf = pytest.importorskip("pypdf", reason="Optional local PDF reader unavailable.")
    from tests.test_pdf_success_path import tiny_pdf_bytes

    content = tiny_pdf_bytes()
    assert "Abstract" in pypdf.PdfReader(io.BytesIO(content)).pages[0].extract_text()
    root, _ = roots
    with selected.activate(), Workspace.open(root) as workspace:
        delivery = workspace.sources.capture_bytes_document(content, metadata("pdf"))
        request = workspace.sources.prepare_capture(request_id="capture", path="raw/web/original.pdf", delivery=delivery)
        receipt = workspace.sources.capture_revision(request)
        assert receipt["status"] == "committed", receipt
        assert (root / "raw/web/original.pdf").read_bytes() == content
        assert receipt["result"]["extraction"] == "not_run" and not receipt["result"]["research_ready"]


def test_public_repair_keeps_original_partial_receipt_and_proves_only_desired_pair(roots, selected, monkeypatch):
    root, _ = roots
    writer = owner("_usage_materialization")
    actual = writer.publish_file
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        result = actual(*args, **kwargs)
        calls += 1
        if calls == 1:
            raise OSError("Controlled pair interruption")
        return result

    with selected.activate(), Workspace.open(root) as workspace:
        delivery = workspace.sources.capture_bytes_document(b"Original exact bytes", metadata())
        request = workspace.sources.prepare_capture(request_id="capture", path="raw/web/captured.txt", delivery=delivery)
        with monkeypatch.context() as scoped:
            scoped.setattr(writer, "publish_file", interrupted)
            receipt = workspace.sources.capture_revision(request)
        assert receipt["status"] in {"partial", "commit_unknown"}
        assert workspace.sources.capture_revision(request) == receipt
        result = workspace.sources.resume_capture("capture")
        assert result["status"] == "reconciled" and result["desired_state_proved"]
        assert not result["completion_proved"] and result["historical"]
        assert workspace.sources.capture_revision(request) == receipt
        assert workspace.sources.resume_capture("capture") == result
        assert (root / "raw/web/captured.txt.provenance.yml").is_file()


def test_public_input_refusals_are_content_free_typed_and_never_publish(roots, selected):
    root, _ = roots
    with selected.activate(), Workspace.open(root) as workspace:
        with pytest.raises(EvidenceWikiError) as failure:
            workspace.sources.capture_bytes_document(b"Private original", {})
        assert failure.value.error_code == "SOURCE_CAPTURE_REFUSED"
        assert "Private original" not in str(failure.value)
        assert not (root / "raw/web/captured.txt").exists()
        with pytest.raises(EvidenceWikiError):
            workspace.sources.capture_path_document(root=root, relative="missing.txt", metadata=metadata(),
                expected_content_sha256="sha256:" + "0" * 64, expected_bytes=1)


def test_closed_handle_refuses_inert_document_building(roots):
    root, _ = roots
    workspace = Workspace.open(root)
    workspace.close()
    with pytest.raises(EvidenceWikiError):
        workspace.sources.capture_bytes_document(b"Exact bytes", metadata())
