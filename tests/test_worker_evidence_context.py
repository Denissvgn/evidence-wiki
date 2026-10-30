"""Current signed usage precedes worker material and exact bounded excerpts."""

from types import SimpleNamespace

from evidence_wiki.execution_evidence import proposal_sources, sources
from evidence_wiki.pack_discovery import owner
from evidence_wiki.research_observation import ResearchObservation
from tests.test_mutation_operations import roots as roots
from tests.test_request_lifecycle_adversarial import protected_source
from tests.test_requirements_owner import setup as setup
from tests.test_retrieval_permissions import protected_context


def test_worker_evidence_revocation_removes_packets_and_bindings(setup,tmp_path,monkeypatch):
    root,*_=setup
    _request,authority,body=protected_source(setup,tmp_path,monkeypatch)
    selected=protected_context(setup,authority)
    policy=SimpleNamespace(to_dict=lambda:{"scope":{"source_scope":["sample-source"]}})
    session=SimpleNamespace(root=root,context=selected,policy=policy)
    with selected.activate():
        observed=ResearchObservation(root)
        packets,bindings=sources(session,observed)
        observed.finish()
        assert len(packets)==len(bindings)==1
        assert packets[0]["source_id"]=="sample-source"
        assert bindings[0]["closure_id"].startswith("sha256:")
        authority.revoke(owner("_evidence_usage"),body)
        observed=ResearchObservation(root)
        assert sources(session,observed)==([],[])
        observed.finish()


def test_proposal_excerpt_has_exact_offsets_and_visible_incompleteness():
    body="before "*2000+"Decisive measurement."+" after"*2000
    packet={"source_id":"sample","record_sha256":"sha256:"+"a"*64,"metadata":{},"content":body,
        "source_binding":{"source_id":"sample","extent_ref":"sample","closure_id":"sha256:"+"b"*64}}
    row=proposal_sources([packet],"Decisive measurement",width=128)[0]
    span=row["excerpt"]
    assert row["content"]==body[span["start_char"]:span["end_char"]]
    assert "Decisive measurement." in row["content"]
    assert len(row["content"])<=128 and not row["context_complete"]
    assert row["semantic_adequacy"]=="not_evaluated" and row["excerpt_ref"].startswith("sha256:")
    assert packet["content"]==body
