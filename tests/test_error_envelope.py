import ast
import contextlib
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
HANDOFF_DOC = REPO_ROOT / "workspace-template" / "docs" / "orchestrator-handoff.md"
PROFILE_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "workspace-init-profile.yml"
HELPER_PATH = SCRIPTS / "_script_errors.py"
ERROR_HELPER_CALLS = {"handle_system_exit", "emit_error", "emit_refusal", "error_envelope"}

# Scripts whose envelope codes need no JSON Output Scripts row, each for a stated reason.
# An exemption belongs here — visible and arguable — rather than in a hand-kept inventory
# of what to check, which is how `orchestration_controller.py` (36 codes),
# `coverage_manifest.py` and `publication_readiness.py` sat outside this file's checks
# without anyone deciding they should.
JSON_MODE_DOC_EXEMPT = {
    # Raise no envelope code of their own: the table lists the codes a host must handle
    # per script, and an empty row states nothing. They still refuse through the shared
    # helper, which `test_documented_json_mode_scripts_use_shared_error_helper` covers.
    "init_research_workspace.py": "raises no envelope code of its own",
    "serve_mcp.py": "raises no envelope code of its own",
    "workspace_gc.py": "raises no envelope code of its own",
    # TODO(CR-14): both need a JSON Output Scripts row, and verify_quotes.py additionally
    # needs GROUNDING_INVALID and GROUNDING_VERIFIER_REQUIRED added to the Stable error
    # codes table. Deferred only to avoid editing orchestrator-handoff.md while parallel
    # CR-14 units are writing to it; tracked in docs/CR/CR-14-backlog.md.
    "normalize_verify.py": "CR-14 follow-up: JSON Output Scripts row pending",
    "verify_quotes.py": "CR-14 follow-up: row + 2 stable-code rows pending",
}


def json_mode_scripts() -> list[str]:
    """Every script that must appear in the JSON Output Scripts table, derived not listed.

    A script qualifies when it imports the shared error helper *and* calls it — the same
    two predicates `test_documented_json_mode_scripts_use_shared_error_helper` already
    applies in the other direction. Deriving it is the point: the previous hardcoded list
    of 18 silently omitted 8 qualifying scripts, so the largest error surface in the
    package (`orchestration_controller.py`) was exempt from these checks by an omission
    nobody had to justify. A new script now joins the checks by existing.
    """
    scripts = []
    for path in sorted(SCRIPTS.glob("*.py")):
        if path.name.startswith("_") or path.name in JSON_MODE_DOC_EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if script_imports_error_helper(tree) and script_calls_error_helper(tree):
            scripts.append(path.name)
    return scripts


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_helper():
    if not HELPER_PATH.is_file():
        raise AssertionError("workspace-template/scripts/_script_errors.py is missing")
    return load_script_module("research_script_errors", "_script_errors.py")


ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{4,}$")
# Codes whose retry verdict legitimately differs between raise sites, each with a reason.
# Empty on purpose: CR-15a found no condition that survived the "then the code is doing
# two jobs" test. An entry here is a claim that one code covers two materially different
# conditions and should be argued for, not a place to silence the guard.
RECOVERABILITY_VARIES_BY_SITE: dict[str, str] = {
    # One code, two genuinely different conditions — and the retry verdict differs with
    # them. Seven sites mean "the workspace is unreadable, changed under us, oversized, or
    # not a regular file": fix it and retry. One (orchestration_controller.py, the
    # post-issue check) means "health or HIGH findings changed *after the work order was
    # issued*", where retry is pointless and the session must be replaced — semantically a
    # sibling of ORCHESTRATION_DELEGATION_CHANGED / _PROVIDER_POLICY_CHANGED /
    # _INTEGRITY_BASELINE_CHANGED, wearing this code instead of its own.
    #
    # Forcing one answer would be wrong either way: True tells a caller to retry a
    # baseline-moved refusal, False tells it not to retry a fixable workspace. The honest
    # fix is a new code in the *_CHANGED family, which is a contract change tracked as
    # CR-15d. Recorded here so the split is argued rather than dispersed across sites.
    "ORCHESTRATION_WORKSPACE_UNSAFE": "post-issue baseline change vs fixable workspace state; split tracked in CR-15d",
}
# Keywords through which a code reaches an error constructor without being its first
# positional argument. Each was found the hard way: a code invisible to a positional-only
# scan, already shipped with no remediation because nothing counted it.
ERROR_CODE_KEYWORDS = {"error_code", "status_error_code", "not_found_code", "code"}


def _code_strings(node: ast.expr, consts: dict[str, str]) -> list[str]:
    """Every code-shaped string this expression can evaluate to, statically.

    Handles the four shapes that occur in this package: a literal, a module constant, a
    conditional (``A if cond else B`` — how ``require_safe_id`` picks between two codes),
    and a boolean fallback. A code built at runtime is out of reach and is declared as
    such in the caller's docstring rather than silently dropped.
    """
    values: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        values = [node.value]
    elif isinstance(node, ast.Name):
        resolved = consts.get(node.id)
        values = [resolved] if isinstance(resolved, str) else []
    elif isinstance(node, ast.IfExp):
        values = _code_strings(node.body, consts) + _code_strings(node.orelse, consts)
    elif isinstance(node, ast.BoolOp):
        values = [s for value in node.values for s in _code_strings(value, consts)]
    return [value for value in values if ERROR_CODE_RE.match(value)]


def collect_raised_error_codes() -> dict[str, set[str]]:
    """Map every statically-discoverable error code to the scripts that raise it."""
    owners: dict[str, set[str]] = {}
    for path in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        consts = {
            node.targets[0].id: node.value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and node.targets
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        # Anything *raised* with a code-shaped first argument is an error constructor,
        # whatever it is called. Matching on the name alone missed `LifecycleFailure`
        # (6 codes), `DomainPackInitFailure` (2) and the `registered_error` factory (3) —
        # a filter built from the names that happened to be in front of its author.
        raised_calls = {
            id(node.exc)
            for node in ast.walk(tree)
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            found: list[str] = []
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None) or ""
            constructs_error = (
                "Error" in name or "Refusal" in name or "Failure" in name or id(node) in raised_calls
            )
            if constructs_error:
                for argument in node.args[:2]:
                    found += _code_strings(argument, consts)
                    if found:
                        break
            # Code-carrying keywords are read from *every* call, not only from error
            # constructors. `STATUS_NOT_REVIEWABLE` is handed to `record_human_reviews`
            # as `status_error_code=` and raised inside it, so a sweep restricted to
            # constructor calls never sees it — the blind spot that let it ship with no
            # remediation. A keyword named `*error_code` carrying a code-shaped string is
            # an error code wherever it appears.
            for keyword in node.keywords:
                if keyword.arg in ERROR_CODE_KEYWORDS:
                    found += _code_strings(keyword.value, consts)
            for code in found:
                owners.setdefault(code, set()).add(path.name)
    return owners


def collect_recoverability_by_site() -> dict[str, dict[bool, list[str]]]:
    """Map each error code to the recoverability its raise sites resolve to.

    Returns ``{code: {resolved_bool: ["file.py:line", ...]}}``. A site that passes no
    ``recoverable=`` is resolved through ``default_recoverable`` exactly as
    ``emit_refusal`` would, because silence is an answer: every code but the two claim
    codes defaults to recoverable, so an omitted flag says "retry is meaningful" just as
    loudly as ``recoverable=True``. Comparing literal keyword values instead would call a
    ``False``/omitted split consistent when the envelope reports two different things.
    """
    helper = load_helper()
    sites: dict[str, dict[bool, list[str]]] = {}
    for path in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        raised = {
            id(node.exc)
            for node in ast.walk(tree)
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None) or ""
            if not ("Error" in name or "Refusal" in name or "Failure" in name or id(node) in raised):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            code = first.value
            if not ERROR_CODE_RE.match(code):
                continue
            declared = None
            for keyword in node.keywords:
                if keyword.arg == "recoverable" and isinstance(keyword.value, ast.Constant):
                    declared = keyword.value.value
            resolved = helper.default_recoverable(code) if declared is None else bool(declared)
            sites.setdefault(code, {}).setdefault(resolved, []).append(f"{path.name}:{node.lineno}")
    return sites


def cli_flag_universe() -> set[str]:
    """Every ``--flag`` this package defines, across workspace scripts and the package CLI.

    Both halves are needed: a remediation may legitimately name a flag of the packaged
    `evidence-wiki` CLI (``--keep-local``, ``--acknowledge-control-repair``) rather than of
    a workspace script. Auditing against workspace scripts alone reports those as unknown,
    which is how a first pass at this check produced four false positives.
    """
    flags: set[str] = set()
    roots = [SCRIPTS.glob("*.py"), (REPO_ROOT / "src" / "evidence_wiki").rglob("*.py")]
    for paths in roots:
        for path in paths:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:  # pragma: no cover - a syntactically broken source fails elsewhere
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if re.fullmatch(r"--[a-z][a-z0-9-]+", node.value):
                        flags.add(node.value)
    return flags


def script_subcommands() -> dict[str, set[str]]:
    """Each workspace script's ``add_parser`` subcommand names."""
    result: dict[str, set[str]] = {}
    for path in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "add_parser"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        }
        if names:
            result[path.name] = names
    return result


def all_remediation_texts() -> list[tuple[str, str]]:
    """Every remediation an operator can see: the registry, plus each inline override.

    Inline text is included because it is what the operator actually reads — a raise site
    passing ``remediation=`` overrides the registry entirely, so auditing only the registry
    would check the fallback and skip the message.
    """
    helper = load_helper()
    texts: list[tuple[str, str]] = [(code, text) for code, text in helper._REMEDIATIONS.items()]
    for path in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # Resolve module constants, exactly as `collect_raised_error_codes` does. Requiring
        # a string literal here read only 10 of the script files and skipped
        # `_provider_accounting.py` entirely, because that module names its codes through
        # `ERROR_LEDGER_INVALID`-style constants — so its remediations, prohibitions
        # included, were invisible to every check built on this function. Two collectors in
        # one file disagreeing about what counts as a code is the drift these guards exist
        # to catch, one level up.
        consts = {
            node.targets[0].id: node.value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and node.targets
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            codes = _code_strings(node.args[0], consts)
            if not codes:
                continue
            for keyword in node.keywords:
                if keyword.arg != "remediation":
                    continue
                # The *value* is resolved through the same constant map as the code.
                # `_provider_accounting.py` passes `remediation=ACCOUNTING_REMEDIATION`, so
                # reading only literals here left its text unread even after the code side
                # was fixed — the same omission twice, one argument apart.
                if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                    text = keyword.value.value
                elif isinstance(keyword.value, ast.Name):
                    text = consts.get(keyword.value.id)
                else:
                    text = None
                if not isinstance(text, str):
                    continue
                for code in codes:
                    texts.append((f"{code} ({path.name}:{keyword.value.lineno})", text))
    return texts


def documented_error_code_rows() -> set[str]:
    """Every error code carrying a row in any shipped documentation table."""
    codes: set[str] = set()
    roots = [REPO_ROOT / "workspace-template" / "docs", REPO_ROOT / "docs"]
    for root in roots:
        for path in sorted(root.rglob("*.md")):
            codes |= set(
                re.findall(r"^\|\s*`([A-Z][A-Z0-9_]{4,})`", path.read_text(encoding="utf-8"), re.M)
            )
    return codes


def markdown_row_cells(line: str) -> list[str]:
    """Split one Markdown table row into cells, honouring `\\|` as escaped content.

    Splitting on a bare `|` silently truncates every row whose cell text contains an
    escaped pipe — and rows here do: the JSON-mode column spells subcommand lists as
    `next\\|submit\\|...`. That put a fragment of the *wrong column* in `cells[2]` for
    `orchestration_controller.py`, `coverage_manifest.py` and `question_resolve.py`, so
    the codes those rows advertise were never compared against the stable-codes table.
    The table's largest error surface was unchecked by the check that exists to cover it,
    which is why nine missing rows for that module went unnoticed until a reviewer read
    them by hand.
    """
    cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [cell.strip().replace("\\|", "|") for cell in cells]


def documented_json_output_script_rows() -> list[tuple[str, str]]:
    text = HANDOFF_DOC.read_text(encoding="utf-8")
    marker = "#### JSON Output Scripts"
    if marker not in text:
        raise AssertionError("orchestrator-handoff.md must include a '#### JSON Output Scripts' table")
    section = text.split(marker, 1)[1].split("\n### ", 1)[0]
    rows: list[tuple[str, str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = markdown_row_cells(stripped)
        if not cells or cells[0].lower() == "script":
            continue
        match = re.search(r"`(?:scripts/)?([\w_]+\.py)`", cells[0])
        if match:
            rows.append((match.group(1), cells[2] if len(cells) > 2 else ""))
    if not rows:
        raise AssertionError("JSON Output Scripts table must list at least one workspace script")
    return rows


def documented_json_output_scripts() -> list[str]:
    return [script for script, _codes in documented_json_output_script_rows()]


def documented_json_output_error_codes() -> set[str]:
    codes: set[str] = set()
    for _script, code_cell in documented_json_output_script_rows():
        codes.update(re.findall(r"`([A-Z][A-Z0-9_]+)`", code_cell))
    if not codes:
        raise AssertionError("JSON Output Scripts table must list fatal error codes")
    return codes


def documented_stable_error_codes() -> set[str]:
    text = HANDOFF_DOC.read_text(encoding="utf-8")
    marker = "Stable error codes:"
    if marker not in text:
        raise AssertionError("orchestrator-handoff.md must document stable error codes")
    section = text.split(marker, 1)[1].split("\n## ", 1)[0]
    codes: set[str] = set()
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = markdown_row_cells(stripped)
        if not cells or cells[0].lower() == "code":
            continue
        match = re.fullmatch(r"`([A-Z][A-Z0-9_]+)`", cells[0])
        if match:
            codes.add(match.group(1))
    if not codes:
        raise AssertionError("Stable error codes table must list at least one code")
    return codes


def script_imports_error_helper(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "_script_errors":
            return True
        if isinstance(node, ast.Import):
            if any(alias.name == "_script_errors" for alias in node.names):
                return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "load_workspace_module":
            if any(isinstance(argument, ast.Constant) and argument.value == "_script_errors" for argument in node.args):
                return True
    return False


def script_calls_error_helper(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name) and function.id in ERROR_HELPER_CALLS:
            return True
        if isinstance(function, ast.Attribute) and function.attr in ERROR_HELPER_CALLS:
            return True
    return False


class ErrorEnvelopeTests(unittest.TestCase):
    def init_workspace(self, root: Path) -> Path:
        init = load_script_module("error_envelope_init", "init_research_workspace.py")
        target = root / "claim-workspace"
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text())
        profile["workspace_init"]["target_path"] = str(target)
        profile["workspace_init"]["questions"] = [
            {"id": "which-benchmarks", "question": "Which benchmarks matter?", "priority": "high"}
        ]
        profile_path = root / "profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))
        with contextlib.redirect_stdout(io.StringIO()):
            init.main(["--profile", str(profile_path)])
        return target

    def run_module(self, module, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def test_helper_builds_contract_shape(self):
        helper = load_helper()

        envelope = helper.error_envelope(
            "CONFIG_MISSING",
            "Missing config: /workspace/research.yml",
            recoverable=True,
            remediation="Run from an initialized workspace or pass --project-root to one.",
        )

        self.assertEqual(
            {
                "schema_version": "1.0",
                "error_code": "CONFIG_MISSING",
                "message": "Missing config: /workspace/research.yml",
                "recoverable": True,
                "remediation": "Run from an initialized workspace or pass --project-root to one.",
            },
            envelope,
        )

    def test_refusal_envelope_is_the_shared_envelope(self):
        """A raised refusal and a printed envelope are the same bytes because they share one builder."""
        helper = load_helper()

        refusal = helper.ScriptRefusal(
            "CLAIM_HELD",
            "Question which-benchmarks is claimed by agent-a.",
            exit_code=3,
            recoverable=False,
            remediation="Use claim --steal --if-older-than HOURS.",
            details={"slug": "which-benchmarks"},
        )

        self.assertEqual(3, refusal.exit_code)
        self.assertEqual("Question which-benchmarks is claimed by agent-a.", refusal.message)
        self.assertEqual("Question which-benchmarks is claimed by agent-a.", str(refusal))
        self.assertEqual(
            helper.error_envelope(
                "CLAIM_HELD",
                "Question which-benchmarks is claimed by agent-a.",
                recoverable=False,
                remediation="Use claim --steal --if-older-than HOURS.",
                details={"slug": "which-benchmarks"},
            ),
            refusal.to_envelope(),
        )

    def test_refusal_defaults_match_an_uncoded_envelope(self):
        helper = load_helper()

        refusal = helper.ScriptRefusal("RUN_UNKNOWN", "unknown run id: run-9", exit_code=2)

        self.assertEqual(
            {
                "schema_version": "1.0",
                "error_code": "RUN_UNKNOWN",
                "message": "unknown run id: run-9",
                "recoverable": True,
                "remediation": "List workspace runs under runs/ and choose an existing run id.",
            },
            refusal.to_envelope(),
        )
        self.assertEqual("refused (RUN_UNKNOWN): unknown run id: run-9", refusal.text_line)

    def test_refusal_from_system_exit_classifies_and_keeps_the_bare_message(self):
        helper = load_helper()

        refusal = helper.ScriptRefusal.from_system_exit(
            SystemExit("Missing config: /tmp/workspace/research.yml"),
            exit_code=2,
        )

        self.assertEqual("CONFIG_MISSING", refusal.error_code)
        self.assertEqual(2, refusal.exit_code)
        # handle_system_exit has always printed the bare message in text mode.
        self.assertEqual("Missing config: /tmp/workspace/research.yml", refusal.text_line)
        self.assertEqual(
            helper.error_envelope("CONFIG_MISSING", "Missing config: /tmp/workspace/research.yml"),
            refusal.to_envelope(),
        )

    def test_refusal_from_system_exit_reraises_a_process_control_exit(self):
        """A SystemExit without a message is not a refusal; handle_system_exit re-raises it too."""
        helper = load_helper()

        with self.assertRaises(SystemExit) as caught:
            helper.ScriptRefusal.from_system_exit(SystemExit(4), exit_code=2)

        self.assertEqual(4, caught.exception.code)

    def test_emit_refusal_renders_both_modes_and_returns_the_exit_code(self):
        helper = load_helper()
        refusal = helper.ScriptRefusal(
            "LOCK_UNAVAILABLE",
            "workspace lock is held",
            exit_code=2,
            details={"lock": "log.md"},
        )

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            json_code = helper.emit_refusal(refusal, json_mode=True)
        self.assertEqual(2, json_code)
        self.assertEqual(refusal.to_envelope(), json.loads(stderr.getvalue()))

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            text_code = helper.emit_refusal(refusal, json_mode=False)
        self.assertEqual(2, text_code)
        self.assertEqual("refused (LOCK_UNAVAILABLE): workspace lock is held\n", stderr.getvalue())

    def test_helper_classifies_known_system_exit_messages(self):
        helper = load_helper()

        cases = {
            "PyYAML is required to read research.yml": "DEPENDENCY_MISSING",
            "Missing config: /tmp/workspace/research.yml": "CONFIG_MISSING",
            "Invalid config: /tmp/workspace/research.yml": "CONFIG_INVALID",
            "Missing manifest: /tmp/workspace/sources/manifest.jsonl": "MANIFEST_MISSING",
            "Invalid JSONL in /tmp/workspace/sources/manifest.jsonl:1": "MANIFEST_INVALID",
            (
                "PDF text extraction requires `pdftotext` from Poppler. "
                "Install Poppler or poppler-utils, then rerun normalize_sources.py."
            ): "DEPENDENCY_MISSING",
            (
                "PDF text extraction requires the `pypdf` Python package. "
                "Install the EvidenceWiki package dependencies, then rerun normalize_sources.py."
            ): "DEPENDENCY_MISSING",
            "Missing baseline file: /tmp/run-baseline.json": "BASELINE_MISSING",
            "Baseline must be question_status.py --format json output or a run_report.py baseline artifact": "BASELINE_INVALID",
            "Provide one or more query terms.": "QUERY_MISSING",
            "Unknown question slug: q-1 (no page under wiki/questions/)": "QUESTION_UNKNOWN",
            "Unknown request id: req-missing (no record in sources/source-requests.jsonl)": "REQUEST_UNKNOWN",
            "Unknown source id: paper:missing": "SOURCE_UNKNOWN",
            "Missing sibling workspace script: /workspace/scripts/lint.py": "TOOLING_MISSING",
            (
                "Intake total cap exceeded: open questions total would be 3, "
                "limit is 2."
            ): "INTAKE_TOTAL_CAP_EXCEEDED",
            (
                "Intake rate limit exceeded: 1 question(s) in the last hour plus "
                "1 new question(s) exceeds run.max_intake_per_hour 1."
            ): "INTAKE_RATE_LIMITED",
            "Intake field length exceeded: 1 field exceeds the intake byte limit.": "INTAKE_FIELD_TOO_LONG",
            (
                "Intake batch too large: 101 question(s) exceeds "
                "run.max_mcp_intake_batch_questions 100."
            ): "INTAKE_BATCH_TOO_LARGE",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(expected, helper.classify_error_code(message))

    def test_workspace_status_json_failure_uses_shared_health_document(self):
        status = load_script_module("error_envelope_status", "workspace_status.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            code, stdout, stderr = self.run_module(status, ["--project-root", tmpdir, "--format", "json"])

        self.assertEqual(2, code)
        self.assertEqual("", stderr)
        document = json.loads(stdout)
        self.assertEqual("1.0", document["schema_version"])
        self.assertEqual("invalid", document["workspace_health"]["status"])
        self.assertFalse(document["workspace_health"]["materially_valid"])
        self.assertIn("WORKSPACE_REQUIRED_FILE_MISSING", document["workspace_health"]["finding_codes"])
        self.assertEqual("attention_required", document["readiness"]["verdict"])
        self.assertTrue(
            any(
                finding.get("artifacts") == ["research.yml"] and finding.get("remediation")
                for finding in document["workspace_health"]["findings"]
            )
        )

    def test_intake_field_length_remediation_names_metadata(self):
        helper = load_helper()

        remediation = helper.remediation_for("INTAKE_FIELD_TOO_LONG")

        self.assertIn("metadata", remediation)

    def test_question_claim_json_conflict_uses_error_envelope_with_details(self):
        claim = load_script_module("error_envelope_question_claim", "question_claim.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            code, _, stderr = self.run_module(
                claim,
                [
                    "--project-root",
                    str(target),
                    "claim",
                    "--slug",
                    "which-benchmarks",
                    "--agent-id",
                    "agent-a",
                    "--format",
                    "json",
                ],
            )
            self.assertEqual(0, code, stderr)

            code, stdout, stderr = self.run_module(
                claim,
                [
                    "--project-root",
                    str(target),
                    "claim",
                    "--slug",
                    "which-benchmarks",
                    "--agent-id",
                    "agent-b",
                    "--format",
                    "json",
                ],
            )

        self.assertEqual(3, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("CLAIM_HELD", envelope["error_code"])
        self.assertFalse(envelope["recoverable"])
        self.assertIn("Use claim --steal --if-older-than", envelope["remediation"])
        self.assertEqual(
            {"action": "claim", "slug": "which-benchmarks", "agent_id": "agent-b"},
            envelope["details"],
        )

    def test_json_output_scripts_table_documents_required_scripts(self):
        documented = documented_json_output_scripts()

        self.assertEqual(sorted(set(documented)), sorted(documented), "JSON Output Scripts table has duplicates")
        required = json_mode_scripts()
        # Guard the guard: a derivation that silently matched nothing would pass forever,
        # and the script this derivation exists to stop exempting must be inside it.
        self.assertGreater(len(required), 18, "JSON-mode derivation found fewer scripts than the list it replaced")
        self.assertIn("orchestration_controller.py", required)
        missing = sorted(set(required) - set(documented))
        self.assertEqual([], missing, "document every required JSON-mode script in orchestrator-handoff.md")

    def test_every_raised_error_code_has_a_specific_remediation_and_a_doc_row(self):
        """CR-14's closing gate: no code falls back to the generic remediation, and every
        code a script can raise is documented in a table.

        The registry and the doc tables are hand-maintained lists that must cover a set
        nobody was counting. Before CR-14, 97 codes fell back to
        ``"Read the message, fix the input or workspace state, and rerun the command."``
        and 37 appeared in no doc at all — while the operator holding one of those
        refusals was told nothing about how to fix it.

        **This guard deliberately does not reuse the scan that scoped CR-14.** That scan
        saw only codes passed as a positional string literal, and missed eight across two
        modules that arrive through a keyword (``status_error_code=``, ``not_found_code=``,
        ``error_code=``). A completeness check built on it would certify coverage over
        exactly the codes it could see and stay silent about the rest — a guard carrying
        the defect it exists to prevent, which is how the JSON Output Scripts check came
        to pass while reading 85 of 131 codes.

        Known blindness, stated rather than implied: a code assembled at runtime (an
        f-string, a lookup, a value read from data) is invisible here, as is one raised by
        a helper this walk does not recognise as an error constructor. The counts asserted
        below are the guard's own scope, so a change that silently shrinks its reach fails
        instead of quietly passing.
        """
        codes = collect_raised_error_codes()
        helper = load_helper()
        generic = helper.remediation_for("__no_such_code_can_ever_be_registered__")

        # Guard the guard: publish the scope, so shrinking it is a failure, not a pass.
        self.assertGreater(len(codes), 180, "error-code collection found suspiciously few codes")
        for expected, why in (
            ("STATUS_NOT_REVIEWABLE", "reached through a status_error_code= keyword"),
            ("GITHUB_NOT_FOUND", "reached through a not_found_code= keyword"),
            ("WORK_ORDER_INVALID", "reached through an error_code= keyword"),
        ):
            self.assertIn(expected, codes, f"{expected} must be collected: {why}")

        documented = documented_error_code_rows()
        self.assertGreater(len(documented), 190, "documentation sweep found suspiciously few code rows")

        unremediated = sorted(code for code in codes if helper.remediation_for(code) == generic)
        self.assertEqual([], unremediated, "these raised codes fall back to the generic remediation")

        undocumented = sorted(code for code in codes if code not in documented)
        self.assertEqual([], undocumented, "these raised codes appear in no documentation table")

    def test_each_error_code_resolves_to_one_recoverability(self):
        """One code, one retry verdict — because a host branching on it has only the code.

        `recoverable` is the only envelope field an automated caller *acts* on rather than
        reads. When one code resolves both ways across its own raise sites, that caller
        retries some occurrences and not others with nothing in the envelope explaining
        the difference. `ORCHESTRATION_STATE_INVALID` answered three ways across 25 sites.

        Where a condition genuinely needs both answers, that is evidence the code is doing
        two jobs and should be split — not licence to vary. Such a case belongs in
        `RECOVERABILITY_VARIES_BY_SITE` with a written reason, where it can be argued with,
        rather than dispersed across raise sites where nobody can see it.

        Sites that pass nothing are resolved through `default_recoverable`, the same way
        `emit_refusal` does: silence answers "recoverable" for every code but the two claim
        codes, so an omitted flag is as much an answer as an explicit one.
        """
        sites = collect_recoverability_by_site()
        # Guard the guard: a collection that silently found nothing would pass forever.
        self.assertGreater(len(sites), 150, "recoverability sweep found suspiciously few codes")
        self.assertIn("ORCHESTRATION_STATE_INVALID", sites, "the worst offender must be inside the sweep")

        split = {
            code: answers
            for code, answers in sites.items()
            if len(answers) > 1 and code not in RECOVERABILITY_VARIES_BY_SITE
        }
        detail = "; ".join(
            f"{code} -> "
            + ", ".join(
                f"{value} at {len(places)} site(s) ({places[0]}…)" for value, places in sorted(answers.items())
            )
            for code, answers in sorted(split.items())
        )
        self.assertEqual({}, split, f"these codes report more than one recoverability: {detail}")

    def test_no_remediation_advises_what_another_forbids(self):
        """A code must not tell one operator to do what it forbids another from doing.

        `ACADEMIC_PROVIDER_REQUEST_LEDGER_INVALID` did exactly that across its seven raise
        sites: four said "**Repair** or restore the run-bound provider-call ledger", three
        said "restore from a trusted backup … **Do not** deduplicate or reset accounting by
        hand". Same code, same artifact, opposite instructions — and the artifact is the
        accounting ledger that enforces provider budgets, so hand-repair is exactly what
        must not be advised. The registry entry already said "do not reset it", making the
        four inline texts contradict their own floor.

        Deliberately narrow, per the lesson from the command checker: this flags only a
        text that advises a verb some *prohibition for the same code* names. Variation
        across conditions is correct and is not reported — `VALUE_INVALID` says different
        things for different bad values, and a check that called those 35 codes defective
        would be muted within a week.
        """
        # Capture the whole prohibition clause, then split it into verbs. Matching a
        # `<verb>( or <verb>)*` shape directly stops at the first comma, so
        # "Do not reset, deduplicate, or hand-edit provider accounting" contributed only
        # "reset" and left two forbidden verbs uncollected — a scanner that reads the
        # phrasing its author happened to write. Hyphenated verbs ("hand-edit") count too,
        # which is why the verb is escaped before it reaches a pattern.
        prohibition_clause = re.compile(r"\bdo not\b([^.;]*)", re.I)
        # Strip *any* negated clause before looking for advice, but collect forbidden verbs
        # only from the unambiguous imperative "do not". "Never" is both: "Never bind …" is
        # a prohibition, while "status polling never requires this lock" and "a committed
        # event is never rewritten" are descriptions. Collecting from it would put
        # `requires` and `rewritten` in the forbidden set and flag ordinary sentences;
        # not stripping it made a registry entry's own prohibition ("never create the
        # marker by hand") read as advice to create one. Under-approximating the forbidden
        # set is the safe direction: it misses a contradiction rather than inventing one.
        whole_clause = re.compile(r"\b(?:do not|never)\b[^.]*\.?", re.I)

        def forbidden_verbs(text: str) -> set[str]:
            verbs: set[str] = set()
            for clause in prohibition_clause.findall(text):
                for token in re.split(r",|\bor\b", clause):
                    word = token.strip().split(" ")[0].strip().lower()
                    if re.fullmatch(r"[a-z][a-z-]*", word or ""):
                        verbs.add(word)
            return verbs

        by_code: dict[str, list[tuple[str, str]]] = {}
        for label, text in all_remediation_texts():
            code = label.split(" ")[0]
            by_code.setdefault(code, []).append((label, text))
        self.assertGreater(len(by_code), 150, "remediation grouping found suspiciously few codes")

        helper = load_helper()
        contradictions: list[str] = []
        for code, entries in by_code.items():
            texts = [text for _, text in entries] + [helper.remediation_for(code)]
            forbidden: set[str] = set()
            for text in texts:
                forbidden |= forbidden_verbs(text)
            if not forbidden:
                continue
            for label, text in entries:
                # Strip prohibitions entirely before looking for advice, so "do not repair
                # or reset" is not read as advising "reset".
                advised = whole_clause.sub("", text.lower())
                for verb in sorted(forbidden):
                    if re.search(rf"\b{re.escape(verb)}\b", advised):
                        contradictions.append(f"{label} advises '{verb}', which another remediation for {code} forbids")
        self.assertEqual([], contradictions)

    def test_documentation_tables_have_a_consistent_column_count(self):
        """Every row in a Markdown table must match its header's column count.

        `test_every_raised_error_code_has_a_specific_remediation_and_a_doc_row` counts a
        code as documented when a row starting `| \\`CODE\\` |` exists *anywhere*, which is
        deliberate — codes are documented across several files. The cost is that it cannot
        tell a row in the right table from a row in the wrong one, and CR-14's insertion
        script put fourteen 3-column error-code rows inside the 2-column "Required
        envelope fields" table by partitioning on a marker and appending after the last
        table row *in the rest of the document*. The completeness guard passed; the page
        rendered as garbage. A reviewer caught it.

        So the shape is checked separately from the membership. A table is a contiguous run
        of lines starting `|`; every row in it must have the header's cell count.
        """
        offenders: list[str] = []
        roots = [REPO_ROOT / "workspace-template" / "docs", REPO_ROOT / "docs"]
        for root in roots:
            for path in sorted(root.rglob("*.md")):
                lines = path.read_text(encoding="utf-8").splitlines()
                index = 0
                while index < len(lines):
                    if not lines[index].startswith("|"):
                        index += 1
                        continue
                    block_start = index
                    while index < len(lines) and lines[index].startswith("|"):
                        index += 1
                    block = lines[block_start:index]
                    expected = len(markdown_row_cells(block[0]))
                    for offset, row in enumerate(block[1:], start=1):
                        if set(row.replace("|", "").replace("-", "").replace(":", "").strip()) == set():
                            continue  # the ---|--- separator
                        if len(markdown_row_cells(row)) != expected:
                            offenders.append(
                                f"{path.relative_to(REPO_ROOT)}:{block_start + offset + 1} has "
                                f"{len(markdown_row_cells(row))} cells, header has {expected}"
                            )
        self.assertEqual([], offenders, "these table rows do not match their header's column count")

    def test_remediations_name_only_commands_that_exist(self):
        """A remediation must not send an operator to a flag or subcommand that isn't there.

        This is the defect CR-14 kept finding by accident: `BUDGET_EXCEEDED` named a
        command without its required `--run-id`/`--agent-id`; `DISCOVERY_RUN_RECOVERY_REQUIRED`
        named `run_controller.py recover`, which refuses without them;
        `REQUEST_NOT_OPEN` — a *reviewed* table cell — told operators to reopen a request
        when no command can. Eight were found by reading, two of them introduced by the
        very changes that fixed the others. Reading is what produced them, so this checks
        mechanically instead.

        Both halves of what an operator can see are audited: the registry and the inline
        overrides, since an inline `remediation=` replaces the registry entry entirely.

        Two deliberate limits, so the check is trusted rather than muted:

        - A `<script>.py <token>` pair is judged only when the token is a subcommand of
          *some* script. Otherwise the token is ordinary prose — `run_controller.py or …`,
          `publication_readiness.py from …` — and a stricter rule reports five such
          sentences as defects, which is how a check earns its way onto an ignore list.
        - A bare verb with no script beside it ("reopen the request") is not mechanically
          decidable and stays out of scope; that class is why `REQUEST_NOT_OPEN` needed a
          human to catch it.
        """
        flags = cli_flag_universe()
        subcommands = script_subcommands()
        known_subcommands = set().union(*subcommands.values()) if subcommands else set()
        texts = all_remediation_texts()

        # Guard the guard: an extraction that silently collected nothing would pass forever.
        self.assertGreater(len(texts), 400, "remediation sweep found suspiciously few texts")
        self.assertGreater(len(flags), 150, "flag universe looks too small to audit against")
        self.assertIn("--require-decisive-scope", flags)

        unknown_flags = [
            f"{code}: {match.group(0)}"
            for code, text in texts
            for match in re.finditer(r"--[a-z][a-z0-9-]+", text)
            if match.group(0) not in flags
        ]
        self.assertEqual([], unknown_flags, "remediations name flags this package does not define")

        wrong_script = [
            f"{code}: `{script} {token}` — {token} belongs to {sorted(o for o, s in subcommands.items() if token in s)}"
            for code, text in texts
            for script, token in re.findall(r"\b([a-z_]+\.py)\s+([a-z][a-z-]*)", text)
            if script in subcommands and token in known_subcommands and token not in subcommands[script]
        ]
        self.assertEqual([], wrong_script, "remediations pair a subcommand with the wrong script")

    def test_non_recoverable_codes_are_mirrored(self):
        """The script helper and the library must agree on which codes are unretryable.

        Two hand-mirrored sets with a comment asking for agreement and nothing checking
        it. They happened to match, but a code added to one and not the other would give
        a host a different retry verdict through the in-process door than through the CLI
        — the same door-disagreement CR-13 fixed for flags, on the field callers act on.
        """
        helper = load_helper()
        sys.path.insert(0, str(REPO_ROOT / "src"))
        try:
            from evidence_wiki import errors as library_errors
        finally:
            sys.path.pop(0)

        self.assertEqual(
            set(helper.NON_RECOVERABLE_CODES),
            set(library_errors._NON_RECOVERABLE_CODES),
            "workspace-template/scripts/_script_errors.py and src/evidence_wiki/errors.py "
            "disagree about which codes are non-recoverable",
        )
        # Both doors must answer identically for every code either of them names.
        for code in sorted(set(helper.NON_RECOVERABLE_CODES) | {"CONFIG_INVALID", "VALUE_INVALID"}):
            with self.subTest(code=code):
                self.assertEqual(
                    helper.default_recoverable(code),
                    library_errors.default_recoverable(code),
                    f"{code} resolves differently in the script helper and the library",
                )

    def test_json_output_scripts_table_uses_stable_error_codes(self):
        missing = sorted(documented_json_output_error_codes() - documented_stable_error_codes())

        self.assertEqual([], missing, "document every JSON Output Scripts table code in Stable error codes")

    def test_intake_limit_error_codes_are_documented(self):
        rows = dict(documented_json_output_script_rows())
        intake_codes = set(re.findall(r"`([A-Z][A-Z0-9_]+)`", rows["intake_questions.py"]))

        self.assertIn("INTAKE_TOTAL_CAP_EXCEEDED", intake_codes)
        self.assertIn("INTAKE_RATE_LIMITED", intake_codes)
        self.assertIn("INTAKE_FIELD_TOO_LONG", intake_codes)
        stable_codes = documented_stable_error_codes()
        self.assertIn("INTAKE_TOTAL_CAP_EXCEEDED", stable_codes)
        self.assertIn("INTAKE_RATE_LIMITED", stable_codes)
        self.assertIn("INTAKE_FIELD_TOO_LONG", stable_codes)
        self.assertIn("INTAKE_BATCH_TOO_LARGE", stable_codes)

    def test_documented_json_mode_scripts_use_shared_error_helper(self):
        for name in documented_json_output_scripts():
            with self.subTest(script=name):
                path = SCRIPTS / name
                self.assertTrue(path.is_file(), f"documented JSON-mode script is missing: {name}")
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                self.assertTrue(script_imports_error_helper(tree), f"{name} must import _script_errors")
                self.assertTrue(
                    script_calls_error_helper(tree),
                    f"{name} must call handle_system_exit, emit_error, or error_envelope",
                )


if __name__ == "__main__":
    unittest.main()
