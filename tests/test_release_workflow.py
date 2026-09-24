import hashlib
import io
import itertools
import json
import os
import shlex
import subprocess
import sys
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


def test_release_checks_fan_out_from_identity_and_all_gate_promotion() -> None:
    jobs = load_workflow()["jobs"]
    producers = {"release-tests", "release-artifacts", "release-scale"}
    for producer in producers:
        assert jobs[producer]["needs"] == "release-identity"
    assert set(jobs["release-gate"]["needs"]) == producers | {"release-identity"}
    assert jobs["publish-to-pypi"]["needs"] == "release-gate"
    for key, job in jobs.items():
        assert "if" not in job and not job.get("continue-on-error", False)
        assert job.get("permissions", {}).get("id-token") is None or key == "publish-to-pypi"
        for step in job["steps"]:
            assert not step.get("continue-on-error", False)
            if "actions/checkout@" in step.get("uses", ""):
                assert step["with"]["ref"] == "${{ github.sha }}"
                assert step["with"]["persist-credentials"] is False
    publisher = jobs["publish-to-pypi"]
    assert not any("run" in step or "actions/checkout@" in step.get("uses", "") for step in publisher["steps"])


def test_release_shards_are_complete_and_retries_keep_diagnostics() -> None:
    jobs = load_workflow()["jobs"]
    test, gate = jobs["release-tests"], jobs["release-gate"]
    assert test["strategy"] == {"fail-fast": False, "max-parallel": 3, "matrix": {"shard": [1, 2, 3]}}
    run = next(step for step in test["steps"] if "tools/run_test_groups.py" in step.get("run", ""))
    assert run["run"] == ".venv/bin/python tools/run_test_groups.py --shard-count 3 --shard-index ${{ matrix.shard }}"
    uploads = [step for step in test["steps"] if "actions/upload-artifact@" in step.get("uses", "")]
    manifest = next(step for step in uploads if step["with"]["path"] == "suite-evidence/manifest.json")
    diagnostics = next(step for step in uploads if step["with"]["path"] == "suite-evidence/")
    assert "if" not in manifest
    assert manifest["with"]["if-no-files-found"] == "error" and manifest["with"]["overwrite"] is True
    assert "github.run_id" in manifest["with"]["name"] and "github.run_attempt" not in manifest["with"]["name"]
    assert diagnostics["if"] == "always()" and "overwrite" not in diagnostics["with"]
    assert "github.run_attempt" in diagnostics["with"]["name"] and "matrix.shard" in diagnostics["with"]["name"]
    assert diagnostics["with"]["retention-days"] == 90
    steps = gate["steps"]
    download = next(step for step in steps if "pattern" in step.get("with", {}))
    verify = next(step for step in steps if "tools.verify_test_shards" in step.get("run", ""))
    promote = next(step for step in steps if step["name"] == "Store verified distributions")
    assert download["with"]["pattern"] == manifest["with"]["name"].replace("${{ matrix.shard }}", "*")
    assert steps.index(download) < steps.index(verify) < steps.index(promote)
    assert verify["env"] == {"SUITE_COMMIT": "${{ github.sha }}", "SUITE_RUN_ID": "${{ github.run_id }}"}
    command = shlex.split(verify["run"].replace("\\\n", " "))
    assert command[command.index("--shard-count") + 1] == "3"
    assert command[command.index("--platform") + 1] == "Linux/X64/3.12"


def inline_python(job_name: str, step_name: str) -> str:
    steps = load_workflow()["jobs"][job_name]["steps"]
    command = next(step["run"] for step in steps if step["name"] == step_name)
    return command.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


def run_inline_python(code: str) -> None:
    exec(compile(code, str(WORKFLOW_PATH), "exec"), {})  # noqa: S102 - exercise the checked-in workflow guard.


@pytest.mark.parametrize("defect", [None, "commit", "tag", "name", "version", "changelog"])
@pytest.mark.parametrize("use_backport", [False, True], ids=["default-parser", "tomli-backport"])
def test_release_identity_checks_run_against_the_event_commit(tmp_path, monkeypatch, defect, use_backport):
    if use_backport:
        monkeypatch.setitem(sys.modules, "tomllib", None)
    project = "other-project" if defect == "name" else "evidence-wiki"
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "{project}"\nversion = "0.7.1"\n')
    package = tmp_path / "src/evidence_wiki"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0.7.2"\n' if defect == "version" else '__version__ = "0.7.1"\n')
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n" if defect == "changelog" else "## 0.7.1 - 2026-09-13\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASE_TAG", "v0.7.2" if defect == "tag" else "v0.7.1")
    monkeypatch.setenv("RELEASE_COMMIT", "a" * 40)

    def checkout_commit(command, **kwargs):
        assert command == ["git", "rev-parse", "HEAD"] and kwargs == {"text": True}
        return ("b" if defect == "commit" else "a") * 40 + "\n"

    monkeypatch.setattr(subprocess, "check_output", checkout_commit)
    code = inline_python("release-identity", "Validate release identity")
    if defect:
        with pytest.raises(SystemExit):
            run_inline_python(code)
    else:
        run_inline_python(code)


@pytest.mark.parametrize("defect", [None, "bytes", "name", "version", "missing", "extra", "duplicate"])
@pytest.mark.parametrize("use_backport", [False, True], ids=["default-parser", "tomli-backport"])
def test_promoted_distributions_are_exactly_the_validated_bytes(tmp_path, monkeypatch, defect, use_backport):
    if use_backport:
        monkeypatch.setitem(sys.modules, "tomllib", None)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.7.1"\n')
    candidate = tmp_path / "release-candidate"
    dist = candidate / "dist"
    dist.mkdir(parents=True)
    payloads = {"wheel": ("evidence_wiki-0.7.1-py3-none-any.whl", b"validated wheel"),
                "sdist": ("evidence_wiki-0.7.1.tar.gz", b"validated source archive")}
    report = {"expected_version": "0.7.1"}
    for kind, (name, content) in payloads.items():
        (dist / name).write_bytes(content)
        report[kind] = {"name": name, "sha256": hashlib.sha256(content).hexdigest()}
    if defect == "bytes":
        (dist / payloads["wheel"][0]).write_bytes(b"replaced wheel")
    elif defect == "name":
        report["wheel"]["name"] = "different.whl"
    elif defect == "version":
        report["expected_version"] = "0.7.2"
    elif defect == "missing":
        (dist / payloads["sdist"][0]).unlink()
    elif defect == "extra":
        (dist / "internal-report.json").write_text("{}")
    elif defect == "duplicate":
        (dist / "another.whl").write_bytes(b"extra wheel")
    (candidate / "artifact-validation.json").write_text(json.dumps(report))
    monkeypatch.chdir(tmp_path)
    code = inline_python("release-gate", "Verify the exact distribution bytes before promotion")
    if defect:
        with pytest.raises(SystemExit):
            run_inline_python(code)
    else:
        run_inline_python(code)


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
    assert "--skip-sdist" not in text and "--membership-only" not in text


def test_ci_runs_the_same_shared_artifact_gate_as_the_release() -> None:
    text = CI_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "tools/validate_installed_artifacts.py --dist-dir dist" in text
    assert "pip install dist/*.whl" not in text
    assert "import pypdf" not in text


def test_ci_shards_cover_every_platform_and_gate_packaging_on_complete_results() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW_PATH.read_text(encoding="utf-8"))
    test, package = workflow["jobs"]["test"], workflow["jobs"]["package"]
    matrix = test["strategy"]["matrix"]
    assert set(matrix) == {"platform", "shard"}
    assert matrix["shard"] == [1, 2, 3]
    expected = {("ubuntu-latest", "3.10"), ("ubuntu-latest", "3.14"),
                ("macos-latest", "3.12"), ("windows-latest", "3.12")}
    assert {(cell["os"], cell["python-version"]) for cell in matrix["platform"]} == expected
    assert len(list(itertools.product(matrix["platform"], matrix["shard"]))) == 12
    assert test["strategy"]["fail-fast"] is False
    assert package["needs"] == "test" and "if" not in package
    commands = [step["run"] for step in test["steps"] if "tools/run_test_groups.py" in step.get("run", "")]
    assert len(commands) == 2
    assert all("--shard-count 3 --shard-index ${{ matrix.shard }}" in command for command in commands)
    assert all("--group-size 48" in command for command in commands)
    assert all("-m ruff check ." in command and "sync_vendored_scripts.py --check" in command for command in commands)
    upload = next(step for step in test["steps"] if "actions/upload-artifact@" in step.get("uses", ""))
    assert upload["if"] == "always()"
    assert upload["with"]["name"].endswith("-shard-${{ matrix.shard }}")
    assert upload["with"]["retention-days"] == 90
    steps = package["steps"]
    download = next(step for step in steps if "actions/download-artifact@" in step.get("uses", ""))
    gate = next(step for step in steps if "-m tools.verify_test_shards" in step.get("run", ""))
    build = next(step for step in steps if "-m build" in step.get("run", ""))
    assert steps.index(download) < steps.index(gate) < steps.index(build)
    assert download["with"] == {"pattern": "suite-${{ github.sha }}-*-shard-*", "path": "suite-shards/"}
    assert gate["env"] == {"SUITE_COMMIT": "${{ github.sha }}", "SUITE_RUN_ID": "${{ github.run_id }}"}
    command = shlex.split(gate["run"].replace("\\\n", " "))
    assert command[command.index("--shard-count") + 1] == "3"
    assert command[command.index("--group-size") + 1] == "48"
    assert {command[i + 1] for i, arg in enumerate(command) if arg == "--platform"} == {
        "Linux/X64/3.10", "Linux/X64/3.14", "macOS/ARM64/3.12", "Windows/X64/3.12"}


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
    assert workflow["on"]["workflow_dispatch"]["inputs"]["profile"]["options"] == ["standard", "near-partition", "both"]
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
    jobs = workflow["jobs"]
    for job, report in (("release-artifacts", "artifact-validation.json"),
                        ("release-scale", "scale-benchmark-standard.json")):
        steps = jobs[job]["steps"]
        collector = next(step for step in steps if "tools/collect_release_diagnostics.py" in step.get("run", ""))
        upload = next(step for step in steps if step.get("with", {}).get("path", "").startswith("release-diagnostics/"))
        assert collector["if"] == upload["if"] == "always()"
        assert collector["run"].endswith("--report " + report)
        assert collector["env"] == {"RELEASE_GATE_STATUS": "${{ job.status }}"}
        assert all(token in upload["with"]["name"] for token in ("github.sha", "runner.arch", "github.run_attempt"))
        assert "dist/" not in upload["with"]["path"] and "overwrite" not in upload["with"]
    steps = jobs["release-gate"]["steps"]
    candidate = next(step for step in jobs["release-artifacts"]["steps"]
                     if step["name"] == "Store candidate distributions and their validation report")
    candidate_download = next(step for step in steps if step["name"] == "Download the validated candidate distributions")
    assert "if" not in candidate
    assert candidate["with"]["overwrite"] is True
    assert candidate["with"]["if-no-files-found"] == "error"
    assert candidate_download["with"] == {"name": candidate["with"]["name"], "path": "release-candidate/"}
    distributions = next(step for step in steps if step["name"] == "Store verified distributions")
    verify = next(step for step in steps if step["name"] == "Verify the exact distribution bytes before promotion")
    assert steps.index(verify) < steps.index(distributions)
    assert "if" not in distributions
    assert distributions["with"]["path"].splitlines() == ["release-candidate/dist/*.whl", "release-candidate/dist/*.tar.gz"]
    assert distributions["with"]["overwrite"] is True
    assert distributions["with"]["if-no-files-found"] == "error"
    publisher_download = jobs["publish-to-pypi"]["steps"][0]
    assert publisher_download["with"] == {"name": distributions["with"]["name"], "path": "dist"}
    assert "if" not in jobs["publish-to-pypi"]


def test_publisher_receives_only_flat_distribution_files(tmp_path):
    jobs = load_workflow()["jobs"]
    upload = next(step for step in jobs["release-gate"]["steps"] if step["name"] == "Store verified distributions")
    download = jobs["publish-to-pypi"]["steps"][0]
    dist = tmp_path / "release-candidate/dist"
    dist.mkdir(parents=True)
    names = {"evidence_wiki-9.9.9-py3-none-any.whl", "evidence_wiki-9.9.9.tar.gz"}
    for name in names:
        (dist / name).write_bytes(b"validated distribution payload")
    (dist.parent / "artifact-validation.json").write_text("{}")
    (tmp_path / "scale-benchmark-standard.json").write_text("{}")
    selected = [path for pattern in upload["with"]["path"].splitlines() for path in tmp_path.glob(pattern)]
    assert len(selected) == 2
    # upload-artifact preserves paths relative to the selected paths' common root.
    artifact_root = Path(os.path.commonpath([path.parent for path in selected]))
    delivered = {Path(download["with"]["path"]) / path.relative_to(artifact_root) for path in selected}
    assert delivered == {Path("dist") / name for name in names}


def test_coverage_measures_subprocesses_and_folds_copied_scripts_onto_the_template() -> None:
    pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")

    assert "[tool.coverage.run]" in pyproject
    assert "branch = true" in pyproject
    assert "parallel = true" in pyproject
    assert 'patch = ["subprocess"]' in pyproject
    assert '"*/scripts/*.py"' in pyproject
    assert "[tool.coverage.paths]" in pyproject
