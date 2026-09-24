"""First attachment, identity changes and interrupted owner transactions."""

import contextlib
import copy
import io
import json
import os
import shutil
from pathlib import Path

import pytest

from evidence_wiki import cli
from evidence_wiki._pack_io import canonical
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.pack_discovery import owner
from evidence_wiki.pack_migrations import apply, plan

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Explicit lifecycle publishers require POSIX directory descriptors.")


def initialize(path, pack=None):
    argv = ["init", "--target", str(path), "--project-name", "Evidence", "--project-description", "Retained observations",
            "--owner-goal", "Preserve support and uncertainty"]
    if pack:
        argv += ["--domain-pack", pack]
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert cli.main(argv) == 0
    owner("intake_questions").run_intake_document(path, {"schema_version": "1.0", "questions": [
        {"id": "q1", "question": "What is supported?", "priority": "high", "origin": "caller"}]},
        dry_run=False, from_file_label="caller")
    (path / "raw/retained.txt").write_text("Original evidence remains unchanged.\n")
    return path


def request(target, candidate):
    return {"schema_version": "evidence-pack-migration-request/v1", "target": str(target), "path": str(candidate),
        "catalog": None, "revision": None, "rationale": "Explicitly change the research guidance identity",
        "keep_local": [], "accept_pack": [], "mappings": {"policies": {}, "request_kinds": {}, "templates": {}}}


def resolved(request):
    try:
        return plan(canonical(request))
    except EvidenceWikiError as error:
        if "conflicts" not in error.details:
            raise
        request["accept_pack"] = [row["target"] for row in error.details["conflicts"]]
        return plan(canonical(request))


def bytes_of(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and ".locks" not in p.parts and ".replaced" not in p.parts}


def renamed_pack(root):
    path = root / "scoped-science"
    shutil.copytree(ROOT / "domain-packs/general-science", path)
    overlay = path / "research.overlay.yml"
    overlay.write_text(overlay.read_text().replace("general-science", "scoped-science"))
    return path


def test_generic_attachment_preview_preserves_sources_and_requires_reevaluation(tmp_path):
    target = initialize(tmp_path / "workspace")
    before = bytes_of(target)
    value = resolved(request(target, ROOT / "domain-packs/general-science"))
    assert bytes_of(target) == before
    assert value["owner_plan"]["impact"]["identity_migration"]["from"] is None
    assert apply(canonical(value))["status"] == "applied"
    assert owner("_domain_pack_lifecycle").inspect_workspace(target)["state"] == "current"
    assert (target / "raw/retained.txt").read_bytes() == before["raw/retained.txt"]
    assert (target / "wiki/questions/q1.md").read_bytes() == before["wiki/questions/q1.md"]
    assert owner("_pack_revision_guard").pending_question(target, "q1") == value["owner_plan"]["revision_id"]
    after = bytes_of(target)
    assert apply(canonical(value))["status"] == "already_applied"
    assert bytes_of(target) == after


def test_identity_migration_requires_explicit_policy_mapping(tmp_path):
    target = initialize(tmp_path / "workspace", "general-science")
    candidate = renamed_pack(tmp_path)
    wanted = request(target, candidate)
    with pytest.raises(EvidenceWikiError) as caught:
        plan(canonical(wanted))
    assert caught.value.details["group"] == "policies"
    wanted["mappings"]["policies"] = {"pack:general-science/study-recency": "pack:scoped-science/study-recency"}
    value = resolved(wanted)
    old = bytes_of(target / "domain-packs/general-science")
    assert apply(canonical(value))["status"] == "applied"
    assert bytes_of(target / "domain-packs/general-science") == old
    state = owner("_domain_pack_lifecycle").load_state(target)
    assert state["pack"]["name"] == "scoped-science"
    assert state["research_revisions"][-1]["from"]["name"] == "general-science"


@pytest.mark.parametrize("changed", ["candidate", "rationale", "mappings", "resolutions"])
def test_rehashed_replay_cannot_claim_a_different_candidate_or_intent(tmp_path, changed):
    from evidence_wiki._pack_io import capture_pack

    target = initialize(tmp_path / "workspace")
    prepared = resolved(request(target, ROOT / "domain-packs/general-science"))
    apply(canonical(prepared))
    before = bytes_of(target)
    altered = copy.deepcopy(prepared)
    if changed == "candidate":
        candidate = renamed_pack(tmp_path)
        altered["request"]["path"] = str(candidate)
        altered["candidate_sha256"] = capture_pack(candidate).tree_sha256
    elif changed == "rationale":
        altered["request"]["rationale"] = "A different requested migration"
    elif changed == "mappings":
        altered["request"]["mappings"]["policies"] = {"pack:previous/policy": None}
    else:
        altered["request"]["keep_local"] = ["config:/different"]
    altered["plan_id"] = owner("_pack_revision_impact").digest({k: v for k, v in altered.items() if k != "plan_id"})
    with pytest.raises(EvidenceWikiError) as error:
        apply(canonical(altered))
    assert error.value.error_code == "ONBOARDING_PLAN_STALE"
    assert bytes_of(target) == before


def test_migration_saved_plan_refuses_candidate_and_question_drift(tmp_path):
    target = initialize(tmp_path / "workspace")
    candidate = renamed_pack(tmp_path)
    value = resolved(request(target, candidate))
    before = bytes_of(target)
    (candidate / "claims.md").write_text("Changed after planning.\n")
    with pytest.raises(EvidenceWikiError, match="refused"):
        apply(canonical(value))
    assert bytes_of(target) == before
    value = resolved(request(target, candidate))
    (target / "wiki/questions/q1.md").write_text((target / "wiki/questions/q1.md").read_text() + "\nNew context.\n")
    before = bytes_of(target)
    with pytest.raises(EvidenceWikiError):
        apply(canonical(value))
    assert bytes_of(target) == before


def test_first_attachment_recovers_owner_journal(tmp_path, monkeypatch):
    target = initialize(tmp_path / "workspace")
    value = resolved(request(target, ROOT / "domain-packs/general-science"))
    lifecycle = owner("_domain_pack_lifecycle")
    write = lifecycle._atomic_write
    def interrupt(path, content, **kwargs):
        if path == target / "research.yml":
            raise KeyboardInterrupt
        return write(path, content, **kwargs)
    with monkeypatch.context() as selected:
        selected.setattr(lifecycle, "_atomic_write", interrupt)
        with pytest.raises(KeyboardInterrupt):
            apply(canonical(value))
    assert (target / "domain-packs/.evidence-wiki-transaction.yml").is_file()
    assert apply(canonical(value))["status"] == "applied"
    assert not (target / "domain-packs/.evidence-wiki-transaction.yml").exists()
    assert lifecycle.inspect_workspace(target)["state"] == "current"


def test_cli_preserves_typed_migration_refusal(tmp_path):
    source = tmp_path / "input.json"
    source.write_text(json.dumps(request(tmp_path / "absent", ROOT / "domain-packs/general-science")))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(["pack", "migration-plan", "--from-file", str(source)])
    assert code == 2
    assert json.loads(out.getvalue())["error_code"] == "DOMAIN_PACK_STATE_INVALID"
