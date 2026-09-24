"""Explicit fleet selections preserve partial success and owner-level recovery."""

import os
import shutil
from pathlib import Path

import pytest

from evidence_wiki._pack_io import canonical
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.fleet_revisions import apply, plan
from tests.test_pack_migrations import bytes_of, initialize

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Explicit lifecycle publishers require POSIX directory descriptors.")


def test_fleet_readonly_plan_and_partial_apply_replay(tmp_path):
    one = initialize(tmp_path / "one", "general-science")
    two = initialize(tmp_path / "two", "general-science")
    candidate = tmp_path / "candidate/general-science"
    shutil.copytree(ROOT / "domain-packs/general-science", candidate)
    (candidate / "claims.md").write_text((candidate / "claims.md").read_text() + "\nPreserve limitations.\n")
    request = {"schema_version": "evidence-fleet-revision-request/v1", "rationale": "Review selected research guidance",
        "candidate": {"path": str(candidate), "catalog": None, "revision": None},
        "workspaces": [{"target": str(root), "keep_local": [], "accept_pack": []} for root in (one, two)]}
    before = [bytes_of(root) for root in (one, two)]
    value = plan(canonical(request))
    assert [bytes_of(root) for root in (one, two)] == before
    assert all(row["status"] == "proposed" and row["semantic_applicability"] == "not_established" for row in value["proposals"])
    (two / "wiki/questions/q1.md").write_text((two / "wiki/questions/q1.md").read_text() + "\nNew scope.\n")
    selected = {"schema_version": "evidence-fleet-revision-apply/v1", "plan": value, "targets": [str(one), str(two)]}
    result = apply(canonical(selected))
    assert result["status"] == "partial"
    assert [row["status"] for row in result["workspaces"]] == ["applied", "failed"]
    assert apply(canonical(selected))["workspaces"][0]["status"] == "already_applied"
    with pytest.raises(EvidenceWikiError):
        apply(canonical({**selected, "targets": []}))


def test_overlapping_workspaces_are_not_independent(tmp_path):
    one = initialize(tmp_path / "one", "general-science")
    value = {"schema_version": "evidence-fleet-revision-request/v1", "rationale": "Inspect explicitly selected workspaces",
        "candidate": {"path": str(ROOT / "domain-packs/general-science"), "catalog": None, "revision": None},
        "workspaces": [{"target": str(root), "keep_local": [], "accept_pack": []} for root in (one, one / "nested")]}
    with pytest.raises(EvidenceWikiError, match="refused"):
        plan(canonical(value))
