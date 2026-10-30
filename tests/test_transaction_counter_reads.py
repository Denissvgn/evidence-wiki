"""Protected counter diagnostics reuse a transaction-owned read replay."""

import sys
from dataclasses import replace

import pytest
import yaml

from evidence_wiki._pack_io import canonical, json_document
from evidence_wiki.pack_discovery import owner
from tests._execution_fixture import authenticate, closure, identifier
from tests._usage_fixture import UsageFixture
from tests.test_coverage_observation import native_case as native_case
from tests.test_draft_admission import draft_root as draft_root


def test_protected_counter_and_materialized_diagnostics_under_writer_lock(native_case, tmp_path, monkeypatch):
    root, normalized, original_context, adapter = native_case
    directory = tmp_path / "permissions"
    directory.mkdir()
    authority = UsageFixture(directory, monkeypatch)
    authority.root = root
    authority.binding = identifier("evidence-host-workspace/v1", {"root": str(root)})
    config = yaml.safe_load((root / "research.yml").read_bytes())
    config.update(authority.config)
    authority.config = config
    (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    claims = json_document((root / config["strict_evidence"]["claims_path"]).read_bytes())
    source_id = claims["claims"][0]["evidence"][0]["source_id"]
    body, _files = authority.source(source_id, normalized=normalized.read_bytes(), retrieval=True)
    descriptor = {"schema_version": "evidence-source-revision/v1", "source_id": source_id, "parents": [],
        "normalized_path": normalized.relative_to(root).as_posix(), "evidence_root": None, "temporal": {}}
    files = {"source-record.json": canonical(descriptor), descriptor["normalized_path"]: normalized.read_bytes()}
    revision = closure(files)
    body["source_revision"] = revision
    body["grant"] = authenticate({**body["grant"]["payload"], "source_revision": revision}, "owner", "usage")
    body["scrub"] = authenticate({**body["scrub"]["payload"], "sanitized_revision": revision}, "runner", "scrubber")
    usage = owner("_evidence_usage")
    authority.transact(usage, "initialize")
    authority.transact(usage, "deposit", body, files)
    selected = replace(original_context, authority_file=authority.policy_path, state_directory=authority.host)
    before = (authority.host / "evidence-state.json").read_bytes()
    publication = owner("_publication_context")
    captures = []
    def profile(frame, event, _result):
        if (event == "call" and frame.f_code.co_name == "authorized_capture"
                and frame.f_code.co_filename.endswith("/_publication_context.py")):
            captures.append((frame.f_locals["root"], frame.f_locals["view"]))

    with selected.activate(), usage.current_view(root, config, exclusive=True) as actual:
        with pytest.raises(ValueError, match="host_state_lock_unavailable"):
            with usage.current_view(root, config):
                pytest.fail("second lock admitted")
        with usage._transaction_observation_reads(root, config, actual):
            previous = sys.getprofile()
            sys.setprofile(profile)
            try:
                result = adapter.observe_questions(root, question_slugs=["q"])[0]
            finally:
                sys.setprofile(previous)
            assert result["owner_observation"]["permission_basis"] == "native_current_usage"
            assert result["anchors"] and result["source_contexts"]
            assert captures and all(view is actual for _, view in captures)
            assert all(path != root for path, _ in captures)
        assert publication.live_read.get() is None
        assert not publication.capture_active()
    assert (authority.host / "evidence-state.json").read_bytes() == before
