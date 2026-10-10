"""Independent commitments, public probe boundaries and complete runtime matrices."""

import ast
import copy
import json
import zipfile
from pathlib import Path

import pytest

from tests.test_qualification_management import installation_reports, synthetic_verify
from tools import probe_installed_public as public
from tools import validate_installed_artifacts as artifacts
from tools import verify_installed_artifacts as verification


def save_reports(root, reports):
    for name, report in reports.items():
        target = root / name
        target.mkdir(parents=True)
        (target / "summary.json").write_text(json.dumps(report), encoding="utf-8")


def test_probe_uses_only_public_package_imports_and_frozen_legacy_vectors():
    tree = ast.parse(Path(public.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("evidence_wiki"):
            assert all(not part.startswith("_") for part in node.module.split("."))
            assert all(not name.name.startswith("_") for name in node.names)
        if isinstance(node, ast.Import):
            assert all(not name.name.startswith("tests") for name in node.names)
    vectors = public.legacy_vectors()
    assert len(vectors) == 8 and all(len(value) == 64 for value in vectors.values())


@pytest.mark.parametrize("case", public.PUBLIC_CASES)
@pytest.mark.parametrize("status", [None, "not_invoked", "unsupported_host", "unknown", "failed", True])
def test_required_public_observation_cannot_skip_or_disappear(case, status):
    expected = {"framework_matrix": {"frameworks": []}}
    report = {"observation_class": "public_installed_fixture_mechanics", "outcomes": dict.fromkeys(public.PUBLIC_CASES, "passed")}
    if status is None:
        report["outcomes"].pop(case)
    else:
        report["outcomes"][case] = status
    with pytest.raises(ValueError, match="outcomes incomplete"):
        public.checked_public_outcomes(report, expected)


@pytest.mark.parametrize("defect", ["source", "expectations", "probe", "framework", "production", "scope", "runtime"])
def test_final_gate_rejects_changed_source_configuration_probe_and_claims(tmp_path, defect):
    dist, reports = installation_reports(tmp_path)
    report = reports["wheel"]
    installed = report["checks"]["installed_wheel"]
    if defect == "source":
        report["candidate_source"]["pyproject.toml"] = "0" * 64
        report["candidate_source_sha256"] = public.digest(report["candidate_source"])
    elif defect == "expectations":
        report["public_expectations"]["installation"]["library_api_version"] = "14"
        report["public_expectations_sha256"] = public.digest(report["public_expectations"])
    elif defect == "probe":
        report["validation_inputs"]["tools/probe_installed_public.py"] = "0" * 64
    elif defect == "framework":
        installed["public_installed"]["framework_matrix_sha256"] = public.digest({"frameworks": []})
    elif defect == "production":
        installed["public_installed"]["production_qualification"] = True
    elif defect == "scope":
        installed["native_regression_scope"] = "public_only"
    else:
        installed["public_installed"]["runtime"]["python_version"] = "3.10.0"
    save_reports(tmp_path / "reports", reports)
    with pytest.raises(ValueError):
        verification.verify(dist, tmp_path / "reports", commit="commit", run_id="run")


def test_archive_membership_does_not_admit_same_version_stale_source(tmp_path):
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("evidence_wiki/__init__.py", b"old same-version package")
    with pytest.raises(artifacts.ValidationError, match="exact member inventory"):
        artifacts.check_archive_source_bytes(wheel, tmp_path / "unused.tar.gz", artifacts.source_identity())


@pytest.mark.parametrize("defect", ["missing_row", "missing_sdist", "duplicate", "unexpected"])
def test_frozen_runtime_matrix_requires_both_artifacts_in_every_exact_cell(tmp_path, defect):
    dist, reports = installation_reports(tmp_path)
    original = reports["wheel"]["qualification_runtime"]
    alternate = {**original, "python_version": "3.14.7"}
    required = [original, alternate]
    if defect != "missing_row":
        for kind in ("wheel", "sdist"):
            if defect == "missing_sdist" and kind == "sdist":
                continue
            other = copy.deepcopy(reports[kind])
            other["qualification_runtime"] = alternate
            other["checks"]["installed_" + kind]["public_installed"]["runtime"] = alternate
            other["checks"]["installed_"+kind]["public_installed"]["marker_environment"].update(python_version="3.14",python_full_version="3.14.7",implementation_version="3.14.7")
            reports["other-" + kind] = other
    if defect == "duplicate":
        required.append(dict(original))
    elif defect == "unexpected":
        required = [original]
    save_reports(tmp_path / "reports", reports)
    with pytest.raises(ValueError, match="runtime|both wheel"):
        synthetic_verify(dist, tmp_path / "reports", commit="commit", run_id="run", required_runtimes=required)


def test_core_requirements_match_project_and_source_commitments_include_configuration():
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    project = tomllib.loads((artifacts.REPO_ROOT / "pyproject.toml").read_text())
    assert sorted(project["project"]["dependencies"]) == sorted(public.CORE_REQUIREMENTS)
    identity = artifacts.source_identity()
    assert {"pyproject.toml", "src/evidence_wiki/__init__.py", "tools/probe_installed_public.py",
            "tools/verify_installed_artifacts.py", "tests/test_service_legacy_vectors.py",
            "workspace-template/docs/agent-resources/unattended-host.json", ".github/workflows/ci.yml"} <= set(identity)
    assert all(not name.startswith(("reports/", "docs/CR/", "docs/llm_wiki/")) for name in identity)


def test_profile_advertisement_matches_all_supported_public_owner_versions():
    from evidence_wiki import contract
    from evidence_wiki.agent_execution import PROFILE_SCHEMA_VERSIONS

    assert PROFILE_SCHEMA_VERSIONS == tuple("research-execution-profile/v" + str(index) for index in range(1, 14))
    advertised = contract()["research_sessions"]["execution_profile_schemas"]
    assert advertised == list(PROFILE_SCHEMA_VERSIONS)
    assert contract()["library_api"]["version"] == "60"
    assert contract()["library_api"]["matrix_version"] == "41"
