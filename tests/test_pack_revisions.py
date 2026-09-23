"""Revision impact, frozen requirements, migration and safe installed-owner routing."""

import contextlib
import io
import json
import shutil
from pathlib import Path
from unittest import mock

import pytest
import yaml

from evidence_wiki import cli
from evidence_wiki._pack_io import canonical
from evidence_wiki.pack_discovery import owner
from evidence_wiki.pack_revisions import apply, plan, status
from tests.test_strict_evidence import host as host

ROOT = Path(__file__).resolve().parents[1]


def invoke(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def workspace(tmp_path):
    target = tmp_path / "workspace"
    code, _, err = invoke("init", "--target", str(target), "--project-name", "revision-research",
        "--project-description", "Evidence criteria", "--owner-goal", "Retain evidence", "--domain-pack", "general-science")
    assert code == 0, err
    owner("intake_questions").run_intake_document(target, {"schema_version": "1.0", "questions": [
        {"id": "q1", "question": "What evidence supports the claim?", "priority": "high", "origin": "caller"},
        {"id": "q2", "question": "What is the other claim?", "priority": "high", "origin": "caller"}]}, dry_run=False, from_file_label="caller")
    return target


@pytest.fixture
def candidate(tmp_path):
    target = tmp_path / "candidate/general-science"
    shutil.copytree(ROOT / "domain-packs/general-science", target)
    overlay = yaml.safe_load((target / "research.overlay.yml").read_bytes())
    overlay["domain_pack"]["version"] = "0.2.0"
    (target / "research.overlay.yml").write_text(yaml.safe_dump(overlay, sort_keys=False))
    return target


def request(workspace, candidate):
    return {"schema_version": "evidence-pack-revision-request/v1", "target": str(workspace), "path": str(candidate),
        "catalog": None, "revision": None, "rationale": "Clarify evidence requirements after observed gaps.", "keep_local": [], "accept_pack": []}


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file() and ".locks" not in p.parts}


def template():
    return {"coverage_profile": "reviewed", "required_facets": [{"facet_id": "evidence", "description": "Confirm supporting evidence.",
        "required": True, "evidence_path": "academic_method_existence", "source_policy": "academic_indexed",
        "freshness_policy": "publication_identity", "identity_policy": "citation_id_resolves", "min_sources": 1}], "optional_facets": []}


def migration(value, *, slug="q1"):
    return {"schema_version": "evidence-pack-reevaluation/v1", "revision_id": value["owner_plan"]["revision_id"],
        "slug": slug, "rationale": "Reviewed the revised guidance and explicit evidence facets.", "template": template(),
        "retired_facets": [], "request_replacements": {}, "computation_migrations": {}}


def test_plan_is_readonly_apply_replays_and_status_retains_reason(workspace, candidate):
    (candidate / "claims.md").write_text((candidate / "claims.md").read_text() + "\nClarify uncertainty.\n")
    before = files(workspace)
    value = plan(request(workspace, candidate))
    impact = value["owner_plan"]["impact"]
    assert impact["bounds"]["questions_affected"] == 2
    assert impact["bounds"]["questions_scanned"] == 2 and not impact["bounds"]["truncated"]
    assert impact["semantic_equivalence"] == "not_established"
    assert files(workspace) == before
    result = apply(canonical(value))
    assert result["status"] == "applied"
    assert result["research"]["pending_questions"] == ["q1", "q2"]
    after = files(workspace)
    assert apply(canonical(value))["status"] == "already_applied"
    assert files(workspace) == after
    assert result["research"]["revisions"][-1]["rationale"] == value["request"]["rationale"]
    assert (workspace / "wiki/questions/q1.md").read_bytes() == before["wiki/questions/q1.md"]
    history = owner("_domain_pack_lifecycle").load_state(workspace)["research_revisions"][-1]["research_before"]
    assert history["wiki/questions/q1.md"].encode() == before["wiki/questions/q1.md"]


def test_version_only_leaves_unaffected_coverage_alone(workspace, candidate):
    value = plan(request(workspace, candidate))
    assert value["owner_plan"]["impact"]["questions"] == []
    assert apply(canonical(value))["research"]["pending_questions"] == []


def test_question_scan_uses_canonical_question_surface(workspace, candidate):
    (workspace / "wiki/questions/README.md").write_text("Navigation only.\n")
    nested = workspace / "wiki/questions/archive"
    nested.mkdir()
    (nested / "prior.md").write_text("---\ntype: question\nstatus: in_progress\n---\nHistorical note.\n")
    value = plan(request(workspace, candidate))
    bounds = value["owner_plan"]["impact"]["bounds"]
    assert bounds["questions_scanned"] == 2 and bounds["question_files_scanned"] == 3
    assert bounds["non_question_files"] == ["wiki/questions/README.md"]
    assert apply(canonical(value))["status"] == "applied"


@pytest.mark.parametrize("path", ["wiki/questions/q1.md", "sources/new.json", "sources/notes.lock", "AGENTS.md", "research.yml"])
def test_saved_plan_refuses_workspace_drift(workspace, candidate, path):
    value = plan(request(workspace, candidate))
    p = workspace / path
    p.write_text((p.read_text() if p.exists() else "{}") + "\n")
    before = files(workspace)
    with pytest.raises(Exception) as caught:
        apply(canonical(value))
    assert caught.value.error_code == "DOMAIN_PACK_REFRESH_CONFLICT"
    assert files(workspace) == before


def test_saved_plan_refuses_candidate_drift(workspace, candidate):
    value = plan(request(workspace, candidate))
    (candidate / "claims.md").write_text("Changed after review.\n")
    before = files(workspace)
    with pytest.raises(Exception) as caught:
        apply(canonical(value))
    assert caught.value.error_code == "ONBOARDING_PLAN_STALE"
    assert files(workspace) == before


@pytest.mark.parametrize("kind", ["bound", "legacy", "managed", "claim"])
def test_active_work_refuses_direct_refresh(workspace, candidate, kind):
    if kind == "managed":
        p = workspace / "runs/orchestrations/work/session.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"status": "active", "pending_action_id": "work"}))
    elif kind == "claim":
        owner("question_claim").run_claim(workspace, slug="q1", agent_id="owner")
    else:
        controller = owner("run_controller")
        controller.run_start(workspace, controller.parse_args(["start", "--run-id", "work", "--agent-id", "owner"]))
        if kind == "legacy":
            p = workspace / "runs/work/run-state.json"
            value = json.loads(p.read_bytes()); value.pop("requirement_basis")
            p.write_text(json.dumps(value))
    before = files(workspace)
    code, _, err = invoke("pack", "refresh", "--target", str(workspace), "--path", str(candidate), "--format", "json")
    assert code == 3, err
    assert json.loads(err)["error_code"] == "DOMAIN_PACK_REVISION_CONFLICT"
    assert files(workspace) == before


def test_changed_pack_bytes_freeze_child_mutations(workspace):
    controller = owner("run_controller")
    controller.run_start(workspace, controller.parse_args(["start", "--run-id", "work", "--agent-id", "owner"]))
    (workspace / "domain-packs/general-science/claims.md").write_text("Different criteria\n")
    with pytest.raises(Exception) as caught:
        controller.run_heartbeat(workspace, controller.parse_args(["heartbeat", "--run-id", "work", "--agent-id", "owner"]))
    assert caught.value.details["reason"] == "run_requirements_changed_or_unbound"


def test_migration_archives_history_resets_stronger_facets_and_is_idempotent(workspace, candidate):
    coverage = owner("coverage_manifest")
    config = coverage.load_config(workspace)
    coverage.run_init_document(workspace, config, slug="q1", template=template())
    path = workspace / "sources/coverage/q1.yml"
    old = yaml.safe_load(path.read_bytes())
    old["required_facets"][0]["accepted_source_ids"] = ["retained-source"]
    path.write_text(yaml.safe_dump(old))
    before = path.read_bytes()
    (candidate / "claims.md").write_text((candidate / "claims.md").read_text() + "\nStronger evidence required.\n")
    value = plan(request(workspace, candidate)); apply(canonical(value))
    document = migration(value)
    document["template"]["required_facets"][0]["min_sources"] = 2
    result = coverage.run_revision(workspace, document)
    assert result["status"] == "migrated" and not result["release_accepted"]
    history = json.loads((workspace / result["archive"]).read_bytes())
    assert history["manifest"] == before.decode()
    new = yaml.safe_load(path.read_bytes())
    assert new["required_facets"][0]["accepted_source_ids"] == []
    assert result["coverage"]["coverage_verdict"] == "blocked"
    after = files(workspace)
    assert coverage.run_revision(workspace, document)["status"] == "already_migrated"
    assert files(workspace) == after


def test_migration_requires_explicit_retirements_and_refuses_forged_evidence(workspace, candidate):
    coverage = owner("coverage_manifest")
    coverage.run_init_document(workspace, coverage.load_config(workspace), slug="q1", template=template())
    (candidate / "claims.md").write_text("New guidance\n")
    value = plan(request(workspace, candidate)); apply(canonical(value))
    for kind in ("retire", "evidence"):
        document = migration(value)
        if kind == "retire":
            document["template"]["required_facets"] = []
        else:
            document["template"]["required_facets"][0]["accepted_source_ids"] = ["invented"]
        before = files(workspace)
        with pytest.raises(Exception) as caught:
            coverage.run_revision(workspace, document)
        assert caught.value.error_code == "DOMAIN_PACK_REVISION_CONFLICT"
        assert files(workspace) == before


def test_migration_interruption_preserves_old_hold_and_retries(workspace, candidate):
    (candidate / "claims.md").write_text("New guidance\n")
    value = plan(request(workspace, candidate)); apply(canonical(value))
    revision = owner("coverage_manifest").load_sibling_module("_coverage_revision")
    publisher = revision.sibling("_usage_materialization")
    real = publisher.publish_file

    def interrupted(root, path, *args, **kwargs):
        if path == "sources/coverage/q1.yml":
            raise OSError("Interrupted")
        return real(root, path, *args, **kwargs)

    with mock.patch.object(publisher, "publish_file", side_effect=interrupted), pytest.raises(OSError):
        owner("coverage_manifest").run_revision(workspace, migration(value))
    assert status(workspace)["questions"][0]["coverage"]["result"]["error_code"] == "COVERAGE_REVISION_REQUIRED"
    assert owner("coverage_manifest").run_revision(workspace, migration(value))["status"] == "migrated"


@pytest.mark.parametrize("mutation", ["remove", "edit"])
def test_missing_or_changed_migration_archive_cannot_clear_hold(workspace, candidate, mutation):
    (candidate / "claims.md").write_text("New reviewed guidance.\n")
    value = plan(request(workspace, candidate)); apply(canonical(value))
    result = owner("coverage_manifest").run_revision(workspace, migration(value))
    archive = workspace / result["archive"]
    if mutation == "remove":
        archive.unlink()
    else:
        archive.write_text(archive.read_text() + "\n")
    summary = status(workspace)["questions"][0]["coverage"]["result"]
    assert summary["error_code"] == "COVERAGE_REVISION_REQUIRED"
    with pytest.raises(Exception) as caught:
        owner("coverage_manifest").run_revision(workspace, migration(value))
    assert isinstance(caught.value, OSError) or getattr(caught.value, "error_code", None) == "DOMAIN_PACK_REVISION_CONFLICT"
    (candidate / "claims.md").write_text("Another guidance revision.\n")
    subsequent = plan(request(workspace, candidate))
    row = next(row for row in subsequent["owner_plan"]["impact"]["questions"] if row["slug"] == "q1")
    assert value["owner_plan"]["revision_id"] in row["migration_requirements"]["prior_revisions"]


def test_revision_cli_discovery_and_plan_output(workspace, candidate, tmp_path):
    path = tmp_path / "plan.json"
    code, result, err = invoke("pack", "revision-plan", "--target", str(workspace), "--path", str(candidate), "--rationale", "Fix observed gap", "--output", str(path))
    assert code == 0, (result, err)
    assert path.is_file()
    code, result, err = invoke("pack", "revision-apply", "--from-file", str(path))
    assert code == 0 and json.loads(result)["status"] == "applied", (result, err)
    code, result, err = invoke("pack", "schemas")
    assert code == 0 and "evidence-pack-revision-plan/v1" in json.loads(result)["schema_ids"]
    code, result, err = invoke("pack", "guide", "--topic", "revisions")
    assert code == 0 and "semantic" in result


def change_overlay(candidate, edit):
    path = candidate / "research.overlay.yml"
    document = yaml.safe_load(path.read_bytes())
    edit(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False))


def test_policy_changes_scope_coverage_and_removed_ids_require_mapping(workspace, candidate):
    policy = "pack:general-science/quality"
    change_overlay(candidate, lambda d: d["domain_pack"]["policy_vocabularies"].update(source_policy={policy: "Require a reviewed quality judgment."}))
    apply(canonical(plan(request(workspace, candidate))))
    coverage = owner("coverage_manifest")
    selected = template(); selected["required_facets"][0]["source_policy"] = policy
    coverage.run_init_document(workspace, coverage.load_config(workspace), slug="q1", template=selected)
    coverage.run_init_document(workspace, coverage.load_config(workspace), slug="q2", template=template())
    other = (workspace / "sources/coverage/q2.yml").read_bytes()
    change_overlay(candidate, lambda d: d["domain_pack"]["policy_vocabularies"]["source_policy"].pop(policy))
    value = plan(request(workspace, candidate))
    assert [row["slug"] for row in value["owner_plan"]["impact"]["questions"]] == ["q1"]
    assert value["owner_plan"]["impact"]["identifiers"]["policies"]["removed"] == [policy]
    apply(canonical(value))
    document = migration(value); document["template"] = selected
    with pytest.raises(Exception) as caught:
        coverage.run_revision(workspace, document)
    assert caught.value.error_code == "COVERAGE_POLICY_UNKNOWN"
    assert coverage.run_revision(workspace, migration(value))["status"] == "migrated"
    assert (workspace / "sources/coverage/q2.yml").read_bytes() == other


def test_removed_request_kind_has_explicit_replacement_and_retained_history(workspace, candidate):
    old_kind, new_kind = "pack:general-science/original", "pack:general-science/replacement"
    change_overlay(candidate, lambda d: d["domain_pack"].update(request_kinds=[{"id": old_kind, "label": "Original", "description": "Original evidence request."}]))
    apply(canonical(plan(request(workspace, candidate))))
    requests = owner("source_requests")

    def add(kind):
        return requests.run_add(requests.parse_args(["--project-root", str(workspace), "add", "--kind", kind,
            "--query", "retained observations", "--rationale", "Resolve evidence gap", "--question-slug", "q1"]))["request"]

    previous = add(old_kind)
    change_overlay(candidate, lambda d: d["domain_pack"].update(request_kinds=[{"id": new_kind, "label": "Replacement", "description": "Explicit evidence replacement."}]))
    value = plan(request(workspace, candidate)); apply(canonical(value))
    (candidate / "claims.md").write_text("Another revision cannot erase the earlier request migration.\n")
    value = plan(request(workspace, candidate)); apply(canonical(value))
    assert value["owner_plan"]["impact"]["questions"][0]["migration_requirements"]["removed_requests"]
    document = migration(value)
    with pytest.raises(Exception) as caught:
        owner("coverage_manifest").run_revision(workspace, document)
    assert caught.value.details["reason"] == "coverage_request_migration_required"
    replacement = add(new_kind)
    document["request_replacements"] = {previous["request_id"]: replacement["request_id"]}
    owner("coverage_manifest").run_revision(workspace, document)
    retained = requests.load_requests(requests.requests_path(workspace, requests.load_config(workspace)))
    assert previous in retained and replacement in retained


def test_computation_removal_requires_explicit_migration_without_dispatch(workspace, candidate):
    from tests._computation_fixture import definition

    initial = definition()
    initial["graphs"]["estimate"] = {"description": "Explicit reference", "constants": {"input": {"value": "2", "unit": "units"}},
        "inputs": {}, "nodes": {"result": {"expr": "constants.input * 3", "unit": "units"}}, "output_mapping": {"total": "result"}, "output_page": None}
    change_overlay(candidate, lambda d: d.update(computation=initial))
    apply(canonical(plan(request(workspace, candidate))))
    question = workspace / "wiki/questions/q1.md"
    question.write_text(question.read_text().replace("status: open", "status: answered"))
    change_overlay(candidate, lambda d: d["computation"]["graphs"].clear())
    value = plan(request(workspace, candidate))
    impact = value["owner_plan"]["impact"]["computation"]
    assert "/graphs/estimate/nodes/result" in impact["removed_definitions"]
    assert not impact["effects_executed"]
    service = owner("_computation_service")
    with mock.patch.object(service, "status", side_effect=AssertionError("refresh must not evaluate calculations")):
        apply(canonical(value))
    change_overlay(candidate, lambda d: d["domain_pack"].update(version="0.3.0"))
    value = plan(request(workspace, candidate)); apply(canonical(value))
    assert "prior_revision_migration_pending" in value["owner_plan"]["impact"]["questions"][0]["reasons"]
    document = migration(value)
    with pytest.raises(Exception) as caught:
        owner("coverage_manifest").run_revision(workspace, document)
    assert caught.value.details["reason"] == "coverage_computation_migration_required"
    document["computation_migrations"] = dict.fromkeys(impact["removed_definitions"])
    owner("coverage_manifest").run_revision(workspace, document)
    assert not (workspace / "runs/computation/state.json").exists()


def test_strict_receipt_cannot_cross_pack_revision(host, candidate):
    from tests.test_strict_evidence import CORE, review

    lifecycle = owner("_domain_pack_lifecycle")
    installed = host.root / "domain-packs/general-science"
    shutil.copytree(ROOT / "domain-packs/general-science", installed)
    overlay = lifecycle.normalize_overlay_paths(lifecycle.load_overlay(installed), "domain-packs/general-science")
    host.config["domain_pack"] = overlay["domain_pack"]
    (host.root / "research.yml").write_text(yaml.safe_dump(host.config, sort_keys=False))
    lifecycle.run_adopt(host.root, accept_local_overrides=True)
    review(host)
    assert CORE.publication(host.root)["verdict"] == "ship"
    (candidate / "claims.md").write_text("Changed guidance under an explicit revision.\n")
    selected = request(host.root, candidate)
    selected["keep_local"] = ["config:/wiki/frontmatter_type_rules/claim/allowed_values"]
    value = plan(selected)
    assert value["owner_plan"]["impact"]["receipt_scope"] == "workspace_wide"
    applied = apply(canonical(value))
    assert applied["research"]["strict"]["state"] == "observed"
    assert not applied["research"]["strict"]["result"]["claims"][0]["accepted"]
    assert CORE.publication(host.root)["verdict"] == "blocked_on_sources"


def test_legacy_parent_can_be_abandoned_without_accepting_pending_work(workspace):
    controller = owner("orchestration_controller")
    session = controller.start_session(workspace, controller.parse_args(["start", "--orchestration-id", "legacy", "--agent-id", "owner"]))
    session.pop("requirement_basis")
    session["pending_action_id"] = "old-action"
    session.pop("pending_trusted_static_inputs", None)
    controller.write_json_atomic(controller.session_path(workspace, "legacy"), session)
    result = controller.abandon_session(workspace, controller.parse_args(["abandon", "--orchestration-id", "legacy", "--agent-id", "owner", "--reason", "Original requirements cannot be reconstructed."]))
    assert result["status"] == "failed" and result["completed_action_count"] == 0
    assert result["pending_action_id"] is None and result["abandoned_work"]["action_id"] == "old-action"
    from evidence_wiki.orchestration_schemas import ORCHESTRATION_SESSION_SCHEMA
    from tests.test_orchestration_contract_schemas import assert_matches_schema

    assert_matches_schema(result, ORCHESTRATION_SESSION_SCHEMA)


def test_accepted_catalog_revision_is_explicit_and_retains_qualification(tmp_path):
    from evidence_wiki.pack_acceptance import accept
    from evidence_wiki.pack_assessment import assess
    from evidence_wiki.pack_catalog import initialize
    from tests.test_pack_authoring import prepared

    draft, created = prepared(tmp_path)
    candidate = Path(created["candidate"])
    workspace = tmp_path / "workspace"
    code, _, err = invoke("init", "--target", str(workspace), "--project-name", "research",
        "--project-description", "Retain scoped observations", "--owner-goal", "Review evidence", "--domain-pack", str(candidate))
    assert code == 0, err
    change_overlay(candidate, lambda d: d["domain_pack"].update(version="0.2.0"))
    assessment = assess(draft)
    catalog = tmp_path / "catalog"
    initialize(catalog, {"drafts": str(tmp_path)})
    accept(draft, assessment_id=assessment["record"]["sha256"], catalog=catalog, root_id="drafts", revision="reviewed", scope="Explicit scoped observations")
    candidates = status(workspace, catalog=catalog)["candidates"]
    assert len(candidates) == 1 and candidates[0]["assessment"]["independent_review"] == "not_verified"
    selected = request(workspace, candidate)
    selected.update(path=None, catalog=str(catalog), revision="reviewed")
    value = plan(selected)
    assert value["qualification"]["assessment_sha256"] == assessment["record"]["sha256"]
    result = apply(canonical(value))
    assert result["research"]["revisions"][-1]["qualification"]["revision"] == "reviewed"
    assert not status(workspace, catalog=catalog)["candidates"]


def test_saved_revision_recovers_owned_interruption(workspace, candidate):
    lifecycle = owner("_domain_pack_lifecycle")
    value = plan(request(workspace, candidate))
    original = lifecycle._atomic_write

    class Interrupted(BaseException):
        pass

    def interrupted(path, *args, **kwargs):
        if path == workspace / "research.yml":
            raise Interrupted()
        return original(path, *args, **kwargs)

    with mock.patch.object(lifecycle, "_atomic_write", side_effect=interrupted), pytest.raises(Interrupted):
        apply(canonical(value))
    assert (workspace / "domain-packs/.evidence-wiki-transaction.yml").is_file()
    result = apply(canonical(value))
    assert result["status"] == "applied"
    assert len(result["research"]["revisions"]) == 1
    assert not (workspace / "domain-packs/.evidence-wiki-transaction.yml").exists()


@pytest.mark.parametrize("setup_state", [None, "prepared", "running", "failed"])
def test_revision_refuses_missing_or_incomplete_setup_journal(workspace, candidate, setup_state):
    from evidence_wiki.setup_store import target_key

    target = {"writable_root": str(workspace.parent), "relative_path": workspace.name}
    (workspace / "docs/research-requirements.json").write_text(json.dumps({"schema_version": "evidence-research-requirements/v1",
        "request": {"payload": {"target": target}}}))
    if setup_state:
        folder = workspace.parent / ".evidence-wiki/setup/targets" / target_key(target) / "transactions/original"
        folder.mkdir(parents=True)
        (folder / "checkpoint.json").write_text(json.dumps({"schema_version": "evidence-setup-checkpoint/v1", "state": setup_state, "pending": "intake"}))
    before = files(workspace)
    with pytest.raises(Exception) as caught:
        apply(canonical(plan(request(workspace, candidate))))
    assert caught.value.details["reason"].startswith("revision_setup_")
    assert files(workspace) == before
