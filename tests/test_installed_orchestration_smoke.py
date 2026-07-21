import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_PYTHON_ENV = "EVIDENCE_WIKI_FAKE_CODEX_WORKSPACE_PYTHON"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_fake_managed_runner_uses_the_pinned_workspace_python(monkeypatch) -> None:
    pinned_python = "/isolated wheel venv/bin/python"
    monkeypatch.setenv(WORKSPACE_PYTHON_ENV, pinned_python)

    assert FAKE_CODEX.workspace_python() == pinned_python


def test_fake_managed_runner_defaults_to_its_own_python(monkeypatch) -> None:
    monkeypatch.delenv(WORKSPACE_PYTHON_ENV, raising=False)

    assert FAKE_CODEX.workspace_python() == sys.executable
