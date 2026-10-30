"""Actual signed retrieval locks and persistent index output boundaries."""

import os

import pytest

from evidence_wiki import retrieval_operations as retrieval
from evidence_wiki.host_context import HostContext
from evidence_wiki.pack_discovery import owner
from tests.test_mutation_operations import roots as roots
from tests.test_native_capture_normalization import normalize
from tests.test_request_lifecycle_adversarial import protected_source
from tests.test_requirements_owner import setup as setup
from tests.test_retrieval_operations import built, require_index_serialization, selected

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Native descriptor-relative retrieval custody is unavailable.")


def protected_context(setup, fixture):
    root, control, *_ = setup
    from tests.test_request_lifecycle_adversarial import protected_context as selected_host
    original = selected_host(setup, fixture)
    return HostContext([root], original.grants | {"retrieval_operations.search", "retrieval_operations.build_index"},
        "caller", [root], authority_file=fixture.policy_path, state_directory=fixture.host,
        mutation_control_directory=control)


def test_signed_usage_lock_covers_native_ranking_and_later_revocation_hides_results(setup, tmp_path, monkeypatch):
    root, *_ = setup
    _request, fixture, body = protected_source(setup, tmp_path, monkeypatch)
    native = owner("query_index")
    actual = native.rank_documents
    blocked = []
    def rank(*args):
        with pytest.raises(ValueError, match="host_state_lock_unavailable"):
            fixture.revoke(owner("_evidence_usage"), body)
        blocked.append(True)
        return actual(*args)
    with protected_context(setup, fixture).activate():
        with monkeypatch.context() as spy:
            spy.setattr(native, "rank_documents", rank)
            result = retrieval.search(root, query="Measured", source_ids=["sample-source"], engine="lexical")
        assert result["status"] == "found" and blocked == [True]
        assert result["results"][0]["usage_revision"] == body["source_revision"]
        memory = retrieval.search(root, query="Measured", source_ids=["sample-source"], engine="fts")
        assert memory["engine"]["used"] == "fts" and memory["results"][0]["usage_revision"] == body["source_revision"]
        fixture.revoke(owner("_evidence_usage"), body)
        denied = retrieval.search(root, query="Measured", source_ids=["sample-source"], engine="lexical")
        assert denied["results"] == [] and denied["status"] == "unknown"
        assert denied["coverage"]["permitted_processed"] == 0


def test_protected_persistent_index_refuses_without_any_cache_creation(setup, tmp_path, monkeypatch):
    from evidence_wiki.errors import UsageError
    from evidence_wiki.mutation_store import MutationStore
    root, control, *_ = setup
    _request, fixture, _body = protected_source(setup, tmp_path, monkeypatch)
    with protected_context(setup, fixture).activate(), MutationStore(root, control).workspace_locked():
        with pytest.raises(UsageError) as caught:
            retrieval.build_index(root, source_ids=["sample-source"])
    assert caught.value.error_code == "RETRIEVAL_PROTECTED_INDEX_UNSUPPORTED"
    assert not (root / "sources/indexes").exists()


def test_actual_index_barrier_contention_refuses_before_publication(roots):
    require_index_serialization()
    from evidence_wiki.mutation_store import MutationStore
    source_id, _result, _verify = normalize(roots, b"Measured reflectance was 0.74 ratio.\n", "plain_text")
    native = owner("query_index")
    lock = native.query_index_lock_path(roots[0] / "sources/indexes/evidence.sqlite")
    locks = owner("_workspace_locks")
    with locks.workspace_lock(lock, timeout_seconds=0), selected(roots).activate(), MutationStore(*roots).workspace_locked():
        with pytest.raises(locks.LockUnavailableError):
            retrieval.build_index(roots[0], source_ids=[source_id])
    assert not (roots[0] / "sources/indexes/evidence.sqlite").exists()


def test_wrong_prior_hash_never_replaces_native_index(roots):
    from evidence_wiki.mutation_store import MutationStore
    source_id, _result, _verify = normalize(roots, b"Measured reflectance was 0.74 ratio.\n", "plain_text")
    first = built(roots, source_id)
    path = roots[0] / first["path"]
    path.write_bytes(b"Host preserved prior cache bytes")
    with selected(roots).activate(), MutationStore(*roots).workspace_locked():
        with pytest.raises(ValueError, match="normalized_destination_conflict"):
            retrieval.build_index(roots[0], source_ids=[source_id], expected_sha256="sha256:" + "0" * 64)
    assert path.read_bytes() == b"Host preserved prior cache bytes"
