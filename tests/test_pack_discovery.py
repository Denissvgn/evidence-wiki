"""Qualify explicit pack identities, local observations and declared domain fit."""

import contextlib
import copy
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from evidence_wiki import pack_catalog, pack_discovery
from evidence_wiki._filesystem import os
from evidence_wiki._pack_io import capture_pack, yaml_document
from evidence_wiki.cli import main
from evidence_wiki.domain_pack_validator import validate_domain_pack
from evidence_wiki.errors import UsageError
from evidence_wiki.pack_decisions import decide, schema_document
from evidence_wiki.pack_discovery import inspect_installed, inventory, owner, select
from tests.test_orchestration_contract_schemas import assert_matches_schema

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def local_pack(tmp_path):
    parent = tmp_path / "assets"
    parent.mkdir()
    path = parent / "general-science"
    shutil.copytree(ROOT / "domain-packs/general-science", path)
    return path


def deploy(path):
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["init", "--target", str(path), "--project-name", "pack-discovery",
                     "--project-description", "Inspect a retained guidance pack.", "--domain-pack", "general-science"]) == 0


def test_bundled_metadata_reuses_existing_rules_and_never_enables_providers():
    value = inventory()
    assert len(value["packs"]) == 5
    for row in value["packs"]:
        assert row["state"] == "available", row
        info = row["metadata"]
        assert info["selection"]["unknown_fields"] == []
        assert info["structural_validation"] == "not_run"
        assert not info["providers_enabled"]
        assert row["newer_revision"] == "unknown"
        report = validate_domain_pack(row["name"])
        assert report["ok"], report
    capital = next(row for row in value["packs"] if row["name"] == "capital-markets")
    assert capital["metadata"]["human_gated"]
    assert "pack:capital-markets/listing-review" in capital["metadata"]["human_review_policies"]


def test_legacy_metadata_unknowns_remain_consumable(local_pack):
    path = local_pack / "research.overlay.yml"
    value = yaml.safe_load(path.read_text())
    del value["domain_pack"]["selection"]
    value["domain_pack"]["coverage_templates"] = None
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    row, _ = select(path=local_pack)
    assert len(row["metadata"]["selection"]["unknown_fields"]) == 4
    assert validate_domain_pack(str(local_pack))["ok"]


@pytest.mark.parametrize("selection", [None, [], {"schema_version": "2.0"}, {"schema_version": "1.0", "force": True},
                                      {"schema_version": "1.0", "typical_questions": [1]}])
def test_invalid_selection_metadata_refuses_through_canonical_validator(local_pack, selection):
    path = local_pack / "research.overlay.yml"
    value = yaml.safe_load(path.read_text())
    value["domain_pack"]["selection"] = selection
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    result = validate_domain_pack(str(local_pack))
    assert not result["ok"]
    assert next(check for check in result["checks"] if check["id"] == "selection_metadata")["status"] == "fail"


def test_identity_uses_the_lifecycle_digest_owner(local_pack):
    snapshot = capture_pack(local_pack)
    assert snapshot.tree_sha256 == owner("_domain_pack_lifecycle").tree_sha256(local_pack)
    before = select(path=local_pack)[0]["identity"]
    path = local_pack / "research.overlay.yml"
    path.write_text("# presentation only\n" + path.read_text())
    after = select(path=local_pack)[0]["identity"]
    assert before["overlay_sha256"] == after["overlay_sha256"]
    assert before["tree_sha256"] != after["tree_sha256"]


def test_explicit_origins_and_resources_do_not_silently_redirect(tmp_path, local_pack):
    workspace = tmp_path / "workspace"
    deploy(workspace)
    with pytest.raises(UsageError) as error:
        select("general-science", target=workspace)
    assert error.value.details["field"] == "pack_selection_ambiguous"
    row, _ = select("bundled:general-science", target=workspace, catalog=tmp_path / "absent")
    assert row["origin"] == "bundled"
    assert select(resource="pack/bundled/general-science/v1")[0]["origin"] == "bundled"
    with pytest.raises(UsageError):
        select(resource="../../private")
    with pytest.raises(UsageError):
        select("bundled:general-science", path=local_pack)


def test_installed_state_is_authoritative_and_inspection_does_not_adopt(tmp_path):
    workspace = tmp_path / "workspace"
    deploy(workspace)
    lifecycle, rows = inspect_installed(workspace)
    assert lifecycle["state"] == "current"
    before = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
    inventory(target=workspace)
    assert before == {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
    (workspace / "domain-packs/general-science/taxonomy.md").write_text("Locally revised guidance.\n")
    assert inspect_installed(workspace)[0]["state"] == "local_modifications"
    state = workspace / "domain-packs/.evidence-wiki-state.yml"
    state.unlink()
    assert inspect_installed(workspace)[0]["state"] == "legacy_untracked"
    assert not state.exists()
    state.write_text("not: lifecycle state\n")
    assert inspect_installed(workspace)[0]["state"] == "state_invalid"
    assert select("installed:general-science", target=workspace)[0]["state"] == "invalid"


def test_catalog_records_stale_and_missing_candidates_without_accepting_old_receipts(tmp_path, local_pack):
    root = tmp_path / "catalog"
    pack_catalog.initialize(root, {"assets": str(local_pack.parent)})
    result = pack_catalog.register(root, revision="science-one", root_id="assets", relative=local_pack.name, scope="Local research")
    assert result["status"] == "registered"
    catalog = json.loads((root / "catalog.json").read_text())
    assert_matches_schema(catalog, schema_document("evidence-pack-catalog/v1"))
    receipt = json.loads((root / ("receipt-" + result["validation_receipt"] + ".json")).read_text())
    assert_matches_schema(receipt, schema_document("evidence-pack-validation/v1"))
    assert pack_catalog.entries(root)[0]["validation"]["state"] == "matching_observation"
    with pytest.raises(UsageError):
        pack_catalog.register(root, revision="science-one", root_id="assets", relative=local_pack.name, scope="Another scope")
    (local_pack / "taxonomy.md").write_text("Changed after observation.\n")
    row = pack_catalog.entries(root)[0]
    assert row["state"] == "mutated" and row["validation"]["state"] == "not_current"
    local_pack.rename(local_pack.with_name("moved"))
    assert pack_catalog.entries(root)[0]["reason"] == "catalog_candidate_missing"


@pytest.mark.parametrize("raw", [b'x: 1\nx: 2', b'a: &a [*a]', b'!!python/object:danger {}', b'[.inf]'])
def test_untrusted_yaml_and_linked_pack_members_refuse(local_pack, raw):
    with pytest.raises(UsageError):
        yaml_document(raw)
    (local_pack / "escape.md").symlink_to(ROOT / "README.md")
    with pytest.raises(UsageError):
        capture_pack(local_pack)


def decision():
    row, _ = select("bundled:general-science")
    return {"schema_version": "evidence-pack-decision/v1", "request_id": "scope", "choice": "reuse",
            "rationale": "Compare study methods with retained uncertainty.",
            "requirements": [{"id": "methods", "text": "Compare study methods.", "kind": "evidence"}],
            "selections": [{"selector": row["selector"], "tree_sha256": row["identity"]["tree_sha256"]}],
            "scope_inputs": {"research_question": "Which methods apply?", "population": "Study population", "time_scope": "Declared study period"},
            "mapping": [{"requirement_id": "methods", "support": "supported", "pack_basis": [{"selector": row["selector"], "pointer": "/description"}], "rationale": "The described scientific scope fits this requirement."}],
            "alternatives": [{"choice": "project_local", "rationale": "Use narrower local guidance if reusable scope is unsuitable."}],
            "gaps": [], "unresolved_scope": [], "local_guidance": None, "partitions": []}


def test_fit_is_caller_judgment_with_scope_and_evidence_limits():
    value = decision()
    assert_matches_schema(value, schema_document("evidence-pack-decision/v1"))
    result = decide(json.dumps(value).encode())
    assert_matches_schema(result, schema_document("evidence-pack-decision-result/v1"))
    assert result["status"] == "valid"
    assert result["mapping_assurance"] == "caller_declared" and not result["research_ready"]
    del value["scope_inputs"]["population"]
    assert decide(json.dumps(value).encode())["status"] == "needs_scope"
    value["selections"].append(copy.deepcopy(value["selections"][0]))
    assert decide(json.dumps(value).encode())["reason"] == "multi_pack_composition_unavailable"


def test_guidance_changes_do_not_supply_missing_evidence():
    value = decision()
    value.update(choice="create", selections=[])
    value["mapping"][0].update(support="gap", pack_basis=[])
    value["gaps"] = [{"requirement_id": "methods", "kind": "source", "detail": "The required study was not obtained."}]
    assert decide(json.dumps(value).encode())["reason"] == "guidance_change_does_not_supply_missing_evidence"


@pytest.mark.parametrize("change", ["stale", "mapping", "mixed", "pointer"])
def test_stale_and_inconsistent_decision_inputs_refuse(change):
    value = decision()
    if change == "stale":
        value["selections"][0]["tree_sha256"] = "0" * 64
    elif change == "mapping":
        value["mapping"] = []
    elif change == "mixed":
        value["local_guidance"] = {"mode": "project_local"}
    else:
        value["mapping"][0]["pack_basis"][0]["pointer"] = "/selection/typical_questions/-1"
    with pytest.raises(UsageError):
        decide(json.dumps(value).encode())


def test_legacy_portable_pack_names_and_invalid_metadata(local_pack):
    path = local_pack.rename(local_pack.with_name("My_science.1"))
    overlay = path / "research.overlay.yml"
    value = yaml.safe_load(overlay.read_text())
    value["domain_pack"]["name"] = path.name
    del value["domain_pack"]["selection"]
    # The portable-name case is independent of foreign policy declarations.
    # This renamed legacy pack uses built-in policies only.
    value["domain_pack"].pop("policy_vocabularies", None)
    overlay.write_text(yaml.safe_dump(value))
    assert select(path=path)[0]["state"] == "available"
    assert pack_discovery.validate_snapshot(path)[1]["ok"]
    del value["domain_pack"]["version"]
    overlay.write_text(yaml.safe_dump(value))
    assert select(path=path)[0]["state"] == "invalid"


def test_installed_inspection_preserves_skew_and_detects_new_control_file(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    deploy(workspace)
    config = workspace / "research.yml"
    value = yaml.safe_load(config.read_text())
    value["domain_pack"]["name"] = "different"
    config.write_text(yaml.safe_dump(value))
    assert inspect_installed(workspace)[0]["state"] == "config_tree_skew"
    lifecycle = owner("_domain_pack_lifecycle")
    inspect = lifecycle.inspect_workspace
    def changed(root):
        result = inspect(root)
        (workspace / "domain-packs/.evidence-wiki-transaction.yml").write_text("transaction_id: interrupted\n")
        return result
    monkeypatch.setattr(lifecycle, "inspect_workspace", changed)
    assert inspect_installed(workspace)[0]["state"] == "state_invalid"


@pytest.mark.parametrize("scope", [{"population": None}, {"population": []}, {"population": " "}, {str(n): "scope" for n in range(33)}])
def test_invalid_scope_values_and_bounds_refuse_without_leaking_keys(scope):
    value = decision()
    value["scope_inputs"] = scope
    with pytest.raises(UsageError) as error:
        decide(json.dumps(value).encode())
    assert "population" not in json.dumps(error.value.details)


def test_partition_proposals_keep_scopes_and_requirement_bases_separate():
    value = decision()
    selected = value["selections"].pop()
    value["choice"] = "partition"
    value["requirements"].append({"id": "second", "text": "Separate study scope", "kind": "scope"})
    value["mapping"].append({"requirement_id": "second", "support": "unknown", "pack_basis": [], "rationale": "Scope is unresolved."})
    value["partitions"] = [{"id": "study", "requirement_ids": ["methods"], "rationale": "Study methods.",
                            "scope_inputs": value["scope_inputs"], "selection": selected},
                           {"id": "other", "requirement_ids": ["second"], "rationale": "Separate scope.", "scope_inputs": {}, "selection": None}]
    result = decide(json.dumps(value).encode())
    assert result["status"] == "partial" and len(result["partitions"]) == 2
    assert_matches_schema(result, schema_document("evidence-pack-decision-result/v1"))
    basis = value["mapping"][0]["pack_basis"]
    value["mapping"][0]["pack_basis"] = []
    with pytest.raises(UsageError):
        decide(json.dumps(value).encode())
    value["mapping"][0]["pack_basis"] = basis
    value["mapping"][1]["pack_basis"] = value["mapping"][0]["pack_basis"]
    with pytest.raises(UsageError) as error:
        decide(json.dumps(value).encode())
    assert error.value.details["field"] == "pack_partition_basis_mismatch"


def test_project_local_guidance_uses_initializer_rules():
    value = decision()
    value.update(choice="project_local", selections=[], local_guidance={"mode": "project_local", "rationale": "Narrow scope."})
    value["mapping"][0]["pack_basis"] = []
    with pytest.raises(UsageError):
        decide(json.dumps(value).encode())
    for field in owner("init_research_workspace").PROJECT_LOCAL_GUIDANCE_LIST_FIELDS:
        value["local_guidance"][field] = ["Caller-declared guidance."]
    assert decide(json.dumps(value).encode())["status"] == "valid"


def test_catalog_root_receipt_and_native_lock_changes(tmp_path, local_pack, monkeypatch):
    path = tmp_path / "catalog"
    pack_catalog.initialize(path, {"assets": str(local_pack.parent)})
    record = pack_catalog.register(path, revision="science", root_id="assets", relative=local_pack.name, scope="Local scope")
    monkeypatch.setattr(pack_discovery, "checker_identity", lambda: "0" * 64)
    assert pack_catalog.entries(path)[0]["validation"]["state"] == "checker_changed"
    receipt = path / ("receipt-" + record["validation_receipt"] + ".json")
    receipt.write_text('{}')
    assert pack_catalog.entries(path)[0]["validation"]["state"] == "not_current"
    local_pack.parent.rename(tmp_path / "moved-assets")
    local_pack.parent.mkdir()
    assert pack_catalog.entries(path)[0]["reason"] == "catalog_root_changed"
    locks = owner("_workspace_locks")
    first = os.open(path / "catalog.lock", os.O_RDWR)
    second = os.open(path / "catalog.lock", os.O_RDWR)
    try:
        with locks.descriptor_lock(first):
            with pytest.raises(locks.LockUnavailableError) as error:
                with locks.descriptor_lock(second, timeout_seconds=0):
                    pytest.fail("A second independent descriptor acquired the lock")
            assert error.value.contended
    finally:
        os.close(first)
        os.close(second)


def test_cli_bounds_guides_schemas_and_redacted_errors(capsys):
    assert main(["pack", "list", "--limit", "1"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["bounds"] == {"total": 5, "returned": 1, "truncated": True}
    assert main(["pack", "guide"]) == 0
    assert "caller-local" in json.loads(capsys.readouterr().out)["content"]
    assert main(["pack", "schemas", "--schema-id", "evidence-pack-decision/v1"]) == 0
    assert json.loads(capsys.readouterr().out) == schema_document("evidence-pack-decision/v1")
    assert main(["pack", "show", "--resource", "secret-private-token"]) == 2
    result = capsys.readouterr()
    assert not result.err and "secret-private-token" not in result.out
    assert json.loads(result.out)["error_code"] == "ONBOARDING_RESOURCE_UNKNOWN"


def test_concurrent_catalog_writers_and_replaced_lock(tmp_path, local_pack, monkeypatch):
    path = tmp_path / "catalog"
    pack_catalog.initialize(path, {"assets": str(local_pack.parent)})
    arguments = [sys.executable, "-c", "from evidence_wiki.cli import main; raise SystemExit(main())", "pack", "catalog", "register", "--catalog", str(path),
                 "--root-id", "assets", "--path", local_pack.name, "--scope", "Concurrent local scope"]
    writers = [subprocess.Popen([*arguments, "--id", key], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
               for key in ("first", "second")]
    try:
        for writer in writers:
            out, err = writer.communicate(timeout=30)
            assert writer.returncode == 0 and not err, out + err
    finally:
        for writer in writers:
            if writer.poll() is None:
                writer.kill()
                writer.wait()
    assert len(pack_catalog.entries(path)) == 2
    before = (path / "catalog.json").read_bytes()
    validate = pack_discovery.validate_snapshot
    def replaced(candidate):
        result = validate(candidate)
        (path / "catalog.lock").unlink()
        (path / "catalog.lock").touch()
        return result
    monkeypatch.setattr(pack_discovery, "validate_snapshot", replaced)
    with pytest.raises(UsageError) as error:
        pack_catalog.register(path, revision="third", root_id="assets", relative=local_pack.name, scope="Local scope")
    assert error.value.details["field"] == "catalog_lock_replaced"
    assert (path / "catalog.json").read_bytes() == before


def test_missing_and_symlinked_installed_pack_state(tmp_path):
    workspace = tmp_path / "workspace"
    deploy(workspace)
    shutil.rmtree(workspace / "domain-packs/general-science")
    lifecycle, rows = inspect_installed(workspace)
    assert lifecycle["state"] == "pack_missing" and rows[0]["state"] == "unavailable"
    state = workspace / "domain-packs/.evidence-wiki-state.yml"
    state.unlink()
    state.symlink_to(ROOT / "README.md")
    assert inspect_installed(workspace)[0]["state"] == "state_invalid"
