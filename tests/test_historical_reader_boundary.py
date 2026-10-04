"""Actual canonical writer denial and parent custody restoration around reads."""

import pytest

from evidence_wiki import Workspace
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_store import MutationStore
from evidence_wiki.pack_discovery import owner
from tests.test_mutation_operations import GRANTS
from tests.test_mutation_operations import roots as roots


def context(roots):
    return HostContext([roots[0]],GRANTS|{"question_claim.run_claim","source_requests.run_add_request"},"caller",[roots[0]],mutation_control_directory=roots[1])


@pytest.mark.parametrize("operation",["claim","request"])
def test_borrowed_reader_denies_other_canonical_writers_then_parent_write_restored(roots,operation):
    with context(roots).activate(),MutationStore(*roots).workspace_locked(),Workspace.open(roots[0]) as workspace:
        with MutationStore(*roots).historical_locked("read",borrow_workspace=True):
            with pytest.raises(EvidenceWikiError) as caught:
                if operation=="claim":workspace.questions.claim(slug="sample",agent_id="caller")
                else:workspace.source_requests.add(kind="web",query_or_identifier="https://example.org/source",rationale="Evidence",question_slugs=["sample"])
        assert caught.value.details["reason"]=="host_mutation_workspace_gate_read_only"
        if operation=="claim":
            workspace.questions.claim(slug="sample",agent_id="caller")
            assert owner("question_status").load_frontmatter(roots[0]/"wiki/questions/sample.md")["status"]=="in_progress"
        else:
            result=workspace.source_requests.add(kind="web",query_or_identifier="https://example.org/source",rationale="Evidence",question_slugs=["sample"])
            assert result["request"]["status"]=="open"


def test_no_context_historical_native_reader_cannot_write_through_legacy_seam(roots):
    with MutationStore(*roots).historical_locked("read"),Workspace.open(roots[0]) as workspace:
        with pytest.raises(EvidenceWikiError):
            workspace.questions.claim(slug="sample",agent_id="caller")
    assert owner("question_status").load_frontmatter(roots[0]/"wiki/questions/sample.md")["status"]=="open"


def test_exception_exit_restores_parent_writer_scope(roots):
    with context(roots).activate(),MutationStore(*roots).workspace_locked(),Workspace.open(roots[0]) as workspace:
        with pytest.raises(RuntimeError,match="exit"):
            with MutationStore(*roots).historical_locked("read",borrow_workspace=True):
                raise RuntimeError("exit")
        workspace.questions.claim(slug="sample",agent_id="caller")
        assert owner("question_status").load_frontmatter(roots[0]/"wiki/questions/sample.md")["status"]=="in_progress"


def test_historical_status_requires_explicit_no_cache_even_without_host_context(roots):
    before = owner("_evidence_revision").capture_workspace(roots[0]).revision_id
    with MutationStore(*roots).historical_locked("read"), Workspace.open(roots[0]) as workspace:
        with pytest.raises(EvidenceWikiError) as refused:
            workspace.status()
        assert refused.value.details["reason"] == "host_mutation_workspace_gate_read_only"
        observed = workspace.status(no_cache=True)
        assert isinstance(observed, dict)
    assert owner("_evidence_revision").capture_workspace(roots[0]).revision_id == before
