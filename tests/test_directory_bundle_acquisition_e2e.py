"""CR-19: a directory-shaped raw delivery must be deliverable inside an acquisition order.

WRITTEN RED, KEPT AS REGRESSION TESTS. Every test here asserts the *desired* end state,
and the first three failed on the controller as it stood when they were written. The
unified attribution predicate has since landed — allowed new raw files became what
`source_inventory.build_records` attributes to the fulfilled/correlated records, rather
than the literal `raw_paths` strings — so all four pass today and stay as its regression
tests. Nothing here is marked `xfail` or skipped: this repository uses neither mechanism,
and a suppressed regression test is a regression test nobody reads.

The defect, stated once:

    `raw_tree_snapshot` records one entry per REGULAR FILE beneath `raw/`, while each
    arm's `allowed_new_raw_paths` builder adds the literal `raw_paths` string of each
    admitted manifest record plus `<string>.provenance.yml`, with no prefix expansion.
    When a record's `raw_paths` names a DIRECTORY — which is exactly what inventory
    derives for a LaTeX/e-print bundle, and exactly what the documented
    `fetch_sources.py arxiv download --format source` command writes — the allowed set
    contains a directory path that the tree snapshot never emits. The guard therefore
    admits ZERO of what the record declares, and every member file of the bundle lands
    in `unexpected_new_raw_paths`.

All three raw-scope arms build that allowed set independently and all three share the
bug, so all three are pinned here — named by function, never by line number:

  * delegated                 `verify_delegated_acquisition_postconditions`
  * provider, non-delegated   `verify_action_postconditions`
  * blocked partial delivery  `verify_blocked_action_postconditions`

The provider case deliberately drives the *documented* first-party command —
`fetch_sources.py … arxiv download --id <id> --format source --request-id … --candidate-id …`
with only the HTTP transport mocked — because that is the flow two shipped documents
instruct an operator to run: `workspace-template/skills/research-acquire.md` and
`workspace-template/docs/acquisition.md`. Pinning it to the real command rather than to a
synthetic fixture is what makes this a regression test for shipped documentation rather
than for a test helper.

The fourth case measures the opposite edge of the same predicate: expansion turns a
directory entry into a whole subtree, so it may only widen admission for records the
acquisition itself created. Its red state is the window between `1cda9e7` and the
narrowing that followed, where a blocked partial delivery could write new files into a
PRE-EXISTING correlated bundle and have them admitted as residue — there it fails on
behaviour, admitting what it should refuse. It also fails against states before
`1cda9e7`, but on a payload field rather than on that behaviour, so only the window is
evidence of the defect it pins.

Nothing here asserts a line number: guards move, and the contract an operator sees is the
error code, the refusal message, and the payload fields. Each failure message carries the
observed refusal and its `unexpected_new_raw_paths` payload, so the red output reads as a
diagnosis of the defect rather than as a bare `0 != 1`.
"""

import io
import json
import sys
import tarfile
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tests.test_orchestration_controller as _toc  # noqa: E402
from tests.test_delegated_acquisition_e2e import (  # noqa: E402
    ACQUIRER,
    DelegatedWorkspace,
)
from tests.test_delegated_acquisition_e2e import (  # noqa: E402
    INVENTORY as DELEGATED_INVENTORY,
)
from tests.test_delegated_acquisition_e2e import (  # noqa: E402
    NORMALIZE as DELEGATED_NORMALIZE,
)

# Read through the module object on purpose: binding `OrchestrationControllerTests` into
# this module's namespace would make pytest re-collect that entire suite inside this file.
ACADEMIC_DOI = _toc.ACADEMIC_DOI
ARXIV_PAYLOAD = _toc.ARXIV_PAYLOAD
CONTROLLER = _toc.CONTROLLER
DISCOVER = _toc.DISCOVER
INVENTORY = _toc.INVENTORY
NORMALIZE = _toc.NORMALIZE
RESOLVE = _toc.RESOLVE
SCRIPTS = _toc.SCRIPTS
SOURCE_REQUESTS = _toc.SOURCE_REQUESTS
CLAIM = _toc.CLAIM

FETCH = _toc.load_script_module("dir_bundle_fetch_sources", SCRIPTS / "fetch_sources.py")

ARXIV_ID = "2601.12345v2"
BUNDLE_RELATIVE = f"raw/papers/arxiv-{ARXIV_ID}"

README_JSON = (
    json.dumps({"process": {"compiler": "pdflatex"}, "texlive_version": "synthetic-2026"}, indent=2)
    + "\n"
)
MAIN_TEX = (
    "\\documentclass{article}\n"
    "\\title{Solid Electrolyte Conductivity Survey}\n"
    "\\author{Ada Example}\n"
    "\\begin{document}\n"
    "\\maketitle\n"
    "\\begin{abstract}\n"
    "Room-temperature ionic conductivity exceeds 1 mS/cm for the reported sulfide family.\n"
    "\\end{abstract}\n"
    "\\section{Findings}\n"
    "The sulfide electrolyte family reported here exceeds the requested conductivity threshold\n"
    "at room temperature across every measured composition in the survey.\n"
    "\\end{document}\n"
)


def manifest_records(workspace: Path) -> list[dict]:
    path = workspace / "sources" / "manifest.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def raw_tree_files(workspace: Path) -> list[str]:
    """Every regular file under `raw/` — the granularity `raw_tree_snapshot` works at."""
    root = workspace / "raw"
    if not root.is_dir():
        return []
    return sorted(p.relative_to(workspace).as_posix() for p in root.rglob("*") if p.is_file())


def diagnose(arm: str, expected: str, code: int, envelope: dict, workspace: Path) -> str:
    """Render the observed refusal so a red run reads as the CR-19 diagnosis itself.

    A bare `assertEqual(0, code)` would print `0 != 1` and tell a reader nothing. The
    payload fields below — the refusal message, `unexpected_new_raw_paths`, and the
    `allowed_new_raw_paths` the guard built from the record's literal directory string —
    are the whole evidence for the bug, so they belong in the failure text.
    """
    envelope = envelope if isinstance(envelope, dict) else {"raw": envelope}
    details = envelope.get("details") if isinstance(envelope.get("details"), dict) else {}
    return "\n".join(
        [
            f"CR-19 [{arm} arm]: expected {expected}, observed exit {code}.",
            f"  error_code ............... {envelope.get('error_code')!r}",
            f"  message .................. {envelope.get('message')!r}",
            f"  unexpected_new_raw_paths . {json.dumps(details.get('unexpected_new_raw_paths'))}",
            f"  allowed_new_raw_paths .... {json.dumps(details.get('allowed_new_raw_paths'))}",
            f"  raw_scope_violations ..... {json.dumps(details.get('raw_scope_violations'))}",
            f"  manifest_scope_violations  {json.dumps(details.get('manifest_scope_violations'))}",
            f"  fulfilled_source_ids ..... {json.dumps(details.get('fulfilled_source_ids'))}",
            f"  remediation .............. {envelope.get('remediation')!r}",
            "  declared raw_paths ....... "
            + json.dumps([record.get("raw_paths") for record in manifest_records(workspace)]),
            f"  actual files under raw/ .. {json.dumps(raw_tree_files(workspace))}",
            f"  full envelope ............ {json.dumps(envelope, indent=2, sort_keys=True)}",
        ]
    )


def write_directory_bundle(
    workspace: Path,
    *,
    request_id: str,
    retrieved_by: str,
    candidate_id: str | None = None,
) -> str:
    """Write the shape `arxiv download --format source` leaves behind: a directory.

    A bundle is a directory of member files with ONE provenance sidecar sitting beside
    the directory (`<dir>.provenance.yml`), not one sidecar per member. Inventory folds
    the members into a single manifest record whose `raw_paths` is the directory itself —
    which is precisely the record shape the raw-scope guards cannot express.
    """
    bundle = workspace / BUNDLE_RELATIVE
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "00README.json").write_text(README_JSON, encoding="utf-8")
    (bundle / "main.tex").write_text(MAIN_TEX, encoding="utf-8")
    sidecar: dict[str, object] = {
        "origin_url": f"https://arxiv.org/e-print/{ARXIV_ID}",
        "retrieved_at": "2026-07-20T00:00:00Z",
        "retrieved_by": retrieved_by,
        "license": "CC-BY-4.0",
        "terms_url": "https://info.arxiv.org/help/license/index.html",
        "terms_note": "Offline fixture: explicit arXiv provenance, no network access.",
        "notes": "Network-free arXiv --format source delivery: a directory-shaped bundle.",
        "request_id": request_id,
        "academic_provider": "arxiv",
        "academic_source_type": "preprint",
        "arxiv_id": ARXIV_ID,
        "doi": ACADEMIC_DOI,
        "title": "Solid Electrolyte Conductivity Survey",
        "authors": ["Ada Example"],
        "published": "2026-01-10T00:00:00Z",
    }
    if candidate_id is not None:
        sidecar["candidate_id"] = candidate_id
    (workspace / f"{BUNDLE_RELATIVE}.provenance.yml").write_text(
        yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
    )
    return BUNDLE_RELATIVE


class DelegatedDirectoryBundleTests(DelegatedWorkspace, unittest.TestCase):
    """The delegated arm: an external acquirer delivers a bundle inside its work order.

    Delegation is the arm where the workspace has the least visibility into how the
    payload was fetched, so the raw-scope postcondition is the only thing standing between
    a delivery and the manifest. A directory-shaped delivery is a perfectly ordinary
    academic payload; refusing it means the delegated route cannot carry papers at all.
    """

    maxDiff = None

    def assert_bundle_is_one_record(self, workspace: Path, relative: str) -> str:
        """Inventory's verdict is the premise of the whole file — assert it, don't assume it.

        If inventory ever stopped folding a bundle into a single directory-valued record,
        the guards below would be being blamed for something that is no longer their fault.
        """
        records = manifest_records(workspace)
        self.assertEqual(1, len(records), f"expected exactly one bundle record, got {records}")
        self.assertEqual(
            [relative],
            records[0]["raw_paths"],
            "inventory must derive one record whose raw_paths IS the bundle directory; "
            f"got {records[0]['raw_paths']} against raw tree {raw_tree_files(workspace)}",
        )
        return str(records[0]["id"])

    def test_a_delegated_order_can_deliver_a_directory_shaped_bundle(self):
        """RED: the delegated raw guard admits none of the bundle it just accepted.

        The record the workspace's own inventory wrote declares `raw/papers/arxiv-<id>`;
        the tree snapshot the guard diffs against contains `.../00README.json` and
        `.../main.tex`. Neither member is in the allowed set, so a lawful delivery is
        refused for being outside the scope of the record that describes it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)

            relative = write_directory_bundle(
                workspace, request_id=request_id, retrieved_by=ACQUIRER
            )
            self.run_script(DELEGATED_INVENTORY, ["--report"], workspace)
            source_id = self.assert_bundle_is_one_record(workspace, relative)
            self.run_script(DELEGATED_NORMALIZE, ["--source-id", source_id], workspace)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            code, envelope = self.submit(
                workspace,
                order["action_id"],
                artifacts=[relative, f"{relative}.provenance.yml", "sources/manifest.jsonl"],
            )
            self.assertEqual(
                0,
                code,
                diagnose("delegated", "the delegated order to be accepted (exit 0)", code, envelope, workspace),
            )


class ProviderDirectoryBundleTests(unittest.TestCase):
    """The provider (non-delegated) and blocked-partial arms, on the controller harness.

    Bound off the shipped harness class through the module object rather than by
    inheritance: `OrchestrationControllerTests` carries its own large suite, and
    subclassing it would re-run every one of those tests under this file.
    """

    maxDiff = None

    run_module = _toc.OrchestrationControllerTests.run_module
    init_workspace = _toc.OrchestrationControllerTests.init_workspace
    controller = _toc.OrchestrationControllerTests.controller
    hydrated_order = _toc.OrchestrationControllerTests.hydrated_order
    json_script = _toc.OrchestrationControllerTests.json_script
    assert_json_script_ok = _toc.OrchestrationControllerTests.assert_json_script_ok
    enable_academic_providers = _toc.OrchestrationControllerTests.enable_academic_providers
    manifest_records = _toc.OrchestrationControllerTests.manifest_records
    start = _toc.OrchestrationControllerTests.start
    submit = _toc.OrchestrationControllerTests.submit

    # -- walk a real session to a pending provider acquisition order -----------------

    def walk_to_acquisition(
        self,
        root: Path,
        *,
        between_actions: Callable[[Path, str, str], None] | None = None,
    ) -> tuple[Path, str, str, dict]:
        """research -> discovery -> candidate_review -> acquisition, all through the loop.

        The order has to be a genuine one: the raw-scope guard compares a `raw/` tree
        snapshot taken when the order was ISSUED against the tree at submission, so an
        order fabricated after the delivery would compare the wrong two trees and could
        not reproduce the defect.

        `between_actions`, when given, is called with `(target, request_id, candidate_id)`
        in the gap the completed candidate review leaves — after that submission, before
        the acquisition order is issued. That gap is the only place a caller can put
        evidence that must be BOTH pre-existing at issuance and stamped for the request
        and candidate this order will scope, because the candidate id does not exist until
        discovery has run. It is also the delivery route the shipped contract recommends
        for a capture that fulfils nothing.
        """
        target = self.init_workspace(root, question=True)
        self.enable_academic_providers(target)
        self.start(target)
        _, research_order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
        self.assertEqual("research", research_order["phase"], research_order)

        request_report = self.assert_json_script_ok(
            SOURCE_REQUESTS,
            [
                "--project-root", str(target), "add",
                "--kind", "paper",
                "--query-or-identifier",
                "solid electrolyte room-temperature ionic conductivity above 1 mS/cm",
                "--rationale", "The open question needs primary academic evidence.",
                "--priority", "high",
                "--question-slug", "test-question",
                "--format", "json",
            ],
        )
        request_id = request_report["request"]["request_id"]
        self.assert_json_script_ok(
            CLAIM,
            ["--project-root", str(target), "claim", "--slug", "test-question",
             "--agent-id", "answer-agent", "--format", "json"],
        )
        self.assert_json_script_ok(
            RESOLVE,
            ["--project-root", str(target), "block", "--slug", "test-question",
             "--agent-id", "answer-agent",
             "--blocked-reason", "No delivered academic evidence is available yet.",
             "--request-id", request_id, "--format", "json"],
        )
        code, _, stderr = self.submit(
            root, target, research_order["action_id"],
            summary="Recorded the evidence gap and blocked the question on a source request.",
            artifacts=["sources/source-requests.jsonl", "wiki/questions/test-question.md"],
        )
        self.assertEqual(0, code, stderr)

        _, discovery_order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
        self.assertEqual("discovery", discovery_order["phase"], discovery_order)
        with (
            mock.patch.object(DISCOVER, "ARXIV_TRANSPORT", lambda _u, _t, _h: ARXIV_PAYLOAD),
            mock.patch.object(
                DISCOVER, "OPENALEX_TRANSPORT", lambda _u, _t, _h: _toc.openalex_payload()
            ),
            mock.patch.object(DISCOVER, "ARXIV_CLOCK", lambda: 0.0),
            mock.patch.object(DISCOVER, "OPENALEX_CLOCK", lambda: 0.0),
            mock.patch.object(DISCOVER, "ARXIV_SLEEP", lambda _seconds: None),
            mock.patch.object(DISCOVER, "OPENALEX_SLEEP", lambda _seconds: None),
            mock.patch.object(DISCOVER, "ARXIV_LAST_REQUEST_AT", None),
            mock.patch.object(DISCOVER, "OPENALEX_LAST_REQUEST_AT", None),
        ):
            discovery = self.assert_json_script_ok(
                DISCOVER,
                ["--project-root", str(target), "--format", "json", "academic",
                 "--request-id", request_id, "--provider", "arxiv", "--provider", "openalex",
                 "--max-results", "15"],
            )
        candidate_id = discovery["candidates"][0]["candidate_id"]
        code, _, stderr = self.submit(
            root, target, discovery_order["action_id"],
            summary="Mocked academic providers produced one deduplicated candidate.",
            artifacts=["sources/discovery/candidates.jsonl"],
        )
        self.assertEqual(0, code, stderr)

        _, review_order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
        self.assertEqual("candidate_review", review_order["phase"], review_order)
        self.assert_json_script_ok(
            DISCOVER,
            ["--project-root", str(target), "--format", "json", "candidates", "select",
             "--candidate-id", candidate_id, "--request-id", request_id,
             "--reason", "Selected the academic-primary arXiv route.",
             "--actor", "review-agent", "--run-id", review_order["run_id"]],
        )
        code, _, stderr = self.submit(
            root, target, review_order["action_id"],
            summary="Reviewed and selected the routable academic candidate.",
            artifacts=["sources/discovery/candidates.jsonl"],
        )
        self.assertEqual(0, code, stderr)

        if between_actions is not None:
            self.assertIsNone(
                CONTROLLER.load_session(target, "orch-test").get("pending_action_id"),
                "a between-actions delivery must happen while no work order brackets it",
            )
            between_actions(target, request_id, candidate_id)

        _, acquisition_order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
        self.assertEqual("acquisition", acquisition_order["phase"], acquisition_order)
        self.assertNotEqual(
            CONTROLLER.ACQUISITION_MODE_DELEGATED,
            acquisition_order.get("acquisition_mode"),
            "these cases must exercise the provider (non-delegated) arm of the raw-scope guard",
        )
        return target, request_id, candidate_id, acquisition_order

    def assert_bundle_is_one_record(self, target: Path, relative: str) -> str:
        records = self.manifest_records(target)
        self.assertEqual(1, len(records), f"expected exactly one bundle record, got {records}")
        self.assertEqual(
            [relative],
            records[0]["raw_paths"],
            "inventory must derive one record whose raw_paths IS the bundle directory; "
            f"got {records[0]['raw_paths']} against raw tree {raw_tree_files(target)}",
        )
        return str(records[0]["id"])

    # -- provider arm: the documented first-party command ----------------------------

    def test_the_documented_arxiv_source_download_completes_inside_an_order(self):
        """RED: the flow two shipped documents tell operators to run cannot be submitted.

        `skills/research-acquire.md` and `docs/acquisition.md` both instruct

            fetch_sources.py … arxiv download --id <id> --format source \\
                --request-id <rid> --candidate-id <cid>

        and that command unpacks the e-print tarball into a DIRECTORY under `raw/papers/`.
        Only the HTTP transport is mocked here; the argument parsing, the unpack, the
        sidecar it writes, and the path it reports are all the shipped code. So the
        refusal this test currently records is not a property of a test fixture — it is
        the documented acquisition route being unusable end to end.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root)

            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w:gz") as tar:
                for name, body in (("main.tex", MAIN_TEX), ("sections/intro.tex", "Intro evidence.\n")):
                    member = body.encode("utf-8")
                    info = tarfile.TarInfo(name=name)
                    info.size = len(member)
                    tar.addfile(info, io.BytesIO(member))
            archive_bytes = archive.getvalue()

            def transport(url, _timeout):
                if "export.arxiv.org/api/query" in url:
                    return ARXIV_PAYLOAD
                return archive_bytes

            original = (
                FETCH.ARXIV_TRANSPORT,
                FETCH.ARXIV_CLOCK,
                FETCH.ARXIV_SLEEP,
                FETCH.ARXIV_LAST_REQUEST_AT,
            )
            FETCH.ARXIV_TRANSPORT = transport
            FETCH.ARXIV_CLOCK = lambda: 0.0
            FETCH.ARXIV_SLEEP = lambda _seconds: None
            FETCH.ARXIV_LAST_REQUEST_AT = None
            try:
                code, download, stderr = self.json_script(
                    FETCH,
                    ["--project-root", str(target), "--format", "json",
                     "arxiv", "download", "--id", ARXIV_ID, "--format", "source",
                     "--request-id", request_id, "--candidate-id", candidate_id],
                )
            finally:
                (
                    FETCH.ARXIV_TRANSPORT,
                    FETCH.ARXIV_CLOCK,
                    FETCH.ARXIV_SLEEP,
                    FETCH.ARXIV_LAST_REQUEST_AT,
                ) = original
            self.assertEqual(0, code, stderr)
            raw_relative = download["target_path"]
            self.assertTrue(
                (target / raw_relative).is_dir(),
                f"`--format source` must unpack a directory; {raw_relative} is not one",
            )

            self.assert_json_script_ok(
                INVENTORY, ["--project-root", str(target), "--report", "--format", "json"]
            )
            source_id = self.assert_bundle_is_one_record(target, raw_relative)
            self.assert_json_script_ok(
                NORMALIZE,
                ["--project-root", str(target), "--source-id", source_id, "--format", "json"],
            )
            self.assert_json_script_ok(
                SOURCE_REQUESTS,
                ["--project-root", str(target), "fulfill", "--request-id", request_id,
                 "--source-id", source_id, "--format", "json"],
            )
            self.assert_json_script_ok(
                DISCOVER,
                ["--project-root", str(target), "--format", "json", "candidates", "transition",
                 "--candidate-id", candidate_id, "--expected-state", "selected",
                 "--to-state", "fetched", "--reason", "Downloaded the arXiv e-print bundle.",
                 "--source-id", source_id, "--actor", "acquire-agent",
                 "--run-id", order["run_id"]],
            )
            self.assert_json_script_ok(
                RESOLVE,
                ["--project-root", str(target), "reopen", "--slug", "test-question",
                 "--agent-id", "acquire-agent", "--source-id", source_id,
                 "--request-id", request_id, "--format", "json"],
            )

            code, payload, stderr = self.submit(
                root, target, order["action_id"],
                summary="Ran the documented arxiv download --format source flow inside the order.",
                artifacts=[raw_relative, f"{raw_relative}.provenance.yml", "sources/manifest.jsonl"],
            )
            self.assertEqual(
                0,
                code,
                diagnose(
                    "provider",
                    "the documented arXiv --format source flow to be accepted (exit 0)",
                    code, payload, target,
                ),
            )

    # -- blocked-partial arm ----------------------------------------------------------

    def test_a_blocked_partial_delivery_of_a_bundle_pauses_rather_than_refusing(self):
        """RED: an honest partial delivery is refused instead of pausing the session.

        `blocked` is how an acquirer reports "I fetched something but could not finish
        with it" — the payload is on disk and inventoried, nothing is fulfilled. The
        blocked-partial raw guard correlates the delivery against the manifest records
        the order scoped, and hits the same directory-vs-file mismatch: the correlated
        record declares the directory, the tree snapshot lists its members. So the arm
        that exists to record incomplete work cannot record this incomplete work, and the
        session gets a hard postcondition failure where `EXIT_PAUSED` is the contract.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root)

            relative = write_directory_bundle(
                target,
                request_id=request_id,
                candidate_id=candidate_id,
                retrieved_by="fetch_sources.py/arxiv",
            )
            self.assert_json_script_ok(
                INVENTORY, ["--project-root", str(target), "--report", "--format", "json"]
            )
            self.assert_bundle_is_one_record(target, relative)

            code, payload, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Downloaded the bundle but could not normalize it; recording a partial delivery.",
                artifacts=[relative, f"{relative}.provenance.yml", "sources/manifest.jsonl"],
            )
            self.assertEqual(
                CONTROLLER.EXIT_PAUSED,
                code,
                diagnose(
                    "blocked-partial",
                    f"the partial delivery to pause the session (exit {CONTROLLER.EXIT_PAUSED})",
                    code, payload, target,
                ),
            )
            self.assertEqual(
                "paused",
                payload.get("phase"),
                diagnose(
                    "blocked-partial",
                    "the session to report phase 'paused'",
                    code, payload, target,
                ),
            )

    def test_a_blocked_partial_delivery_may_not_write_into_a_pre_existing_bundle(self):
        """A bundle the action did not create is not a subtree it may write into.

        The attribution predicate the test above depends on expands a directory-valued
        `raw_paths` to every regular file beneath it. That expansion is what makes a
        bundle deliverable at all, and it is safe for a record the action itself created:
        the acquirer wrote the whole subtree, so admitting the whole subtree admits only
        its own work.

        Applied to a record that ALREADY EXISTED when the order was issued, the same
        expansion is a licence to write anywhere inside somebody else's payload. A
        pre-existing correlated bundle would let a partial delivery drop arbitrary new
        files into its subtree and have every one of them admitted, because inventory
        attributes files to the record whose directory contains them and asks nothing
        about who put them there. The residue a blocked partial delivery is allowed to
        leave is scoped to records the action created; a pre-existing correlated record
        contributes its literal declared paths and nothing more, which is exactly what it
        contributed before expansion existed.

        Getting a record into that state is the whole difficulty, and the gap between a
        completed candidate review and the acquisition order is the only place it fits:
        the candidate id does not exist until discovery has run, and the record must be in
        the manifest before issuance. Delivered and inventoried there, the bundle is
        pre-existing evidence correlated to the request and candidate the next order
        scopes — which is also the between-actions route the shipped delivery contract
        recommends for a capture that fulfils nothing.

        A file is then planted deep inside that subtree while the order is open, and the
        submission must be refused naming it. The allowed set is asserted alongside the
        refusal, because "refused" on its own is also what a fixture that never reached
        this arm would produce: the two literal declared paths are the guard's own
        evidence that it evaluated a pre-existing record, and the absence of the bundle's
        own member files from the unexpected set is its evidence that they were in the
        issuance baseline rather than newly delivered.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivered: dict[str, str] = {}

            def deliver_bundle_between_actions(
                workspace: Path, request_id: str, candidate_id: str
            ) -> None:
                delivered["relative"] = write_directory_bundle(
                    workspace,
                    request_id=request_id,
                    candidate_id=candidate_id,
                    retrieved_by="fetch_sources.py/arxiv",
                )
                self.assert_json_script_ok(
                    INVENTORY, ["--project-root", str(workspace), "--report", "--format", "json"]
                )
                delivered["source_id"] = self.assert_bundle_is_one_record(
                    workspace, delivered["relative"]
                )

            target, _, _, order = self.walk_to_acquisition(
                root, between_actions=deliver_bundle_between_actions
            )
            relative = delivered["relative"]
            self.assertEqual(
                [delivered["source_id"]],
                [str(record["id"]) for record in self.manifest_records(target)],
                "the bundle record must be the manifest state the acquisition order inherits",
            )

            planted = f"{relative}/sections/planted-appendix.tex"
            planted_path = target / planted
            planted_path.parent.mkdir(parents=True, exist_ok=True)
            planted_path.write_text(
                "\\section{Appendix}\nPlanted inside a bundle this action did not deliver.\n",
                encoding="utf-8",
            )

            code, payload, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Fetched more of the bundle but could not finish; recording a partial delivery.",
                artifacts=[relative, f"{relative}.provenance.yml", "sources/manifest.jsonl"],
            )
            self.assertEqual(
                CONTROLLER.EXIT_INVALID,
                code,
                diagnose(
                    "blocked-partial",
                    "the planted file to be refused (exit "
                    f"{CONTROLLER.EXIT_INVALID}) rather than admitted as partial-delivery residue",
                    code, payload, target,
                ),
            )
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", payload["error_code"], payload)
            self.assertTrue(payload["recoverable"], payload)
            self.assertIn(
                "changed raw evidence outside correlated partial deliveries",
                payload["message"],
                payload,
            )
            details = payload["details"]
            self.assertEqual(
                [planted],
                details["unexpected_new_raw_paths"],
                "the refusal must name the planted file, and name only it: the bundle's own "
                f"members were delivered before issuance. Raw tree: {raw_tree_files(target)}",
            )
            self.assertEqual(
                [relative, f"{relative}.provenance.yml"],
                sorted(details["allowed_new_raw_paths"]),
                "a pre-existing correlated record may contribute only its literal declared "
                "paths; anything wider means the subtree expansion reached it",
            )
            self.assertEqual(
                [delivered["source_id"]],
                [str(record["id"]) for record in self.manifest_records(target)],
                "a refused partial delivery must leave the manifest as it found it",
            )
            session = CONTROLLER.load_session(target, "orch-test")
            self.assertEqual("active", session["status"], session)
            self.assertEqual(
                order["action_id"],
                session["pending_action_id"],
                "the refusal must leave the same order open for the acquirer to correct and "
                f"resubmit, not pause or close the session: {stderr}",
            )


if __name__ == "__main__":
    unittest.main()
