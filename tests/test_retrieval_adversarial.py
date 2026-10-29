"""Independent real SQLite, current bytes and canonical gate boundary checks."""

import os
import shutil
import sqlite3

import pytest

from evidence_wiki import retrieval_operations as retrieval
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_store import MutationStore
from evidence_wiki.pack_discovery import owner
from tests.test_mutation_operations import GRANTS
from tests.test_mutation_operations import roots as roots
from tests.test_native_capture_normalization import normalize
from tests.test_retrieval_operations import require_index_serialization

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Native descriptor-relative retrieval custody is unavailable.")


def selected(roots):
    root,control=roots
    return HostContext([root],GRANTS|{"retrieval_operations.search","retrieval_operations.build_index"},"caller",[root],mutation_control_directory=control)


def built(roots,source_id):
    require_index_serialization()
    with selected(roots).activate(),MutationStore(*roots).workspace_locked():
        return retrieval.build_index(roots[0],source_ids=[source_id])


def test_cached_declared_counts_and_unselected_fake_rows_do_not_become_current_stats(roots):
    source_id,_,_=normalize(roots,b"Current measured reflectance 0.74 ratio.\n","plain_text")
    result=built(roots,source_id)
    connection=sqlite3.connect(roots[0]/result["path"])
    connection.execute("UPDATE metadata SET value='999999999999' WHERE key='document_count'")
    connection.execute("INSERT INTO documents_fts(path,scope,kind,title,headings,source_ids,body,headings_json,source_ids_json,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("sources/normalized/other.md","normalized","secret","Private hidden source","", "hidden", "Private malicious reflectance 99", "[]", '["hidden"]',"fake"))
    connection.commit();connection.close()
    with selected(roots).activate():
        current=retrieval.search(roots[0],query="reflectance",source_ids=[source_id],engine="auto",index_path=result["path"])
    assert current["coverage"]["permitted_processed"]==1 and len(current["results"])==1
    assert "Private" not in str(current) and "999999999999" not in str(current)
    assert "0.74" in current["results"][0]["excerpt"]["text"]


@pytest.mark.parametrize("relation",["metadata","documents_fts"])
def test_cache_virtual_views_are_never_hydrated(roots,relation):
    source_id,_,_=normalize(roots,b"Current measured reflectance 0.74 ratio.\n","plain_text")
    result=built(roots,source_id)
    connection=sqlite3.connect(roots[0]/result["path"])
    connection.execute("DROP TABLE "+relation)
    connection.execute("CREATE VIEW "+relation+" AS SELECT zeroblob(2147483647) AS body")
    connection.commit();connection.close()
    with selected(roots).activate():
        current=retrieval.search(roots[0],query="reflectance",source_ids=[source_id],engine="auto",index_path=result["path"])
    assert current["engine"]["fallback"]=="index_schema_or_scope_unsupported"
    assert current["engine"]["used"]=="lexical" and "0.74" in current["results"][0]["excerpt"]["text"]


def test_historical_standalone_native_gate_cannot_publish(roots):
    source_id,_,_=normalize(roots,b"Current measured reflectance 0.74 ratio.\n","plain_text")
    with MutationStore(*roots).locked("historical"):
        pass
    with selected(roots).activate(),MutationStore(*roots).historical_locked("historical"):
        with pytest.raises(EvidenceWikiError) as caught:
            retrieval.build_index(roots[0],source_ids=[source_id])
    assert caught.value.details["reason"]=="host_mutation_workspace_gate_read_only"
    assert not (roots[0]/"sources/indexes").exists()


def test_historical_borrowed_native_read_scope_cannot_publish(roots):
    source_id,_,_=normalize(roots,b"Current measured reflectance 0.74 ratio.\n","plain_text")
    with MutationStore(*roots).locked("historical"):
        pass
    with selected(roots).activate(),MutationStore(*roots).workspace_locked():
        with MutationStore(*roots).historical_locked("historical",borrow_workspace=True):
            with pytest.raises(EvidenceWikiError):
                retrieval.build_index(roots[0],source_ids=[source_id])
    assert not (roots[0]/"sources/indexes").exists()


def test_missing_native_sqlite_serialize_capability_is_finite_typed_refusal_before_output(roots,monkeypatch):
    source_id,_,_=normalize(roots,b"Current measured reflectance 0.74 ratio.\n","plain_text")
    connect=sqlite3.connect
    class WithoutSerialize:
        def __init__(self,*args,**kwargs):self.inner=connect(*args,**kwargs)
        def __getattr__(self,name):
            if name=="serialize":raise AttributeError("native SQLite serialization unavailable")
            return getattr(self.inner,name)
    monkeypatch.setattr(retrieval.sqlite3,"connect",WithoutSerialize)
    with selected(roots).activate(),MutationStore(*roots).workspace_locked():
        with pytest.raises(EvidenceWikiError):
            retrieval.build_index(roots[0],source_ids=[source_id])
    assert not (roots[0]/"sources/indexes").exists()


def test_readonly_rank_cannot_mutate_model_or_query_command_environment(roots,monkeypatch):
    source_id,_,_=normalize(roots,b"Current measured reflectance 0.74 ratio.\n","plain_text")
    native=owner("query_index")
    for method in ("main","query_cache","build_index"):
        if hasattr(native,method):monkeypatch.setattr(native,method,lambda *a,**k:pytest.fail("Legacy command/cache entry invoked"))
    import subprocess
    monkeypatch.setattr(subprocess,"run",lambda *a,**k:pytest.fail("Read-only retrieval invoked a process"))
    with selected(roots).activate():
        current=retrieval.search(roots[0],query="reflectance",source_ids=[source_id],engine="fts")
    assert current["status"]=="found" and not current["provider_calls"] and not current["index_writes"]


def test_cross_workspace_cache_cannot_return_foreign_current_text(roots,tmp_path):
    source_id,_,_=normalize(roots,b"Current measured reflectance 0.74 ratio.\n","plain_text")
    result=built(roots,source_id)
    cache=roots[0]/result["path"]
    foreign=tmp_path/"foreign";shutil.copytree(roots[0],foreign)
    connection=sqlite3.connect(foreign/result["path"])
    connection.execute("UPDATE documents_fts SET body='Foreign secret measured value 99'")
    connection.commit();connection.close()
    cache.write_bytes((foreign/result["path"]).read_bytes())
    with selected(roots).activate():
        current=retrieval.search(roots[0],query="reflectance",source_ids=[source_id],engine="auto",index_path=result["path"])
    assert current["workspace_binding"]["root"]==str(roots[0])
    assert current["engine"]["used"]=="lexical" and "Foreign" not in str(current)
    assert "0.74" in current["results"][0]["excerpt"]["text"]


def test_wrong_index_cas_preserves_actual_prior_bytes(roots):
    source_id,_,_=normalize(roots,b"Current measured reflectance 0.74 ratio.\n","plain_text")
    first=built(roots,source_id);path=roots[0]/first["path"]
    path.write_bytes(b"Existing different host-owned prior bytes")
    before=path.read_bytes()
    with selected(roots).activate(),MutationStore(*roots).workspace_locked():
        with pytest.raises(ValueError,match="normalized_destination_conflict"):
            retrieval.build_index(roots[0],source_ids=[source_id],expected_sha256="sha256:"+"0"*64)
    assert path.read_bytes()==before



def test_selected_original_host_denial_does_not_disclose_permitted_unprocessed_count(roots,monkeypatch):
    from evidence_wiki.errors import ConfigError
    source_id,_,_=normalize(roots,b"Current measured reflectance 0.74 ratio.\n","plain_text")
    forbidden=roots[0]/"raw/web/captured.txt"
    original=HostContext.require_path
    def scope(context,path,**kwargs):
        if path==forbidden:
            raise ConfigError("HOST_CONTEXT_REFUSED","Selected original denied.",details={"reason":"host_path_outside_scope"})
        return original(context,path,**kwargs)
    monkeypatch.setattr(HostContext,"require_path",scope)
    with selected(roots).activate():
        try:
            current=retrieval.search(roots[0],query="reflectance",source_ids=[source_id],engine="lexical")
        except ConfigError:
            return
    assert current["coverage"]["permitted_unprocessed"]==0,current
    assert current["results"]==[] and current["status"]=="unknown"
