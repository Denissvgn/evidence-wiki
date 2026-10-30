"""Real source inspection and signed usage locks at the private holder boundary.

Only session identity is bypassed; these are not full native session acceptance tests.
"""

import json
import os
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from evidence_wiki import _review_dependency_inputs as native
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.pack_discovery import owner
from evidence_wiki.retrieval_operations import _documents, _ScopedView
from tests.test_mutation_operations import roots as roots
from tests.test_request_lifecycle_adversarial import protected_source
from tests.test_requirements_owner import setup as setup
from tests.test_retrieval_permissions import protected_context

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Native descriptor-relative source custody is unavailable.")


@pytest.fixture
def source_inputs(setup, tmp_path, monkeypatch):
    root, *_ = setup
    _, fixture, _ = protected_source(setup, tmp_path, monkeypatch)
    normalized = (root / "sources/normalized/sample-source.md").read_bytes()
    evidence = {path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / "raw").rglob("*") if path.is_file()}
    body, files = fixture.source("sample-source", normalized=normalized, evidence=evidence)
    fixture.transact(owner("_evidence_usage"), "deposit", body, files)
    manifest = root / "sources/manifest.jsonl"
    record = json.loads(manifest.read_bytes())
    record["usage_revision_id"] = body["source_revision"]
    manifest.write_text(json.dumps(record) + "\n")
    capture = SimpleNamespace(files={path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*") if path.is_file()})
    holder = object.__new__(native._Inputs)
    holder._root = root
    # Session provenance is outside this focused source/usage integration boundary.
    monkeypatch.setattr(holder, "_scope", lambda *args: None)
    return root, fixture, body, capture, holder, protected_context(setup, fixture)


def consume(inputs, view):
    root, fixture, _, capture, holder, _ = inputs
    return holder.source_documents(root, fixture.config, capture, view)


def assert_source(rows, body):
    assert len(rows) == 1
    assert rows[0]["source_id"] == "sample-source"
    assert rows[0]["source_revision"] == body["source_revision"]
    assert rows[0]["content"].strip()
    assert rows[0]["record_sha256"].startswith("sha256:")


@pytest.mark.parametrize("exclusive", [False, True])
def test_native_source_facts_reuse_current_usage_lock(source_inputs, exclusive):
    root, fixture, body, _, _, context = source_inputs
    publication = owner("_publication_context")
    with context.activate(), owner("_evidence_usage").current_view(root, fixture.config, exclusive=exclusive) as view:
        if exclusive:
            # The old direct call loses the supplied view in nested inspection.
            source_view = _ScopedView(root)
            _, facts, unprocessed, unknown = _documents(source_view, ["sample-source"], usage_view=view)
            assert facts == {} and unprocessed == 1 and unknown is False
        assert_source(consume(source_inputs, view), body)
        assert publication.captured_view(root, fixture.config) is None
        assert not publication.capture_active()


def test_same_active_capture_is_preserved_and_reset(source_inputs):
    root, fixture, body, _, _, context = source_inputs
    publication = owner("_publication_context")
    with context.activate(), owner("_evidence_usage").current_view(root, fixture.config, exclusive=True) as view:
        with publication.authorized_capture(root, fixture.config, view):
            assert_source(consume(source_inputs, view), body)
            assert publication.captured_view(root, fixture.config) is view
        assert not publication.capture_active()
        assert_source(consume(source_inputs, view), body)
        assert not publication.capture_active()


@pytest.mark.parametrize("foreign", ["root", "config", "view"])
def test_foreign_active_capture_refuses_and_restores(source_inputs, foreign):
    root, fixture, body, _, _, context = source_inputs
    publication = owner("_publication_context")
    usage = owner("_evidence_usage")
    with context.activate(), usage.current_view(root, fixture.config) as view:
        other = usage.current_view(root, fixture.config) if foreign == "view" else nullcontext(view)
        with other as capture_view:
            capture_root = root.parent if foreign == "root" else root
            capture_config = {**fixture.config, "foreign": True} if foreign == "config" else fixture.config
            with publication.authorized_capture(capture_root, capture_config, capture_view):
                with pytest.raises(EvidenceWikiError) as caught:
                    consume(source_inputs, view)
                assert caught.value.error_code == "RESEARCH_REVIEW_DEPENDENCY_INPUTS_CHANGED"
                assert publication.captured_view(capture_root, capture_config) is capture_view
            assert not publication.capture_active()
            assert_source(consume(source_inputs, view), body)


@pytest.mark.parametrize("change", ["normalized", "revocation"])
def test_fresh_source_read_refuses_changed_bytes_or_permission(source_inputs, change):
    root, fixture, body, _, _, context = source_inputs
    usage = owner("_evidence_usage")
    publication = owner("_publication_context")
    with context.activate():
        with usage.current_view(root, fixture.config, exclusive=True) as view:
            assert_source(consume(source_inputs, view), body)
        if change == "normalized":
            path = root / "sources/normalized/sample-source.md"
            path.write_bytes(path.read_bytes() + b"\nChanged source material.\n")
        else:
            fixture.revoke(usage, body)
        with usage.current_view(root, fixture.config, exclusive=True) as view:
            with pytest.raises((EvidenceWikiError, ValueError)):
                consume(source_inputs, view)
            assert not publication.capture_active()


@pytest.mark.parametrize("foreign", ["root", "config", "view"])
def test_warm_source_cache_refuses_foreign_capture_and_clears(source_inputs, foreign):
    root, fixture, body, capture, holder, context = source_inputs
    usage, publication = owner("_evidence_usage"), owner("_publication_context")
    with context.activate(), usage.current_view(root, fixture.config) as view:
        scope = native._CounterScope(holder, root, fixture.config, capture, {}, view, native._MINT)
        scope._token = native._COUNTERS.set(scope)
        try:
            assert_source(consume(source_inputs, view), body)
            assert scope._source_documents is not None and scope._source_view is not None
            other = usage.current_view(root, fixture.config) if foreign == "view" else nullcontext(view)
            with other as capture_view:
                capture_root = root.parent if foreign == "root" else root
                config = {**fixture.config, "foreign": True} if foreign == "config" else fixture.config
                with publication.authorized_capture(capture_root, config, capture_view):
                    with pytest.raises(EvidenceWikiError) as caught:
                        consume(source_inputs, view)
                    assert caught.value.error_code == "RESEARCH_REVIEW_DEPENDENCY_INPUTS_CHANGED"
                    assert scope._source_documents is None and scope._source_view is None
                    assert publication.captured_view(capture_root, config) is capture_view
            assert not publication.capture_active()
        finally:
            scope.clear()
            native._COUNTERS.reset(scope._token)


def test_warm_source_cache_qualifies_again_in_same_capture(source_inputs, monkeypatch):
    root, fixture, body, capture, holder, context = source_inputs
    publication, qualifier = owner("_publication_context"), owner("_publication_usage")
    actual = qualifier.qualify_capture
    qualified = []

    def qualify(*args, **kwargs):
        result = actual(*args, **kwargs)
        qualified.append(result)
        return result

    monkeypatch.setattr(qualifier, "qualify_capture", qualify)
    with context.activate(), owner("_evidence_usage").current_view(root, fixture.config, exclusive=True) as view:
        scope = native._CounterScope(holder, root, fixture.config, capture, {}, view, native._MINT)
        scope._token = native._COUNTERS.set(scope)
        try:
            assert_source(consume(source_inputs, view), body)
            retained = scope._source_view
            with publication.authorized_capture(root, fixture.config, view):
                rows = consume(source_inputs, view)
                assert_source(rows, body)
                assert scope._source_view is retained
                assert publication.captured_view(root, fixture.config) is view
                rows[0]["content"] = "Changed returned copy"
                assert_source(consume(source_inputs, view), body)
            assert len(qualified) == 3
            assert all(row["selected"] == {"sample-source": body["source_revision"]} for row in qualified)
            assert not publication.capture_active()
        finally:
            scope.clear()
            native._COUNTERS.reset(scope._token)


def test_native_inspection_exception_resets_capture_and_releases_lock(source_inputs, monkeypatch):
    from evidence_wiki import retrieval_operations

    root, fixture, body, _, _, context = source_inputs
    usage, publication = owner("_evidence_usage"), owner("_publication_context")
    actual = retrieval_operations._documents
    inspected = []

    def fail_after_native_inspection(*args, **kwargs):
        result = actual(*args, **kwargs)
        assert len(result[1]) == 1 and result[2:] == (0, False)
        inspected.append(result)
        raise RuntimeError("injected inspection failure")

    with context.activate():
        with monkeypatch.context() as fault:
            fault.setattr(retrieval_operations, "_documents", fail_after_native_inspection)
            with pytest.raises(RuntimeError, match="injected inspection failure"):
                with usage.current_view(root, fixture.config, exclusive=True) as view:
                    consume(source_inputs, view)
            assert inspected and not publication.capture_active()
        with usage.current_view(root, fixture.config, exclusive=True) as view:
            assert_source(consume(source_inputs, view), body)
        # A real signed write also proves no usage lock escaped the failed read.
        fixture.revoke(usage, body)
