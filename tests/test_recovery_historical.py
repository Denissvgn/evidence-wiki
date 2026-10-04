"""Canonical historical custody permits recovery reads but no recovery writes."""

from dataclasses import replace

import pytest

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.mutation_store import MutationStore
from evidence_wiki.pack_discovery import owner
from tests import test_execution_session as execution_cases


def test_actual_historical_recovery_status_reads_without_any_workspace_or_journal_effect(tmp_path, monkeypatch):
    root, policy, execution, context = execution_cases.fixture.__wrapped__(tmp_path, monkeypatch)
    context = replace(context, grants=context.grants | {"research_session.recovery_status", "research_session.reconcile_recovery"})
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="historical-read", execution=execution) as session:
            with native.gate(session):
                native._begin(session)
            records = execution.records(session)
            before = owner("_evidence_revision").capture_workspace(root).revision_id
            with MutationStore(root, context.mutation_control_directory).historical_locked("read-current"):
                value = workspace.research.recovery_status(policy, run_id="historical-read", execution=execution)
                assert value["status"] == "reconcilable" and not value["authorized"]
                assert session.recovery_status()["journal_sha256"] == value["journal_sha256"]
                with pytest.raises(EvidenceWikiError):
                    session.reconcile_recovery(request_id="forbidden", expected_basis_id=value["current_basis_id"])
            assert execution.records(session) == records
            assert owner("_evidence_revision").capture_workspace(root).revision_id == before
