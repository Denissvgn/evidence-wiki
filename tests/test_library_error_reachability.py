"""AC-2's other half: every code named here is *reached* through the live API.

``tests/test_library_errors.py`` proves the registry is complete -- every key in
the packaged ``_script_errors._REMEDIATIONS`` maps to a typed family, and an
envelope round-trips into it. That is necessary and not sufficient. A code can be
mapped perfectly and still be unreachable: a refusal that escapes the facade
untranslated, a condition the CLI can provoke and the API cannot, a ``SystemExit``
that leaves as a ``SystemExit``. None of that is visible from the registry.

So this module triggers the genuine condition -- a real workspace, a real
``Workspace`` call, no stand-in for the refusing script -- and holds what escapes
to three properties:

1. it is an instance of the **package** family class (not the workspace script's
   own same-named refusal, which is what a broken translation would let through);
2. its ``error_code`` is exactly the documented string;
3. it is **not** a ``SystemExit``. Two real refusal classes -- ``ExportRefusal``
   and ``IntakeValidationError`` -- inherit ``SystemExit`` deliberately, so a
   library that stopped containing them would kill an ASGI worker rather than
   return an error. ``INTAKE_TOTAL_CAP_EXCEEDED`` below is one of those.

Only (1) and (3) bite if the facade stops translating: the raw refusal carries the
same ``error_code`` string, so asserting the code alone would keep passing over a
completely broken seam. That is why the family assertion is not decoration.

Where the same condition is reachable from a shell, the packaged script is run as
a subprocess and its stderr envelope compared against the exception -- the two
doors agreeing on ``error_code`` is the whole point of the seam design.

**A sibling module rather than an extension of ``test_library_errors.py``.** That
suite opens no workspace, spawns no subprocess and runs in milliseconds; every
case here builds a workspace, and one of them (``LOCK_UNAVAILABLE``) deliberately
spends ten seconds waiting out a real lock timeout. Merging them would make the
fast registry check pay the slow suite's cost for no gain in either.

**Runtime.** ``LockReachabilityTests`` takes ~10s: the workspace mutation lock's
own bounded wait is what a second writer actually experiences, and there is no
supported knob to shorten it (``EVIDENCE_WIKI_SINGLE_WRITER`` bypasses the refusal
rather than hurrying it). ``OrchestrationReachabilityTests`` spawns the deployed
controller a few times, ~4s -- its two contended-driver cases cost about a second
between them and wait for nothing, because CR-8 made a contended *session* lock
refuse immediately instead of queueing. The ten seconds above belongs to the
question lock and to nothing else. Everything else is sub-second.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import _script_host, errors  # noqa: E402
from evidence_wiki._facades import orchestrate as orchestrate_facade  # noqa: E402
from evidence_wiki.workspace import Workspace  # noqa: E402

SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "madrid-autonomo-workspace"

AGENT = "agent-reachability"
OTHER = "agent-other"
VERIFIER = "verifier-reachability"

#: A source the fixture's manifest already knows, so grounding may cite it.
SOURCE_ID = "web:aeat-census"
SAFE_SOURCE_ID = "web--aeat-census"
RECORD_BODY = "The census filing path is Modelo 036."

ANSWER_PAGE = "wiki/outputs/api-answer.md"

#: The family each reached code must arrive as. Written out rather than looked up
#: from ``errors.error_class_for`` so that a registry edit which quietly re-homes a
#: code cannot make these cases agree with it by construction; the two are
#: compared in :meth:`MatrixTests.test_the_declared_matrix_agrees_with_the_registry`.
EXPECTED_FAMILY: dict[str, type[errors.EvidenceWikiError]] = {
    "CONFIG_MISSING": errors.ConfigError,
    "WORKSPACE_UNREADABLE": errors.ConfigError,
    "TOOLING_MISSING": errors.ConfigError,
    "LOCK_UNAVAILABLE": errors.LockError,
    "CLAIM_HELD": errors.ClaimError,
    "SLUG_UNKNOWN": errors.QuestionError,
    "QUESTION_UNKNOWN": errors.QuestionError,
    "COVERAGE_REQUIRED": errors.CoverageError,
    "COVERAGE_BLOCKED": errors.CoverageError,
    "GROUNDING_QUOTE_INVALID": errors.GroundingError,
    "GROUNDING_ANCHOR_INVALID": errors.GroundingError,
    "INTAKE_TOTAL_CAP_EXCEEDED": errors.IntakeError,
    "ORCHESTRATION_WORKSPACE_UNSAFE": errors.OrchestrationError,
    "ORCHESTRATION_DRIVER_BUSY": errors.OrchestrationError,
}


def load_script_module(name: str, filename: str):
    """Load one packaged script under a private module name."""
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - fixture guard
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


#: Wall-clock stamp the competing driver publishes in its holder block. Frozen
#: rather than generated so the block a case published and the block that came
#: back out of the JSON envelope can be compared whole.
HOLDER_ACQUIRED_AT = "2026-08-10T09:15:00Z"


class ContendedSession(NamedTuple):
    """A live orchestration session with a second driver holding its lock."""

    root: Path
    session: Any


def question_batch(*slugs: str) -> str:
    questions = [{"id": slug, "question": f"Reachability question {slug}?", "priority": "high"} for slug in slugs]
    return json.dumps({"schema_version": "1.0", "questions": questions})


class ReachabilityAsserts:
    """The three AC-2 assertions, and the CLI comparison, in one place.

    Deliberately not a ``TestCase``: subclassing one that carried tests would
    re-run them under every child class.
    """

    def reached(self, error_code: str, call: Callable[[], Any]) -> errors.EvidenceWikiError:
        """Drive ``call``, then judge whatever escaped.

        ``BaseException`` is caught rather than ``assertRaises``-ed on the
        expected class, because *which* class escaped is the finding: a
        ``SystemExit`` reaching here must be reported as the containment failure
        it is, not as an unrelated test error.
        """
        raised: BaseException | None = None
        try:
            call()
        except BaseException as exc:  # noqa: BLE001 - inspecting what escaped is the test
            raised = exc
        self.assertIsNotNone(raised, f"{error_code}: the call returned instead of refusing")
        self.assertNotIsInstance(
            raised,
            SystemExit,
            f"{error_code}: SystemExit escaped the library and would have killed a host process",
        )
        self.assertIsInstance(
            raised,
            EXPECTED_FAMILY[error_code],
            f"{error_code}: reached the host as {type(raised).__name__}, not the documented family",
        )
        self.assertEqual(error_code, raised.error_code)
        return raised

    def cli_refusal(self, script: str, root: Path, argv: Sequence[str]) -> tuple[dict[str, Any], int]:
        """Return the stderr error envelope the packaged script prints, and its exit status."""
        completed = subprocess.run(  # noqa: S603
            [sys.executable, str(SCRIPTS / script), "--project-root", str(root), *argv],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, completed.returncode, f"{script} {' '.join(argv)}: the CLI did not refuse")
        self.assertNotIn("Traceback", completed.stderr, f"{script}: the CLI crashed instead of refusing")
        return json.loads(completed.stderr), completed.returncode

    def agrees_with_the_cli(
        self,
        raised: errors.EvidenceWikiError,
        script: str,
        root: Path,
        argv: Sequence[str],
    ) -> None:
        """Hold the exception and the CLI's envelope to the same coded refusal.

        ``message`` and ``details`` are left out on purpose: both legitimately
        carry absolute paths and wall-clock stamps. The code, its recoverability,
        its remediation and the status the CLI exits with are what a host branches
        on, and those must agree exactly or the two doors are not one operation.
        """
        envelope, returncode = self.cli_refusal(script, root, argv)
        self.assertEqual(envelope["error_code"], raised.error_code)
        self.assertEqual(envelope["recoverable"], raised.recoverable)
        self.assertEqual(envelope["remediation"], raised.remediation)
        self.assertEqual(returncode, raised.exit_code)


class PreparedWorkspace(ReachabilityAsserts):
    """One populated workspace, built once, copied per case that mutates.

    Built on ``tests/fixtures/madrid-autonomo-workspace`` rather than by running
    ``init``: the fixture already carries a valid ``research.yml``, a source
    manifest, a coverage manifest and question pages, so the only preparation
    left is the state each case needs -- and it is prepared through the API's own
    operations wherever an operation exists for it.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.scratch = Path(cls._tmp.name)
        cls.prepared = cls.scratch / "prepared"
        shutil.copytree(FIXTURE, cls.prepared)

        cls.batch_path = cls.scratch / "batch.json"
        cls.batch_path.write_text(
            question_batch(
                "claim-me",
                "lock-me",
                "coverage-absent",
                "coverage-blocked",
                "quote-me",
                "anchor-mismatch",
                "anchor-unresolved",
            ),
            encoding="utf-8",
        )

        cls._write_answer_page()
        cls._write_normalized_evidence()
        cls._write_grounding_documents()

        with Workspace.open(cls.prepared) as ws:
            ws.questions.add_batch(from_file=str(cls.batch_path))
            for slug in ("coverage-absent", "coverage-blocked", "quote-me", "anchor-mismatch", "anchor-unresolved"):
                ws.questions.claim(slug=slug, agent_id=AGENT)
            ws.questions.set_grounding(slug="quote-me", agent_id=AGENT, from_file=str(cls.quote_document))
            ws.questions.set_grounding(
                slug="anchor-mismatch", agent_id=AGENT, from_file=str(cls.anchor_mismatch_document)
            )
            ws.questions.set_grounding(
                slug="anchor-unresolved", agent_id=AGENT, from_file=str(cls.anchor_unresolved_document)
            )

        cls._write_blocked_coverage_manifest()

        # Every case that only refuses shares this copy: a refusal writes nothing
        # by construction, so they cannot disturb each other or the original.
        cls.shared = cls.scratch / "shared"
        shutil.copytree(cls.prepared, cls.shared)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @classmethod
    def _write_answer_page(cls) -> None:
        page = cls.prepared / ANSWER_PAGE
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            "---\ntype: output\ncreated: '2026-08-10'\nupdated: '2026-08-10'\nsource_ids: []\n---\n\nAn answer page.\n",
            encoding="utf-8",
        )

    @classmethod
    def _write_normalized_evidence(cls) -> None:
        """A normalized record plus its structured view for the two grounding forms.

        The fixture ships the manifest entry for ``web:aeat-census`` but no
        normalized record, and both grounding forms need one: without it every
        entry fails as ``source_not_normalized``, which would make the quote case
        pass for the wrong reason -- absence of evidence rather than evidence that
        contradicts the claim.
        """
        normalized = cls.prepared / "sources" / "normalized"
        normalized.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"census": {"registration_form": "Modelo 036"}}, indent=2, sort_keys=True)
        data = payload.encode("utf-8") + b"\n"
        (normalized / f"{SAFE_SOURCE_ID}.structured.json").write_bytes(data)
        record = {
            "type": "normalized_source",
            "source_id": SOURCE_ID,
            "title": "AEAT census",
            "structured_view": {
                "path": f"sources/normalized/{SAFE_SOURCE_ID}.structured.json",
                "content_hash": f"sha256:{hashlib.sha256(data).hexdigest()}",
            },
        }
        (normalized / f"{SAFE_SOURCE_ID}.md").write_text(
            "---\n" + yaml.safe_dump(record, sort_keys=False) + f"---\n\n# AEAT census\n\n{RECORD_BODY}\n",
            encoding="utf-8",
        )

    @classmethod
    def _write_grounding_documents(cls) -> None:
        cls.quote_document = cls.scratch / "grounding-quote.yml"
        cls.quote_document.write_text(
            yaml.safe_dump(
                {
                    "grounding": [
                        {
                            "claim": "The census filing path is Modelo 037.",
                            "source_id": SOURCE_ID,
                            "quote": "The census filing path is Modelo 037.",
                        }
                    ]
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        cls.anchor_mismatch_document = cls.scratch / "grounding-anchor-mismatch.yml"
        cls.anchor_mismatch_document.write_text(
            yaml.safe_dump(
                {
                    "grounding": [
                        {
                            "claim": "The census filing form is Modelo 037.",
                            "source_id": SOURCE_ID,
                            "anchor": {"pointer": "census/registration_form", "expected": "Modelo 037"},
                        }
                    ]
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        cls.anchor_unresolved_document = cls.scratch / "grounding-anchor-unresolved.yml"
        cls.anchor_unresolved_document.write_text(
            yaml.safe_dump(
                {
                    "grounding": [
                        {
                            "claim": "The census filing fee is 0 EUR.",
                            "source_id": SOURCE_ID,
                            "anchor": {"pointer": "census/filing_fee", "expected": "0 EUR"},
                        }
                    ]
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    @classmethod
    def _write_blocked_coverage_manifest(cls) -> None:
        """Give ``coverage-blocked`` the fixture's own already-blocked manifest.

        Reusing the fixture's document rather than hand-rolling one keeps the
        blocked verdict a property of state the package produces: its facets name
        blocking source requests the fixture's request store actually holds.
        """
        source = cls.prepared / "sources" / "coverage" / "autonomo-madrid.yml"
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
        document["question_slug"] = "coverage-blocked"
        (cls.prepared / "sources" / "coverage" / "coverage-blocked.yml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )

    def copy(self, name: str) -> Path:
        """A private copy of the prepared workspace, for a case that mutates."""
        target = self.scratch / name
        if target.exists():  # pragma: no cover - only on a re-run within one process
            shutil.rmtree(target)
        shutil.copytree(self.prepared, target)
        return target

    def open_shared(self) -> Workspace:
        handle = Workspace.open(self.shared)
        self.addCleanup(handle.close)
        return handle


class HandleReachabilityTests(ReachabilityAsserts, unittest.TestCase):
    """The two codes ``Workspace.open`` and the closed-handle guard own."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_config_missing_is_reached_by_opening_a_directory_without_research_yml(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        raised = self.reached("CONFIG_MISSING", lambda: Workspace.open(bare))
        self.assertEqual("research.yml", raised.details.get("expected"))

    def test_workspace_unreadable_is_reached_by_opening_a_non_directory(self):
        not_a_directory = self.tmp / "research.yml"
        not_a_directory.write_text("project:\n  name: not a workspace root\n", encoding="utf-8")
        self.reached("WORKSPACE_UNREADABLE", lambda: Workspace.open(not_a_directory))
        self.reached("WORKSPACE_UNREADABLE", lambda: Workspace.open(self.tmp / "definitely" / "absent"))

    def test_workspace_unreadable_is_reached_by_calling_a_closed_handle(self):
        root = self.tmp / "ws"
        root.mkdir()
        (root / "research.yml").write_text("project:\n  name: closed\n", encoding="utf-8")
        handle = Workspace.open(root)
        handle.close()
        # Both doors onto the scripts: the handle's own methods and a namespace's.
        self.reached("WORKSPACE_UNREADABLE", handle.export_answers)
        self.reached("WORKSPACE_UNREADABLE", lambda: handle.questions.claim(slug="anything", agent_id=AGENT))


class QuestionReachabilityTests(PreparedWorkspace, unittest.TestCase):
    """Unknown slugs and a contested claim, driven through ``ws.questions``."""

    def test_slug_unknown_is_reached_by_claiming_a_question_that_does_not_exist(self):
        raised = self.reached(
            "SLUG_UNKNOWN",
            lambda: self.open_shared().questions.claim(slug="no-such-question", agent_id=AGENT),
        )
        self.agrees_with_the_cli(
            raised,
            "question_claim.py",
            self.shared,
            ("claim", "--slug", "no-such-question", "--agent-id", AGENT, "--format", "json"),
        )

    def test_question_unknown_is_reached_by_verifying_a_question_that_does_not_exist(self):
        # A sibling code for the same mistake: the grounding verifier has its own
        # vocabulary for "no such slug", and a host must get that one from it.
        raised = self.reached(
            "QUESTION_UNKNOWN",
            lambda: self.open_shared().grounding.verify(["no-such-question"]),
        )
        self.agrees_with_the_cli(
            raised,
            "verify_quotes.py",
            self.shared,
            ("--slug", "no-such-question", "--format", "json"),
        )

    def test_claim_held_is_reached_by_two_api_claimants_on_one_question(self):
        root = self.copy("claim-conflict")
        with Workspace.open(root) as ws:
            first = ws.questions.claim(slug="claim-me", agent_id=AGENT)
            self.assertTrue(first["applied"])
            raised = self.reached(
                "CLAIM_HELD",
                lambda: ws.questions.claim(slug="claim-me", agent_id=OTHER),
            )
        # CLAIM_HELD is the one refusal that is not exit 2 and not recoverable;
        # a host that retries it would spin forever against the holder.
        self.assertFalse(raised.recoverable)
        self.assertEqual(errors.EXIT_CONFLICT, raised.exit_code)
        # The refusal names the holder the first API call installed, so the two
        # claims really did meet on one question rather than on two copies of it.
        self.assertIn(AGENT, str(raised))
        self.assertEqual(OTHER, raised.details.get("agent_id"))
        self.agrees_with_the_cli(
            raised,
            "question_claim.py",
            root,
            ("claim", "--slug", "claim-me", "--agent-id", OTHER, "--format", "json"),
        )


class CoverageReachabilityTests(PreparedWorkspace, unittest.TestCase):
    """The coverage gate on ``ws.questions.answer(require_coverage=True)``."""

    def answer(self, ws: Workspace, slug: str) -> dict[str, Any]:
        return ws.questions.answer(
            slug=slug,
            agent_id=AGENT,
            answer_page=ANSWER_PAGE,
            allow_uncited=True,
            require_coverage=True,
        )

    def cli_argv(self, slug: str) -> tuple[str, ...]:
        return (
            "answer",
            "--slug", slug,
            "--agent-id", AGENT,
            "--answer-page", ANSWER_PAGE,
            "--allow-uncited",
            "--require-coverage",
            "--format", "json",
        )

    def test_coverage_required_is_reached_when_the_manifest_is_absent(self):
        raised = self.reached("COVERAGE_REQUIRED", lambda: self.answer(self.open_shared(), "coverage-absent"))
        self.assertEqual("sources/coverage/coverage-absent.yml", raised.details.get("manifest_path"))
        self.agrees_with_the_cli(raised, "question_resolve.py", self.shared, self.cli_argv("coverage-absent"))

    def test_coverage_blocked_is_reached_when_the_manifest_does_not_pass(self):
        raised = self.reached("COVERAGE_BLOCKED", lambda: self.answer(self.open_shared(), "coverage-blocked"))
        self.assertEqual("blocked", raised.details.get("coverage_verdict"))
        self.assertTrue(raised.details.get("failed_required_facets"), "the refusal must name what blocked it")
        self.agrees_with_the_cli(raised, "question_resolve.py", self.shared, self.cli_argv("coverage-blocked"))

    def test_the_refused_answer_left_the_question_untouched(self):
        # A refusal that had already mutated the page would make every case above
        # depend on the order they run in, and would be a defect in its own right.
        page = self.shared / "wiki" / "questions" / "coverage-blocked.md"
        before = page.read_text(encoding="utf-8")
        with contextlib.suppress(errors.CoverageError):
            self.answer(self.open_shared(), "coverage-blocked")
        self.assertEqual(before, page.read_text(encoding="utf-8"))


class GroundingReachabilityTests(PreparedWorkspace, unittest.TestCase):
    """Both grounding forms, refused on the ``--write`` path they gate.

    ``ws.grounding.verify`` returns a report for a failed verification and only
    raises when it is asked to *stamp* one -- that split is the documented
    contract, so the write path is where these codes live.
    """

    def verify_argv(self, slug: str) -> tuple[str, ...]:
        return ("--slug", slug, "--write", "--verified-by", VERIFIER, "--format", "json")

    def test_a_failed_verification_alone_is_a_report_rather_than_a_refusal(self):
        # The premise of the two cases below, asserted rather than assumed: if a
        # plain verify started raising, they would pass without proving anything
        # about the write gate.
        report = self.open_shared().grounding.verify(["quote-me"])
        self.assertEqual("not_verified", report["overall_result"])
        self.assertFalse(report["questions"][0]["all_verified"])

    def test_grounding_quote_invalid_is_reached_by_a_quote_the_record_does_not_contain(self):
        raised = self.reached(
            "GROUNDING_QUOTE_INVALID",
            lambda: self.open_shared().grounding.verify(["quote-me"], write=True, verified_by=VERIFIER),
        )
        failures = raised.details.get("failures") or []
        self.assertEqual(["quote"], sorted({failure["form"] for failure in failures}))
        # The record exists and simply does not contain the quote. Without this,
        # the case would also pass for an unnormalized source -- absence of
        # evidence rather than evidence that contradicts the claim.
        self.assertEqual(["quote_not_found"], [failure["result"] for failure in failures])
        self.agrees_with_the_cli(raised, "verify_quotes.py", self.shared, self.verify_argv("quote-me"))

    def test_grounding_anchor_invalid_is_reached_by_an_anchor_that_does_not_hold(self):
        # Both anchor failures the CR-7 form distinguishes: an ``expected`` the
        # record contradicts, and a pointer that resolves to nothing. Either must
        # report the *anchor* code -- a caller told "a quote did not verify" would
        # go looking for a quote there is none of.
        for slug, expected_result in (
            ("anchor-mismatch", "anchor_value_mismatch"),
            ("anchor-unresolved", "anchor_pointer_not_found"),
        ):
            with self.subTest(slug=slug):
                raised = self.reached(
                    "GROUNDING_ANCHOR_INVALID",
                    lambda slug=slug: self.open_shared().grounding.verify(
                        [slug], write=True, verified_by=VERIFIER
                    ),
                )
                failures = raised.details.get("failures") or []
                self.assertEqual(["anchor"], sorted({failure["form"] for failure in failures}))
                self.assertEqual([expected_result], [failure["result"] for failure in failures])
                self.agrees_with_the_cli(raised, "verify_quotes.py", self.shared, self.verify_argv(slug))

    def test_the_refused_write_stamped_nothing(self):
        page = self.shared / "wiki" / "questions" / "quote-me.md"
        before = page.read_text(encoding="utf-8")
        with contextlib.suppress(errors.GroundingError):
            self.open_shared().grounding.verify(["quote-me"], write=True, verified_by=VERIFIER)
        self.assertEqual(before, page.read_text(encoding="utf-8"))
        self.assertNotIn("verified_by", before)


class IntakeReachabilityTests(PreparedWorkspace, unittest.TestCase):
    """The open-question cap, and the containment that makes it survivable.

    ``IntakeValidationError`` is ``(ScriptRefusal, SystemExit)`` on purpose, so
    this is the case where "not a ``SystemExit``" is a live assertion rather than
    a formality.
    """

    def lowered_cap_workspace(self, name: str) -> tuple[Path, Path]:
        root = self.copy(name)
        research = root / "research.yml"
        research.write_text(
            research.read_text(encoding="utf-8").replace(
                "max_open_questions_total: 250", "max_open_questions_total: 1"
            ),
            encoding="utf-8",
        )
        batch = self.scratch / f"{name}-batch.json"
        batch.write_text(question_batch("over-the-cap"), encoding="utf-8")
        return root, batch

    def test_intake_total_cap_exceeded_is_reached_by_a_batch_over_the_configured_cap(self):
        root, batch = self.lowered_cap_workspace("intake-cap")
        with Workspace.open(root) as ws:
            raised = self.reached("INTAKE_TOTAL_CAP_EXCEEDED", lambda: ws.questions.add_batch(from_file=str(batch)))
        self.assertEqual(1, raised.details.get("max_open_questions_total"))
        self.assertEqual(1, raised.details.get("new_questions"))
        self.agrees_with_the_cli(
            raised, "intake_questions.py", root, ("--from-file", str(batch), "--format", "json")
        )

    def test_the_refused_batch_created_no_pages(self):
        root, batch = self.lowered_cap_workspace("intake-cap-no-writes")
        with Workspace.open(root) as ws, contextlib.suppress(errors.IntakeError):
            ws.questions.add_batch(from_file=str(batch))
        self.assertFalse((root / "wiki" / "questions" / "over-the-cap.md").exists())


class ToolingReachabilityTests(PreparedWorkspace, unittest.TestCase):
    """A packaged script that cannot be loaded reaches the host as a config error.

    Provoked with a genuinely incomplete assets tree rather than by raising
    ``SystemExit`` by hand, so the refusal is the one ``_script_host._load_script``
    actually produces for a broken installation. The script *load* sits inside
    ``call_seam``'s guarded region precisely so this arrives typed; moving it out
    would turn a broken install into a process exit.
    """

    def incomplete_assets_root(self) -> Path:
        assets = self.scratch / "incomplete-assets"
        if assets.exists():  # pragma: no cover - only on a re-run within one process
            shutil.rmtree(assets)
        scripts = assets / "workspace-template" / "scripts"
        scripts.mkdir(parents=True)
        # The loader and the error vocabulary survive; the operation's own script
        # does not. That is what a truncated or partially installed wheel looks like.
        for name in ("_workspace_module_loader.py", "_script_errors.py"):
            shutil.copy2(SCRIPTS / name, scripts / name)
        return assets

    def test_tooling_missing_is_reached_when_the_operations_script_is_not_installed(self):
        assets = self.incomplete_assets_root()
        handle = self.open_shared()
        with unittest.mock.patch.object(_script_host, "shared_assets_root", return_value=assets):
            raised = self.reached("TOOLING_MISSING", handle.export_answers)
        self.assertIn("export_answers.py", str(raised))


class LockReachabilityTests(PreparedWorkspace, unittest.TestCase):
    """The workspace mutation lock, held by a competing writer.

    **Slow on purpose (~10s).** ``workspace_lock`` waits its bounded timeout
    before refusing, and that wait *is* the condition under test: a host whose
    second writer gave up sooner would see a different failure. There is no
    supported way to shorten it -- ``EVIDENCE_WIKI_SINGLE_WRITER`` bypasses the
    refusal rather than hurrying it, and shortening it by patching the lock would
    replace the thing being tested.

    The ten seconds is this lock's alone. The per-session *driver* lock runs the
    same module against a different file, but since CR-8 it is acquired with
    ``timeout_seconds=0`` and refuses the moment it finds a holder, so its cases
    in ``OrchestrationReachabilityTests`` below cost nothing and report
    ``ORCHESTRATION_DRIVER_BUSY`` rather than the ``LOCK_UNAVAILABLE`` reached
    here. Two locks, two codes, two very different waits.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.locks = load_script_module("library_reachability_locks", "_workspace_locks.py")

    def test_lock_unavailable_is_reached_while_another_writer_holds_the_question_lock(self):
        root = self.copy("lock-contention")
        page = root / "wiki" / "questions" / "lock-me.md"
        lock_path = page.parent / ".locks" / f"{page.stem}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        held, release = threading.Event(), threading.Event()

        def hold_the_question_lock():
            with self.locks.workspace_lock(lock_path, purpose="competing writer"):
                held.set()
                release.wait(120)

        holder = threading.Thread(target=hold_the_question_lock, daemon=True)
        holder.start()
        self.addCleanup(holder.join, 30)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(30), "the competing writer never acquired the question lock")

        with Workspace.open(root) as ws:
            raised = self.reached("LOCK_UNAVAILABLE", lambda: ws.questions.claim(slug="lock-me", agent_id=OTHER))
        # A lock refusal is the one condition a host is *supposed* to retry.
        self.assertTrue(raised.recoverable)


class OrchestrationReachabilityTests(ReachabilityAsserts, unittest.TestCase):
    """``ORCHESTRATION_WORKSPACE_UNSAFE`` and ``ORCHESTRATION_DRIVER_BUSY`` through ``ws.orchestrate``.

    Needs a workspace created by ``init``: the orchestrate facade drives the
    workspace's *deployed* controller at ``<root>/scripts/``, which no fixture
    tree carries.

    ``ORCHESTRATION_WORKSPACE_UNSAFE`` is the controller's runtime guard --
    "workspace health or HIGH validation findings changed after the work order was
    issued" -- reached by making the workspace need operator attention while an
    action is pending. A question parked for human review is the cheapest way to
    move ``readiness.verdict`` to ``attention_required``, which is the same gate a
    HIGH lint finding trips; see
    :meth:`test_a_workspace_needing_attention_before_the_route_is_chosen_is_an_answer`
    for why the *timing* rather than the finding is what makes it a refusal. This
    is a different code path from ``tests/test_library_orchestrate.py``'s
    symlinked-question case, which reaches the same code through the question-file
    integrity guard.

    ``ORCHESTRATION_DRIVER_BUSY`` is CR-8's refusal of a second driver on one
    session, and it is here rather than in ``LockReachabilityTests`` because the
    interesting part is not the lock. It is that a refusal invented in the
    *workspace's* controller, carried out of a subprocess as JSON, and never
    mentioned in this package's own remediation table, still arrives as a typed
    ``OrchestrationError`` with its holder block intact -- and that the brand-new
    process exit status the controller pairs it with changes none of that.
    """

    @classmethod
    def setUpClass(cls):
        cls.init = load_script_module("library_reachability_init", "init_research_workspace.py")
        cls.intake = load_script_module("library_reachability_intake", "intake_questions.py")
        cls.locks = load_script_module("library_reachability_driver_locks", "_workspace_locks.py")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def workspace(self, name: str) -> Path:
        target = self.tmp / name
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.init.main(
                [
                    "--target", str(target),
                    "--project-name", "library-reachability",
                    "--project-description", "Live-path error reachability workspace.",
                ]
            )
        self.assertEqual(0, int(code or 0), "fixture workspace failed to initialize")
        batch = self.tmp / f"{name}-batch.json"
        batch.write_text(question_batch("work-me", "park-me"), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.intake.main(
                ["--project-root", str(target), "--from-file", str(batch), "--format", "json"]
            )
        self.assertEqual(0, int(code or 0), "fixture questions failed to intake")
        return target

    def park_for_review(self, target: Path, slug: str) -> None:
        """Record the frontmatter a coverage-gated answer writes when it parks a question."""
        page = target / "wiki" / "questions" / f"{slug}.md"
        text = page.read_text(encoding="utf-8")
        self.assertIn("status: open", text)
        page.write_text(
            text.replace(
                "status: open",
                "status: human_review\n"
                "human_review_required: true\n"
                "human_review_status: pending\n"
                "human_review_policies:\n"
                "  - pack:fixture-pack/manual-check",
                1,
            ),
            encoding="utf-8",
        )

    def test_orchestration_workspace_unsafe_is_reached_by_next_after_the_workspace_degrades(self):
        target = self.workspace("unsafe")
        with Workspace.open(target) as ws:
            session = ws.orchestrate.start(AGENT, orchestration_id="orch-reachability")
            order = session.next()
            self.assertEqual("orchestration_work_order", order["artifact_type"])
            self.park_for_review(target, "park-me")

            raised = self.reached("ORCHESTRATION_WORKSPACE_UNSAFE", session.next)

        self.assertEqual("attention_required", raised.details.get("readiness_verdict"))
        self.assertFalse(raised.recoverable, "an unsafe workspace needs an operator, not a retry")

    def test_a_workspace_needing_attention_before_the_route_is_chosen_is_an_answer(self):
        """The correction that keeps the case above from being written the easy way.

        Degrading the workspace *before* the first ``next`` does not refuse: the
        controller chooses no route and reports a terminal ``no_ship`` session,
        which a host must read as a document. Only a workspace that degrades while
        an action is pending trips the runtime guard. Pinned here so that a later
        reader does not "simplify" the case above into one that silently stops
        reaching the code.
        """
        target = self.workspace("no-ship")
        self.park_for_review(target, "park-me")
        with Workspace.open(target) as ws:
            session = ws.orchestrate.start(AGENT, orchestration_id="orch-no-ship")
            finished = session.next()

        self.assertEqual("orchestration_session", finished["artifact_type"])
        self.assertEqual("no_ship", finished["status"])

    def competing_holder(self) -> dict[str, Any]:
        """The holder block a second driver publishes beside the session lock.

        The shape CR-8 settled on: who is holding (``agent_id``), which OS process
        is holding (``pid``, ``hostname``), what it is doing (``command``) and
        since when (``acquired_at``). Every field is here because a host that has
        just been refused wants to report the holder to an operator, and a block
        that arrives half-empty is a worse answer than no block at all.
        """
        return {
            "agent_id": OTHER,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "command": "next",
            "acquired_at": HOLDER_ACQUIRED_AT,
        }

    def hold_the_session_lock(self, target: Path, orchestration_id: str) -> None:
        """Leave a competing driver holding this session's lock for the rest of the case.

        A thread, and that is enough. The call being refused runs inside the
        *deployed controller's* subprocess, so the advisory lock this thread holds
        is held by a different process as far as the refusing code can tell --
        which is the whole reason the refusal happens at all. Spawning a second
        interpreter to own a file descriptor would cost a second and prove the
        same thing.

        The lock is the workspace's own ``.locks/session.lock``, the file the
        controller takes around ``start``, ``next`` and ``submit``, acquired
        through the packaged ``workspace_lock`` with a holder block exactly as a
        real driver acquires it. Nothing here stands in for the contention.
        """
        lock_path = target / "runs" / "orchestrations" / orchestration_id / ".locks" / "session.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        held, release = threading.Event(), threading.Event()

        def hold_it():
            with self.locks.workspace_lock(
                lock_path, purpose="competing driver", holder=self.competing_holder()
            ):
                held.set()
                release.wait(120)

        driver = threading.Thread(target=hold_it, daemon=True)
        driver.start()
        # Cleanups run last-in first-out, so the release is signalled before the
        # join waits on it; the other order would block for the full 120s.
        self.addCleanup(driver.join, 30)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(30), "the competing driver never acquired the session lock")

    def contended_session(self, name: str) -> ContendedSession:
        """A started session whose lock a second driver already holds."""
        target = self.workspace(name)
        handle = Workspace.open(target)
        self.addCleanup(handle.close)
        session = handle.orchestrate.start(AGENT, orchestration_id=f"orch-{name}")
        self.hold_the_session_lock(target, session.orchestration_id)
        return ContendedSession(target, session)

    def test_orchestration_driver_busy_is_reached_by_next_while_a_second_driver_holds_the_session(self):
        """CR-8's contention refusal, reached through the library rather than a shell.

        The code exists nowhere in this package: it is minted by the workspace's
        deployed controller, absent from ``_script_errors._REMEDIATIONS``, and
        reaches a host only because ``error_class_for`` maps the
        ``ORCHESTRATION_`` prefix. That is precisely the arrangement this module
        exists to distrust -- a mapping that is right on paper proves nothing
        about a code that never survives the seam -- so the condition is provoked
        for real and the exception is judged on what arrives.
        """
        contended = self.contended_session("driver-busy")

        raised = self.reached("ORCHESTRATION_DRIVER_BUSY", contended.session.next)

        # Contention is the one orchestration refusal a host is *supposed* to
        # retry. ``ORCHESTRATION_WORKSPACE_UNSAFE`` above is the one it must not,
        # and before CR-8 both arrived under codes that could not tell them apart.
        self.assertTrue(raised.recoverable)
        # The holder block survives the JSON envelope the subprocess boundary
        # forces it through. Compared whole rather than key by key: a field that
        # goes missing, or a pid that comes back as a string, is a narrowing of
        # what a host can report about the driver blocking it, and would pass a
        # per-key check written against the fields someone happened to think of.
        self.assertEqual(self.competing_holder(), raised.details.get("holder"))
        self.assertEqual(contended.session.orchestration_id, raised.details.get("orchestration_id"))
        # The message names the holder too, so an operator reading one log line
        # learns who is busy without reaching into ``details``.
        self.assertIn(OTHER, str(raised))

    def test_the_controllers_new_exit_status_classifies_nothing_and_does_not_reach_the_exception(self):
        """Exit 5 is invisible to the facade: on purpose in one direction, by accident in the other.

        CR-8 gave contention its own process exit status -- ``EXIT_DRIVER_BUSY``,
        5 -- the first status this package's orchestration path had ever seen from
        the controller. Two questions follow, with different answers.

        **Classification.** The exit status must not decide *what happened*.
        ``orchestration._controller_json`` raises an ``OrchestrationHostError``
        carrying whatever envelope the child printed, and the facade's
        ``_typed_error`` returns ``error_from_envelope(envelope)`` whenever there
        is one, falling back to ``ORCHESTRATION_HOST_FAILED`` only when there is
        not and to ``ORCHESTRATION_HOST_EXITED`` only for a ``SystemExit``. So an
        unrecognized status cannot demote a coded refusal into a host error. This
        case asserts that against the status actually observed from a shell, 5,
        because "an unknown status changes nothing" is worth nothing if the status
        under test was 2 the whole time.

        **The reported ``exit_code`` is 5, matching both shells.** That agreement
        is not automatic. No error envelope in this package carries an ``exit_code``
        key -- ``_script_errors.error_envelope`` never emits one -- so
        ``errors.error_from_envelope`` reconstructs the status from the static
        ``errors._EXIT_CODE_OVERRIDES`` table. ``ORCHESTRATION_DRIVER_BUSY`` was the
        first refusal in the workspace scripts to exit with a status that table had
        not been told about, and until it was added there the exception reported 2
        while the controller exited 5 -- breaking ``docs/library-api.md``'s promise
        that the attribute is "the status the CLI would have exited with". This case
        is what keeps the entry honest: delete it from ``_EXIT_CODE_OVERRIDES`` and
        the equality below fails.

        (The controller's other non-zero statuses, 3 ``EXIT_BLOCKED`` and 4
        ``EXIT_PAUSED``, need no entry: they are not refusals at all. They accompany
        a *session document* on stdout and are returned rather than raised, so no
        exception ever carried them -- see ``test_library_orchestrate.py::
        test_a_terminal_or_paused_session_is_returned_on_a_non_zero_exit``.)
        """
        contended = self.contended_session("driver-busy-exit-status")

        raised = self.reached("ORCHESTRATION_DRIVER_BUSY", contended.session.next)
        envelope, returncode = self.cli_refusal(
            "orchestration_controller.py",
            contended.root,
            ("next", "--orchestration-id", contended.session.orchestration_id, "--format", "json"),
        )

        self.assertEqual(
            5, returncode, "the controller no longer exits EXIT_DRIVER_BUSY for a contended session"
        )
        # Everything a host branches on agrees across the two doors ...
        self.assertEqual(envelope["error_code"], raised.error_code)
        self.assertEqual(envelope["recoverable"], raised.recoverable)
        self.assertEqual(envelope["remediation"], raised.remediation)
        # ... and the status nothing has a branch for did not divert the
        # controller's own coded refusal into either of the facade's host codes.
        self.assertNotIn(
            raised.error_code,
            {orchestrate_facade.HOST_ERROR_CODE, orchestrate_facade.EXITED_ERROR_CODE},
        )
        # ... including the status itself, which is the point of the override entry:
        # a host that branches on ``exit_code`` sees what a shell would see.
        self.assertEqual(returncode, raised.exit_code)
        self.assertEqual(errors.EXIT_DRIVER_BUSY, raised.exit_code)


class MatrixTests(unittest.TestCase):
    """The declared matrix, held to the registry it claims to exercise."""

    def test_the_declared_matrix_agrees_with_the_registry(self):
        for code, family in EXPECTED_FAMILY.items():
            with self.subTest(error_code=code):
                self.assertIs(family, errors.error_class_for(code))

    def test_every_declared_code_is_one_the_package_documents(self):
        # ``ORCHESTRATION_WORKSPACE_UNSAFE`` is emitted by the controller and is
        # deliberately absent from the asset's remediation table, so it is checked
        # against the controller source instead.
        script_errors = _script_host.load_packaged_script(_script_host.shared_assets_root(), "_script_errors")
        controller = (SCRIPTS / "orchestration_controller.py").read_text(encoding="utf-8")
        for code in EXPECTED_FAMILY:
            with self.subTest(error_code=code):
                self.assertTrue(
                    code in script_errors._REMEDIATIONS or f'"{code}"' in controller,
                    f"{code} is not a code this package documents anywhere",
                )


if __name__ == "__main__":
    unittest.main()
