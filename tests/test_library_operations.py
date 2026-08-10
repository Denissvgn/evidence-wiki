"""The documented API operations, held to the CLI they exist beside.

CR-6 adds an embeddable library API so a long-lived host can call workspace
operations in-process. The whole promise of that API is that it is *the same
operation* the command line performs -- same document out, same workspace state
and audit trail behind it, same refusal when it refuses. This suite is where that
promise is checked per method.

Two properties, per operation:

* **Document equality.** The dict the API method returns equals the JSON document
  the script's own CLI prints for the same invocation. Parsed JSON is compared,
  not bytes: choosing indentation and where to write it is the CLI's job, and a
  host never sees it. Mutating operations run on *twin copies* of one prepared
  workspace, because a claim or a resolution is not idempotent -- run twice
  against one workspace the second run sees the first run's writes and is a
  different operation. Refusal cases share one workspace, since a refusal writes
  nothing by construction.
* **Typed refusal.** A refusal arrives as the right exception class carrying the
  right ``error_code``, and the envelope on that exception equals the envelope the
  CLI printed on stderr.

``tests/test_seam_conformance.py`` already holds each script's ``run_<op>`` seam
to its own ``main``. This suite is the layer above: that the *API method* reaches
the right seam with the right keywords. The two together are what make
"``ws.questions.block(...)`` and ``evidence-wiki``'s block are one operation" a
checked statement rather than an intention -- see
``AuditTrailEqualityTests`` for the workspace-state half of it.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import _script_host, errors  # noqa: E402
from evidence_wiki._facades import _base  # noqa: E402
from evidence_wiki.workspace import Workspace  # noqa: E402

SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"

SUCCESS = "success"
REFUSAL = "refusal"

HOLDER = "agent-holder"
OTHER = "agent-other"
ANSWER_PAGE = "wiki/synthesis/api-answer.md"
GROUNDED_SOURCE = "web:vendor-official-product-spec"

#: Slugs the fixture creates. Each mutating case owns one, so no two cases can
#: interfere through shared question state even on a shared workspace.
SLUGS = (
    "claim-me",
    "release-me",
    "answer-me",
    "block-me",
    "defer-me",
    "reject-me",
    "grounding-me",
    "held",
    "coverage-me",
)

#: Slugs claimed by ``HOLDER`` before the twin copies are taken, so a claim
#: timestamp already on a page is identical in both copies and only a timestamp
#: the case itself writes is genuinely per-invocation.
PRECLAIMED = ("release-me", "answer-me", "block-me", "defer-me", "reject-me", "grounding-me", "held")

COVERAGE_TEMPLATE = """\
coverage_profile: academic-method-existence
required_facets:
  - facet_id: paper-identity
    description: Confirm the method exists in a real scholarly index.
    required: true
    evidence_path: academic_method_existence
    source_policy: academic_indexed
    freshness_policy: publication_identity
    identity_policy: citation_id_resolves
    min_sources: 1
optional_facets: []
"""

GROUNDING_DOCUMENT = """\
grounding:
  - claim: The product spec is vendor-controlled.
    source_id: web:vendor-official-product-spec
    quote: Vendor-controlled product specification.
    location_hint: Official product spec
"""

MANIFEST_RECORD = {
    "id": GROUNDED_SOURCE,
    "kind": "markdown",
    "raw_paths": ["raw/links/vendor.md"],
    "status": "normalized",
    "detected_at": "2026-08-01T00:00:00Z",
}


def batch_document(*slugs: str) -> str:
    questions = "".join(
        f"  - question: Library API question {slug}?\n    id: {slug}\n    priority: high\n" for slug in slugs
    )
    return f'schema_version: "1.0"\nquestions:\n{questions}'


def run_script(script: str, root: str, argv: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess:
    """Run one packaged script as a real subprocess, the way an operator would.

    A subprocess rather than an in-process ``main`` call on purpose: an in-process
    run would have to redirect ``sys.stdout``, and the point of comparing against
    the CLI is to compare against what the CLI actually writes to a real stdout.
    """
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPTS / script), "--project-root", root, *argv],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd),
    )


@dataclass(frozen=True)
class ApiCase:
    """One documented API method, paired against the argv that does the same thing.

    ``argv`` is everything after ``--project-root <root>``; the runner supplies the
    root, so a case cannot accidentally depend on the child's cwd. ``call`` drives
    the API method on an open handle and returns what it returned.

    Both sides receive ``paths`` -- the fixture's temporary files, which do not
    exist when the table is written. ``argv`` entries name them with ``{}``
    placeholders and ``call`` indexes the same mapping, so the two sides cannot
    drift onto different files.

    ``volatile`` names dotted paths whose values cannot agree between two runs on
    two copies -- wall clock, and the absolute workspace root, which is by
    definition different in the twin. Each is required to be *present on both
    sides* before being blanked, so a field the API drops entirely is still
    caught.

    ``cli_returncode`` is the status the CLI exits with on a *successful* run.
    It is not always zero: the grounding verifier prints its report and then
    exits non-zero when a claim did not verify, which is exactly the case the API
    must return as a document rather than raise.
    """

    name: str
    surface: str
    script: str
    argv: tuple[str, ...]
    call: Callable[[Workspace, dict[str, str]], dict[str, Any]]
    expect: str = SUCCESS
    volatile: tuple[str, ...] = field(default=())
    cli_returncode: int = 0


class LibraryOperationFixture(unittest.TestCase):
    """A workspace with questions, claims, a coverage manifest and a known source."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.scratch = Path(cls._tmp.name)
        cls.prepared = cls.scratch / "prepared"
        cls._init_workspace(cls.prepared)

        cls.batch_path = cls.scratch / "batch.yaml"
        cls.batch_path.write_text(batch_document(*SLUGS), encoding="utf-8")
        cls._script("intake_questions.py", cls.prepared, "--from-file", str(cls.batch_path), "--format", "json")

        # A second batch the ``add_batch`` case injects, distinct from the fixture's own.
        cls.second_batch_path = cls.scratch / "second-batch.yaml"
        cls.second_batch_path.write_text(batch_document("late-arrival"), encoding="utf-8")

        cls.grounding_path = cls.scratch / "grounding.yml"
        cls.grounding_path.write_text(GROUNDING_DOCUMENT, encoding="utf-8")

        cls.coverage_template = cls.scratch / "coverage-template.yml"
        cls.coverage_template.write_text(COVERAGE_TEMPLATE, encoding="utf-8")
        cls._script(
            "coverage_manifest.py", cls.prepared,
            "init", "--slug", "coverage-me", "--template", str(cls.coverage_template), "--format", "json",
        )

        # ``grounding set`` refuses a source the manifest does not know, so the
        # fixture has to know one. Written rather than inventoried: the record's
        # provenance is irrelevant here, only that the id resolves.
        manifest = cls.prepared / "sources" / "manifest.jsonl"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(MANIFEST_RECORD) + "\n")

        answer_page = cls.prepared / ANSWER_PAGE
        answer_page.parent.mkdir(parents=True, exist_ok=True)
        answer_page.write_text(
            "---\ntype: synthesis\ntitle: API answer\n---\n\nAn answer page a resolution can point at.\n",
            encoding="utf-8",
        )

        for slug in PRECLAIMED:
            cls._script(
                "question_claim.py", cls.prepared,
                "claim", "--slug", slug, "--agent-id", HOLDER, "--format", "json",
            )

        # Refusal cases share one workspace: a refusal writes nothing, so they
        # cannot disturb each other or the twins taken from ``prepared``.
        cls.shared = cls.scratch / "shared"
        shutil.copytree(cls.prepared, cls.shared)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @classmethod
    def _init_workspace(cls, target: Path) -> None:
        completed = subprocess.run(  # noqa: S603
            [
                sys.executable, "-m", "evidence_wiki.cli", "init",
                "--target", str(target),
                "--project-name", "library-api",
                "--project-description", "library API operation fixture",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(f"fixture init failed: {completed.stderr}")

    @classmethod
    def _script(cls, script: str, root: Path, *argv: str) -> subprocess.CompletedProcess:
        completed = run_script(script, str(root), argv, cwd=cls.scratch)
        if completed.returncode != 0:
            raise AssertionError(f"fixture setup failed: {script} {' '.join(argv)}\n{completed.stderr}")
        return completed

    def twin(self, name: str) -> tuple[Path, Path]:
        """Copy the prepared workspace twice: one root for the CLI, one for the API."""
        for_cli = self.scratch / f"{name}-cli"
        for_api = self.scratch / f"{name}-api"
        for target in (for_cli, for_api):
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(self.prepared, target)
        return for_cli, for_api

    def blanked(self, document: Any, volatile: Sequence[str], *, label: str) -> Any:
        """Return ``document`` with each volatile path replaced, requiring it present."""
        result = copy.deepcopy(document)
        for path in volatile:
            node = result
            *parents, leaf = path.split(".")
            for key in parents:
                self.assertIsInstance(node, dict, f"{label}: {path} does not resolve")
                self.assertIn(key, node, f"{label}: {path} is absent")
                node = node[key]
            self.assertIsInstance(node, dict, f"{label}: {path} does not resolve")
            self.assertIn(leaf, node, f"{label}: volatile field {path} is absent")
            node[leaf] = "<volatile>"
        return result


def cases() -> tuple[ApiCase, ...]:
    """Every name in the published ``library_api.surface`` this unit owns.

    Written as a table so the surface list and the cases can be compared
    mechanically -- ``test_every_owned_surface_name_has_a_case`` does exactly that,
    which is what stops a method being added to the contract and never exercised.
    """
    return (
        ApiCase(
            name="status_document",
            surface="workspace.status",
            script="workspace_status.py",
            argv=("--format", "json"),
            call=lambda ws, paths: ws.status(),
            # The root really is different in the twin; blanking it is what makes
            # the rest of a 250-line document a meaningful comparison.
            volatile=("generated_at", "workspace_health.project_root"),
        ),
        ApiCase(
            name="status_unknown_run_id",
            surface="workspace.status",
            script="workspace_status.py",
            argv=("--format", "json", "--run-id", "no-such-run"),
            call=lambda ws, paths: ws.status(run_id="no-such-run"),
            expect=REFUSAL,
        ),
        ApiCase(
            name="coverage_evaluate",
            surface="coverage.evaluate",
            script="coverage_manifest.py",
            argv=("evaluate", "--slug", "coverage-me", "--format", "json"),
            call=lambda ws, paths: ws.coverage.evaluate("coverage-me"),
            volatile=("manifest.updated_at",),
        ),
        ApiCase(
            name="coverage_rejected_slug",
            surface="coverage.evaluate",
            script="coverage_manifest.py",
            argv=("evaluate", "--slug", "../escape", "--format", "json"),
            call=lambda ws, paths: ws.coverage.evaluate("../escape"),
            expect=REFUSAL,
        ),
        ApiCase(
            name="grounding_verify",
            surface="grounding.verify",
            script="verify_quotes.py",
            argv=("--slug", "held", "--format", "json"),
            call=lambda ws, paths: ws.grounding.verify(["held"]),
            volatile=("generated_at",),
            # The fixture question carries no grounding, so nothing verifies and
            # the CLI exits EXIT_NOT_VERIFIED after printing the report. The API
            # must return that same report rather than turn the exit into a raise.
            cli_returncode=1,
        ),
        ApiCase(
            name="grounding_verify_unknown_slug",
            surface="grounding.verify",
            script="verify_quotes.py",
            argv=("--slug", "no-such-question", "--format", "json"),
            call=lambda ws, paths: ws.grounding.verify(["no-such-question"]),
            expect=REFUSAL,
        ),
        ApiCase(
            name="questions_claim",
            surface="questions.claim",
            script="question_claim.py",
            argv=("claim", "--slug", "claim-me", "--agent-id", OTHER, "--format", "json"),
            call=lambda ws, paths: ws.questions.claim(slug="claim-me", agent_id=OTHER),
            volatile=("holder.claimed_at",),
        ),
        ApiCase(
            name="questions_claim_held_by_another_agent",
            surface="questions.claim",
            script="question_claim.py",
            argv=("claim", "--slug", "held", "--agent-id", OTHER, "--format", "json"),
            call=lambda ws, paths: ws.questions.claim(slug="held", agent_id=OTHER),
            expect=REFUSAL,
        ),
        ApiCase(
            name="questions_release",
            surface="questions.release",
            script="question_claim.py",
            argv=("release", "--slug", "release-me", "--agent-id", HOLDER, "--format", "json"),
            call=lambda ws, paths: ws.questions.release(slug="release-me", agent_id=HOLDER),
        ),
        ApiCase(
            name="questions_answer",
            surface="questions.answer",
            script="question_resolve.py",
            argv=(
                "answer", "--slug", "answer-me", "--agent-id", HOLDER, "--answer-page", ANSWER_PAGE,
                "--allow-uncited", "--confidence", "medium", "--format", "json",
            ),
            call=lambda ws, paths: ws.questions.answer(
                slug="answer-me",
                agent_id=HOLDER,
                answer_page=ANSWER_PAGE,
                allow_uncited=True,
                confidence="medium",
            ),
        ),
        ApiCase(
            name="questions_block",
            surface="questions.block",
            script="question_resolve.py",
            argv=(
                "block", "--slug", "block-me", "--agent-id", HOLDER,
                "--blocked-reason", "No admissible evidence yet.", "--format", "json",
            ),
            call=lambda ws, paths: ws.questions.block(
                slug="block-me", agent_id=HOLDER, blocked_reason="No admissible evidence yet."
            ),
        ),
        ApiCase(
            name="questions_defer",
            surface="questions.defer",
            script="question_resolve.py",
            argv=(
                "defer", "--slug", "defer-me", "--agent-id", HOLDER,
                "--reason", "Out of scope for this run.", "--format", "json",
            ),
            call=lambda ws, paths: ws.questions.defer(
                slug="defer-me", agent_id=HOLDER, reason="Out of scope for this run."
            ),
        ),
        ApiCase(
            name="questions_reject",
            surface="questions.reject",
            script="question_resolve.py",
            argv=(
                "reject", "--slug", "reject-me", "--agent-id", HOLDER,
                "--reason", "Not a research question.", "--format", "json",
            ),
            call=lambda ws, paths: ws.questions.reject(
                slug="reject-me", agent_id=HOLDER, reason="Not a research question."
            ),
        ),
        ApiCase(
            name="questions_reopen_refuses_an_open_question",
            surface="questions.reopen",
            script="question_resolve.py",
            argv=(
                "reopen", "--slug", "claim-me", "--agent-id", HOLDER,
                "--source-id", GROUNDED_SOURCE, "--format", "json",
            ),
            call=lambda ws, paths: ws.questions.reopen(
                slug="claim-me", agent_id=HOLDER, source_id=[GROUNDED_SOURCE]
            ),
            expect=REFUSAL,
        ),
        ApiCase(
            name="questions_approve_refuses_an_empty_reviewer",
            surface="questions.approve",
            script="question_resolve.py",
            argv=("approve", "--slug", "held", "--reviewer", "  ", "--format", "json"),
            call=lambda ws, paths: ws.questions.approve(slug="held", reviewer="  "),
            expect=REFUSAL,
        ),
        ApiCase(
            name="questions_review_refuses_an_unknown_verdict",
            surface="questions.review",
            script="question_resolve.py",
            argv=(
                "review", "--slug", "held", "--policy", "source_policy", "--verdict", "maybe",
                "--reviewed-by", "reviewer-a", "--format", "json",
            ),
            call=lambda ws, paths: ws.questions.review(
                slug="held", policy="source_policy", verdict="maybe", reviewed_by="reviewer-a"
            ),
            expect=REFUSAL,
        ),
        ApiCase(
            name="questions_set_grounding",
            surface="questions.set_grounding",
            script="question_resolve.py",
            argv=(
                "grounding", "set", "--slug", "grounding-me", "--agent-id", HOLDER,
                "--from-file", "{grounding_file}", "--format", "json",
            ),
            call=lambda ws, paths: ws.questions.set_grounding(
                slug="grounding-me", agent_id=HOLDER, from_file=paths["grounding_file"]
            ),
        ),
        ApiCase(
            name="questions_add_batch",
            surface="questions.add_batch",
            script="intake_questions.py",
            argv=("--from-file", "{second_batch}", "--format", "json"),
            call=lambda ws, paths: ws.questions.add_batch(from_file=paths["second_batch"]),
            volatile=("generated_at",),
        ),
        ApiCase(
            name="questions_add_batch_refuses_a_missing_file",
            surface="questions.add_batch",
            script="intake_questions.py",
            argv=("--from-file", "{missing_batch}", "--format", "json"),
            call=lambda ws, paths: ws.questions.add_batch(from_file=paths["missing_batch"]),
            expect=REFUSAL,
        ),
    )


CASES = cases()


class ApiDocumentEqualityTests(LibraryOperationFixture):
    """Each API method against the same invocation through the script's own CLI."""

    @property
    def paths(self) -> dict[str, str]:
        """The fixture's temporary files, named the way the case table names them."""
        return {
            "grounding_file": str(self.grounding_path),
            "second_batch": str(self.second_batch_path),
            "missing_batch": str(self.scratch / "definitely-absent.yaml"),
        }

    def resolve(self, value: str) -> str:
        """Expand a ``{name}`` placeholder in one argv entry."""
        return value.format(**self.paths)

    def test_every_owned_surface_name_has_a_case(self):
        from evidence_wiki._contract import LIBRARY_API_SURFACE

        owned = {
            "workspace.status",
            "coverage.evaluate",
            "grounding.verify",
            "questions.claim",
            "questions.release",
            "questions.answer",
            "questions.block",
            "questions.defer",
            "questions.reject",
            "questions.reopen",
            "questions.approve",
            "questions.review",
            "questions.set_grounding",
            "questions.add_batch",
        }
        self.assertTrue(owned <= set(LIBRARY_API_SURFACE), "a case names a surface the contract does not publish")
        self.assertEqual(owned, {case.surface for case in CASES}, "a published method has no case")

    def test_api_document_equals_the_cli_document(self):
        for case in (case for case in CASES if case.expect == SUCCESS):
            with self.subTest(case=case.name):
                cli_root, api_root = self.twin(case.name)
                argv = tuple(self.resolve(item) for item in case.argv)
                completed = run_script(case.script, str(cli_root), argv, cwd=self.scratch)
                self.assertEqual(
                    case.cli_returncode,
                    completed.returncode,
                    f"{case.name}: unexpected CLI exit\n{completed.stderr}",
                )

                with Workspace.open(api_root) as ws:
                    document = case.call(ws, self.paths)

                self.assertIsInstance(document, dict, f"{case.name}: the API returned no document")
                self.assertEqual(
                    self.blanked(json.loads(completed.stdout), case.volatile, label=f"{case.name} cli"),
                    self.blanked(document, case.volatile, label=f"{case.name} api"),
                    f"{case.name}: the API document and the CLI document disagree",
                )

    def test_a_refusal_is_the_typed_exception_the_cli_envelope_describes(self):
        for case in (case for case in CASES if case.expect == REFUSAL):
            with self.subTest(case=case.name):
                argv = tuple(self.resolve(item) for item in case.argv)
                completed = run_script(case.script, str(self.shared), argv, cwd=self.scratch)
                self.assertNotEqual(0, completed.returncode, f"{case.name}: the CLI did not refuse")
                self.assertNotIn("Traceback", completed.stderr, f"{case.name}: the CLI crashed")
                envelope = json.loads(completed.stderr)

                with Workspace.open(self.shared) as ws:
                    with self.assertRaises(errors.EvidenceWikiError) as caught:
                        case.call(ws, self.paths)

                raised = caught.exception
                self.assertEqual(envelope["error_code"], raised.error_code, case.name)
                self.assertEqual(envelope["message"], raised.message, case.name)
                self.assertEqual(envelope["recoverable"], raised.recoverable, case.name)
                self.assertEqual(envelope["remediation"], raised.remediation, case.name)
                self.assertEqual(envelope.get("details", {}), raised.details, case.name)
                self.assertEqual(completed.returncode, raised.exit_code, case.name)
                self.assertIsInstance(
                    raised,
                    errors.error_class_for(envelope["error_code"]),
                    f"{case.name}: {envelope['error_code']} reached the host as the wrong family",
                )

    def test_a_claim_conflict_is_a_claim_error_carrying_claim_held(self):
        # Spelled out rather than left implicit in the table: CLAIM_HELD is the
        # one refusal a host is expected to branch on by class, and the one that
        # is not exit 2.
        with Workspace.open(self.shared) as ws:
            with self.assertRaises(errors.ClaimError) as caught:
                ws.questions.claim(slug="held", agent_id=OTHER)
        self.assertEqual("CLAIM_HELD", caught.exception.error_code)
        self.assertEqual(3, caught.exception.exit_code)
        self.assertFalse(caught.exception.recoverable)

    def test_a_failed_verification_is_a_report_not_an_exception(self):
        # The grounding verifier's whole value is the document describing which
        # claim failed against which record. A host cannot read that off an
        # exception, so only a genuine refusal may raise.
        _, api_root = self.twin("grounding-failure")
        page = api_root / "wiki" / "questions" / "held.md"
        page.write_text(
            page.read_text(encoding="utf-8").replace(
                "status: in_progress",
                "status: in_progress\ngrounding:\n"
                "  - claim: Unsupported claim.\n"
                f"    source_id: {GROUNDED_SOURCE}\n"
                "    quote: A sentence that appears in no normalized record.\n",
                1,
            ),
            encoding="utf-8",
        )
        with Workspace.open(api_root) as ws:
            report = ws.grounding.verify(["held"])
        self.assertIsInstance(report, dict)
        self.assertNotEqual("verified", report["overall_result"], "the verification should have failed")
        self.assertFalse(report["questions"][0]["all_verified"])

    def test_an_empty_slug_sequence_refuses_where_the_cli_cannot_be_asked(self):
        # ``--slug`` is argparse-required, so the CLI can never reach this refusal;
        # the seam can, and a host that passes an empty sequence must get the coded
        # refusal rather than a whole-workspace verification it did not ask for.
        with Workspace.open(self.shared) as ws:
            with self.assertRaises(errors.QuestionError) as caught:
                ws.grounding.verify([])
        self.assertEqual("SLUG_INVALID", caught.exception.error_code)


class AuditTrailEqualityTests(LibraryOperationFixture):
    """AC-1: the two doors must leave the same workspace state and the same log.

    Document equality is only half the promise. A host that blocks a question
    in-process and one that blocks it from a shell must leave a workspace an
    auditor cannot tell apart -- otherwise the trail this package exists to keep
    honest would record only the callers who came through the command line.
    """

    TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")

    def masked(self, text: str) -> str:
        return self.TIMESTAMP.sub("<timestamp>", text)

    def test_claim_then_block_leaves_identical_state_through_both_doors(self):
        cli_root, api_root = self.twin("audit")
        run = run_script(
            "question_claim.py", str(cli_root),
            ("claim", "--slug", "claim-me", "--agent-id", HOLDER, "--format", "json"),
            cwd=self.scratch,
        )
        self.assertEqual(0, run.returncode, run.stderr)
        run = run_script(
            "question_resolve.py", str(cli_root),
            (
                "block", "--slug", "claim-me", "--agent-id", HOLDER,
                "--blocked-reason", "Awaiting the vendor spec.", "--format", "json",
            ),
            cwd=self.scratch,
        )
        self.assertEqual(0, run.returncode, run.stderr)

        with Workspace.open(api_root) as ws:
            ws.questions.claim(slug="claim-me", agent_id=HOLDER)
            ws.questions.block(
                slug="claim-me", agent_id=HOLDER, blocked_reason="Awaiting the vendor spec."
            )

        for relative in ("log.md", "wiki/questions/claim-me.md"):
            with self.subTest(file=relative):
                self.assertEqual(
                    self.masked((cli_root / relative).read_text(encoding="utf-8")),
                    self.masked((api_root / relative).read_text(encoding="utf-8")),
                    f"{relative} differs between the CLI and the API",
                )


class StructuralTranslationTests(unittest.TestCase):
    """A refusal must be recognized by *shape*, never by class identity.

    The module loader isolates every sibling stem on each load, ``_script_errors``
    included, so each loaded script owns its own ``ScriptRefusal`` class object. A
    package-side ``except SomeImportedScriptRefusal`` would compile, read as though
    it handled the case, and catch nothing at runtime -- defeating AC-2, that every
    documented ``error_code`` is reachable as a typed exception. These cases fail
    if anyone reintroduces identity-based catching.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir(parents=True)
        (self.root / "research.yml").write_text("project:\n  name: structural\n", encoding="utf-8")
        self.ws = Workspace.open(self.root)
        self.addCleanup(self.ws.close)

    def load(self, stem: str):
        return _script_host.load_packaged_script(_script_host.shared_assets_root(), stem)

    def test_the_premise_each_script_owns_its_own_refusal_class(self):
        coverage = self.load("coverage_manifest")
        verify = self.load("verify_quotes")
        self.assertIsNot(
            coverage._script_errors.ScriptRefusal,
            verify.ScriptRefusal,
            "if these become one object the loader changed and this suite's premise needs re-deriving",
        )

    def test_a_foreign_refusal_is_translated_although_isinstance_would_miss_it(self):
        coverage = self.load("coverage_manifest")
        verify = self.load("verify_quotes")

        with self.assertRaises(errors.QuestionError) as caught:
            self.ws.coverage.evaluate("../escape")

        original = caught.exception.__cause__
        self.assertIsNotNone(original, "the typed error should chain from the refusal it translated")
        self.assertEqual("SLUG_INVALID", caught.exception.error_code)
        # The refusal really did come from the isolated module...
        self.assertIsInstance(original, coverage.CoverageManifestError)
        # ...and an `except` arm naming any *other* script's ScriptRefusal -- which
        # is all package code could ever import -- would not have caught it.
        self.assertNotIsInstance(original, verify.ScriptRefusal)

    def test_a_refusal_shape_alone_is_enough(self):
        """No relation to any ``ScriptRefusal``: only ``to_envelope`` and ``error_code``."""

        class Impostor(Exception):
            error_code = "COVERAGE_MANIFEST_INVALID"

            def to_envelope(self):
                return {
                    "schema_version": "1.0",
                    "error_code": "COVERAGE_MANIFEST_INVALID",
                    "message": "shaped like a refusal, related to nothing",
                    "recoverable": True,
                    "remediation": "Fix the manifest.",
                }

        def loader(stem: str):
            del stem
            raise Impostor

        with self.assertRaises(errors.CoverageError) as caught:
            _base.call_seam(loader, "coverage_manifest", "run_evaluate", self.root)
        self.assertEqual("COVERAGE_MANIFEST_INVALID", caught.exception.error_code)

    def test_an_ordinary_exception_is_not_dressed_up_as_a_workspace_error(self):
        def loader(stem: str):
            del stem
            raise ValueError("a bug in the caller")

        with self.assertRaises(ValueError):
            _base.call_seam(loader, "coverage_manifest", "run_evaluate", self.root)

    def test_is_refusal_and_the_package_predicate_agree(self):
        """The packaged asset is the specification; the package mirrors it, not imports it."""
        script_errors = self.load("_script_errors")
        coverage = self.load("coverage_manifest")
        for candidate in (
            coverage.CoverageManifestError("X_CODE", "boom"),
            ValueError("boom"),
            SystemExit("plain funnel"),
            SystemExit(3),
        ):
            with self.subTest(candidate=type(candidate).__name__):
                self.assertEqual(
                    script_errors.is_refusal(candidate),
                    _base.refusal_envelope(candidate) is not None,
                )


class SystemExitContainmentTests(unittest.TestCase):
    """A library must never let ``SystemExit`` reach its caller.

    Inside an ASGI worker a stray ``SystemExit`` terminates the process. Several
    workspace scripts still funnel fatal conditions through ``SystemExit(str)``,
    and a few raise one *outside* the ``try`` their seam wraps, so this is a live
    path rather than a hypothetical one. The split is deliberate and matches
    ``ScriptRefusal.from_system_exit``: a string code is a refusal and is typed; a
    non-string code is process control and is re-raised untouched.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir(parents=True)
        (self.root / "research.yml").write_text("project:\n  name: exits\n", encoding="utf-8")
        self.ws = Workspace.open(self.root)
        self.addCleanup(self.ws.close)

    def loader_raising(self, exc: BaseException) -> Callable[[str], Any]:
        real = self.ws._script

        def loader(stem: str):
            if stem == "_script_errors":
                return real(stem)
            raise exc

        return loader

    def test_a_string_system_exit_is_classified_exactly_as_the_cli_classifies_it(self):
        script_errors = _script_host.load_packaged_script(_script_host.shared_assets_root(), "_script_errors")
        for message in (
            "Missing config: research.yml not found",
            "Missing sibling workspace script: question_status.py",
            "Missing packaged script: /nowhere/coverage_manifest.py",
            "pyyaml is required for this script",
        ):
            with self.subTest(message=message):
                with self.assertRaises(errors.EvidenceWikiError) as caught:
                    _base.call_seam(self.loader_raising(SystemExit(message)), "any", "run_any")
                self.assertEqual(script_errors.classify_error_code(message), caught.exception.error_code)
                self.assertEqual(message, caught.exception.message)
                self.assertNotIsInstance(caught.exception, SystemExit)

    def test_a_non_string_system_exit_is_process_control_and_is_left_alone(self):
        for code in (0, 3, None):
            with self.subTest(code=code):
                with self.assertRaises(SystemExit) as caught:
                    _base.call_seam(self.loader_raising(SystemExit(code)), "any", "run_any")
                self.assertEqual(code, caught.exception.code)

    def test_a_refusal_that_is_also_a_system_exit_does_not_escape_as_one(self):
        """``IntakeValidationError(ScriptRefusal, SystemExit)`` is the real dual-inherited case.

        It is deliberately both -- ``serve_mcp.py`` sorts it with ``except
        SystemExit`` -- which makes it the one refusal that could leave the
        library as a process exit if the handler order here were ever reversed.
        Raised directly rather than provoked, because the workspace state that
        provokes it (a signed handoff whose signature fails) is beside the point:
        what matters is which arm catches the class.
        """
        intake = _script_host.load_packaged_script(_script_host.shared_assets_root(), "intake_questions")
        self.assertTrue(issubclass(intake.IntakeValidationError, SystemExit))
        self.assertTrue(issubclass(intake.IntakeValidationError, Exception))
        refusal = intake.IntakeValidationError(
            "Handoff signature verification failed.",
            error_code="HANDOFF_SIGNATURE_INVALID",
            details={"reason": "digest mismatch"},
        )
        self.assertIsNotNone(_base.refusal_envelope(refusal), "a dual-inherited refusal must be recognized")

        try:
            _base.call_seam(self.loader_raising(refusal), "intake_questions", "run_intake")
        except errors.IntakeError as exc:
            self.assertNotIsInstance(exc, SystemExit)
            self.assertEqual("HANDOFF_SIGNATURE_INVALID", exc.error_code)
            self.assertEqual({"reason": "digest mismatch"}, exc.details)
        except SystemExit:  # pragma: no cover - the failure this test exists to catch
            self.fail("a SystemExit escaped the library and would have killed a host process")

    def test_an_unloadable_script_reaches_the_host_typed(self):
        # ``_script_host`` reports a missing packaged script as SystemExit(str),
        # and that happens *inside* the guarded region because loading is part of
        # the call rather than a step before it.
        with self.assertRaises(errors.ConfigError) as caught:
            self.ws.coverage._call("no_such_packaged_script", "run_anything", str(self.root))
        self.assertEqual("TOOLING_MISSING", caught.exception.error_code)


class ClosedHandleTests(unittest.TestCase):
    """Every operation on a closed handle refuses, including the namespaced ones."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir(parents=True)
        (self.root / "research.yml").write_text("project:\n  name: closed\n", encoding="utf-8")

    def test_closed_handle_refuses_every_operation_this_unit_added(self):
        ws = Workspace.open(self.root)
        ws.close()
        operations = {
            "status": lambda: ws.status(),
            "coverage.evaluate": lambda: ws.coverage.evaluate("anything"),
            "grounding.verify": lambda: ws.grounding.verify(["anything"]),
            "questions.claim": lambda: ws.questions.claim(slug="a", agent_id="b"),
            "questions.release": lambda: ws.questions.release(slug="a", agent_id="b"),
            "questions.answer": lambda: ws.questions.answer(slug="a", agent_id="b", answer_page="p.md"),
            "questions.block": lambda: ws.questions.block(slug="a", agent_id="b", blocked_reason="r"),
            "questions.defer": lambda: ws.questions.defer(slug="a", agent_id="b", reason="r"),
            "questions.reject": lambda: ws.questions.reject(slug="a", agent_id="b", reason="r"),
            "questions.reopen": lambda: ws.questions.reopen(slug="a", agent_id="b", source_id=["s"]),
            "questions.approve": lambda: ws.questions.approve(slug="a", reviewer="r"),
            "questions.review": lambda: ws.questions.review(
                slug="a", policy="p", verdict="accepted", reviewed_by="r"
            ),
            "questions.set_grounding": lambda: ws.questions.set_grounding(
                slug="a", agent_id="b", from_file="g.yml"
            ),
            "questions.add_batch": lambda: ws.questions.add_batch(from_file="b.yaml"),
        }
        for label, call in operations.items():
            with self.subTest(operation=label):
                with self.assertRaises(errors.ConfigError) as caught:
                    call()
                self.assertEqual("WORKSPACE_UNREADABLE", caught.exception.error_code)


if __name__ == "__main__":
    unittest.main()
