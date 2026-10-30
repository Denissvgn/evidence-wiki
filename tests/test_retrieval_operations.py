"""Actual current bytes, permission filtering and bounded lexical observations."""

import copy
import os
import sqlite3

import pytest

from evidence_wiki import retrieval_operations as retrieval
from evidence_wiki.host_context import HostContext
from evidence_wiki.pack_discovery import owner
from tests.test_mutation_operations import GRANTS
from tests.test_mutation_operations import roots as roots
from tests.test_native_capture_normalization import normalize

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Native descriptor-relative retrieval custody is unavailable.")


def selected(roots):
    root, control = roots
    return HostContext([root], GRANTS | {"retrieval_operations.search", "retrieval_operations.build_index"},
        "caller", [root], mutation_control_directory=control)


def test_actual_lexical_excerpt_and_closing_revision(roots):
    source_id, _result, _verify = normalize(roots, b"Measured reflectance was 0.74 ratio for the selected specimen.\n", "plain_text")
    with selected(roots).activate():
        result = retrieval.search(roots[0], query="reflectance", source_ids=[source_id], engine="lexical")
    assert result["status"] == "found", result
    row = result["results"][0]
    assert row["source_id"] == source_id
    assert "0.74 ratio" in row["excerpt"]["text"]
    assert row["excerpt"]["location_basis"] == "exact_normalized_body"
    assert row["usage_revision"] is None and row["permission"]["compatibility"] == "legacy-research"
    assert not result["index_writes"] and not result["provider_calls"] and not result["evidence_accepted"]


def test_denied_source_never_reaches_native_ranking_or_disclosed_counts(roots, monkeypatch):
    source_id, _result, _verify = normalize(roots, b"Private forbidden measured original.\n", "plain_text")
    native = owner("normalize_sources")
    path = roots[0] / "sources/normalized" / (native.safe_source_id(source_id) + ".md")
    path.write_text(path.read_text().replace("---\n", "---\nretrieval_eligible: false\n", 1))
    documents = []
    actual = owner("query_index").rank_documents
    def rank(rows, *args):
        documents.extend(copy.deepcopy(rows))
        return actual(rows, *args)
    with selected(roots).activate(), monkeypatch.context() as spy:
        spy.setattr(owner("query_index"), "rank_documents", rank)
        result = retrieval.search(roots[0], query="Private", source_ids=[source_id], engine="lexical")
    assert not documents and result["status"] == "unknown"
    assert result["coverage"]["permitted_processed"] == 0
    assert result["coverage"]["permitted_unprocessed"] == 0
    assert result["results"] == []
    assert "Private" not in str({key: value for key, value in result.items() if key != "query"})


def test_source_change_during_rank_refuses_instead_of_returning_old_excerpt(roots, monkeypatch):
    source_id, _result, _verify = normalize(roots, b"Current measured source context.\n", "plain_text")
    native = owner("query_index")
    actual = native.rank_documents
    def rank(*args):
        rows = actual(*args)
        (roots[0] / "raw/web/captured.txt").write_bytes(b"Changed original context.")
        return rows
    from evidence_wiki.errors import EvidenceWikiError
    with selected(roots).activate(), monkeypatch.context() as faults:
        faults.setattr(native, "rank_documents", rank)
        with pytest.raises(EvidenceWikiError):
            retrieval.search(roots[0], query="measured", source_ids=[source_id], engine="lexical")


def test_selected_eligible_unnormalized_source_is_unknown_with_unprocessed_count(roots):
    with selected(roots).activate():
        result = retrieval.search(roots[0], query="measured", source_ids=["sample-source"], engine="lexical")
    assert result["status"] == "unknown" and result["coverage"]["permitted_unprocessed"] == 1


def test_empty_match_differs_from_unknown_or_incomplete_selection(roots):
    source_id, _result, _verify = normalize(roots, b"Measured reflectance was 0.74 ratio.\n", "plain_text")
    with selected(roots).activate():
        result = retrieval.search(roots[0], query="zzzzzzzzunknownterm", source_ids=[source_id], engine="lexical")
    assert result["status"] == "empty" and result["coverage"]["complete"]


def test_native_memory_fts_and_lexical_retrieve_current_same_selected_record(roots):
    source_id, _result, _verify = normalize(roots, b"Measured reflectance was 0.74 ratio.\n", "plain_text")
    with selected(roots).activate():
        lexical = retrieval.search(roots[0], query="reflectance", source_ids=[source_id], engine="lexical")
        indexed = retrieval.search(roots[0], query="reflectance", source_ids=[source_id], engine="fts")
    assert indexed["engine"]["used"] == "fts", indexed
    assert indexed["engine"]["index_status"] == "eligible_memory_snapshot"
    assert indexed["results"][0]["source_revision"] == lexical["results"][0]["source_revision"]
    assert indexed["results"][0]["excerpt"] == lexical["results"][0]["excerpt"]
    assert not indexed["index_writes"]


def require_index_serialization():
    if not all(callable(getattr(sqlite3.Connection, name, None)) for name in ("serialize", "deserialize")):
        pytest.skip("This native index fixture requires SQLite serialize/deserialize support.")


def built(roots, source_id):
    require_index_serialization()
    from evidence_wiki.mutation_store import MutationStore
    with selected(roots).activate(), MutationStore(*roots).workspace_locked():
        return retrieval.build_index(roots[0], source_ids=[source_id])


def test_explicit_native_index_build_and_readonly_hash_validated_search(roots):
    source_id, _result, _verify = normalize(roots, b"Measured reflectance was 0.74 ratio.\n", "plain_text")
    result = built(roots, source_id)
    path = roots[0] / result["path"]
    before = path.read_bytes()
    with selected(roots).activate():
        found = retrieval.search(roots[0], query="reflectance", source_ids=[source_id], engine="auto", index_path=result["path"])
    assert found["engine"]["used"] == "fts" and found["engine"]["index_status"] == "current", found
    assert path.read_bytes() == before
    assert not found["index_writes"]


def test_cached_body_tampering_falls_back_to_current_bytes(roots):
    import sqlite3
    source_id, _result, _verify = normalize(roots, b"Measured reflectance was 0.74 ratio.\n", "plain_text")
    result = built(roots, source_id)
    connection = sqlite3.connect(roots[0] / result["path"])
    try:
        connection.execute("UPDATE documents_fts SET body='Cached forbidden false value 99'")
        connection.commit()
    finally:
        connection.close()
    with selected(roots).activate():
        found = retrieval.search(roots[0], query="reflectance", source_ids=[source_id], engine="auto", index_path=result["path"])
    assert found["engine"]["used"] == "lexical" and found["engine"]["fallback"] == "index_stale_content", found
    assert "0.74" in found["results"][0]["excerpt"]["text"]
    assert "forbidden" not in str(found)


def test_index_write_without_actual_native_gate_refuses(roots):
    from evidence_wiki.errors import EvidenceWikiError
    with selected(roots).activate(), pytest.raises(EvidenceWikiError):
        retrieval.build_index(roots[0], source_ids=["sample-source"])
    assert not (roots[0] / "sources/indexes").exists()


def test_matching_current_body_with_changed_capture_scope_cannot_use_old_index(roots):
    import json
    source_id, _result, _verify = normalize(roots, b"Measured reflectance was 0.74 ratio.\n", "plain_text")
    result = built(roots, source_id)
    manifest = roots[0] / "sources/manifest.jsonl"
    records = [json.loads(line) for line in manifest.read_bytes().splitlines()]
    target = next(row for row in records if row["id"] == source_id)
    target["provenance"]["scope"]["other_declared_key"] = "different"
    manifest.write_text("".join(json.dumps(row) + "\n" for row in records))
    with selected(roots).activate():
        found = retrieval.search(roots[0], query="reflectance", source_ids=[source_id], engine="auto", index_path=result["path"])
    assert found["engine"]["used"] == "lexical", found
    assert found["status"] == "unknown" and found["coverage"]["permitted_unprocessed"] == 1, found
    assert found["results"] == [], found


@pytest.mark.parametrize("kind", ["null_body", "deep_metadata"])
def test_malformed_cached_rows_or_metadata_never_leak_or_throw_untyped(roots, kind):
    import sqlite3
    source_id, _result, _verify = normalize(roots, b"Measured reflectance was 0.74 ratio.\n", "plain_text")
    result = built(roots, source_id)
    connection = sqlite3.connect(roots[0] / result["path"])
    try:
        if kind == "null_body":
            connection.execute("UPDATE documents_fts SET body=NULL")
        else:
            connection.execute("UPDATE metadata SET value=? WHERE key='selected_source_basis'", ["[" * 1500 + "null" + "]" * 1500])
        connection.commit()
    finally:
        connection.close()
    with selected(roots).activate():
        result = retrieval.search(roots[0], query="reflectance", source_ids=[source_id], engine="fts", index_path=result["path"])
    assert result["engine"]["used"] == "lexical" and result["engine"]["fallback"] == "index_unavailable", result
    assert "0.74" in result["results"][0]["excerpt"]["text"]


@pytest.mark.parametrize("values", [{"query": "\ud800"}, {"source_ids": ["\ud800"]}, {"limit": True}, {"engine": {}}, {"source_ids": []}])
def test_hostile_types_refuse_before_native_rank_or_path_read(roots, values, monkeypatch):
    from evidence_wiki.errors import UsageError
    monkeypatch.setattr(owner("query_index"), "rank_documents", lambda *args: pytest.fail("Invalid request reached ranking"))
    arguments = {"query": "study", "source_ids": ["sample-source"], "engine": "lexical", **values}
    with selected(roots).activate(), pytest.raises(UsageError):
        retrieval.search(roots[0], **arguments)


@pytest.mark.parametrize("relation", ["metadata", "documents_fts"])
def test_hostile_cache_views_cannot_expand_computed_rows_before_schema_refusal(roots, relation):
    import sqlite3
    source_id, _result, _verify = normalize(roots, b"Measured reflectance was 0.74 ratio.\n", "plain_text")
    result = built(roots, source_id)
    connection = sqlite3.connect(roots[0] / result["path"])
    try:
        connection.execute("DROP TABLE " + relation)
        connection.execute("CREATE VIEW " + relation + " AS SELECT zeroblob(2147483647) AS value")
        connection.commit()
    finally:
        connection.close()
    with selected(roots).activate():
        found = retrieval.search(roots[0], query="reflectance", source_ids=[source_id], engine="auto", index_path=result["path"])
    assert found["engine"]["used"] == "lexical"
    assert found["engine"]["fallback"] == "index_schema_or_scope_unsupported", found
    assert "0.74" in found["results"][0]["excerpt"]["text"]


def test_denied_material_changes_do_not_change_disclosed_observation_commitment(roots):
    source_id, _result, _verify = normalize(roots, b"Private forbidden measured original.\n", "plain_text")
    native = owner("normalize_sources")
    path = roots[0] / "sources/normalized" / (native.safe_source_id(source_id) + ".md")
    path.write_text(path.read_text().replace("---\n", "---\nretrieval_eligible: false\n", 1))
    with selected(roots).activate():
        first = retrieval.search(roots[0], query="Private", source_ids=[source_id], engine="lexical")
        path.write_text(path.read_text() + "\nDifferent withheld body content.\n")
        second = retrieval.search(roots[0], query="Private", source_ids=[source_id], engine="lexical")
    assert first["observation"] == second["observation"]
    assert first["coverage"] == second["coverage"]
    assert first["results"] == second["results"] == []
