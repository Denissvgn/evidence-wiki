"""Current source excerpts and explicit index request replay through the core API."""

import os
import sqlite3

import pytest

from evidence_wiki import Workspace
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.onboarding_contract import _matches
from evidence_wiki.pack_discovery import owner
from evidence_wiki.source_contracts import schema_document
from tests.test_mutation_operations import roots as roots
from tests.test_native_capture_normalization import normalize
from tests.test_retrieval_operations import require_index_serialization, selected

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Native descriptor-relative retrieval custody is unavailable.")


def test_public_search_is_exact_bounded_readonly_and_schema_decodable(roots):
    source_id, _result, _verify = normalize(roots,
        b"Measured reflectance was 0.74 ratio.\n  Original whitespace and limitations remain.\n", "plain_text")
    root, _ = roots
    with selected(roots).activate(), Workspace.open(root) as workspace:
        before = owner("_evidence_revision").capture_workspace(root).revision_id
        view = workspace.sources.search("reflectance", source_ids=[source_id], engine="lexical")
        _matches(view, schema_document(view["schema_version"]))
        assert view["status"] == "found" and "0.74 ratio" in view["results"][0]["excerpt"]["text"]
        assert view["results"][0]["excerpt"]["location_basis"] == "exact_normalized_body"
        assert view["results"][0]["usage_revision"] is None
        assert not view["provider_calls"] and not view["index_writes"] and not view["evidence_accepted"]
        assert owner("_evidence_revision").capture_workspace(root).revision_id == before


def test_public_serialized_index_is_replayed_and_stale_cache_falls_back_without_rebuild(roots):
    require_index_serialization()
    source_id, _result, _verify = normalize(roots, b"Measured reflectance was 0.74 ratio with context.\n", "plain_text")
    root, _ = roots
    with selected(roots).activate(), Workspace.open(root) as workspace:
        request = workspace.mutations.prepare(request_id="index", operation="sources.build_index",
            arguments={"source_ids": [source_id]})
        receipt = workspace.sources.build_index(request)
        assert receipt["status"] == "committed", receipt
        _matches(receipt["result"], schema_document(receipt["result"]["schema_version"]))
        assert workspace.sources.build_index(request) == receipt
        path = receipt["result"]["path"]
        before = (root / path).read_bytes()
        indexed = workspace.sources.search("reflectance", source_ids=[source_id], index_path=path)
        assert indexed["engine"]["used"] == "fts" and indexed["engine"]["index_status"] == "current"
        assert (root / path).read_bytes() == before
        connection = sqlite3.connect(root / path)
        try:
            connection.execute("UPDATE documents_fts SET body='Foreign cached evidence'")
            connection.commit()
        finally:
            connection.close()
        changed = (root / path).read_bytes()
        fallback = workspace.sources.search("reflectance", source_ids=[source_id], index_path=path)
        assert fallback["engine"]["used"] == "lexical" and fallback["engine"]["fallback"] == "index_stale_content"
        assert "0.74 ratio" in fallback["results"][0]["excerpt"]["text"]
        assert (root / path).read_bytes() == changed


def test_closed_source_handle_cannot_load_retrieval_owner(roots, monkeypatch):
    from evidence_wiki import retrieval_operations
    workspace = Workspace.open(roots[0])
    namespace = workspace.sources
    workspace.close()
    monkeypatch.setattr(retrieval_operations, "search", lambda *args, **kwargs: pytest.fail("Closed handle loaded search"))
    with pytest.raises(EvidenceWikiError):
        namespace.search("query", source_ids=["sample-source"])
