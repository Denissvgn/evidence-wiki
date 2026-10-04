"""Actual private usage decisions and source changes guard every source page."""

import importlib
from dataclasses import replace

import pytest
import yaml

from evidence_wiki._pack_io import canonical, json_document
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.mutation_store import capture_revision
from evidence_wiki.pack_discovery import owner
from tests._execution_fixture import authenticate, closure, identifier
from tests._usage_fixture import UsageFixture
from tests.test_coverage_observation import native_case as native_case
from tests.test_draft_admission import draft_root as draft_root


@pytest.fixture
def protected(native_case, tmp_path, monkeypatch):
    root, normalized, context, _adapter = native_case
    operations = importlib.import_module("evidence_wiki.review_page_operations")
    directory = tmp_path / "page-permissions"
    directory.mkdir()
    authority = UsageFixture(directory, monkeypatch)
    authority.root = root
    authority.binding = identifier("evidence-host-workspace/v1", {"root": str(root)})
    config = yaml.safe_load((root / "research.yml").read_bytes())
    config.update(authority.config)
    authority.config = config
    (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    claim = json_document((root / config["strict_evidence"]["claims_path"]).read_bytes())["claims"][0]
    sid = claim["evidence"][0]["source_id"]
    body, _files = authority.source(sid, normalized=normalized.read_bytes())
    descriptor = {"schema_version": "evidence-source-revision/v1", "source_id": sid, "parents": [],
        "normalized_path": normalized.relative_to(root).as_posix(), "evidence_root": "evidence", "temporal": {}}
    files = {"source-record.json": canonical(descriptor), descriptor["normalized_path"]: normalized.read_bytes()}
    files.update({"evidence/" + path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / "raw").rglob("*") if path.is_file() and not any(part.startswith(".") for part in path.relative_to(root).parts)})
    revision = closure(files)
    body["source_revision"] = revision
    body["grant"] = authenticate({**body["grant"]["payload"], "source_revision": revision}, "owner", "usage")
    body["scrub"] = authenticate({**body["scrub"]["payload"], "sanitized_revision": revision}, "runner", "scrubber")
    usage = owner("_evidence_usage")
    authority.transact(usage, "initialize")
    authority.transact(usage, "deposit", body, files)
    selected = replace(context, authority_file=authority.policy_path, state_directory=authority.host,
        grants=context.grants | {"review_page_operations.prepare", "review_page_operations.page", "coverage_observation.observe"})
    return root, normalized, claim, selected, authority, body, operations


def test_actual_current_protected_source_page_has_no_effect(protected):
    root, _normalized, claim, context, authority, _body, operations = protected
    before = capture_revision(root)["revision_id"]
    private_before = (authority.host / "evidence-state.json").read_bytes()
    with context.activate():
        handle = operations.prepare(root, claim_id=claim["id"], context_id="protected-page",
            source_ids=[item["source_id"] for item in claim["evidence"]])
        header = handle.header()
        pages = [handle.page(row["page_id"]) for row in header["pages"]]
        complete = handle.complete(pages)
    assert pages and complete["complete"] and not complete["accepted"]
    assert capture_revision(root)["revision_id"] == before
    assert (authority.host / "evidence-state.json").read_bytes() == private_before


@pytest.mark.parametrize("change", ["withdrawal", "material", "rubric"])
def test_actual_current_owner_change_stops_old_page_before_text(protected, change):
    root, normalized, claim, context, authority, body, operations = protected
    with context.activate():
        handle = operations.prepare(root, claim_id=claim["id"], context_id="old-page")
        page_id = handle.header()["pages"][0]["page_id"]
        if change == "withdrawal":
            authority.revoke(owner("_evidence_usage"), body, whole=True)
        elif change == "material":
            normalized.write_bytes(normalized.read_bytes() + b"\nChanged source content.\n")
        else:
            config = yaml.safe_load((root / "research.yml").read_bytes())
            config["strict_evidence"]["rubric"]["criteria"]["support"] = "A changed required source criterion."
            (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))
        before = capture_revision(root)["revision_id"]
        with pytest.raises((EvidenceWikiError, ValueError)):
            handle.page(page_id)
        assert capture_revision(root)["revision_id"] == before
