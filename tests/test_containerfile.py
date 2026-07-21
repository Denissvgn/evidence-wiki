from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_containerfile_builds_source_into_a_non_root_runtime() -> None:
    text = (REPO_ROOT / "Containerfile").read_text(encoding="utf-8")

    assert text.count("FROM python:3.12-slim-bookworm@sha256:") == 2
    assert " AS builder" in text
    assert "COPY . ." in text
    assert "/opt/evidence-wiki-venv/bin/python -m pip install --no-cache-dir ." in text
    assert "/opt/evidence-wiki-venv/bin/evidence-wiki --version" in text
    assert "apt-get install --yes --no-install-recommends poppler-utils" in text
    assert "rm -rf /var/lib/apt/lists/*" in text
    assert "USER 10001:10001" in text
    assert 'ENTRYPOINT ["/opt/evidence-wiki-venv/bin/evidence-wiki"]' in text


def test_container_context_excludes_local_and_generated_state() -> None:
    ignored = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    for path in (
        ".git/",
        ".venv/",
        ".pytest_cache/",
        ".env",
        ".research-handoff-secret",
        "/AGENTS.md",
        "dist/",
        "pilot-workspaces/",
        "reports/",
    ):
        assert path in ignored


def test_containerfile_has_no_removed_candidate_qualification_contract() -> None:
    text = (REPO_ROOT / "Containerfile").read_text(encoding="utf-8").casefold()

    for removed_term in ("candidate_manifest", "wheel_sha256", "qualification", "release/"):
        assert removed_term not in text
