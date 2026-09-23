"""Qualify pre-workspace discovery, installed identity and content boundaries."""

import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from evidence_wiki import agent, agent_resources
from evidence_wiki._agent_catalog import CATALOG_PATH, resource_paths
from evidence_wiki._script_host import load_packaged_script
from evidence_wiki.errors import UsageError
from evidence_wiki.onboarding_contract import decode_document, encode_document
from evidence_wiki.onboarding_schemas import schema_document, schema_ids
from evidence_wiki.resources import missing_required_assets, required_asset_manifest
from tests._script_loader import load_module

ROOT = Path(__file__).resolve().parents[1]


def envelope(kind, version, payload):
    return {"schema_version": version, "kind": kind, "request_id": "example", "payload": payload}


def test_exports_match_owners_and_all_resources_remain_usable():
    module = load_module("resource_export", ROOT / "tools/sync_agent_resources.py")
    assert all((ROOT / path).read_bytes() == content for path, content in module.exports().items())
    entries = agent_resources.resource_index()["resources"]
    assert 1 <= len(entries) <= 64
    for entry in entries:
        document = agent_resources.resource_document(entry["id"])
        assert hashlib.sha256(document["content"].encode()).hexdigest() == entry["sha256"]
        encode_document("onboarding/resource/v2", envelope("resource", "2.0", document))
    assert not missing_required_assets(ROOT)
    assert set(resource_paths().values()) <= set(required_asset_manifest()["agent"])


def test_instruction_links_and_policy_identity():
    guide = agent_resources.resource_document("guide/bootstrap/v1")
    assert 1300 <= (len(guide["content"].encode()) + 3) // 4 <= 1700
    for resource_id in re.findall(r"\]\(([^)]+)\)", guide["content"]):
        assert agent_resources.resource_document(resource_id)
    policy = json.loads(agent_resources.resource_document("example/strict-policy/v1")["content"])
    strict = load_packaged_script(ROOT, "_strict_contract")
    strict.policy_document(policy)
    assert policy["instructions"] == {"docs/installed-agent.md": "sha256:" + guide["sha256"],
                                      "skills/research-run.md": "sha256:" + agent_resources.resource_document("guide/research/v1")["sha256"]}


@pytest.mark.parametrize("resource_id", ["../../private", "guide/bootstrap/v2", "/tmp/private", "https://example.test",
                                          "guide\\bootstrap\\v1", "guide/bootstrap/v1\x00", None, []])
def test_resource_ids_are_closed_and_redacted(resource_id):
    with pytest.raises(UsageError) as error:
        agent_resources.resource_document(resource_id)
    assert error.value.error_code == "ONBOARDING_RESOURCE_UNKNOWN"
    assert error.value.details == {"field": "resource_id"}


def test_zip_resource_lifetime_and_progressive_loading(tmp_path):
    selected = "guide/bootstrap/v1"
    relative = resource_paths()[selected]
    archive = tmp_path / "resources.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        for path in (CATALOG_PATH, relative):
            stream.write(ROOT / path, path)
    with zipfile.ZipFile(archive) as stream, patch.object(agent_resources, "_asset_tree", return_value=zipfile.Path(stream)):
        # The other inventory entries are intentionally absent: index and a
        # selected read must not extract or load unrelated resources.
        inventory = agent_resources.resource_index()
        document = agent_resources.resource_document(selected)
    archive.unlink()
    assert document["content"].startswith("# Start research")
    assert inventory["bounds"]["truncated"] is False
    document["content"] = "changed by caller"
    assert agent_resources.resource_document(selected)["content"] != document["content"]


@pytest.mark.parametrize("fault", ["missing", "changed", "symlink", "directory", "catalog_path", "catalog_shape"])
def test_broken_or_redirected_assets_refuse(tmp_path, fault):
    selected = "guide/bootstrap/v1"
    relative = resource_paths()[selected]
    catalog_path = tmp_path / CATALOG_PATH
    catalog_path.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / CATALOG_PATH, catalog_path)
    guide = tmp_path / relative
    if fault == "changed":
        guide.write_text("unexpected content")
    elif fault == "symlink":
        guide.symlink_to(ROOT / relative)
    elif fault == "directory":
        guide.mkdir()
    elif fault.startswith("catalog"):
        catalog = json.loads(catalog_path.read_text())
        if fault == "catalog_path":
            catalog["resources"][selected]["path"] = "../../private"
        else:
            del catalog["resources"][selected]["sha256"]
        catalog_path.write_text(json.dumps(catalog))
    with patch.object(agent_resources, "_asset_tree", return_value=tmp_path), pytest.raises(UsageError) as error:
        agent_resources.resource_document(selected)
    assert error.value.error_code == "ONBOARDING_ENVIRONMENT_INCOMPATIBLE"


def test_empty_directory_bootstrap_is_read_only_and_does_not_load_runners(tmp_path, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("Bootstrap attempted workspace execution or a write")
    with (patch("evidence_wiki._contract.contract", forbidden),
          patch("evidence_wiki._script_host.load_packaged_script", forbidden),
          patch("socket.socket", forbidden), patch("subprocess.Popen", forbidden),
          patch("tempfile.TemporaryDirectory", forbidden), patch.object(Path, "write_text", forbidden)):
        assert agent.main(["--target", str(tmp_path), "--format", "json"]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    value = decode_document("onboarding/bootstrap/v2", captured.out.encode())
    assert len(captured.out.encode()) <= agent.SUMMARY_BYTES
    payload = value["payload"]
    assert payload["workspace"] == "absent"
    assert payload["strict_selection"]["effective_assurance"] is None
    assert payload["strict_selection"]["selection_scope"] == "new_workspace_template"
    assert not list(tmp_path.iterdir())
    assert str(tmp_path) not in captured.out


@pytest.mark.parametrize("marker", ["absent", "partial", "present", "malformed", "symlink", "oversize", "scalar"])
def test_workspace_observation_is_bounded_and_not_a_readiness_claim(tmp_path, marker):
    if marker != "absent":
        (tmp_path / "research.yml").write_text("not read during bootstrap")
    if marker in {"present", "symlink"}:
        metadata = tmp_path / "workspace-system.yml"
        if marker == "symlink":
            metadata.symlink_to(ROOT / "workspace-template/workspace-system.yml")
        else:
            metadata.write_text("workspace_system: {starter_version: '0.5.0', schema_version: '0.1'}")
    elif marker in {"malformed", "oversize", "scalar"}:
        (tmp_path / "workspace-system.yml").write_text(
            {"malformed": "[bad", "oversize": "x" * 8193, "scalar": "workspace_system: scalar"}[marker])
    value = agent.bootstrap(str(tmp_path))
    expected = "absent" if marker == "absent" else "present" if marker == "present" else "invalid"
    assert value["workspace"] == expected
    assert "not inspected" in value["environment"]["observation"]
    if marker == "present":
        assert value["environment"]["workspace_version"] == "0.5.0"
        assert value["installation"]["starter_version"] != "0.5.0"
    assert agent.bootstrap(str(ROOT / "workspace-template"))["workspace"] == "not_inspected"


@pytest.mark.parametrize("arguments,field", [
    (["plan"], "arguments"), (["resource"], "arguments"), (["--unknown"], "arguments"),
    (["--format", "xml"], "arguments"), (["--require", "agent unavailable-operation"], "unsupported_requirement/0"),
    (["--require", "onboarding/research_request/v99"], "unsupported_requirement/0"),
    (["--require", "pi"], "unsupported_requirement/0"),
    (["--assurance", "host_enforced"], "host_enforcement_not_verified"),
    (["resource", "guide/bootstrap/v1", "--require", "strict-evidence/v1"], "arguments"),
    (["summary", "--target", "."], "arguments"), (["--request-id", "bad/id"], "/request_id"),
])
def test_json_refusals_are_single_redacted_documents(arguments, field, capsys):
    assert agent.main([*arguments, "--format", "json"]) == 2
    captured = capsys.readouterr()
    value = decode_document("onboarding/error/v1", captured.out.encode())
    assert value["details"]["field"] == field
    assert not captured.err


def test_compact_summary_negotiates_without_full_contract(capsys):
    assert agent.main(["summary", "--format", "json", "--require", "strict-evidence/v1",
                       "--require", "declarative-computation/v1", "--require", "onboarding/research_request/v2"]) == 0
    raw = capsys.readouterr().out.encode()
    summary = decode_document("onboarding/capabilities/v1", raw)["payload"]
    assert len(raw) < agent.SUMMARY_BYTES
    assert summary["strict"]["checker"]["available"]
    assert summary["strict"]["host_probe"] == "not_run"
    assert all("host_enforced" not in value for value in summary["frameworks"]["qualified"])
    effects = {row["name"]: row["effect"] for row in summary["operations"]}
    assert effects["doctor"] == effects["pack validate"] == "temporary_write"
    assert effects["strict export"] == "read"
    assert set(schema_ids()) <= set(summary["schema_ids"])
    with patch.object(agent, "resource_availability", return_value={"script/_strict_evidence/v1": False}):
        assert agent.main(["summary", "--require", "strict-evidence/v1", "--format", "json"]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "ONBOARDING_ENVIRONMENT_INCOMPATIBLE"


def test_top_level_help_discovers_bootstrap(capsys):
    from evidence_wiki.cli import main

    assert main(["--help"]) == 0
    assert capsys.readouterr().out.split("Usage:\n", 1)[1].splitlines()[0].startswith("  evidence-wiki agent")


def test_bootstrap_refuses_a_changed_resource_snapshot(tmp_path):
    original = agent.resource_document

    def changed(resource_id):
        value = original(resource_id)
        if resource_id == "guide/bootstrap/v1":
            value["sha256"] = "0" * 64
        return value

    with patch.object(agent, "resource_document", side_effect=changed), pytest.raises(UsageError) as error:
        agent.bootstrap(str(tmp_path))
    assert error.value.details == {"field": "resource_snapshot_changed"}


def test_negotiation_requires_dynamic_helpers_not_only_entry_scripts(tmp_path):
    catalog = json.loads((ROOT / CATALOG_PATH).read_text())
    paths = {CATALOG_PATH, *resource_paths().values(),
             *("workspace-template/scripts/" + name for name in catalog["runtime_files"])}
    for relative in paths:
        if relative == "workspace-template/scripts/_evidence_authority.py":
            continue
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    with patch.object(agent_resources, "_asset_tree", return_value=tmp_path):
        assert all(agent_resources.resource_availability().values())
        with pytest.raises(UsageError) as error:
            agent.capabilities()
    assert error.value.details == {"field": "required_runtime_missing"}


def test_schema_exports_and_examples_validate_with_owners(tmp_path):
    for resource_id in schema_ids():
        assert json.loads(agent_resources.resource_document(resource_id)["content"]) == schema_document(resource_id)
    for stem, accessor in (("_strict_contract", "schema_documents"), ("_computation_contract", "schemas")):
        for key, expected in getattr(load_packaged_script(ROOT, stem), accessor)().items():
            assert json.loads(agent_resources.resource_document(key)["content"]) == expected
    profile = json.loads(agent_resources.resource_document("example/init-profile/v1")["content"])
    profile["workspace_init"]["target_path"] = str(tmp_path / "workspace")
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    result = subprocess.run([sys.executable, "-m", "evidence_wiki.cli", "init", "--profile", str(path),
                             "--scope-root", str(tmp_path), "--dry-run"], cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "workspace").exists()
    for name in ("pack", "sample-benchmark", "sample-portfolio", "sample-filing"):
        files = json.loads(agent_resources.resource_document(f"example/{name}/v1")["content"])["files"]
        directory = tmp_path / yaml.safe_load(files["research.overlay.yml"])["domain_pack"]["name"]
        for relative, content in files.items():
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        from evidence_wiki.domain_pack_validator import validate_domain_pack

        report = validate_domain_pack(str(directory), root=ROOT)
        assert report["ok"], json.dumps(report)
        if name != "pack":
            config = yaml.safe_load(files["research.overlay.yml"])
            load_packaged_script(ROOT, "_computation_contract").declaration(config["computation"])
