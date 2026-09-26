import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests._script_loader import load_module

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_PYTHON_ENV = "EVIDENCE_WIKI_FAKE_CODEX_WORKSPACE_PYTHON"


SMOKE = load_module(
    "installed_orchestration_smoke_under_test",
    REPO_ROOT / "tools" / "smoke_installed_orchestration.py",
)
FAKE_CODEX = load_module(
    "installed_orchestration_fake_codex_under_test",
    REPO_ROOT / "tests" / "fixtures" / "fake_codex_cli.py",
)


def test_smoke_passes_its_python_to_the_fake_managed_runner() -> None:
    environment = SMOKE.fake_codex_environment()

    assert environment[WORKSPACE_PYTHON_ENV] == sys.executable


def test_smoke_expected_starter_version_matches_authoritative_metadata() -> None:
    document = yaml.safe_load(
        (REPO_ROOT / "workspace-template" / "workspace-system.yml").read_text(encoding="utf-8")
    )

    SMOKE.verify_starter_version({"starter_version": document["workspace_system"]["starter_version"],
                                  "package_version": "independently-versioned-package"})


@pytest.mark.parametrize("version", ["2.3.4", "3.0.0rc1"])
def test_smoke_follows_changed_canonical_starter_metadata(tmp_path, monkeypatch, version):
    directory = tmp_path / "workspace-template"
    directory.mkdir()
    (directory / "workspace-system.yml").write_text(yaml.safe_dump({"workspace_system": {"starter_version": version}}))
    monkeypatch.setattr(SMOKE, "REPO_ROOT", tmp_path)
    SMOKE.verify_starter_version({"starter_version": version, "package_version": "different"})
    with pytest.raises(SystemExit, match="unexpected starter version"):
        SMOKE.verify_starter_version({"starter_version": "stale", "package_version": version})


@pytest.mark.parametrize("contract", [{}, [], {"starter_version": None}, {"starter_version": False},
                                     {"starter_version": "wrong"}])
def test_starter_mismatch_refuses_before_managed_smoke_effects(tmp_path, monkeypatch, contract):
    monkeypatch.setattr(sys, "argv", ["smoke", "--cli", str(tmp_path / "evidence-wiki")])
    monkeypatch.setattr(SMOKE, "run", lambda argv: subprocess.CompletedProcess(argv, 0, json.dumps(contract), ""))
    monkeypatch.setattr(SMOKE.tempfile, "TemporaryDirectory", lambda **kwargs: pytest.fail("smoke effects started"))
    with pytest.raises(SystemExit, match="unexpected starter version"):
        SMOKE.main()


@pytest.mark.parametrize("content", [None, "[", "{}", "workspace_system: []", "workspace_system: {starter_version: null}",
    "workspace_system: {starter_version: false}", "workspace_system: {starter_version: 1.0}",
    "workspace_system: {starter_version: ''}", "workspace_system: {starter_version: ' padded '}" ])
def test_missing_or_invalid_expected_metadata_does_not_default_to_reported_version(tmp_path, monkeypatch, content):
    directory = tmp_path / "workspace-template"
    directory.mkdir()
    if content is not None:
        (directory / "workspace-system.yml").write_text(content)
    monkeypatch.setattr(SMOKE, "REPO_ROOT", tmp_path)
    with pytest.raises(SystemExit, match="[Qq]ualification metadata"):
        SMOKE.verify_starter_version({"starter_version": "reported", "package_version": "reported"})


def test_isolated_smoke_uses_captured_hash_bound_metadata_without_checkout_fallback(tmp_path):
    from tools.validate_installed_artifacts import isolated_fixtures, validation_identity

    captured = isolated_fixtures(tmp_path)
    relative = "workspace-template/workspace-system.yml"
    metadata = (captured / relative).read_bytes()
    assert metadata == (REPO_ROOT / relative).read_bytes()
    assert json.loads((captured / "inputs.json").read_text())[relative] == hashlib.sha256(metadata).hexdigest()
    assert validation_identity()[relative] == hashlib.sha256(metadata).hexdigest()
    isolated = load_module("isolated_orchestration_smoke", captured / "tools/smoke_installed_orchestration.py")
    version = yaml.safe_load(metadata)["workspace_system"]["starter_version"]
    isolated.verify_starter_version({"starter_version": version})
    (captured / relative).unlink()
    with pytest.raises(SystemExit, match="qualification metadata"):
        isolated.verify_starter_version({"starter_version": version})


def test_fake_managed_runner_uses_the_pinned_workspace_python(monkeypatch) -> None:
    pinned_python = "/isolated wheel venv/bin/python"
    monkeypatch.setenv(WORKSPACE_PYTHON_ENV, pinned_python)

    assert FAKE_CODEX.workspace_python() == pinned_python


def test_fake_managed_runner_defaults_to_its_own_python(monkeypatch) -> None:
    monkeypatch.delenv(WORKSPACE_PYTHON_ENV, raising=False)

    assert FAKE_CODEX.workspace_python() == sys.executable
