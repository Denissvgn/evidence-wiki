"""Deferred health reads resolve the selected assets after loader scope closes."""

import builtins
import shutil
import sys
from pathlib import Path

import pytest

from evidence_wiki._script_host import load_packaged_script
from evidence_wiki.pack_discovery import owner

CONFIG = ("project: {}\nraw: {}\nsources: {}\nwiki: {}\ntaxonomy: {}\ningest: {}\n"
          "lint: {}\noutputs: {}\nintegrations: {}\nvalue: [original]\n")


@pytest.fixture
def assets(tmp_path):
    root = tmp_path / "assets"
    target = root / "workspace-template/scripts"
    target.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "workspace-template/scripts"
    for name in ("_workspace_health.py", "_workspace_module_loader.py", "_yaml_safe.py"):
        shutil.copyfile(source / name, target / name)
    return root


def deny_bare_helper(monkeypatch):
    actual = builtins.__import__

    def imported(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "_yaml_safe":
            raise ModuleNotFoundError("Ambient helper is unavailable")
        return actual(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", imported)
    monkeypatch.delitem(sys.modules, "_yaml_safe", raising=False)


def test_deferred_owner_read_works_after_scope_and_observes_new_config(assets, tmp_path, monkeypatch):
    deny_bare_helper(monkeypatch)
    before = list(sys.path)
    health = load_packaged_script(assets, "_workspace_health")
    assert sys.path == before
    assert "_yaml_safe" not in sys.modules
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = workspace / "research.yml"
    config.write_text(CONFIG, encoding="utf-8")
    findings = []
    first = health._load_config(workspace, findings)
    assert findings == [] and first["value"] == ["original"]
    first["value"].append("caller edit")
    assert health._load_config(workspace, [])["value"] == ["original"]
    config.write_text(CONFIG.replace("[original]", "[current]"), encoding="utf-8")
    assert health._load_config(workspace, [])["value"] == ["current"]
    assert sys.path == before and "_yaml_safe" not in sys.modules


def test_actual_materialized_health_keeps_available_dependencies_healthy(assets, monkeypatch):
    deny_bare_helper(monkeypatch)
    health = load_packaged_script(assets, "_workspace_health")
    revision = owner("_evidence_revision")
    directories = (".", *health.REQUIRED_DIRECTORIES)
    files = {name: b"Required local file\n" for name in health.REQUIRED_FILES}
    files["research.yml"] = CONFIG.encode()
    captured = revision.WorkspaceRevision(files, directories)
    before = list(sys.path)
    with captured.materialize() as workspace:
        result = health.evaluate_workspace_health(workspace, check_optional_tools=False,
            optional_tool_availability={"pypdf": True, "pdftotext": True})
        assert result["status"] == "healthy", result
        assert result["materially_valid"] is True
        assert "REQUIRED_DEPENDENCY_MISSING" not in result["finding_codes"]
    assert sys.path == before and "_yaml_safe" not in sys.modules


def test_actual_missing_yaml_dependency_remains_deferred_and_invalid(assets, tmp_path, monkeypatch):
    actual = builtins.__import__

    def imported(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "yaml":
            raise ImportError("PyYAML is unavailable")
        return actual(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", imported)
    health = load_packaged_script(assets, "_workspace_health")
    (tmp_path / "research.yml").write_text(CONFIG, encoding="utf-8")
    findings = []
    assert health._load_config(tmp_path, findings) == {}
    assert findings[0]["code"] == "REQUIRED_DEPENDENCY_MISSING"
    assert findings[0]["readiness_effect"] == "invalid"


def test_scoped_reader_preserves_yaml_refusal_after_load_scope(assets, tmp_path, monkeypatch):
    deny_bare_helper(monkeypatch)
    health = load_packaged_script(assets, "_workspace_health")
    (tmp_path / "research.yml").write_text("value: !!python/object/apply:builtins.eval ['1+1']\n", encoding="utf-8")
    findings = []
    assert health._load_config(tmp_path, findings) == {}
    assert findings[0]["code"] == "RESEARCH_CONFIG_INVALID"
