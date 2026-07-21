from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPO_ROOT / "workspace-template"
SCRIPTS = TEMPLATE_ROOT / "scripts"
MINIMAL_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "minimal-project"
MATRIX_PATH = REPO_ROOT / "tests" / "fixtures" / "publication-security" / "matrix.yml"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INVENTORY = load_script_module("publication_security_inventory", "source_inventory.py")
NORMALIZE = load_script_module("publication_security_normalize", "normalize_sources.py")
INIT = load_script_module("publication_security_init", "init_research_workspace.py")
WORKSPACE_GC = load_script_module("publication_security_workspace_gc", "workspace_gc.py")
TRANSPORT = load_script_module("publication_security_transport", "_acquisition_transport.py")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def raw_snapshot(workspace: Path) -> dict[str, tuple[str, int]]:
    raw_root = workspace / "raw"
    return {
        path.relative_to(workspace).as_posix(): (sha256_file(path), stat.S_IMODE(path.stat().st_mode))
        for path in sorted(raw_root.rglob("*"))
        if path.is_file() and not path.is_symlink() and ".locks" not in path.relative_to(raw_root).parts
    }


def nested_keys(value) -> set[str]:
    if isinstance(value, dict):
        return {str(key) for key in value} | {
            nested
            for item in value.values()
            for nested in nested_keys(item)
        }
    if isinstance(value, list):
        return {nested for item in value for nested in nested_keys(item)}
    return set()


def capture_main(function, arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = function(arguments)
    return int(code or 0), stdout.getvalue(), stderr.getvalue()


@contextlib.contextmanager
def restrictive_umask():
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


class PublicationSecurityMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))

    def build_workspace(self, root: Path, *, hostile: bool = False) -> Path:
        workspace = root / "workspace"
        shutil.copytree(MINIMAL_FIXTURE, workspace)
        shutil.rmtree(workspace / "raw")
        shutil.rmtree(workspace / "sources")
        (workspace / "raw" / "evidence").mkdir(parents=True)
        (workspace / "sources" / "normalized").mkdir(parents=True)

        config = yaml.safe_load((workspace / "research.yml").read_text(encoding="utf-8"))
        config["raw"]["source_roots"] = ["raw/evidence"]
        config["sources"]["manifest_path"] = "sources/manifest.jsonl"
        config["sources"]["normalized_dir"] = "sources/normalized"
        config["integrations"] = {
            "codebase_analysis": {
                "enabled": True,
                "read_only": True,
                "output_dir": "sources/code_wikis",
            }
        }
        (workspace / "research.yml").write_text(
            yaml.safe_dump(config, sort_keys=False),
            encoding="utf-8",
        )

        if hostile:
            self.seed_hostile_payloads(workspace)
        else:
            (workspace / "raw" / "evidence" / "source.html").write_text(
                "<!doctype html><title>Security fixture</title><p>Inert evidence.</p>\n",
                encoding="utf-8",
            )
        return workspace

    def seed_hostile_payloads(self, workspace: Path) -> Path:
        sentinel = workspace.parent / "publication-security-command-must-not-run"
        for payload in self.matrix["payloads"]:
            path = workspace / payload["relative_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            content = payload["content"].replace("__SENTINEL__", str(sentinel))
            path.write_bytes(content.encode("utf-8"))

        latex_readme = workspace / "raw" / "evidence" / "hostile-latex" / "00README.json"
        latex_readme.write_text(
            json.dumps({"sources": [{"filename": "main.tex", "usage": "toplevel"}]}),
            encoding="utf-8",
        )
        repository = workspace / "raw" / "evidence" / "hostile-repository"
        (repository / "pyproject.toml").write_text(
            "[project]\nname = 'hostile-fixture'\nversion = '0.0.0'\n",
            encoding="utf-8",
        )
        return sentinel

    def assert_values_absent(self, label: str, payload: str | bytes, canaries: dict[str, str]) -> None:
        content = payload if isinstance(payload, bytes) else payload.encode("utf-8", errors="replace")
        for name, value in canaries.items():
            self.assertNotIn(value.encode(), content, f"{name} leaked in {label}")

    def assert_tree_has_no_values(self, root: Path, canaries: dict[str, str]) -> None:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                self.assert_values_absent(path.relative_to(root).as_posix(), path.read_bytes(), canaries)

    def assert_archives_have_no_values(self, root: Path, canaries: dict[str, str]) -> None:
        for artifact in sorted(root.iterdir()):
            if not artifact.is_file():
                continue
            self.assert_values_absent(artifact.name, artifact.read_bytes(), canaries)
            if artifact.suffix == ".whl":
                with zipfile.ZipFile(artifact) as archive:
                    for name in archive.namelist():
                        if not name.endswith("/"):
                            self.assert_values_absent(f"{artifact.name}:{name}", archive.read(name), canaries)
            elif artifact.name.endswith(".tar.gz"):
                with tarfile.open(artifact, "r:gz") as archive:
                    for member in archive.getmembers():
                        if not member.isfile():
                            continue
                        extracted = archive.extractfile(member)
                        self.assertIsNotNone(extracted)
                        with extracted:
                            self.assert_values_absent(
                                f"{artifact.name}:{member.name}",
                                extracted.read(),
                                canaries,
                            )

    def test_credential_canaries_are_absent_from_local_surfaces_and_built_artifacts(self):
        suffix = uuid.uuid4().hex
        canaries = {
            name: f"EVIDENCE-WIKI-CANARY-{index}-{suffix}"
            for index, name in enumerate(self.matrix["credential_canaries"]["environment_variables"], start=1)
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.build_workspace(root)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            surfaces: list[tuple[str, str]] = []

            with mock.patch.dict(os.environ, canaries, clear=False):
                inventory = capture_main(
                    INVENTORY.main,
                    ["--project-root", str(workspace), "--format", "json"],
                )
                normalize = capture_main(
                    NORMALIZE.main,
                    ["--project-root", str(workspace), "--all", "--append-log", "--format", "json"],
                )
                self.assertEqual(0, inventory[0], inventory[2])
                self.assertEqual(0, normalize[0], normalize[2])
                surfaces.extend(
                    [
                        ("inventory stdout", inventory[1]),
                        ("inventory stderr", inventory[2]),
                        ("normalization stdout", normalize[1]),
                        ("normalization stderr", normalize[2]),
                    ]
                )

                try:
                    INVENTORY.validate_workspace_relative_path("../outside", "sources.manifest_path")
                except SystemExit as exc:
                    surfaces.append(("failure exception", str(exc)))
                else:  # pragma: no cover - fail-closed assertion
                    self.fail("unsafe path unexpectedly passed validation")

                echoed = RuntimeError(
                    "provider echoed "
                    + " ".join(canaries.values())
                    + f" at https://example.org/source?api_key={canaries['OPENALEX_API_KEY']}"
                )
                surfaces.append(
                    (
                        "redacted exception",
                        TRANSPORT.redact_diagnostic(echoed, secrets=tuple(canaries.values())),
                    )
                )
                for name, value in canaries.items():
                    url = f"https://user:{value}@example.org/source?access_token={value}&label={name}"
                    surfaces.append((f"redacted URL for {name}", TRANSPORT.redact_url(url)))

                build = subprocess.run(  # noqa: S603 - fixed interpreter and structured build argv
                    [
                        sys.executable,
                        "-m",
                        "build",
                        "--no-isolation",
                        "--sdist",
                        "--wheel",
                        "--outdir",
                        str(artifacts),
                        str(REPO_ROOT),
                    ],
                    cwd=root,
                    env={**os.environ, "PYTHONHASHSEED": "0"},
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=180,
                )
                self.assertEqual(0, build.returncode, build.stdout + build.stderr)
                surfaces.extend([("build stdout", build.stdout), ("build stderr", build.stderr)])

            for label, surface in surfaces:
                self.assert_values_absent(label, surface, canaries)
            self.assert_tree_has_no_values(workspace, canaries)
            self.assert_archives_have_no_values(artifacts, canaries)

    def test_subprocess_paths_remain_structured_argv_elements(self):
        executable_variants = (
            "pdftotext with spaces",
            "pdftotext;touch forbidden",
            "pdftotext$(touch forbidden)",
            'pdftotext"quoted',
            "pdftotext\n--version",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            marker = root / "forbidden"
            option_like_pdf = root / "--help;$(touch forbidden)\n'quoted'.pdf"
            completed = subprocess.CompletedProcess([], 0, stdout="synthetic PDF text", stderr="")

            for executable in executable_variants:
                with self.subTest(executable=executable):
                    with mock.patch.object(NORMALIZE.subprocess, "run", return_value=completed) as runner:
                        text, warnings, ran = NORMALIZE.run_pdftotext(
                            executable,
                            option_like_pdf,
                            option_like_pdf.name,
                            layout=True,
                        )

                    self.assertTrue(ran)
                    self.assertEqual("synthetic PDF text", text)
                    self.assertEqual([], warnings)
                    argv = runner.call_args.args[0]
                    self.assertEqual(executable, argv[0])
                    self.assertEqual(str(option_like_pdf), argv[-2])
                    self.assertEqual("-", argv[-1])
                    self.assertIsInstance(argv, list)
                    self.assertIs(runner.call_args.kwargs.get("shell", False), False)
                    self.assertFalse(marker.exists())

    def test_malicious_formats_are_inert_data_and_cannot_self_promote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), hostile=True)
            sentinel = workspace.parent / "publication-security-command-must-not-run"
            before = raw_snapshot(workspace)
            completed = subprocess.CompletedProcess([], 0, stdout="Synthetic PDF parser output.\n", stderr="")

            inventory = capture_main(
                INVENTORY.main,
                ["--project-root", str(workspace), "--format", "json"],
            )
            self.assertEqual(0, inventory[0], inventory[2])
            with (
                mock.patch.object(NORMALIZE.shutil, "which", return_value="pdftotext"),
                mock.patch.object(NORMALIZE.subprocess, "run", return_value=completed) as runner,
            ):
                normalize = capture_main(
                    NORMALIZE.main,
                    [
                        "--project-root",
                        str(workspace),
                        "--all",
                        "--force",
                        "--pdf-extractor",
                        "poppler",
                        "--format",
                        "json",
                    ],
                )

            self.assertEqual(0, normalize[0], normalize[2])
            self.assertFalse(sentinel.exists())
            self.assertEqual(before, raw_snapshot(workspace))
            # One explicit Poppler version probe plus reading-order and layout
            # extraction passes.
            self.assertEqual(3, runner.call_count)
            for call in runner.call_args_list:
                self.assertIsInstance(call.args[0], list)
                self.assertIs(call.kwargs.get("shell", False), False)

            manifest_records = [
                json.loads(line)
                for line in (workspace / "sources" / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(manifest_records)
            self.assertTrue(all(record["status"] == "discovered" for record in manifest_records))
            for record in manifest_records:
                keys = nested_keys(record)
                self.assertNotIn("trust_tier", keys)
                self.assertNotIn("official_source", keys)
                self.assertNotIn("license", keys)

            normalized = sorted((workspace / "sources" / "normalized").glob("*.md"))
            self.assertTrue(normalized)
            for path in normalized:
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("---\n"), path)
                frontmatter = yaml.safe_load(text.split("---\n", 2)[1])
                self.assertNotIn("trust_tier", frontmatter)
                self.assertNotIn("official_source", frontmatter)
                self.assertNotIn("license", frontmatter)

    def test_read_only_raw_evidence_survives_workflow_cleanup_and_upgrade(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            raw_file = workspace / "raw" / "evidence" / "source.html"
            raw_file.chmod(0o400)
            before = raw_snapshot(workspace)
            try:
                inventory = capture_main(INVENTORY.main, ["--project-root", str(workspace)])
                normalize = capture_main(NORMALIZE.main, ["--project-root", str(workspace), "--all"])
                self.assertEqual(0, inventory[0], inventory[2])
                self.assertEqual(0, normalize[0], normalize[2])

                old_run = workspace / "runs" / "old-terminal-run"
                old_run.mkdir(parents=True)
                (old_run / "run-state.json").write_text(
                    json.dumps(
                        {
                            "updated_at": "2000-01-01T00:00:00Z",
                            "state": {"current": "complete"},
                        }
                    ),
                    encoding="utf-8",
                )
                report = WORKSPACE_GC.build_report(workspace, older_than_days=1, apply=True)
                self.assertEqual(1, report["counts"]["archived"])

                created, _updated = INIT.refresh_managed_path(
                    TEMPLATE_ROOT,
                    workspace,
                    "scripts",
                    False,
                )
                self.assertTrue(created)
                self.assertEqual(before, raw_snapshot(workspace))
            finally:
                raw_file.chmod(0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX umask semantics require a POSIX host")
    def test_restrictive_umask_keeps_generated_state_owner_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            old_run = workspace / "runs" / "old-terminal-run"
            old_run.mkdir(parents=True)
            (old_run / "run-state.json").write_text(
                json.dumps(
                    {
                        "updated_at": "2000-01-01T00:00:00Z",
                        "state": {"current": "complete"},
                    }
                ),
                encoding="utf-8",
            )

            with restrictive_umask():
                inventory = capture_main(INVENTORY.main, ["--project-root", str(workspace)])
                normalize = capture_main(NORMALIZE.main, ["--project-root", str(workspace), "--all"])
                report = WORKSPACE_GC.build_report(workspace, older_than_days=1, apply=True)
                INIT.refresh_managed_path(TEMPLATE_ROOT, workspace, "scripts", False)

            self.assertEqual(0, inventory[0], inventory[2])
            self.assertEqual(0, normalize[0], normalize[2])
            self.assertEqual(1, report["counts"]["archived"])
            generated = [
                workspace / "sources" / "manifest.jsonl",
                *sorted((workspace / "sources" / "normalized").glob("*.md")),
                workspace / "runs" / "archive" / "old-terminal-run.tar.gz",
                workspace / "scripts" / "source_inventory.py",
            ]
            self.assertTrue(all(path.is_file() for path in generated))
            for path in generated:
                mode = stat.S_IMODE(path.stat().st_mode)
                self.assertEqual(0, mode & 0o077, f"over-broad mode {mode:o}: {path}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
