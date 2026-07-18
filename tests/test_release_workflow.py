from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish.yml"


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


def test_release_gate_checks_identity_quality_and_built_wheel() -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    for required in (
        "github.event.release.tag_name",
        'project["version"]',
        "src/evidence_wiki/__init__.py",
        "CHANGELOG.md",
        "-m pytest -q",
        "-m ruff check .",
        "tools/sync_vendored_scripts.py --check",
        "git diff --check",
        "-m twine check",
        "pip install dist/*.whl",
    ):
        assert required in text


def test_workflow_uses_oidc_without_a_stored_pypi_credential() -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "id-token: write" in text
    assert "secrets." not in text
    assert "password:" not in text
    assert "api-token" not in text
