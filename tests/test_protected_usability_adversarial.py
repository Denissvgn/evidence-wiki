"""Signed current permissions stay distinct from retained extraction facts."""

import os

import pytest

from evidence_wiki import Workspace
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.pack_discovery import owner
from tests.test_mutation_operations import roots as roots
from tests.test_request_lifecycle_adversarial import protected_context, protected_source
from tests.test_requirements_owner import setup as setup

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Native descriptor-relative normalization custody is unavailable.")


def selected(setup, fixture):
    context = protected_context(setup, fixture)
    return HostContext(context.allowed_roots, context.grants | {"source_usability_operations.current"},
        context.actor_ref, context.writable_roots, authority_file=fixture.policy_path,
        state_directory=fixture.host, mutation_control_directory=setup[1])


def test_actual_signed_revision_and_revocation_are_public_current_rights(setup, tmp_path, monkeypatch):
    root, *_ = setup
    _request_id, fixture, body = protected_source(setup, tmp_path, monkeypatch)
    with selected(setup, fixture).activate(), Workspace.open(root) as workspace:
        before = owner("_evidence_revision").capture_workspace(root).revision_id
        current = workspace.sources.usability(["sample-source"])
        assert current["sources"][0]["usability"] == "usable", current
        assert current["current_rights"][0]["state"] == "qualified", current
        assert current["current_rights"][0]["source_revision"] == body["source_revision"]
        assert current["stored_consistency"] == "native_contract_checked"
        assert not current["claim_support_eligible"] and not current["authorized"]
        assert owner("_evidence_revision").capture_workspace(root).revision_id == before
        fixture.revoke(owner("_evidence_usage"), body)
        denied = workspace.sources.usability(["sample-source"])
        assert denied["current_rights"][0]["state"] == "ineligible", denied
        assert not denied["current_rights"][0]["observation"]["eligible"]
        assert denied["sources"][0]["usability"] != "usable"
        assert not denied["claim_support_eligible"]
        assert owner("_evidence_revision").capture_workspace(root).revision_id == before


def test_current_permission_does_not_grant_protected_raw_normalization(setup, tmp_path, monkeypatch):
    root, *_ = setup
    _request_id, fixture, _body = protected_source(setup, tmp_path, monkeypatch)
    with selected(setup, fixture).activate(), Workspace.open(root) as workspace:
        before = (root / "sources/normalized/sample-source.md").read_bytes()
        with pytest.raises(EvidenceWikiError):
            workspace.normalize.sources(source_ids=["sample-source"], force=True)
        assert (root / "sources/normalized/sample-source.md").read_bytes() == before
