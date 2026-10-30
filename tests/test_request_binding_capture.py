"""Original mappings retain native capture checks without reading unrelated ledgers."""

import json
import os
import sys

import pytest
import yaml

from evidence_wiki import research_session
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.pack_discovery import owner
from evidence_wiki.workflow_contracts import WorkflowContractError


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "workspace"
    (root / "wiki/questions").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "research.yml").write_text("wiki:\n  root: wiki\n")
    question = {"type": "question", "question": "What was retained?", "metadata": {
        "research_id": "original", "original_text": "What was retained?",
        "original_ids_json": json.dumps(["original"])}}
    (root / "wiki/questions/question.md").write_text("---\n" + yaml.safe_dump(question) + "---\nRetained context.\n")
    requirements = {"schema_version": "evidence-research-requirements/v1", "request": {
        "schema_version": "2.0", "kind": "research_request", "payload": {
            "questions": [{"id": "original", "text": "What was retained?"}], "derived_questions": []}}}
    (root / "docs/research-requirements.json").write_text(json.dumps(requirements))
    return root


def test_mapping_matches_canonical_owner_and_unrelated_journal_bytes_are_not_read(root, monkeypatch):
    expected = digest([{"original_id": "original", "original_text": "What was retained?",
        "question_ids": ["original"], "missing_question_ids": []}])
    assert research_session._request_binding(root) == expected
    for index in range(13):
        folder = root / "runs" / str(index)
        folder.mkdir(parents=True)
        (folder / "events.jsonl").write_bytes(b"retained historical ledger\n" * 8192)
    revisions = owner("_evidence_revision")
    original_read = revisions.read_observed_file
    reads = []

    def read(*args, **kwargs):
        reads.append(args[1])
        return original_read(*args, **kwargs)

    monkeypatch.setattr(revisions, "read_observed_file", read)
    assert research_session._request_binding(root) == expected
    if sys.platform != "win32":
        assert set(reads) == {"research.yml", "docs/research-requirements.json", "wiki/questions/question.md"}
        assert len(reads) == 6


@pytest.mark.parametrize("relative", ["research.yml", "docs/research-requirements.json", "wiki/questions/question.md"])
def test_selected_bytes_changing_between_mapping_and_closing_read_are_refused(root, monkeypatch, relative):
    original_inputs = research_session._request_inputs
    calls = 0

    def capture(path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            selected = root / relative
            selected.write_bytes(selected.read_bytes() + b"\n")
        return original_inputs(path, **kwargs)

    monkeypatch.setattr(research_session, "_request_inputs", capture)
    with pytest.raises(WorkflowContractError) as caught:
        research_session._request_binding(root)
    assert caught.value.error_code == "RESEARCH_ORIGINALS_CHANGED"


@pytest.mark.parametrize("configuration", ["wiki: [", "[]", "wiki:\n  root: ../outside\n", "wiki:\n  root: other\n"])
def test_closing_configuration_changes_keep_original_mapping_refusal(root, monkeypatch, configuration):
    original_inputs = research_session._request_inputs
    calls = 0

    def capture(path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            (root / "research.yml").write_text(configuration)
        return original_inputs(path, **kwargs)

    monkeypatch.setattr(research_session, "_request_inputs", capture)
    with pytest.raises(WorkflowContractError) as caught:
        research_session._request_binding(root)
    assert caught.value.error_code == "RESEARCH_ORIGINALS_CHANGED"


@pytest.mark.parametrize("replacement", ["missing", "directory"])
def test_closing_configuration_disappearance_keeps_original_mapping_refusal(root, monkeypatch, replacement):
    original_inputs = research_session._request_inputs
    calls = 0

    def capture(path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            selected = root / "research.yml"
            selected.unlink()
            if replacement == "directory":
                selected.mkdir()
        return original_inputs(path, **kwargs)

    monkeypatch.setattr(research_session, "_request_inputs", capture)
    with pytest.raises(WorkflowContractError) as caught:
        research_session._request_binding(root)
    assert caught.value.error_code == "RESEARCH_ORIGINALS_CHANGED"


@pytest.mark.parametrize("change", ["add", "delete", "replace"])
@pytest.mark.skipif(sys.platform == "win32", reason="Exercises the POSIX selected-file reader.")
def test_question_membership_or_identity_change_during_read_is_retried_and_refused(root, monkeypatch, change):
    revisions = owner("_evidence_revision")
    original_read = revisions.read_observed_file
    page = root / "wiki/questions/question.md"
    raw = page.read_bytes()
    replacement = root.parent / "replacement.md"
    count = 0

    def read(*args, **kwargs):
        nonlocal count
        result = original_read(*args, **kwargs)
        if args[1] == "research.yml":
            count += 1
            if change == "add":
                (page.parent / f"extra-{count}.md").write_bytes(raw)
            elif change == "delete":
                if page.exists():
                    page.unlink()
                else:
                    page.write_bytes(raw)
            else:
                replacement.write_bytes(raw)
                replacement.replace(page)
        return result

    monkeypatch.setattr(revisions, "read_observed_file", read)
    with pytest.raises(Exception) as caught:
        research_session._request_inputs(root)
    assert caught.value.error_code == "EVIDENCE_REVISION_CHANGED"
    assert count == revisions.MAX_ATTEMPTS


@pytest.mark.parametrize("relative", ["research.yml", "docs/research-requirements.json", "wiki/questions/question.md"])
@pytest.mark.skipif(sys.platform == "win32", reason="Exercises the POSIX selected-file reader.")
def test_same_size_restored_mtime_rewrite_during_read_is_refused(root, monkeypatch, relative):
    revisions = owner("_evidence_revision")
    original_read = revisions.read_observed_file
    selected = root / relative

    def read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        if args[1] == relative:
            previous = selected.stat()
            raw = selected.read_bytes()
            selected.write_bytes(raw[:-1] + (b" " if raw[-1:] != b" " else b"\n"))
            os.utime(selected, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        return result

    monkeypatch.setattr(revisions, "read_observed_file", read)
    with pytest.raises(Exception) as caught:
        research_session._request_inputs(root)
    assert caught.value.error_code == "EVIDENCE_REVISION_CHANGED"


@pytest.mark.parametrize("relative", ["wiki/questions/question.md", "runs/unrelated.jsonl"])
@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_unsafe_selected_and_unselected_entries_remain_refused(root, tmp_path, relative, link):
    selected = root / relative
    selected.parent.mkdir(parents=True, exist_ok=True)
    if selected.exists():
        selected.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"untrusted bytes")
    if link == "symlink":
        selected.symlink_to(outside)
    else:
        os.link(outside, selected)
    with pytest.raises(Exception) as caught:
        research_session._request_inputs(root)
    assert caught.value.error_code == "EVIDENCE_REVISION_UNSAFE"


def test_question_parent_link_and_global_file_bounds_remain_refused(root, tmp_path, monkeypatch):
    revisions = owner("_evidence_revision")
    monkeypatch.setattr(revisions, "MAX_FILES", 2)
    with pytest.raises(Exception) as caught:
        research_session._request_inputs(root)
    assert caught.value.error_code == "EVIDENCE_REVISION_LIMIT"
    monkeypatch.undo()
    questions = root / "wiki/questions"
    moved = tmp_path / "questions"
    questions.rename(moved)
    questions.symlink_to(moved, target_is_directory=True)
    with pytest.raises(Exception) as caught:
        research_session._request_inputs(root)
    assert caught.value.error_code == "EVIDENCE_REVISION_UNSAFE"


@pytest.mark.parametrize("change", ["missing", "ambiguous", "requirements", "configuration"])
def test_missing_or_changed_original_mapping_is_not_hidden_by_a_previous_call(root, change):
    research_session._request_binding(root)
    page = root / "wiki/questions/question.md"
    if change == "missing":
        page.unlink()
    elif change == "ambiguous":
        (page.parent / "duplicate.md").write_bytes(page.read_bytes())
    elif change == "requirements":
        path = root / "docs/research-requirements.json"
        document = json.loads(path.read_text())
        document["request"]["payload"]["questions"][0]["text"] = "Changed original"
        path.write_text(json.dumps(document))
    else:
        (root / "research.yml").write_text("wiki:\n  root: other\n")
    with pytest.raises((WorkflowContractError, ValueError)):
        research_session._request_binding(root)


def test_non_file_markdown_directory_is_not_a_mapping_input(root):
    (root / "wiki/questions/ignored.md").mkdir()
    captured, _ = research_session._request_inputs(root)
    assert "wiki/questions/ignored.md" not in captured.files
    assert research_session._request_binding(root)


def test_requirements_directory_is_not_read_as_a_regular_file(root):
    selected = root / "docs/research-requirements.json"
    selected.unlink()
    selected.mkdir()
    captured, _ = research_session._request_inputs(root)
    assert "docs/research-requirements.json" not in captured.files
    with pytest.raises(WorkflowContractError) as caught:
        research_session._request_binding(root)
    assert caught.value.error_code == "RESEARCH_ORIGINALS_CHANGED"


@pytest.mark.skipif(sys.platform == "win32", reason="Exercises the native POSIX capture preflight.")
def test_unsupported_no_follow_capture_remains_refused(root, monkeypatch):
    revisions = owner("_evidence_revision")
    monkeypatch.setattr(revisions.os, "supports_dir_fd", set())
    with pytest.raises(revisions.ScriptRefusal) as caught:
        research_session._request_inputs(root)
    assert caught.value.error_code == "EVIDENCE_REVISION_UNSUPPORTED"


@pytest.mark.skipif(sys.platform == "win32", reason="Exercises the POSIX selected-file reader.")
def test_selected_read_os_error_uses_native_refusal_translation(root, monkeypatch):
    revisions = owner("_evidence_revision")

    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(revisions, "read_observed_file", denied)
    with pytest.raises(revisions.ScriptRefusal) as caught:
        research_session._request_inputs(root)
    assert caught.value.error_code == "EVIDENCE_REVISION_UNSAFE"
    assert caught.value.details["reason"] == "Permission denied"


@pytest.mark.skipif(sys.platform == "win32", reason="Exercises POSIX file permissions.")
def test_unrelated_readability_is_left_to_the_unchanged_full_capture(root):
    expected = research_session._request_binding(root)
    unrelated = root / "unreadable.log"
    unrelated.write_bytes(b"Unrelated retained bytes.")
    unrelated.chmod(0)
    try:
        try:
            unrelated.read_bytes()
        except PermissionError:
            pass
        else:
            pytest.skip("The current user can read files with no read permission.")
        assert research_session._request_binding(root) == expected
        revisions = owner("_evidence_revision")
        with pytest.raises(revisions.ScriptRefusal) as caught:
            revisions.capture_workspace(root)
        assert caught.value.error_code == "EVIDENCE_REVISION_UNSAFE"
    finally:
        unrelated.chmod(0o600)


@pytest.mark.parametrize("change", ["restored_bytes", "directory_identity"])
@pytest.mark.skipif(sys.platform == "win32", reason="Exercises the POSIX selected-file reader.")
def test_unselected_metadata_changes_are_still_detected_during_capture(root, monkeypatch, change):
    folder = root / "runs/unrelated"
    folder.mkdir(parents=True)
    ledger = folder / "events.jsonl"
    ledger.write_bytes(b"Retained journal bytes.")
    revisions = owner("_evidence_revision")
    original_read = revisions.read_observed_file
    count = 0

    def read(*args, **kwargs):
        nonlocal count
        result = original_read(*args, **kwargs)
        if args[1] == "research.yml":
            count += 1
            if change == "restored_bytes":
                previous = ledger.stat()
                raw = ledger.read_bytes()
                ledger.write_bytes(raw[:-1] + b"!")
                ledger.write_bytes(raw)
                os.utime(ledger, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            else:
                folder.rename(root.parent / f"previous-directory-{count}")
                folder.mkdir()
                ledger.write_bytes(b"Retained journal bytes.")
        return result

    monkeypatch.setattr(revisions, "read_observed_file", read)
    with pytest.raises(revisions.ScriptRefusal) as caught:
        research_session._request_inputs(root)
    assert caught.value.error_code == "EVIDENCE_REVISION_CHANGED"
    assert count == revisions.MAX_ATTEMPTS


def test_unicode_configured_question_root_preserves_exact_markdown_projection(root):
    expected = research_session._request_binding(root)
    (root / "wiki").rename(root / "études")
    (root / "research.yml").write_text("wiki:\n  root: études\n")
    nested = root / "études/questions/nested"
    nested.mkdir()
    (nested / "retained.md").write_text("Nested retained context.")
    (nested / "ignored.txt").write_text("Unrelated text.")
    captured, _ = research_session._request_inputs(root)
    assert set(captured.files) == {"research.yml", "docs/research-requirements.json",
        "études/questions/question.md", "études/questions/nested/retained.md"}
    assert research_session._request_binding(root) == expected


def test_windows_selection_retains_the_native_full_capture_fallback(root, monkeypatch):
    revisions = owner("_evidence_revision")
    captured = revisions.capture_workspace(root)
    calls = []

    def capture(path):
        calls.append(path)
        return captured

    monkeypatch.setattr(revisions, "capture_workspace", capture)
    monkeypatch.setattr(sys, "platform", "win32")
    selected, config = research_session._request_inputs(root)
    assert calls == [root]
    assert config == {"wiki": {"root": "wiki"}}
    assert set(selected.files) == {"research.yml", "docs/research-requirements.json", "wiki/questions/question.md"}
