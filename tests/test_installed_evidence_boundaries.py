"""Real bounded process receipts and adversarial archive/dependency boundaries."""

import copy
import hashlib
import io
import json
import stat
import subprocess
import sys
import tarfile
import zipfile
from importlib.metadata import distribution

import pytest

from tools import _installation_contract as contract
from tools._installed_receipts import evidence_inventory, replay_commands
from tools._qualification_process import CommandRunner
from tools.probe_installed_public import runtime_identity
from tools.validate_installed_artifacts import REPO_ROOT, qualification_expectations
from tools.verify_installed_artifacts import verify


@pytest.fixture(scope="module")
def metadata_basis():
    from pip._vendor.packaging.markers import default_environment

    expected = qualification_expectations()
    resolved = {}
    for name in ("evidence-wiki", "pypdf", "pyyaml", "ruamel-yaml", "tzdata"):
        item = distribution(name)
        resolved[name] = {"version": item.version, "requires_python": item.metadata.get("Requires-Python"),
                          "requires_dist": sorted(item.requires or []),
                          "entry_points": {entry.name: entry.value for entry in item.entry_points if entry.group == "console_scripts"}}
    return expected, resolved, {**runtime_identity(), "marker_environment": default_environment()}


@pytest.mark.parametrize("defect", [None, "version", "requires_python", "entrypoint", "conditional", "extra", "markers"])
def test_installed_metadata_requires_real_constraints_and_frozen_declarations(metadata_basis, defect):
    expected, original, runtime = copy.deepcopy(metadata_basis)
    if defect == "version":
        original["pypdf"]["version"] = "0.0"
    elif defect == "requires_python":
        original["evidence-wiki"]["requires_python"] = ">=2"
    elif defect == "entrypoint":
        original["evidence-wiki"]["entry_points"]["evidence-wiki"] = "foreign:main"
    elif defect == "conditional":
        original["evidence-wiki"]["requires_dist"].append("foreign-sdk; python_version >= '3.10'")
    elif defect == "extra":
        original["foreign-sdk"] = {"version": "1.0", "requires_dist": [], "requires_python": None}
    elif defect == "markers":
        runtime["marker_environment"]["python_version"] = "3.10"
    if defect:
        with pytest.raises(ValueError):
            contract.validate_resolved_metadata(original, expected["project_metadata"], expected["dependency_rules"], runtime)
    else:
        assert contract.validate_resolved_metadata(original, expected["project_metadata"], expected["dependency_rules"], runtime) == set(original)


def test_conditional_transitive_dependency_is_required_only_for_selected_python(metadata_basis):
    expected, resolved, runtime = copy.deepcopy(metadata_basis)
    runtime["python_version"] = "3.10.21"
    runtime["marker_environment"].update(python_version="3.10", python_full_version="3.10.21", implementation_version="3.10.21")
    with pytest.raises(ValueError, match="typing-extensions"):
        contract.validate_resolved_metadata(resolved, expected["project_metadata"], expected["dependency_rules"], runtime)
    resolved["typing-extensions"] = {"version": "4.15.0", "requires_dist": [], "requires_python": ">=3.9"}
    assert "typing-extensions" in contract.validate_resolved_metadata(resolved, expected["project_metadata"], expected["dependency_rules"], runtime)


@pytest.mark.parametrize("defect", [None, "outside", "version", "bytes", "traversal"])
def test_actual_dependency_origin_and_imported_version_cannot_be_shadowed(tmp_path, defect):
    prefix = tmp_path / ".venv"
    prefix.mkdir()
    origin = prefix / "pypdf.py"
    data = b"an observed local dependency module"
    origin.write_bytes(data)
    resolved = {"evidence-wiki": {"version": "1.1.2"}, "pypdf": {"version": "6.14.2"}}
    value = {"module": "pypdf", "origin": str(origin), "version": "6.14.2", "bytes": len(data),
             "sha256": hashlib.sha256(data).hexdigest()}
    if defect == "outside":
        value["origin"] = str(tmp_path / "checkout/pypdf.py")
    elif defect == "version":
        value["version"] = "0.0"
    elif defect == "bytes":
        origin.write_bytes(b"shadowed module contents")
    elif defect == "traversal":
        value["origin"] = str(prefix) + "/../shadow.py"
    if defect:
        with pytest.raises(ValueError):
            contract.validate_dependency_origins(resolved, {"pypdf": value}, str(prefix), read_files=True)
    else:
        assert contract.validate_dependency_origins(resolved, {"pypdf": value}, str(prefix), read_files=True)


@pytest.mark.parametrize("defect", [None, "log", "receipt", "missing", "duplicate", "extra", "no_pid", "exit"])
def test_retained_receipts_reconstruct_actual_start_exit_and_bytes(tmp_path, defect):
    runner = CommandRunner(tmp_path)
    runner.run([sys.executable, "-c", "print('actual process output')"], label="one", cwd=REPO_ROOT, timeout=10)
    original = evidence_inventory(tmp_path)
    records = copy.deepcopy(runner.records)
    if defect == "log":
        (tmp_path / records[0]["stdout"]).write_text("fabricated output")
        assert evidence_inventory(tmp_path) != original
    elif defect == "receipt":
        (tmp_path / records[0]["receipt"]).write_text("{}")
    elif defect == "missing":
        records.clear()
    elif defect == "duplicate":
        records.append(copy.deepcopy(records[0]))
    elif defect == "extra":
        extra = copy.deepcopy(records[0]); extra["stage"] = "not-declared"; records.append(extra)
    elif defect == "no_pid":
        records[0].pop("pid")
    elif defect == "exit":
        records[0]["exit_code"] = 2
    if defect:
        with pytest.raises(ValueError):
            replay_commands(tmp_path, records, ["one"])
    else:
        result = replay_commands(tmp_path, records, ["one"])
        assert result["one"]["stdout"] == "actual process output\n"


def test_unsigned_reports_cannot_earn_installed_qualification(tmp_path):
    with pytest.raises(ValueError, match="original producer report SHA256"):
        verify(tmp_path / "not-built", tmp_path, commit="source", run_id="run")


@pytest.mark.parametrize("payload", [None, "explicit host-selected payload"])
def test_noninteractive_commands_use_actual_devnull_or_explicit_payload(tmp_path, monkeypatch, payload):
    original = subprocess.Popen
    observed = []
    def launch(*args, **options):
        observed.append(options["stdin"])
        return original(*args, **options)
    monkeypatch.setattr(subprocess, "Popen", launch)
    runner = CommandRunner(tmp_path)
    result = runner.run([sys.executable, "-c", "import sys; print(sys.stdin.read())"], label="input", input=payload, timeout=10)
    assert result.stdout == (payload or "") + "\n"
    assert (observed[0] == subprocess.DEVNULL) is (payload is None)
    assert runner.records[0]["stdin_mode"] == ("devnull" if payload is None else "explicit_payload")


@pytest.mark.parametrize("member", ["../escape.py", "/absolute.py", "a\\b.py", "C:/file.py"])
def test_archive_paths_refuse_before_source_parity(tmp_path, member):
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(member, b"x")
    with pytest.raises(ValueError, match="unsafe archive path"):
        contract.admitted_archive(wheel, "wheel")


@pytest.mark.parametrize("defect", ["duplicate", "link", "directory_payload"])
def test_zip_duplicates_links_and_directory_bytes_are_not_hidden(tmp_path, defect):
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("owner.py", b"x")
        if defect == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate"):
                archive.writestr("owner.py", b"same")
        elif defect == "link":
            link = zipfile.ZipInfo("linked.py"); link.create_system = 3; link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, b"owner.py")
        else:
            archive.writestr("reports/", b"hidden")
    with pytest.raises(ValueError):
        contract.admitted_archive(wheel, "wheel")


def test_raw_tar_metadata_expansion_is_bounded_before_pax_parsing(tmp_path, monkeypatch):
    sdist = tmp_path / "candidate.tar.gz"
    with tarfile.open(sdist, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo("project/owner.py"); member.size = 1; member.pax_headers = {"comment": "x" * 4096}
        archive.addfile(member, io.BytesIO(b"x"))
    monkeypatch.setattr(contract, "MAX_EXPANDED_BYTES", 1024)
    with pytest.raises(ValueError, match="raw tar expansion"):
        contract.admitted_archive(sdist, "sdist")


def test_exact_source_maps_require_nonanchor_files_and_refuse_unbound_members(tmp_path):
    source = {"owner.py": hashlib.sha256(b"x").hexdigest(), "other.py": hashlib.sha256(b"y").hexdigest()}
    layout = {"maps": {"wheel": {"owner.py": "owner.py", "other.py": "other.py"}}, "generated": {"wheel": []}}
    for members in ({"owner.py": b"x"}, {"owner.py": b"x", "other.py": b"y", "unbound.py": b"z"}):
        wheel = tmp_path / "candidate.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        with pytest.raises(ValueError, match="exact member inventory"):
            contract.check_source_archives(wheel, tmp_path / "unused.tar.gz", source, layout)


def test_custody_is_supplied_outside_report_and_checked_before_json(tmp_path):
    # The externally authorized hash must match exact bytes. A downloaded JSON
    # field cannot choose that expected hash or replace the original observation.
    raw = b'{"expected_hash":"invented"}'
    assert hashlib.sha256(raw).hexdigest() != "0" * 64
    with pytest.raises(ValueError):
        verify(tmp_path / "not-built", tmp_path, commit="source", run_id="run", report_sha256=[json.dumps("invented")])


def test_default_backend_force_includes_are_source_owned_and_exact(tmp_path):
    from tests.test_qualification_management import installation_reports

    project = tmp_path / "project"
    package = project / "src/example"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("__version__ = '1.0.0'\n")
    (project / ".gitignore").write_text("private-output/\n")
    (project / "LICENSE").write_text("MIT\n")
    (project / "README.md").write_text("Public package.\n")
    (project / "pyproject.toml").write_text('''
[build-system]
requires = ["hatchling==1.30.1"]
build-backend = "hatchling.build"
[project]
name = "example"
version = "1.0.0"
readme = "README.md"
license = {text = "MIT"}
requires-python = ">=3.10"
[project.scripts]
example = "example:main"
[tool.hatch.build.targets.wheel]
packages = ["src/example"]
[tool.hatch.build.targets.sdist]
include = ["/src"]
''')
    layout, sources = contract.source_layout(project)
    assert layout["maps"]["sdist"][".gitignore"] == ".gitignore"
    assert project / ".gitignore" in sources
    assert {"pyproject.toml", "README.md", "LICENSE", ".gitignore"} <= set(layout["maps"]["sdist"])

    # A valid source-byte archive fixture exercises the complete production
    # map, then deliberately removes or changes a default force-included file.
    dist, reports = installation_reports(tmp_path)
    wheel = next(dist.glob("*.whl"))
    sdist = next(dist.glob("*.tar.gz"))
    source = reports["wheel"]["candidate_source"]
    expected = reports["wheel"]["archive_expectations"]
    contract.check_source_archives(wheel, sdist, source, expected)
    with tarfile.open(sdist, "r:gz") as archive:
        original = [(member.name, archive.extractfile(member).read()) for member in archive if member.isfile()]
    for changed in (False, True):
        target = tmp_path / ("changed.tar.gz" if changed else "missing.tar.gz")
        with tarfile.open(target, "w:gz") as archive:
            for name, data in original:
                if name.endswith("/.gitignore"):
                    if not changed:
                        continue
                    data = b"different source configuration\n"
                member = tarfile.TarInfo(name); member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        with pytest.raises(ValueError, match="inventory differs|bytes differ"):
            contract.check_source_archives(wheel, target, source, expected)
