"""Machine-output purity: under `--format json`, stdout carries exactly one JSON document.

A host drives this package by parsing stdout. If a script can interleave a progress
line, a warning, or an error object with its report, every embedder has to parse
defensively — "find the first `{` and hope" — and a stray line silently turns a
successful command into an unparseable failure. This suite makes the guarantee
testable per script rather than per reviewer.

Scripts are run as subprocesses on purpose. An in-process test would redirect Python's
own `sys.stdout` and miss the case this contract most needs to catch: a child process
inheriting the real stdout and writing to it.

Enrollment is automatic. Any script that declares `--format` is required to appear in
the case table below, so a new command cannot quietly skip the contract.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
#: The CR-5 fixture provider distribution, discoverable by putting its parent on the
#: child's import path. Registration is packaging metadata, so a subcommand that only
#: exists for registered providers is unreachable until something is registered.
PROVIDER_PLUGIN_ROOT = REPO_ROOT / "tests" / "fixtures" / "provider-plugins"

# Declares `--format` so callers can detect json mode, but is a shared helper rather
# than a command. Excluded explicitly so the enrollment guard stays honest.
NOT_A_COMMAND = {"_script_errors.py"}

DOCUMENT = "document"  # exactly one JSON document
JSONL = "jsonl"  # one JSON document per line, a documented stream contract
EMPTY = "empty"  # nothing on stdout; a fatal error belongs on stderr


@dataclass(frozen=True)
class Case:
    """One invocation and the stdout shape the contract requires of it."""

    argv: tuple[str, ...]
    stdout: str
    note: str = ""
    #: Put the CR-5 fixture provider distribution on the child's import path. Off by
    #: default so every pre-existing case runs in exactly the environment it always did.
    plugins_installed: bool = False


def enrolled_scripts() -> set[str]:
    """Every script that accepts `--format`, which is every script this contract binds."""
    return {
        path.name
        for path in SCRIPTS.glob("*.py")
        if '"--format"' in path.read_text(encoding="utf-8") and path.name not in NOT_A_COMMAND
    }


# Invocations are written out in full — including how each script is pointed at the
# workspace — so a case says exactly what it runs. `--format` placement matters for
# scripts whose sub-parsers do not accept it, so that is explicit too.
ROOT = ("--project-root", "{ws}")
WORKSPACE_CASES: dict[str, tuple[Case, ...]] = {
    "coverage_manifest.py": (Case((*ROOT, "validate", "--slug", "{slug}", "--format", "json"), DOCUMENT),),
    "discover_sources.py": (
        Case((*ROOT, "--format", "json", "candidates", "list"), DOCUMENT),
        Case(
            ("--project-root", "{provider_ws}", "--format", "json", "registered", "search",
             "--id", "keepa-search-fixture", "--request-file", "{registered_search_request}"),
            EMPTY,
            "registered discovery: the deepest path that reaches no network is the "
            "provider's own refusal of the request document",
            plugins_installed=True,
        ),
    ),
    "doctor.py": (Case((*ROOT, "--format", "json"), DOCUMENT),),
    "export_answers.py": (Case((*ROOT, "--format", "json"), DOCUMENT),),
    "fetch_sources.py": (
        Case(
            (*ROOT, "--format", "json", "arxiv", "search", "--query", "probe", "--max-results", "1"),
            EMPTY,
            "acquisition is disabled by default, so the reachable path is the refusal envelope",
        ),
        Case(
            ("--project-root", "{provider_ws}", "--format", "json", "registered", "get",
             "--id", "keepa-fixture", "--request-file", "requests/keepa.json"),
            EMPTY,
            "registered acquisition: the provider refuses the request document, which is as "
            "far as this command goes without a network",
            plugins_installed=True,
        ),
    ),
    "fleet_status.py": (Case(("--target", "{ws}", "--format", "json"), DOCUMENT),),
    "intake_questions.py": (Case((*ROOT, "--from-file", "{batch}", "--format", "json"), DOCUMENT),),
    "lint.py": (Case((*ROOT, "--format", "json"), DOCUMENT),),
    "normalize_sources.py": (
        Case((*ROOT, "--all", "--format", "json"), DOCUMENT),
        Case((*ROOT, "--all", "--dry-run", "--format", "json"), DOCUMENT),
    ),
    "normalize_verify.py": (Case((*ROOT, "--format", "json"), DOCUMENT),),
    "orchestration_controller.py": (
        Case((*ROOT, "status", "--orchestration-id", "{orchestration}", "--format", "json"), DOCUMENT),
    ),
    "publication_readiness.py": (Case((*ROOT, "--format", "json"), DOCUMENT),),
    "query_index.py": (Case((*ROOT, "retrieval", "--format", "json"), DOCUMENT),),
    "question_claim.py": (
        Case((*ROOT, "claim", "--slug", "{slug}", "--agent-id", "purity", "--format", "json"), DOCUMENT),
    ),
    "question_resolve.py": (
        Case(
            (*ROOT, "defer", "--slug", "{slug}", "--agent-id", "purity", "--reason", "probe",
             "--allow-unclaimed", "--format", "json"),
            DOCUMENT,
        ),
        Case(
            (*ROOT, "grounding", "set", "--slug", "{grounding_slug}", "--from-file", "{grounding_file}",
             "--agent-id", "purity", "--allow-unclaimed", "--format", "json"),
            DOCUMENT,
            "the one nested subcommand: it rewrites frontmatter and appends to log.md, and still emits one document",
        ),
    ),
    "question_status.py": (Case((*ROOT, "--format", "json"), DOCUMENT),),
    "run_controller.py": (Case((*ROOT, "status", "--run-id", "{run}", "--format", "json"), DOCUMENT),),
    "run_report.py": (Case((*ROOT, "--run-id", "{run}", "--format", "json"), DOCUMENT),),
    "smoke_validate_workspace.py": (Case((*ROOT, "--format", "json"), DOCUMENT),),
    "source_inventory.py": (
        Case((*ROOT, "--report", "--format", "json"), DOCUMENT),
        Case(
            (*ROOT, "--dry-run", "--format", "json"),
            JSONL,
            "documented in docs/source-manifest.md: --dry-run without --report keeps the JSONL stream contract",
        ),
    ),
    "source_requests.py": (
        Case((*ROOT, "list", "--format", "json"), DOCUMENT),
        Case(
            (*ROOT, "record-attempt-failure", "--request-id", "{request}", "--failure-code", "no_result",
             "--orchestration-id", "{orchestration}", "--action-id", "purity-action", "--format", "json"),
            DOCUMENT,
            "a mutating command that also appends to log.md must still emit one document",
        ),
    ),
    "verify_citations.py": (Case((*ROOT, "--format", "json"), DOCUMENT),),
    "verify_quotes.py": (Case((*ROOT, "--slug", "{slug}", "--format", "json"), DOCUMENT),),
    "workspace_gc.py": (Case((*ROOT, "--format", "json"), DOCUMENT),),
    "workspace_status.py": (Case((*ROOT, "--format", "json"), DOCUMENT),),
}

# The same commands against a directory that is not a workspace. Most refuse with the
# shared envelope; the reporters below describe the broken workspace instead, which is
# their job and still exactly one document.
REPORTS_ON_A_BROKEN_WORKSPACE = {
    "doctor.py",
    "lint.py",
    "publication_readiness.py",
    "smoke_validate_workspace.py",
    "workspace_gc.py",
    "workspace_status.py",
    "fleet_status.py",
}


class JsonStdoutPurityTests(unittest.TestCase):
    workspace: Path
    not_a_workspace: Path
    provider_workspace: Path
    tmp: tempfile.TemporaryDirectory
    substitutions: dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.workspace = root / "workspace"
        cls.not_a_workspace = root / "not-a-workspace"
        cls.not_a_workspace.mkdir()
        cls.provider_workspace = root / "provider-workspace"

        # A fully initialized workspace, not the minimal fixture: several commands only
        # reach a success path when the workspace has the structure init produces, and a
        # command that errors out tests far less of this contract than one that reports.
        cls._run_setup(
            "init_research_workspace.py",
            [
                "--target", str(cls.workspace),
                "--project-name", "stdout-purity",
                "--project-description", "Machine-output purity conformance workspace.",
            ],
        )

        slug = "purity-probe"
        (cls.workspace / "wiki" / "questions").mkdir(parents=True, exist_ok=True)
        (cls.workspace / "wiki" / "questions" / f"{slug}.md").write_text(
            "---\ntype: question\nslug: purity-probe\nstatus: open\n"
            "created: 2026-08-08\nupdated: 2026-08-08\n---\n\n# Purity probe\n",
            encoding="utf-8",
        )
        (cls.workspace / "raw" / "links").mkdir(parents=True, exist_ok=True)
        (cls.workspace / "raw" / "links" / "probe.txt").write_text(
            "https://example.org/stdout-purity\n", encoding="utf-8"
        )
        batch = root / "batch.yaml"
        batch.write_text(
            'schema_version: "1.0"\nquestions:\n  - question: Does stdout stay pure?\n    priority: high\n',
            encoding="utf-8",
        )

        run_id = "run-purity"
        project = ["--project-root", str(cls.workspace)]
        cls._run_setup("source_inventory.py", [*project, "--report"])
        cls._run_setup("coverage_manifest.py", [*project, "init", "--slug", slug])
        cls._run_setup("run_controller.py", [*project, "start", "--run-id", run_id, "--agent-id", "purity"])
        cls._run_setup("orchestration_controller.py", [*project, "start", "--agent-id", "purity"])
        orchestrations = sorted((cls.workspace / "runs" / "orchestrations").glob("orch-*"))
        assert orchestrations, "setup could not create an orchestration session to report on"

        cls._run_setup(
            "source_requests.py",
            [*project, "add", "--kind", "other", "--query-or-identifier", "purity probe",
             "--rationale", "Gives the attempt-audit command an open request to record against."],
        )
        requests_file = cls.workspace / "sources" / "source-requests.jsonl"
        request_id = json.loads(requests_file.read_text(encoding="utf-8").splitlines()[0])["request_id"]

        # `grounding set` writes onto its own question: the case above it resolves {slug} to a
        # terminal status, which this command correctly refuses. The cited source id is read
        # from the inventoried manifest rather than guessed, since it carries a content hash.
        grounding_slug = "purity-grounding"
        (cls.workspace / "wiki" / "questions" / f"{grounding_slug}.md").write_text(
            "---\ntype: question\nslug: purity-grounding\nstatus: open\n"
            "created: 2026-08-08\nupdated: 2026-08-08\n---\n\n# Purity grounding probe\n",
            encoding="utf-8",
        )
        manifest = cls.workspace / "sources" / "manifest.jsonl"
        source_id = json.loads(manifest.read_text(encoding="utf-8").splitlines()[0])["id"]
        grounding_file = root / "grounding.yml"
        grounding_file.write_text(
            "grounding:\n"
            '  - claim: "The probe link was inventoried."\n'
            f'    source_id: "{source_id}"\n'
            '    quote: "Machine-output purity conformance workspace."\n',
            encoding="utf-8",
        )

        # A second workspace, for the two subcommands that only exist for registered
        # providers. It is separate on purpose: reaching them means enabling acquisition
        # and discovery and authorizing provider ids, and doing that to the shared
        # workspace would change what every other case above is running against.
        registered_search_request = cls._build_provider_workspace()

        cls.substitutions = {
            "{slug}": slug,
            "{batch}": str(batch),
            "{run}": run_id,
            "{orchestration}": orchestrations[0].name,
            "{request}": request_id,
            "{grounding_slug}": grounding_slug,
            "{grounding_file}": str(grounding_file),
            "{provider_ws}": str(cls.provider_workspace),
            "{registered_search_request}": str(registered_search_request),
        }

    @classmethod
    def _build_provider_workspace(cls) -> Path:
        """Authorize both fixture providers, and hand each one a request it will refuse.

        A refused request document is the deepest point these commands reach without a
        network: registration is resolved, the id is authorized, the provider is loaded
        and asked, and the refusal is rendered -- all of it new code that must not print
        a word to stdout.
        """
        cls._run_setup(
            "init_research_workspace.py",
            [
                "--target", str(cls.provider_workspace),
                "--project-name", "stdout-purity-registered",
                "--project-description", "Machine-output purity for registered providers.",
            ],
        )
        config_path = cls.provider_workspace / "research.yml"
        config = config_path.read_text(encoding="utf-8")
        config = config.replace(
            "  acquisition:\n    enabled: false\n    providers: []\n",
            "  acquisition:\n    enabled: true\n    providers:\n      - keepa-fixture\n",
        ).replace(
            "  discovery:\n    enabled: false\n    providers: []\n",
            "  discovery:\n    enabled: true\n    providers:\n      - keepa-search-fixture\n",
        )
        assert "keepa-fixture" in config and "keepa-search-fixture" in config, (
            "the research.yml integrations block no longer matches what this setup edits"
        )
        config_path.write_text(config, encoding="utf-8")

        acquisition_request = cls.provider_workspace / "requests" / "keepa.json"
        acquisition_request.parent.mkdir(parents=True, exist_ok=True)
        # 'asin' must be a ten-character product id, so the provider refuses this one.
        acquisition_request.write_text('{"asin": "not-an-asin"}\n', encoding="utf-8")
        search_request = cls.provider_workspace / "requests" / "search.json"
        search_request.write_text('{"query": ""}\n', encoding="utf-8")
        return search_request

    @classmethod
    def _run_setup(cls, script: str, argv: list[str]) -> None:
        subprocess.run(
            [sys.executable, str(SCRIPTS / script), *argv],
            capture_output=True,
            text=True,
            check=False,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def resolve(self, argv: tuple[str, ...], project_root: Path) -> list[str]:
        resolved: list[str] = []
        for token in argv:
            value = token.replace("{ws}", str(project_root))
            for key, replacement in self.substitutions.items():
                value = value.replace(key, replacement)
            resolved.append(value)
        return resolved

    def child_environment(self, case: Case) -> dict[str, str] | None:
        if not case.plugins_installed:
            return None
        environment = dict(os.environ)
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(PROVIDER_PLUGIN_ROOT) if not existing else f"{PROVIDER_PLUGIN_ROOT}{os.pathsep}{existing}"
        )
        return environment

    def run_script(self, script: str, case: Case, project_root: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *self.resolve(case.argv, project_root)],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(project_root),
            env=self.child_environment(case),
        )

    # -- assertions --------------------------------------------------------------

    def assert_single_document(self, stdout: str, context: str) -> None:
        text = stdout.strip()
        self.assertTrue(text, f"{context}: expected one JSON document, got empty stdout")
        decoder = json.JSONDecoder()
        try:
            payload, consumed = decoder.raw_decode(text)
        except json.JSONDecodeError as exc:
            self.fail(f"{context}: stdout is not JSON ({exc}); first 200 chars: {text[:200]!r}")
        # Full consumption, not "find the first `{`": anything after the document —
        # a log line, a second object — is the failure this contract exists to prevent.
        self.assertEqual(
            len(text),
            consumed,
            f"{context}: stdout carried more than one document; trailing: {text[consumed:consumed + 200]!r}",
        )
        self.assertIsInstance(payload, (dict, list), f"{context}: expected a JSON object or array")

    def assert_jsonl(self, stdout: str, context: str) -> None:
        lines = [line for line in stdout.splitlines() if line.strip()]
        self.assertTrue(lines, f"{context}: expected a JSONL stream, got empty stdout")
        for number, line in enumerate(lines, start=1):
            with self.subTest(line=number):
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    self.fail(f"{context}: line {number} is not JSON ({exc}): {line[:200]!r}")

    def assert_error_envelope(self, stderr: str, context: str) -> None:
        text = stderr.strip()
        self.assertTrue(text, f"{context}: a failure must explain itself on stderr")
        decoder = json.JSONDecoder()
        try:
            envelope, consumed = decoder.raw_decode(text)
        except json.JSONDecodeError as exc:
            self.fail(f"{context}: stderr is not the error envelope ({exc}): {text[:200]!r}")
        self.assertEqual(len(text), consumed, f"{context}: stderr carried more than the envelope")
        self.assertIn("error_code", envelope, f"{context}: envelope has no error_code")
        self.assertIn("message", envelope, f"{context}: envelope has no message")

    def check(self, script: str, case: Case, project_root: Path, label: str) -> None:
        result = self.run_script(script, case, project_root)
        context = f"{script} [{label}] exit={result.returncode}"
        if case.note:
            context += f" ({case.note})"
        if case.stdout == DOCUMENT:
            self.assert_single_document(result.stdout, context)
        elif case.stdout == JSONL:
            self.assert_jsonl(result.stdout, context)
        else:
            self.assertEqual("", result.stdout.strip(), f"{context}: stdout must be empty")
            self.assert_error_envelope(result.stderr, context)

    # -- the contract ------------------------------------------------------------

    def test_every_format_script_is_enrolled(self):
        """A new `--format` script cannot skip the contract by not being listed."""
        self.assertEqual(
            enrolled_scripts(),
            set(WORKSPACE_CASES),
            "scripts accepting --format must appear in WORKSPACE_CASES (or NOT_A_COMMAND with a reason)",
        )

    def test_stdout_carries_one_document_in_a_real_workspace(self):
        for script, cases in sorted(WORKSPACE_CASES.items()):
            for index, case in enumerate(cases):
                with self.subTest(script=script, case=index):
                    self.check(script, case, self.workspace, "workspace")

    def test_stdout_stays_pure_when_the_workspace_is_unreadable(self):
        for script, cases in sorted(WORKSPACE_CASES.items()):
            case = cases[0]
            expected = DOCUMENT if script in REPORTS_ON_A_BROKEN_WORKSPACE else EMPTY
            with self.subTest(script=script):
                self.check(
                    script, dataclasses.replace(case, stdout=expected), self.not_a_workspace, "no workspace"
                )


if __name__ == "__main__":
    unittest.main()
