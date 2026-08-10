"""What the library API promises a long-lived, multi-threaded host.

The API exists so an ASGI service can hold workspace handles open and serve
concurrent requests in-process instead of spawning a subprocess per call. That
changes what can go wrong: a subprocess has its own interpreter, its own stdout
and its own copy of every module, and none of that isolation survives the move
in-process. Four properties are what make the move safe, and each has a case
here.

* **Concurrent calls on one handle are independent.** Two threads calling
  through the same handle each get a complete document, not two halves of one.
* **N handles share one assets root.** The packaged scripts are materialized once
  per process, not once per handle -- otherwise a zip install would pay a full
  asset extraction per open workspace and seed a disjoint module cache with each.
* **Contention arrives as a typed exception.** A claim another writer holds must
  refuse with ``ClaimError``/``CLAIM_HELD``. The alternative -- two writers both
  believing they hold it -- is silent workspace corruption, which is the failure
  this package exists to prevent.
* **``sys.stdout`` is never touched.** A documented API call must not redirect,
  replace or restore the host's stdout. This is checked by *object identity*
  rather than by content: a capture that is put back afterwards still breaks a
  host that captured its own stdout in the meantime, and identity is the only
  assertion that notices.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import _script_host, errors, resources  # noqa: E402
from evidence_wiki.workspace import Workspace  # noqa: E402

SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"

HOLDER = "agent-holder"
SLUGS = ("alpha", "beta", "gamma", "delta", "epsilon")

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


#: Driven in a genuinely cold interpreter by ``ColdLoadContentionTests``. Every
#: thread's first call is also the process's first call, which is when the
#: packaged scripts are loaded and ``sys.path`` is briefly mutated -- the window
#: a host serving its first concurrent requests actually lives in.
COLD_START_PROBE = """\
import sys, threading
from evidence_wiki import Workspace

root = sys.argv[1]
failures = []
barrier = threading.Barrier(8)

def run(call):
    def worker():
        barrier.wait()
        try:
            document = call()
            if not isinstance(document, dict) or "schema_version" not in document:
                failures.append(f"incomplete document: {document!r}")
        except BaseException as exc:
            failures.append(f"{type(exc).__name__}: {exc}")
    return worker

with Workspace.open(root) as ws:
    # Eight threads over five stems, each writing operation on a slug of its own
    # so the only contention under test is the load, not the workspace state.
    calls = [
        ws.status,
        ws.status,
        ws.status,
        lambda: ws.coverage.evaluate("alpha"),
        lambda: ws.grounding.verify(["alpha"]),
        lambda: ws.grounding.verify(["beta"]),
        lambda: ws.questions.claim(slug="delta", agent_id="cold-start"),
        lambda: ws.questions.claim(slug="epsilon", agent_id="cold-start"),
    ]
    threads = [threading.Thread(target=run(call)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
        if thread.is_alive():
            failures.append("a worker thread deadlocked")

if failures:
    print("\\n".join(failures))
    raise SystemExit(1)
print("COLD-START-OK")
"""


def batch_document(*slugs: str) -> str:
    questions = "".join(
        f"  - question: Concurrency question {slug}?\n    id: {slug}\n    priority: high\n" for slug in slugs
    )
    return f'schema_version: "1.0"\nquestions:\n{questions}'


def blanked(document: Any, *paths: str) -> Any:
    """Return ``document`` with per-invocation fields replaced, requiring each present."""
    result = json.loads(json.dumps(document))
    for path in paths:
        node = result
        *parents, leaf = path.split(".")
        for key in parents:
            node = node[key]
        if leaf not in node:
            raise AssertionError(f"expected {path} in the document")
        node[leaf] = "<volatile>"
    return result


class ConcurrencyFixture(unittest.TestCase):
    """One initialized workspace with questions and a coverage manifest."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.scratch = Path(cls._tmp.name)
        cls.root = cls.scratch / "workspace"
        completed = subprocess.run(  # noqa: S603
            [
                sys.executable, "-m", "evidence_wiki.cli", "init",
                "--target", str(cls.root),
                "--project-name", "concurrency",
                "--project-description", "library API concurrency fixture",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(f"fixture init failed: {completed.stderr}")

        batch = cls.scratch / "batch.yaml"
        batch.write_text(batch_document(*SLUGS), encoding="utf-8")
        cls.script("intake_questions.py", "--from-file", str(batch), "--format", "json")

        template = cls.scratch / "coverage-template.yml"
        template.write_text(COVERAGE_TEMPLATE, encoding="utf-8")
        cls.script(
            "coverage_manifest.py",
            "init", "--slug", "alpha", "--template", str(template), "--format", "json",
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @classmethod
    def script(cls, script: str, *argv: str, root: Path | None = None, check: bool = True):
        completed = subprocess.run(  # noqa: S603
            [
                sys.executable, str(SCRIPTS / script),
                "--project-root", str(cls.root if root is None else root),
                *argv,
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(cls.scratch),
        )
        if check and completed.returncode != 0:
            raise AssertionError(f"{script} {' '.join(argv)} failed: {completed.stderr}")
        return completed

    def in_threads(self, calls: list) -> list:
        """Run every call on its own thread, released together, and return their outcomes.

        A barrier rather than a bare ``start()`` loop: without one the first
        thread routinely finishes before the last is scheduled, and the test
        would pass on a code path that never actually overlapped.
        """
        barrier = threading.Barrier(len(calls))
        outcomes: list[Any] = [None] * len(calls)

        def run(index: int, call) -> None:
            barrier.wait()
            try:
                outcomes[index] = ("ok", call())
            except BaseException as exc:  # noqa: BLE001 - the outcome is the assertion subject
                outcomes[index] = ("raised", exc)

        threads = [threading.Thread(target=run, args=(index, call)) for index, call in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
            self.assertFalse(thread.is_alive(), "a worker thread did not finish; the API may have deadlocked")
        return outcomes


class ConcurrentCallTests(ConcurrencyFixture):
    """Two threads through one handle each get a whole answer."""

    def test_two_threads_calling_status_get_complete_independent_documents(self):
        with Workspace.open(self.root) as ws:
            baseline = ws.status()
            outcomes = self.in_threads([ws.status, ws.status, ws.status])

        for index, (kind, value) in enumerate(outcomes):
            with self.subTest(thread=index):
                self.assertEqual("ok", kind, f"thread {index} raised {value!r}")
                self.assertEqual(
                    blanked(baseline, "generated_at"),
                    blanked(value, "generated_at"),
                    "a concurrent status document is not the document a serial call produces",
                )

        # Distinct objects, not one shared dict handed to every caller: a host
        # that mutates its own result must not corrupt another request's.
        documents = [value for _, value in outcomes]
        self.assertEqual(len(documents), len({id(document) for document in documents}))

    def test_status_and_coverage_interleave_without_losing_either_result(self):
        with Workspace.open(self.root) as ws:
            status_baseline = ws.status()
            coverage_baseline = ws.coverage.evaluate("alpha")
            outcomes = self.in_threads(
                [
                    ws.status,
                    lambda: ws.coverage.evaluate("alpha"),
                    ws.status,
                    lambda: ws.coverage.evaluate("alpha"),
                ]
            )

        for index, (kind, value) in enumerate(outcomes):
            with self.subTest(thread=index):
                self.assertEqual("ok", kind, f"thread {index} raised {value!r}")

        for index in (0, 2):
            self.assertEqual(
                blanked(status_baseline, "generated_at"),
                blanked(outcomes[index][1], "generated_at"),
            )
        for index in (1, 3):
            self.assertEqual(
                blanked(coverage_baseline, "manifest.updated_at"),
                blanked(outcomes[index][1], "manifest.updated_at"),
            )


class SharedAssetsRootTests(ConcurrencyFixture):
    """Two handles on two different roots must enter the assets root exactly once."""

    @contextlib.contextmanager
    def counting_assets_root(self):
        """Reset the process-wide root, count entries, then restore it.

        ``shared_assets_root`` memoizes on first use, so by the time this test
        runs the rest of the suite has already entered it and the counter would
        read zero for the wrong reason. Clearing the memo first is what makes the
        assertion mean what it says.
        """
        real = resources.assets_root
        entries: list[int] = []

        @contextlib.contextmanager
        def counted():
            entries.append(1)
            with real() as root:
                yield root

        saved_stack = _script_host._SHARED_ASSETS_STACK
        saved_root = _script_host._SHARED_ASSETS_ROOT
        _script_host._SHARED_ASSETS_STACK = None
        _script_host._SHARED_ASSETS_ROOT = None
        resources.assets_root = counted
        try:
            yield entries
        finally:
            resources.assets_root = real
            test_stack = _script_host._SHARED_ASSETS_STACK
            if test_stack is not None and test_stack is not saved_stack:
                test_stack.close()
            _script_host._SHARED_ASSETS_STACK = saved_stack
            _script_host._SHARED_ASSETS_ROOT = saved_root

    def test_two_handles_on_two_roots_share_one_assets_root(self):
        second = self.scratch / "second-workspace"
        completed = subprocess.run(  # noqa: S603
            [
                sys.executable, "-m", "evidence_wiki.cli", "init",
                "--target", str(second),
                "--project-name", "concurrency-second",
                "--project-description", "a second workspace on the same process",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

        with self.counting_assets_root() as entries:
            with Workspace.open(self.root) as first_handle, Workspace.open(second) as second_handle:
                # Opening validates the workspace and touches no assets at all.
                self.assertEqual(0, len(entries))
                first_document = first_handle.status()
                second_document = second_handle.status()

            self.assertEqual(1, len(entries), "the assets root was entered more than once")

        # Two roots, two genuinely different documents off one shared extraction.
        self.assertNotEqual(
            first_document["workspace_health"]["project_root"],
            second_document["workspace_health"]["project_root"],
        )

    def test_concurrent_first_use_still_enters_the_assets_root_once(self):
        # The memo is guarded by a lock; without it two threads racing on first
        # use would each extract, and the winner's root would silently leak.
        with self.counting_assets_root() as entries:
            with Workspace.open(self.root) as ws:
                outcomes = self.in_threads([ws.status, ws.status, ws.status, ws.status])
            for kind, value in outcomes:
                self.assertEqual("ok", kind, f"a thread raised {value!r}")
            self.assertEqual(1, len(entries), "concurrent first use entered the assets root more than once")


class ColdLoadContentionTests(ConcurrencyFixture):
    """Loading a packaged script is global state, and must be loaded once.

    A workspace script is loaded by temporarily inserting its directory into
    ``sys.path`` and its siblings into ``sys.modules``, then restoring both --
    that is how a copied workspace keeps its plain sibling imports working. Both
    are process-global, so two threads doing it at once can interleave one's
    insert with the other's restore.

    The packaged loader does guard this, with a lock that lives on the loader
    *module object* -- and sibling isolation hands out copies of that object, so
    the guard excluded far less than it looked like it did. Two threads racing on
    the very first load each built their own loader module; and every script got
    its own copy too, so ``workspace_status``'s lazy sibling loads and
    ``question_claim``'s were arbitrated by two unrelated locks *while seams were
    running*. The symptom was a random ``ModuleNotFoundError`` for
    ``_workspace_module_loader`` or ``_script_errors``, or an ``AttributeError``
    against a module already torn down -- on the first concurrent calls, exactly
    the moment a freshly started host serves its first requests.
    """

    #: Modules evicted by ``cold_module_caches`` are parked here for the lifetime
    #: of the process rather than dropped. Freeing a script module clears its
    #: globals, and anything still holding one of its functions -- a sibling, a
    #: lock heartbeat thread -- then runs against ``None``. The leak is deliberate
    #: and bounded: it is a handful of modules, in a test process.
    _EVICTED: list[dict] = []

    @contextlib.contextmanager
    def cold_module_caches(self):
        """Empty the process-wide module caches, then hand them back untouched."""
        saved_scripts = dict(_script_host._SCRIPT_MODULE_CACHE)
        saved_loaders = dict(_script_host._LOADER_MODULE_CACHE)
        _script_host._SCRIPT_MODULE_CACHE.clear()
        _script_host._LOADER_MODULE_CACHE.clear()
        try:
            yield
        finally:
            self._EVICTED.append(dict(_script_host._SCRIPT_MODULE_CACHE))
            self._EVICTED.append(dict(_script_host._LOADER_MODULE_CACHE))
            _script_host._SCRIPT_MODULE_CACHE.clear()
            _script_host._SCRIPT_MODULE_CACHE.update(saved_scripts)
            _script_host._LOADER_MODULE_CACHE.clear()
            _script_host._LOADER_MODULE_CACHE.update(saved_loaders)

    def test_a_cold_cache_under_contention_loads_each_stem_exactly_once(self):
        with self.cold_module_caches():
            with Workspace.open(self.root) as ws:
                outcomes = self.in_threads([lambda: ws._script("question_claim") for _ in range(8)])
            modules = []
            for index, (kind, value) in enumerate(outcomes):
                with self.subTest(thread=index):
                    self.assertEqual("ok", kind, f"a concurrent cold load raised {value!r}")
                    modules.append(value)
            self.assertEqual(
                1,
                len({id(module) for module in modules}),
                "one stem produced several module objects; sibling isolation is per-object",
            )
            self.assertEqual(
                1,
                len(_script_host._LOADER_MODULE_CACHE),
                "several loader modules for one root means several locks and no exclusion",
            )

    def test_a_freshly_started_process_serves_concurrent_first_calls(self):
        """The real scenario, in a real cold interpreter.

        The case above has to empty the process-wide caches to reach first-use,
        and that surgery leaves earlier modules bound to a loader object the new
        ones do not share -- a state the product never produces, so a failure
        there could be the harness's fault. A subprocess has no such doubt: it is
        genuinely cold, exactly like a host serving its first requests, and it
        drives whole operations rather than a bare load.
        """
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", COLD_START_PROBE, str(self.root)],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONPATH": str(SRC_ROOT)},
            timeout=180,
        )
        self.assertEqual(0, completed.returncode, f"cold start failed:\n{completed.stdout}\n{completed.stderr}")
        self.assertIn("COLD-START-OK", completed.stdout)


class ClaimContentionTests(ConcurrencyFixture):
    """File-lock arbitration must surface as a typed exception, never as corruption."""

    def holder_of(self, slug: str) -> str | None:
        text = (self.root / "wiki" / "questions" / f"{slug}.md").read_text(encoding="utf-8")
        match = re.search(r"^claimed_by:\s*(\S+)\s*$", text, re.MULTILINE)
        return match.group(1).strip("\"'") if match else None

    def test_a_claim_held_by_another_writer_refuses_with_claim_held(self):
        # The holder is a *separate process*, so the arbitration under test is the
        # real cross-process one a deployed host contends with, not an artifact of
        # two threads sharing an interpreter.
        self.script("question_claim.py", "claim", "--slug", "beta", "--agent-id", HOLDER, "--format", "json")
        self.assertEqual(HOLDER, self.holder_of("beta"))

        with Workspace.open(self.root) as ws:
            outcomes = self.in_threads(
                [
                    lambda: ws.questions.claim(slug="beta", agent_id="agent-one"),
                    lambda: ws.questions.claim(slug="beta", agent_id="agent-two"),
                ]
            )

        for index, (kind, value) in enumerate(outcomes):
            with self.subTest(claimant=index):
                self.assertEqual("raised", kind, f"claimant {index} took a claim another writer holds")
                self.assertIsInstance(value, errors.ClaimError)
                self.assertEqual("CLAIM_HELD", value.error_code)
                self.assertEqual(3, value.exit_code)
                self.assertFalse(value.recoverable, "a held claim is not something a retry fixes")

        self.assertEqual(HOLDER, self.holder_of("beta"), "a refused claim rewrote the page anyway")

    def test_racing_claimants_leave_exactly_one_winner(self):
        claimants = [f"agent-{index}" for index in range(4)]
        with Workspace.open(self.root) as ws:
            outcomes = self.in_threads(
                [(lambda agent=agent: ws.questions.claim(slug="gamma", agent_id=agent)) for agent in claimants]
            )

        winners = [
            claimants[index]
            for index, (kind, value) in enumerate(outcomes)
            if kind == "ok" and value["applied"]
        ]
        losers = [value for kind, value in outcomes if kind == "raised"]

        self.assertEqual(1, len(winners), f"expected exactly one winner, got {winners}")
        for loser in losers:
            with self.subTest(error_code=getattr(loser, "error_code", None)):
                # Either arbitration outcome is correct and both are typed. What
                # must never happen is an untyped exception or a silent success.
                self.assertIsInstance(loser, errors.EvidenceWikiError)
                self.assertIn(loser.error_code, {"CLAIM_HELD", "LOCK_UNAVAILABLE"})
        self.assertEqual(len(claimants), len(winners) + len(losers), "a claimant neither won nor refused")
        self.assertEqual(winners[0], self.holder_of("gamma"), "the page records a holder that never won")


class StdoutIdentityTests(ConcurrencyFixture):
    """No documented API call may touch the host's stdout.

    The workspace scripts print; their seams return. If anyone ever reintroduces
    stdout capture on the API path -- redirecting ``sys.stdout`` to a buffer and
    putting it back -- this fails. Identity is the assertion rather than content
    because putting it back is not good enough: a host that captured its own
    stdout concurrently would have had its capture swapped out from under it.
    """

    def test_sys_stdout_object_identity_survives_every_operation(self):
        before_stdout = sys.stdout
        before_stderr = sys.stderr
        with Workspace.open(self.root) as ws:
            operations = {
                "status": lambda: ws.status(),
                "coverage.evaluate": lambda: ws.coverage.evaluate("alpha"),
                "grounding.verify": lambda: ws.grounding.verify(["alpha"]),
                "questions.claim": lambda: ws.questions.claim(slug="alpha", agent_id="stdout-probe"),
                "questions.release": lambda: ws.questions.release(slug="alpha", agent_id="stdout-probe"),
            }
            for label, call in operations.items():
                with self.subTest(operation=label):
                    call()
                    self.assertIs(before_stdout, sys.stdout, f"{label} replaced sys.stdout")
                    self.assertIs(before_stderr, sys.stderr, f"{label} replaced sys.stderr")

    def test_a_refusal_does_not_touch_stdout_either(self):
        # The refusal path is where a print would be most tempting: the CLI
        # renders an envelope there. The seam raises instead, and the API types it.
        before_stdout = sys.stdout
        with Workspace.open(self.root) as ws:
            with self.assertRaises(errors.EvidenceWikiError):
                ws.coverage.evaluate("../escape")
        self.assertIs(before_stdout, sys.stdout)

    def test_the_api_writes_nothing_to_the_stream_it_was_given(self):
        # Identity is the contract; this is the companion check that the call is
        # also silent, so a host's own captured output stays its own.
        with Workspace.open(self.root) as ws:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                ws.status()
                ws.coverage.evaluate("alpha")
            self.assertEqual("", buffer.getvalue(), "an API call printed to stdout")


if __name__ == "__main__":
    unittest.main()
