import io
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

from tests._script_loader import load_module

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish.yml"
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
VALIDATOR_PATH = REPO_ROOT / "tools" / "validate_installed_artifacts.py"
VALIDATOR = load_module("installed_artifact_validator_under_test", VALIDATOR_PATH)
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
NOTICES_PATH = REPO_ROOT / "THIRD_PARTY_NOTICES.md"


def load_workflow() -> dict:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    # PyYAML 1.1 treats the GitHub Actions key `on` as boolean true.
    workflow["on"] = workflow.pop(True)
    return workflow


def step_uses(job: dict) -> list[str]:
    return [step["uses"] for step in job["steps"] if "uses" in step]


def test_pypi_workflow_can_only_start_from_a_published_release() -> None:
    workflow = load_workflow()

    assert workflow["on"] == {"release": {"types": ["published"]}}
    assert "workflow_dispatch" not in workflow["on"]
    assert "push" not in workflow["on"]


def test_publish_job_is_downstream_of_the_release_gate() -> None:
    workflow = load_workflow()
    release_gate = workflow["jobs"]["release-gate"]
    publish = workflow["jobs"]["publish-to-pypi"]

    assert publish["needs"] == "release-gate"
    assert publish["environment"] == {
        "name": "pypi",
        "url": "https://pypi.org/p/evidence-wiki",
    }
    assert release_gate.get("permissions", {}).get("id-token") is None
    assert publish["permissions"] == {"id-token": "write"}
    assert any(use.startswith("pypa/gh-action-pypi-publish@") for use in step_uses(publish))
    assert not any(use.startswith("pypa/gh-action-pypi-publish@") for use in step_uses(release_gate))


def test_release_gate_checks_identity_quality_and_runs_the_shared_artifact_gate() -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    for required in (
        "github.event.release.tag_name",
        'project["version"]',
        "src/evidence_wiki/__init__.py",
        "CHANGELOG.md",
        "tools/run_test_groups.py",
        "-m ruff check .",
        "tools/sync_vendored_scripts.py --check",
        "git diff --check",
        "-m twine check",
        "tools/validate_installed_artifacts.py",
        "--dist-dir dist",
        '--expected-version "${RELEASE_TAG#v}"',
        "artifact-validation.json",
    ):
        assert required in text
    # The inline copies are gone: the workflow must not carry its own smoke again.
    assert "pip install dist/*.whl" not in text
    assert "import pypdf" not in text


def test_ci_runs_the_same_shared_artifact_gate_as_the_release() -> None:
    text = CI_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "tools/validate_installed_artifacts.py --dist-dir dist" in text
    assert "pip install dist/*.whl" not in text
    assert "import pypdf" not in text


def test_shared_artifact_gate_carries_every_check_the_workflows_used_to_inline() -> None:
    text = VALIDATOR_PATH.read_text(encoding="utf-8")

    for required in (
        "pip",
        "install",
        "import pypdf",
        "pypdf.__version__",
        "import ruamel.yaml",
        "ruamel.yaml.__version__",
        'ruamel.yaml.YAML(typ="rt", pure=True)',
        "round_trip_yaml.preserve_quotes = True",
        "did not preserve YAML comments and quotes",
        '"general-science"',
        '"refresh"',
        'pack_refresh.get("status") != "no_changes"',
        "workspace-template/scripts/_domain_pack_lifecycle.py",
        "ORCHESTRATION_RESULT_SCHEMA",
        'properties["schema_version"]',
        "workspace-template/docs/orchestration.md",
        "orchestrator/skills/research-orchestrate.md",
        "resources.missing_required_assets",
        "smoke_installed_orchestration.py",
        "site-packages",
        "build_wheel_from_sdist",
        "sha256",
    ):
        assert required in text, required


def make_wheel(path: Path, members: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for member in members:
            archive.writestr(member, "x")
    return path


def make_sdist(path: Path, members: list[str], root: str = "evidence_wiki-9.9.9") -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for member in members:
            info = tarfile.TarInfo(name=f"{root}/{member}")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    return path


def test_archive_membership_accepts_a_policy_conformant_pair(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path / "evidence_wiki-9.9.9-py3-none-any.whl", list(VALIDATOR.REQUIRED_WHEEL_MEMBERS))
    sdist = make_sdist(tmp_path / "evidence_wiki-9.9.9.tar.gz", list(VALIDATOR.REQUIRED_SDIST_MEMBERS))

    summary = VALIDATOR.check_archive_membership(wheel, sdist)

    assert summary == {
        "wheel_members": len(VALIDATOR.REQUIRED_WHEEL_MEMBERS),
        "sdist_members": len(VALIDATOR.REQUIRED_SDIST_MEMBERS),
    }


@pytest.mark.parametrize(
    "leak",
    [
        "docs/CR/internal-backlog.md",
        "docs/llm_wiki/index.md",
        "reports/codebase-review.txt",
        "RELEASING.md",
        "AGENTS.md",
        ".venv/bin/python",
        "src/evidence_wiki/__pycache__/cli.cpython-312.pyc",
        "workspace-template/.research-cache/query-index.sqlite3",
        ".llm-wiki/skills/README.md",
        "dist/evidence_wiki-0.0.0.tar.gz",
    ],
)
def test_archive_membership_refuses_internal_material_in_the_sdist(tmp_path: Path, leak: str) -> None:
    wheel = make_wheel(tmp_path / "evidence_wiki-9.9.9-py3-none-any.whl", list(VALIDATOR.REQUIRED_WHEEL_MEMBERS))
    sdist = make_sdist(tmp_path / "evidence_wiki-9.9.9.tar.gz", [*VALIDATOR.REQUIRED_SDIST_MEMBERS, leak])

    with pytest.raises(SystemExit) as refusal:
        VALIDATOR.check_archive_membership(wheel, sdist)

    assert "ships internal material" in str(refusal.value)
    assert leak in str(refusal.value)


def test_archive_membership_allows_the_packaged_workspace_agent_files() -> None:
    # Root-level AGENTS.md is maintainer-local; the starter's copy is a shipped asset.
    assert VALIDATOR.forbidden_members(["AGENTS.md"]) == ["AGENTS.md"]
    assert VALIDATOR.forbidden_members(["workspace-template/AGENTS.md"]) == []
    assert VALIDATOR.forbidden_members(["evidence_wiki/assets/workspace-template/AGENTS.md"]) == []
    assert VALIDATOR.forbidden_members(["tests/fixtures/madrid-autonomo-workspace/reports/expected-summary.json"]) == []


def test_archive_membership_refuses_a_wheel_missing_a_required_asset(tmp_path: Path) -> None:
    members = [member for member in VALIDATOR.REQUIRED_WHEEL_MEMBERS if not member.endswith("research-orchestrate.md")]
    wheel = make_wheel(tmp_path / "evidence_wiki-9.9.9-py3-none-any.whl", members)
    sdist = make_sdist(tmp_path / "evidence_wiki-9.9.9.tar.gz", list(VALIDATOR.REQUIRED_SDIST_MEMBERS))

    with pytest.raises(SystemExit) as refusal:
        VALIDATOR.check_archive_membership(wheel, sdist)

    assert "missing required members" in str(refusal.value)
    assert "research-orchestrate.md" in str(refusal.value)


def test_find_artifacts_refuses_an_ambiguous_dist_directory(tmp_path: Path) -> None:
    make_wheel(tmp_path / "a-1-py3-none-any.whl", ["a"])
    make_wheel(tmp_path / "a-2-py3-none-any.whl", ["a"])
    make_sdist(tmp_path / "a-1.tar.gz", ["a"])

    with pytest.raises(SystemExit) as refusal:
        VALIDATOR.find_artifacts(tmp_path)

    assert "exactly one wheel and one sdist" in str(refusal.value)


def test_round_trip_yaml_runtime_dependency_is_pinned_and_noticed() -> None:
    pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")
    notices = NOTICES_PATH.read_text(encoding="utf-8")

    assert '"ruamel.yaml>=0.19.1,<0.20"' in pyproject
    assert "ruamel.yaml (MIT)" in notices


def test_workflow_uses_oidc_without_a_stored_pypi_credential() -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "id-token: write" in text
    assert "secrets." not in text
    assert "password:" not in text
    assert "api-token" not in text


def test_sdist_force_includes_every_tracked_agents_file() -> None:
    """``.gitignore`` ignores AGENTS.md everywhere and re-admits three tracked copies.

    The sdist builder honours the ignore and not the re-admission, which silently
    dropped a required starter asset from every sdist: a wheel built from one could
    not locate its assets root. The force-include list is what keeps them shipping.
    """
    pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")
    assert "[tool.hatch.build.targets.sdist.force-include]" in pyproject
    for tracked in (
        "workspace-template/AGENTS.md",
        "examples/urban-heat-resilience-workspace/AGENTS.md",
        "tests/fixtures/madrid-autonomo-workspace/AGENTS.md",
    ):
        assert (REPO_ROOT / tracked).is_file(), tracked
        assert f'"{tracked}" = "{tracked}"' in pyproject, tracked
    assert "workspace-template/AGENTS.md" in VALIDATOR.REQUIRED_SDIST_MEMBERS
    assert "evidence_wiki/assets/workspace-template/AGENTS.md" in VALIDATOR.REQUIRED_WHEEL_MEMBERS


SCALE_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "scale.yml"


def load_scale_workflow() -> dict:
    workflow = yaml.safe_load(SCALE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    workflow["on"] = workflow.pop(True)
    return workflow


def test_scale_workflow_runs_on_a_schedule_on_demand_and_for_labelled_pull_requests() -> None:
    workflow = load_scale_workflow()

    assert set(workflow["on"]) == {"schedule", "workflow_dispatch", "pull_request"}
    assert workflow["on"]["workflow_dispatch"]["inputs"]["profile"]["options"] == ["standard", "near-partition"]
    assert workflow["permissions"] == {"contents": "read"}
    for job in workflow["jobs"].values():
        assert "performance" in job["if"]
        assert not any(use.startswith("pypa/gh-action-pypi-publish@") for use in step_uses(job))


def test_scale_workflow_enforces_budgets_and_keeps_evidence_keyed_to_the_commit() -> None:
    text = SCALE_WORKFLOW_PATH.read_text(encoding="utf-8")

    for required in (
        "tools/scale_benchmark.py",
        "--require-budget",
        "--output",
        "${GITHUB_SHA}",
        "near-partition",
        "tools/run_test_groups.py --coverage",
        "tools/coverage_report.py snapshot",
        "tools/coverage_report.py report",
        "if: always()",
        "retention-days: 90",
    ):
        assert required in text, required
    assert "retry" not in text.lower().replace("retries until", "")


def test_release_gate_measures_the_standard_profile_and_stores_it() -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "tools/scale_benchmark.py" in text
    assert "--profile standard" in text
    assert "--require-budget" in text
    assert "scale-benchmark-standard.json" in text


def test_release_failure_diagnostics_do_not_admit_failed_distributions() -> None:
    workflow = load_workflow()
    steps = workflow["jobs"]["release-gate"]["steps"]
    collector = next(step for step in steps if "tools/collect_release_diagnostics.py" in step.get("run", ""))
    upload = next(step for step in steps if step.get("with", {}).get("path", "").startswith("release-diagnostics/"))
    distributions = next(step for step in steps if step["name"] == "Store verified distributions")
    assert collector["if"] == upload["if"] == "always()"
    assert "github.sha" in upload["with"]["name"] and "runner.arch" in upload["with"]["name"]
    assert "dist/" not in upload["with"]["path"]
    assert "if" not in distributions
    assert "if" not in workflow["jobs"]["publish-to-pypi"]


def test_coverage_measures_subprocesses_and_folds_copied_scripts_onto_the_template() -> None:
    pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")

    assert "[tool.coverage.run]" in pyproject
    assert "branch = true" in pyproject
    assert "parallel = true" in pyproject
    assert 'patch = ["subprocess"]' in pyproject
    assert '"*/scripts/*.py"' in pyproject
    assert "[tool.coverage.paths]" in pyproject
