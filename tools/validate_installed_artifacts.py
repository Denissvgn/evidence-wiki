#!/usr/bin/env python3
"""Validate built distributions the same way in CI and at release time.

One validator, two callers. The CI ``package`` job and the release gate used to
carry their own copies of the installed-wheel smoke, which had already drifted:
the release copy probed the round-trip YAML dependency and the CI copy did not,
and neither ever installed anything derived from the sdist. This tool is the
single path both invoke:

1. **Archive membership.** Every member of the wheel and the sdist is checked
   against the public packaging policy: required assets must be present and
   internal material (reports, planning documents, caches, environments, build
   output) must be absent. The sdist's broad ``include`` list is not evidence of
   a leak on its own; the archive is.
2. **Installed wheel.** The wheel is installed into a fresh virtual environment
   and exercised from there: package identity, that imports resolve inside the
   fresh install rather than the checkout, pinned runtime dependencies and the
   YAML round-trip behaviour this package relies on, deploy, pack refresh,
   orchestration start/next/status, the required-asset manifest, the result
   schema, and the managed failure/resume smoke.
3. **Installed sdist.** The sdist is unpacked into an isolated directory, a
   wheel is built from it, and that wheel goes through exactly the same
   installed checks. A distribution that only works because the wheel was built
   from the checkout fails here.

The summary printed at the end records the artifact names, their SHA-256
digests, the version, and each check's result, so a release can cite the exact
bytes it verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import zipfile
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_TOOL = REPO_ROOT / "tools" / "smoke_installed_orchestration.py"
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import resources  # noqa: E402 - the checkout's manifest names what the archives must carry.

#: Every asset the package contract requires, as archive-relative paths.
REQUIRED_ASSET_PATHS = tuple(
    relative for group in resources.required_asset_manifest().values() for relative in group
)

#: Members that must never ship, as path prefixes relative to the archive's
#: project root. Matched against the sdist's members after its leading
#: ``<name>-<version>/`` directory is stripped, and against wheel members as-is.
FORBIDDEN_PREFIXES = (
    ".git/",
    ".github/",
    ".llm-wiki/",
    ".pytest_cache/",
    ".research-cache/",
    ".ruff_cache/",
    ".venv/",
    "build/",
    "dist/",
    "docs/CR/",
    "docs/llm_wiki/",
    "htmlcov/",
    "pilot-workspaces/",
    "reports/",
    "temp/",
    "temp_codebase_project/",
    "temp_workspace/",
    "venv/",
)

#: Individual top-level files that are maintainer-local and must not ship.
FORBIDDEN_TOP_LEVEL_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "RELEASING.md",
    ".coverage",
    ".env",
    ".research-handoff-secret",
)

#: Path components that mark cache or environment residue anywhere in a tree.
FORBIDDEN_COMPONENTS = ("__pycache__", ".venv", ".research-cache")
FORBIDDEN_SUFFIXES = (".pyc", ".pyo")

#: Members every wheel must carry: the package plus every asset the contract names.
REQUIRED_WHEEL_MEMBERS = (
    "evidence_wiki/__init__.py",
    "evidence_wiki/cli.py",
    *(f"evidence_wiki/assets/{relative}" for relative in REQUIRED_ASSET_PATHS),
)

#: Members every sdist must carry: what a from-source build and its checks need,
#: including every asset the wheel built from it will have to force-include.
REQUIRED_SDIST_MEMBERS = (
    "pyproject.toml",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "src/evidence_wiki/__init__.py",
    "tools/smoke_installed_orchestration.py",
    "tools/validate_installed_artifacts.py",
    "tests/_publication_fixture.py",
    "tests/fixtures/fake_codex_cli.py",
    "tests/fixtures/madrid-autonomo-workspace/AGENTS.md",
    "examples/urban-heat-resilience-workspace/AGENTS.md",
    *REQUIRED_ASSET_PATHS,
)

SDIST_ROOT_RE = re.compile(r"^[^/]+/")


class ValidationError(SystemExit):
    """A failed check; the message names the artifact and the reason."""

    def __init__(self, message: str) -> None:
        super().__init__(f"artifact validation failed: {message}")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_artifacts(dist_dir: Path) -> tuple[Path, Path]:
    """Return exactly one wheel and one sdist, refusing an ambiguous directory."""
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValidationError(
            f"{dist_dir} must contain exactly one wheel and one sdist; "
            f"found {[path.name for path in wheels]} and {[path.name for path in sdists]}"
        )
    return wheels[0], sdists[0]


def wheel_members(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return sorted(archive.namelist())


def sdist_members(path: Path) -> list[str]:
    """Members relative to the project root, with the ``<name>-<version>/`` prefix removed."""
    with tarfile.open(path, "r:gz") as archive:
        names = [member.name for member in archive.getmembers() if member.isfile()]
    stripped: list[str] = []
    for name in names:
        if SDIST_ROOT_RE.match(name) is None:
            raise ValidationError(f"{path.name}: sdist member is not below a root directory: {name}")
        stripped.append(SDIST_ROOT_RE.sub("", name, count=1))
    return sorted(stripped)


def forbidden_members(members: list[str]) -> list[str]:
    """Members the public packaging policy forbids, in archive order."""
    flagged: list[str] = []
    for member in members:
        posix = PurePosixPath(member)
        if member.startswith(FORBIDDEN_PREFIXES):
            flagged.append(member)
        elif member in FORBIDDEN_TOP_LEVEL_FILES:
            flagged.append(member)
        elif any(component in FORBIDDEN_COMPONENTS for component in posix.parts):
            flagged.append(member)
        elif posix.suffix in FORBIDDEN_SUFFIXES:
            flagged.append(member)
    return flagged


def missing_members(members: list[str], required: tuple[str, ...]) -> list[str]:
    present = set(members)
    return [member for member in required if member not in present]


def check_archive_membership(wheel: Path, sdist: Path) -> dict[str, object]:
    wheel_names = wheel_members(wheel)
    sdist_names = sdist_members(sdist)
    problems: list[str] = []
    for label, names, required in (
        (wheel.name, wheel_names, REQUIRED_WHEEL_MEMBERS),
        (sdist.name, sdist_names, REQUIRED_SDIST_MEMBERS),
    ):
        flagged = forbidden_members(names)
        if flagged:
            problems.append(f"{label} ships internal material: {', '.join(flagged[:10])}")
        missing = missing_members(names, required)
        if missing:
            problems.append(f"{label} is missing required members: {', '.join(missing)}")
    if problems:
        raise ValidationError("; ".join(problems))
    return {"wheel_members": len(wheel_names), "sdist_members": len(sdist_names)}


def run(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    process = subprocess.run(  # noqa: S603 - argv is fixed by this repository-owned validator.
        argv,
        check=False,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        encoding="utf-8",
        errors="replace",
    )
    if process.returncode != 0:
        raise ValidationError(
            f"command returned {process.returncode}: {argv!r}\nstdout:\n{process.stdout}\nstderr:\n{process.stderr}"
        )
    return process.stdout


def venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def venv_cli(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "evidence-wiki.exe"
    return venv / "bin" / "evidence-wiki"


def create_venv_with_wheel(root: Path, wheel: Path) -> Path:
    venv = root / "venv"
    run([sys.executable, "-m", "venv", str(venv)])
    python = venv_python(venv)
    run([str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", str(wheel)])
    return venv


INSTALLED_PROBE = textwrap.dedent(
    '''
    import json
    import sys
    from importlib.metadata import version
    from io import StringIO
    from pathlib import Path

    import evidence_wiki
    import pypdf
    import ruamel.yaml
    from evidence_wiki import resources
    from evidence_wiki.orchestration import ORCHESTRATION_RESULT_SCHEMA

    expected, pack_refresh_path = sys.argv[1], sys.argv[2]

    if evidence_wiki.__version__ != version("evidence-wiki"):
        raise SystemExit("Installed package metadata and module versions differ")
    if expected and evidence_wiki.__version__ != expected:
        raise SystemExit(
            f"Installed wheel version {evidence_wiki.__version__!r} does not match expected version {expected!r}"
        )
    installed_path = Path(evidence_wiki.__file__).resolve()
    if "site-packages" not in installed_path.parts:
        raise SystemExit(f"EvidenceWiki was not imported from the installed wheel: {installed_path}")
    if not pypdf.__version__.startswith("6."):
        raise SystemExit(f"Installed wheel resolved unsupported pypdf version {pypdf.__version__!r}")
    if not ruamel.yaml.__version__.startswith("0.19."):
        raise SystemExit(f"Installed wheel resolved unsupported ruamel.yaml version {ruamel.yaml.__version__!r}")

    round_trip_yaml = ruamel.yaml.YAML(typ="rt", pure=True)
    round_trip_yaml.preserve_quotes = True
    round_trip_source = '# operator note\\nquoted: "preserve me"\\n'
    round_trip_document = round_trip_yaml.load(round_trip_source)
    round_trip_output = StringIO()
    round_trip_yaml.dump(round_trip_document, round_trip_output)
    if round_trip_output.getvalue() != round_trip_source:
        raise SystemExit("Installed ruamel.yaml dependency did not preserve YAML comments and quotes")

    pack_refresh = json.loads(Path(pack_refresh_path).read_text(encoding="utf-8"))
    if pack_refresh.get("status") != "no_changes" or pack_refresh.get("log_appended") is not False:
        raise SystemExit(f"Installed-wheel bundled pack refresh was not a no-op: {pack_refresh!r}")
    if not (Path(pack_refresh["target"]) / "domain-packs" / ".evidence-wiki-state.yml").is_file():
        raise SystemExit("Installed-wheel pack initialization did not create lifecycle state")

    properties = ORCHESTRATION_RESULT_SCHEMA["properties"]
    if properties["schema_version"] != {"type": "string", "enum": ["1.0"]}:
        raise SystemExit("Installed wheel contains an incompatible orchestration result schema")
    if any("type" not in definition for definition in properties.values()):
        raise SystemExit("Installed wheel result schema contains an untyped property")

    with resources.assets_root() as assets:
        missing = resources.missing_required_assets(assets)
        if missing:
            raise SystemExit(f"Installed wheel is missing required assets: {missing}")
        required = (
            "workspace-template/docs/orchestration.md",
            "workspace-template/docs/orchestrator-handoff.md",
            "workspace-template/docs/run-controller.md",
            "workspace-template/scripts/_domain_pack_lifecycle.py",
            "workspace-template/skills/research-run.md",
            "workspace-template/skills/research-discover.md",
            "workspace-template/skills/research-acquire.md",
            "workspace-template/skills/research-verify.md",
            "orchestrator/skills/research-orchestrate.md",
        )
        absent = [relative for relative in required if not (assets / relative).is_file()]
        if absent:
            raise SystemExit(f"Installed wheel is missing orchestration assets: {absent}")
    print(json.dumps({"version": evidence_wiki.__version__, "installed_from": str(installed_path)}))
    '''
)


PUBLICATION_PROBE = textwrap.dedent(
    '''
    import importlib.util
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    import yaml
    import evidence_wiki
    from evidence_wiki import Workspace
    from evidence_wiki.errors import PublicationError, RevisionError

    cli, fixture_path, profile_path, target = map(Path, sys.argv[1:])
    assert Path(evidence_wiki.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    spec = importlib.util.spec_from_file_location("publication_fixture", fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    profile = yaml.safe_load(profile_path.read_text())
    profile["workspace_init"]["target_path"] = str(target)
    profile["workspace_init"]["questions"] = [{"id": "vendor-product-spec", "question": "What is the product spec?", "priority": "high"}]
    local_profile = target.parent / "publication-profile.yml"
    local_profile.write_text(yaml.safe_dump(profile))
    subprocess.run([str(cli), "init", "--profile", str(local_profile)], check=True, capture_output=True, text=True, timeout=60)
    fixture.write_ship_ready_vendor_fixture(target)
    question = target / "wiki/questions/vendor-product-spec.md"
    (question.parent / "awaiting-review.md").write_text(question.read_text().replace("status: answered", "status: human_review"))
    def contents():
        return {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    before = contents()
    command = [str(cli), "publication", "--target", str(target), "--format", "json", "--question"]
    with Workspace.open(target) as workspace:
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            try:
                workspace.publish_selected(["vendor-product-spec"])
            except RevisionError as error:
                assert error.error_code == "EVIDENCE_REVISION_UNSUPPORTED"
            else:
                raise AssertionError("unsupported capture was accepted")
            result = subprocess.run([*command, "vendor-product-spec"], capture_output=True, text=True, timeout=60)
            assert result.returncode == 2 and json.loads(result.stderr)["error_code"] == "EVIDENCE_REVISION_UNSUPPORTED"
            outcome = "unsupported-refusal-verified"
        else:
            document = workspace.publish_selected(["vendor-product-spec"])
            result = subprocess.run([*command, "vendor-product-spec"], capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stderr + result.stdout
            rendered = json.loads(result.stdout)
            assert document["verdict"] == "ship"
            for key in ("revision", "producer_id", "gate_scope", "question_slugs", "verdict"):
                assert document[key] == rendered[key], key
            assert document["export"]["questions"] == rendered["export"]["questions"]
            try:
                workspace.publish_selected(["absent"])
            except PublicationError as error:
                refusal = {key: getattr(error, key) for key in ("error_code", "message", "details", "recoverable", "remediation")}
            else:
                raise AssertionError("unknown question was accepted")
            result = subprocess.run([*command, "absent"], capture_output=True, text=True, timeout=60)
            rendered_refusal = json.loads(result.stderr)
            assert result.returncode == 2
            assert all(rendered_refusal.get(key, {}) == value for key, value in refusal.items())
            outcome = "passed"
    assert before == contents(), "publication changed the live workspace"
    print(json.dumps({"selected_publication": outcome}))
    '''
)


PACKET_PROBE = textwrap.dedent(
    '''
    import hashlib
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    import yaml
    from evidence_wiki import Workspace, contract

    cli, fixtures, root = map(Path, sys.argv[1:])
    profile = "qualified_context_packet/v1"
    record = {"id": "codebase:sample", "kind": "codebase_architecture", "raw_paths": [],
        "raw_fingerprint": "sha256:synthetic", "metadata": {}}
    folder = root / "sources/code_wikis/codebase--sample"
    folder.mkdir(parents=True)
    config = {"sources": {"manifest_path": "sources/manifest.jsonl"}, "integrations": {"codebase_analysis": {"intake_profile": profile}}}
    (root / "research.yml").write_text(yaml.safe_dump(config))
    (root / "sources/manifest.jsonl").write_text(json.dumps(record) + "\\n")
    normalized = root / "sources/normalized/codebase--sample.md"
    normalized.parent.mkdir()
    headings = ["Citation Metadata", "Abstract", "Outline", "Extracted Text", "Figures and Tables",
        "Links", "Raw Source Paths", "Parse Warnings"]
    body = "\\n".join("\\n## " + heading + "\\n\\n- None recorded.\\n" for heading in headings)

    def write_record(report, producer):
        frontmatter = {"type": "normalized_source", "normalized_format": 1, "source_id": record["id"],
            "source_kind": record["kind"], "status": "content_extracted", "evidence_usable": True,
            "created": "2026-09-10", "updated": "2026-09-10", "raw_paths": [],
            "manifest_path": "sources/manifest.jsonl", "raw_fingerprint": record["raw_fingerprint"],
            "normalizer": {"name": producer, "version": "1"}, "parse_warnings": []}
        if report is not None:
            frontmatter["qualified_context"] = report
        normalized.write_text("---\\n" + yaml.safe_dump(frontmatter) + "---\\n" + body)

    cases = []
    with Workspace.open(root) as workspace:
        assert workspace.normalize.profiles() == contract()["intake_profiles"]
        for fixture in sorted(fixtures.glob("*.json")):
            packet = fixture.read_bytes()
            manifest = {"schema_version": "1", "artifact_kind": "codebase_evidence", "source_id": record["id"],
                "intake_profile": profile, "packet_path": "packet.json", "generated_at": "2026-09-10T00:00:00Z",
                "producer": {"name": "agent-wiki-cli", "version": "1.8.0"},
                "invocation": {"executed_by": "external_worker", "argv": ["llm-wiki", "context"],
                    "plugins_enabled": False, "hooks_enabled": False, "network_access": False},
                "files": [{"path": "packet.json", "size_bytes": len(packet), "sha256": hashlib.sha256(packet).hexdigest()}]}
            (folder / "packet.json").write_bytes(packet)
            (folder / "artifact-manifest.json").write_text(json.dumps(manifest))
            report = workspace.normalize.validate_packet(record["id"])
            result = subprocess.run([str(cli), "normalize", "packet", "--target", str(root), "--source-id", record["id"]],
                capture_output=True, text=True, timeout=60)
            assert json.loads(result.stdout) == report, result.stderr
            if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
                assert not report["valid"] and report["reason"] == "delivery_capture_unsupported"
                assert result.returncode == 1
            else:
                assert report["valid"] and report["policy_satisfied"] and result.returncode == 0, report
                assert report["packet_id"] == json.loads(packet)["packet_id"]
                assert report["worker_authentication"] == "not_established"
                for producer in ["normalize_sources.py", "external-tool"]:
                    write_record(report, producer)
                    verification = workspace.normalize.verify()
                    assert verification["overall_result"] == "verified", verification
                    checked = subprocess.run([str(cli), "normalize", "verify", "--target", str(root), "--format", "json"],
                        capture_output=True, text=True, timeout=60)
                    assert checked.returncode == 0 and json.loads(checked.stdout)["overall_result"] == "verified", checked.stderr
                    write_record(None, producer)
                    assert workspace.normalize.verify()["overall_result"] == "not_verified"
                write_record(report, "external-tool")
            cases.append(fixture.stem)
        config["integrations"]["codebase_analysis"]["require_live_reconciliation"] = True
        (root / "research.yml").write_text(yaml.safe_dump(config))
        report = workspace.normalize.validate_packet(record["id"])
        assert not report.get("policy_satisfied")
    assert cases, "native packet corpus is absent"
    print(json.dumps({"qualified_packet_intake": cases, "required_live_policy": "refused"}))
    '''
)


def validate_installed(venv: Path, scratch: Path, expected_version: str | None, label: str) -> dict[str, object]:
    """Exercise one fresh installation from outside the checkout."""
    python = venv_python(venv)
    cli = venv_cli(venv)
    if not cli.is_file():
        raise ValidationError(f"{label}: the installed distribution did not provide the evidence-wiki entry point")
    outside = scratch / "outside-checkout"
    outside.mkdir()
    workspace = scratch / "provider-workspace"
    # Every command runs from a directory that is not the checkout, so a module
    # resolved from the source tree instead of the install would be a failure here.
    run([str(cli), "--version"], cwd=outside)
    contract_path = scratch / "contract.json"
    contract_path.write_text(run([str(cli), "contract"], cwd=outside), encoding="utf-8")
    run(
        [
            str(cli),
            "deploy",
            "--target",
            str(workspace),
            "--project-name",
            "provider-workspace",
            "--project-description",
            f"Installed {label} orchestration smoke",
            "--domain-pack",
            "general-science",
            "--discovery-provider",
            "arxiv",
            "--acquisition-provider",
            "arxiv",
        ],
        cwd=outside,
    )
    pack_refresh_path = scratch / "pack-refresh.json"
    pack_refresh_path.write_text(
        run(
            [str(cli), "pack", "refresh", "--target", str(workspace), "--path", "general-science", "--format", "json"],
            cwd=outside,
        ),
        encoding="utf-8",
    )
    session = ["--target", str(workspace), "--orchestration-id", "wheel-smoke", "--agent-id", "artifact-smoke"]
    run([str(cli), "orchestrate", "start", *session, "--format", "json"], cwd=outside)
    run([str(cli), "orchestrate", "next", *session, "--format", "json"], cwd=outside)
    status = json.loads(
        run(
            [str(cli), "orchestrate", "status", "--target", str(workspace), "--orchestration-id", "wheel-smoke", "--format", "json"],
            cwd=outside,
        )
    )
    session_document = status.get("session", status)
    if session_document.get("pending_action_id") != "action-0001":
        raise ValidationError(f"{label}: installed orchestration did not issue its first action: {session_document}")
    probe = run(
        [str(python), "-c", INSTALLED_PROBE, expected_version or "", str(pack_refresh_path)],
        cwd=outside,
    )
    probe_result = json.loads(probe.strip().splitlines()[-1])
    run([str(python), str(SMOKE_TOOL), "--cli", str(cli)], cwd=outside)
    publication = run([
        str(python), "-c", PUBLICATION_PROBE, str(cli),
        str(REPO_ROOT / "tests/_publication_fixture.py"),
        str(REPO_ROOT / "tests/fixtures/workspace-init-profile.yml"),
        str(scratch / "publication-workspace"),
    ], cwd=outside)
    packets = run([
        str(python), "-c", PACKET_PROBE, str(cli),
        str(REPO_ROOT / "tests/fixtures/codebase-intake/native-packets"),
        str(scratch / "packet-workspace"),
    ], cwd=outside)
    return {"label": label, **probe_result, "managed_smoke": "passed", **json.loads(publication), **json.loads(packets)}


def build_wheel_from_sdist(sdist: Path, scratch: Path) -> Path:
    """Unpack the sdist and build a wheel from it, away from the checkout."""
    unpack_root = scratch / "sdist-unpacked"
    unpack_root.mkdir()
    with tarfile.open(sdist, "r:gz") as archive:
        for member in archive.getmembers():
            target = (unpack_root / member.name).resolve()
            if unpack_root.resolve() not in target.parents:
                raise ValidationError(f"{sdist.name}: member escapes its root directory: {member.name}")
        archive.extractall(unpack_root)  # noqa: S202 - members were checked above.
    roots = [child for child in unpack_root.iterdir() if child.is_dir()]
    if len(roots) != 1:
        raise ValidationError(f"{sdist.name}: expected one project root, found {[root.name for root in roots]}")
    out_dir = scratch / "sdist-dist"
    run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(out_dir), str(roots[0])])
    wheels = sorted(out_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise ValidationError(f"{sdist.name}: building from the sdist produced {[path.name for path in wheels]}")
    return wheels[0]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist-dir", type=Path, default=REPO_ROOT / "dist", help="Directory holding one wheel and one sdist.")
    parser.add_argument("--expected-version", default=None, help="Version the installed package must report.")
    parser.add_argument("--skip-sdist", action="store_true", help="Validate only the wheel (not for release use).")
    parser.add_argument("--membership-only", action="store_true", help="Check archive contents without installing.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    wheel, sdist = find_artifacts(args.dist_dir.resolve())
    checks: dict[str, object] = {}
    summary: dict[str, object] = {
        "wheel": {"name": wheel.name, "sha256": sha256_of(wheel)},
        "sdist": {"name": sdist.name, "sha256": sha256_of(sdist)},
        "expected_version": args.expected_version,
        "checks": checks,
    }
    checks["membership"] = check_archive_membership(wheel, sdist)
    if not args.membership_only:
        with tempfile.TemporaryDirectory(prefix="evidence-wiki-artifacts-") as tmpdir:
            scratch = Path(tmpdir)
            wheel_scratch = scratch / "wheel"
            wheel_scratch.mkdir()
            checks["installed_wheel"] = validate_installed(
                create_venv_with_wheel(wheel_scratch, wheel), wheel_scratch, args.expected_version, "wheel"
            )
            if not args.skip_sdist:
                sdist_scratch = scratch / "sdist"
                sdist_scratch.mkdir()
                rebuilt = build_wheel_from_sdist(sdist, sdist_scratch)
                direct_members = [name for name in wheel_members(wheel) if not name.endswith("RECORD")]
                rebuilt_members = [name for name in wheel_members(rebuilt) if not name.endswith("RECORD")]
                if direct_members != rebuilt_members:
                    only_direct = sorted(set(direct_members) - set(rebuilt_members))
                    only_rebuilt = sorted(set(rebuilt_members) - set(direct_members))
                    raise ValidationError(
                        "the wheel built from the sdist does not match the direct wheel; "
                        f"only in direct: {only_direct[:10]}; only in sdist-built: {only_rebuilt[:10]}"
                    )
                checks["installed_sdist"] = validate_installed(
                    create_venv_with_wheel(sdist_scratch, rebuilt), sdist_scratch, args.expected_version, "sdist"
                )
                checks["installed_sdist"]["rebuilt_wheel_sha256"] = sha256_of(rebuilt)
            shutil.rmtree(scratch, ignore_errors=True)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
