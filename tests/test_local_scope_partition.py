"""Nonlocal declarations cannot enlarge independent local evidence authority."""

import json

import pytest

from evidence_wiki import Workspace
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_contracts import canonical
from evidence_wiki.pack_discovery import owner
from tests import test_draft_admission as admission


@pytest.fixture
def scoped(tmp_path, monkeypatch):
    value = admission.draft_root.__wrapped__(tmp_path, monkeypatch)
    context = HostContext([value[0]], admission.GRANTS | {"claim_diagnostics.current"}, "caller", [value[0]],
        mutation_control_directory=value[1])
    return value, context


def declare(root, scopes):
    path = root / "docs/research-requirements.json"
    document = json.loads(path.read_bytes())
    document["request"]["payload"]["authority"]["source_scope"] = scopes
    path.write_bytes(canonical(document) + b"\n")
    admission.update_policy(root)


def test_mixed_uri_declaration_keeps_local_diagnostics_and_unaccepted_admission(scoped):
    (root, _control, proposal, _record), context = scoped
    declare(root, [str(root / "raw"), "https://example.org/study"])
    with context.activate(), Workspace.open(root) as workspace:
        before = owner("_evidence_revision").capture_workspace(root).revision_id
        diagnosed = workspace.strict.diagnose(proposal)
        assert diagnosed["claims"][0]["mechanical_result"] == "verified"
        assert diagnosed["accepted"] is False
        assert owner("_evidence_revision").capture_workspace(root).revision_id == before
        expected = workspace.mutations.revision()["revision_id"]
        admitted = workspace.strict.admit(proposal, request_id="mixed-admit", expected_revision=expected)
        assert admitted["status"] == "committed"
        assert admitted["result"]["accepted"] is False and admitted["result"]["review_registered"] is False
        assert not owner("strict_evidence").run_operation(root, "check")["claims"][0]["accepted"]


@pytest.mark.parametrize("scopes", [["https://example.org/study"], ["https://other.example/ungranted"]])
def test_uri_declaration_alone_never_authorizes_local_files(scoped, scopes):
    (root, _control, proposal, _record), context = scoped
    declare(root, scopes)
    before = admission.observations(root)
    with context.activate(), Workspace.open(root) as workspace, pytest.raises(EvidenceWikiError):
        workspace.strict.diagnose(proposal)
    assert admission.observations(root) == before
