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


def test_source_layout_excludes_backup_files_from_both_archives(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_bytes((artifacts.REPO_ROOT / "pyproject.toml").read_bytes())
    for name in (
        "README.md", "LICENSE", "src/evidence_wiki/__init__.py",
        "src/evidence_wiki/module.py", "src/evidence_wiki/module.py.orig",
        "src/evidence_wiki/nested/module.py.orig", "tests/test_module.py.orig",
        "workspace-template/AGENTS.md", "domain-packs/example.txt", "orchestrator/example.txt",
        "examples/urban-heat-resilience-workspace/AGENTS.md",
        "tests/fixtures/madrid-autonomo-workspace/AGENTS.md",
    ):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")

    layout, sources = artifacts.source_layout(project)

    assert layout["maps"]["wheel"]["evidence_wiki/module.py"] == "src/evidence_wiki/module.py"
    assert layout["maps"]["sdist"]["src/evidence_wiki/module.py"] == "src/evidence_wiki/module.py"
    for members in layout["maps"].values():
        assert not any(name.endswith(".orig") for name in members)
        assert not any(name.endswith(".orig") for name in members.values())
    assert not any(path.name.endswith(".orig") for path in sources)
    assert (project / "src/evidence_wiki/module.py.orig").read_text(encoding="utf-8") == "fixture\n"

    # Forced wheel assets bypass Hatch's global excludes; keep that membership
    # visible so the producer's archive projection guard can reject a mismatch.
    asset_backup = project / "workspace-template/example.txt.orig"
    asset_backup.write_text("asset backup\n", encoding="utf-8")
    layout, sources = artifacts.source_layout(project)
    assert layout["maps"]["wheel"]["evidence_wiki/assets/workspace-template/example.txt.orig"] == "workspace-template/example.txt.orig"
    assert "workspace-template/example.txt.orig" not in layout["maps"]["sdist"]
    assert asset_backup in sources


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


@pytest.mark.parametrize("damage", [None, "invented-github", "missing-sdist", "local-as-hosted", "local-run-id", "changed-source"])
def test_local_installation_binding_keeps_both_source_bound_artifacts(tmp_path, damage):
    dist, reports = installation_reports(tmp_path)
    for row in reports.values():
        row["workflow"] = dict.fromkeys(("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"))
    if damage == "invented-github":
        reports["wheel"]["workflow"]["GITHUB_SHA"] = "invented"
    elif damage == "missing-sdist":
        reports.pop("sdist")
    elif damage == "changed-source":
        reports["wheel"]["candidate_source"]["pyproject.toml"] = "0" * 64
        reports["wheel"]["candidate_source_sha256"] = public.digest(reports["wheel"]["candidate_source"])
    evidence = tmp_path / "reports"
    save_reports(evidence, reports)
    options = {"commit": "commit", "run_id": None, "provenance": "local"}
    if damage == "local-as-hosted":
        options.update(provenance="github", run_id="run")
    elif damage == "local-run-id":
        options["run_id"] = "invented"
    if damage is None:
        result = synthetic_verify(dist, evidence, **options)
        assert result["status"] == "passed" and result["provenance"] == "local"
        assert result["workflow"] is None and result["candidate_commit"] == "commit"
        assert set(result["installations"]) == {"wheel", "sdist"}
    else:
        with pytest.raises(ValueError):
            synthetic_verify(dist, evidence, **options)


@pytest.mark.parametrize("defect", ["missing_row", "missing_sdist", "duplicate", "unexpected"])
def test_frozen_runtime_matrix_requires_both_artifacts_in_every_exact_cell(tmp_path, defect):
    dist, reports = installation_reports(tmp_path)
    original = reports["wheel"]["qualification_runtime"]
    major, minor, patch = original["python_version"].split(".")
    alternate_version = f"{major}.{minor}.{int(patch) - 1 if int(patch) else 1}"
    alternate = {**original, "python_version": alternate_version}
    required = [original, alternate]
    if defect != "missing_row":
        for kind in ("wheel", "sdist"):
            if defect == "missing_sdist" and kind == "sdist":
                continue
            other = copy.deepcopy(reports[kind])
            other["qualification_runtime"] = alternate
            other["checks"]["installed_" + kind]["public_installed"]["runtime"] = alternate
            other["checks"]["installed_"+kind]["public_installed"]["marker_environment"].update(
                python_version=f"{major}.{minor}", python_full_version=alternate_version,
                implementation_version=alternate_version)
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

    assert PROFILE_SCHEMA_VERSIONS == tuple("research-execution-profile/v" + str(index) for index in range(1, 18))
    advertised = contract()["research_sessions"]["execution_profile_schemas"]
    assert advertised == list(PROFILE_SCHEMA_VERSIONS)
    assert contract()["library_api"]["version"] == "64"
    assert contract()["library_api"]["matrix_version"] == "44"


class PagedPublicResources:
    def __init__(self, rows, documents, *, page_size=2):
        self.rows, self.documents, self.page_size = rows, documents, page_size
        self.offsets = []

    def resources(self, *, offset=0):
        self.offsets.append(offset)
        selected = self.rows[offset:offset + self.page_size]
        return {"resources": selected, "bounds": {
            "total": len(self.rows), "returned": len(selected),
            "truncated": len(selected) < len(self.rows)}}

    def resource(self, resource_id):
        return self.documents[resource_id]


def public_resource_fixture():
    rows = [{"id": str(index), "media_type": "application/schema+json"} for index in range(5)]
    documents = {row["id"]: {"content": json.dumps({"title": row["id"]})} for row in rows}
    expected = {
        "resource_documents": {key: public.digest(value) for key, value in documents.items()},
        "schema_documents": {key: public.digest(json.loads(value["content"])) for key, value in documents.items()},
    }
    return rows, documents, expected


def test_public_resources_read_all_pages_even_when_tail_is_truncated():
    rows, documents, expected = public_resource_fixture()
    host = PagedPublicResources(rows, documents)
    first = host.resources()
    assert first["bounds"]["truncated"] is True
    assert public.resource_inventory(host, first) == rows
    assert host.offsets == [0, 2, 4]
    host.offsets.clear()
    public.checked_resource_documents(host, expected)
    assert host.offsets == [0, 2, 4]


@pytest.mark.parametrize("defect", ["missing_tail", "resource_mismatch", "schema_mismatch"])
def test_public_resources_reject_tail_omission_and_document_mismatch(defect):
    rows, documents, expected = public_resource_fixture()
    if defect == "missing_tail":
        rows.pop()
    elif defect == "resource_mismatch":
        documents["4"] = {"content": '{"title":"changed"}'}
    else:
        expected["schema_documents"]["4"] = "0" * 64
    with pytest.raises(ValueError, match="inventory differs|document differs"):
        public.checked_resource_documents(PagedPublicResources(rows, documents), expected)


@pytest.mark.parametrize("defect", ["duplicate", "nonprogress", "total", "returned", "total_float", "returned_bool"])
def test_public_resource_pagination_rejects_invalid_followup_pages(defect):
    rows, documents, _ = public_resource_fixture()
    host = PagedPublicResources(rows, documents)
    original = host.resources

    def resources(*, offset=0):
        page = original(offset=offset)
        if offset:
            if defect == "duplicate":
                page["resources"][0] = rows[0]
            elif defect == "nonprogress":
                page["resources"] = []
                page["bounds"]["returned"] = 0
            elif defect == "total_float":
                page["bounds"]["total"] = float(page["bounds"]["total"])
            elif defect == "returned_bool":
                page["bounds"]["returned"] = True
            else:
                page["bounds"][defect] += 1
        return page

    host.resources = resources
    with pytest.raises(ValueError, match="Duplicate|no progress|bounds differ"):
        public.resource_inventory(host)
