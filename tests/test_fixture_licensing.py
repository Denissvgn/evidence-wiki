from __future__ import annotations

import fnmatch
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures"
CATALOG_PATH = FIXTURES_ROOT / "fixture-provenance.yml"
NOTICES_PATH = REPO_ROOT / "THIRD_PARTY_NOTICES.md"

REQUIRED_FIELDS = {
    "stable_id",
    "classification",
    "source_work",
    "origin_url",
    "created_at",
    "copyright_holder",
    "license",
    "license_url",
    "terms_url",
    "redistribution_permission",
    "attribution",
    "transformations",
    "shipped_paths",
}


def load_catalog() -> dict:
    payload = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_every_distributed_fixture_has_exactly_one_rights_record() -> None:
    catalog = load_catalog()
    records = catalog.get("fixtures")
    assert isinstance(records, list) and records

    stable_ids = [record.get("stable_id") for record in records]
    assert len(stable_ids) == len(set(stable_ids))

    for record in records:
        assert REQUIRED_FIELDS <= set(record), record.get("stable_id")
        assert record["redistribution_permission"] is True, record["stable_id"]
        assert record["classification"] in {"synthetic", "redistributable_third_party"}
        assert record["shipped_paths"], record["stable_id"]
        assert (REPO_ROOT / record["license_url"]).is_file(), record["stable_id"]

    fixture_files = sorted(path for path in FIXTURES_ROOT.rglob("*") if path.is_file())
    assert fixture_files
    for path in fixture_files:
        relative = path.relative_to(REPO_ROOT).as_posix()
        matches = [
            record["stable_id"]
            for record in records
            if any(fnmatch.fnmatchcase(relative, pattern) for pattern in record["shipped_paths"])
        ]
        assert len(matches) == 1, f"{relative}: expected one fixture-rights record, found {matches}"


def test_synthetic_pdf_fixtures_do_not_redistribute_paper_text() -> None:
    fixture_files = sorted((FIXTURES_ROOT / "pdf-extraction").glob("*/*.txt"))
    assert fixture_files
    for path in fixture_files:
        text = path.read_text(encoding="utf-8")
        assert "SYNTHETIC" in text[:160], path
        assert "Copyright" not in text, path
        assert "@" not in text, path
        assert len(text) >= 500, path


def test_notice_and_catalog_state_fail_closed_policy() -> None:
    catalog = load_catalog()
    policy = catalog["policy"]
    notices = NOTICES_PATH.read_text(encoding="utf-8")

    assert policy["unmapped_fixture"] == "validation_error"
    assert policy["uncertain_redistribution"] == "remove_or_replace"
    assert policy["guessed_spdx_identifiers"] == "forbidden"
    assert "not excerpts from the referenced papers" in " ".join(notices.split())
    assert "fixture-provenance.yml" in notices


def test_project_authored_primitives_are_mit_without_relicensing_dependencies() -> None:
    catalog = load_catalog()
    notices = NOTICES_PATH.read_text(encoding="utf-8")
    project_license = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert project_license.startswith("MIT License\n")
    assert 'license = { text = "MIT" }' in pyproject
    for record in catalog["fixtures"]:
        if record["classification"] == "synthetic":
            assert record["license"] == "MIT", record["stable_id"]
            assert record["license_url"] == "LICENSE", record["stable_id"]

    normalized_notices = " ".join(notices.split())
    assert "source code, documentation, templates, and examples" in normalized_notices
    assert "dependencies retain their own copyright and license terms" in normalized_notices
    assert "do not relicense those projects" in normalized_notices
