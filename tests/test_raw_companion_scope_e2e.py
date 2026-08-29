"""An undeclared file delivered beside a source artifact is refused, naming the path, on every arm.

An acquirer that writes a second file next to the artifact it delivered has created raw
evidence no manifest record accounts for. Every acquisition arm refuses that, and this
file pins the refusal an operator actually sees -- the message and the payload fields --
on all three:

  * delegated                 ``verify_delegated_acquisition_postconditions``
  * provider, non-delegated   ``verify_action_postconditions``
  * blocked partial delivery  ``verify_blocked_action_postconditions``

The undeclared file is DOT-PREFIXED, which is what makes the refusal reachable at raw
scope rather than one guard earlier. ``source_inventory.should_skip`` drops any path with
a dot-prefixed component before the raw walk ever sees it, so such a file never becomes a
manifest record; ``orchestration_controller.raw_tree_snapshot`` consults no skip predicate
and lstats every regular file under each raw root. The snapshot sees it, inventory does
not, and the raw-scope guard is the one left to say no.

That asymmetry is the whole subject, so every plant here is written BEFORE the inventory
run that makes the delivery a record -- which is also the shape an acquirer capturing both
in one step leaves behind. Inventory then sees the extra file and declines to record it,
and the assertion that it did so is a live measurement rather than a restatement of the
writing order. A plant written after the last inventory run would be refused by the same
guard for a different reason, and would say nothing about the skip.

A dot-prefixed member INSIDE a delivered directory-shaped bundle is a different question --
the record's subtree expansion admits it, and that admission is pinned elsewhere -- so
every plant here sits BESIDE a file-shaped capture, where no record's expansion can reach
it.

The delegated arm carries two more cases, which vary one character and one moment to show
which guard answers and why:

  * the same sibling PLAIN-NAMED, written in the same place, earns a manifest record the
    order never fulfilled with. ``require`` raises on the first failing check, so the
    manifest-scope guard refuses and names the record, not the path;
  * that plain-named file written after every inventory run has no record naming it, and
    falls back through to the raw-scope guard.

So the trio differs only in the dot and in the timing, and an acquirer repairing the wrong
one of those has been told the wrong thing.

Every refusal here is asserted against its own control: with the planted file removed, the
same action id resubmits and is accepted -- a pause, on the arm whose acceptance is a pause.
Without that, a fixture refused for some earlier reason -- one that never reached the guard
under test -- would satisfy the assertions above it and prove nothing.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tests.test_orchestration_controller as _toc  # noqa: E402
from tests.test_delegated_acquisition_e2e import (  # noqa: E402
    INVENTORY as DELEGATED_INVENTORY,
)
from tests.test_delegated_acquisition_e2e import (  # noqa: E402
    PAYLOAD,
    DelegatedWorkspace,
)

# Read through the module object on purpose: binding `OrchestrationControllerTests` into
# this module's namespace would make pytest re-collect that entire suite inside this file.
ARXIV_PAYLOAD = _toc.ARXIV_PAYLOAD
CLAIM = _toc.CLAIM
CONTROLLER = _toc.CONTROLLER
DISCOVER = _toc.DISCOVER
INVENTORY = _toc.INVENTORY
NORMALIZE = _toc.NORMALIZE
RESOLVE = _toc.RESOLVE
SOURCE_REQUESTS = _toc.SOURCE_REQUESTS

# The completed arms say the same thing one word apart, and the delegated spelling is the
# provider one with a prefix. Both spellings are asserted where they belong; the un-prefixed
# constant doubles as the needle for "this refusal was NOT the raw-scope one", where it
# matches either arm.
RAW_SCOPE_REFUSAL = (
    "acquisition changed raw evidence outside newly fulfilled manifest source scope"
)
DELEGATED_RAW_SCOPE_REFUSAL = f"delegated {RAW_SCOPE_REFUSAL}"
BLOCKED_RAW_SCOPE_REFUSAL = (
    "blocked acquisition changed raw evidence outside correlated partial deliveries"
)
# Likewise un-prefixed, so one needle covers the delegated and provider manifest-scope
# refusals both.
MANIFEST_SCOPE_REFUSAL = (
    "changed, removed, or added evidence-manifest records outside fulfilled source scope"
)

#: What the undeclared delivery looks like: an extra capture named after the artifact it
#: was observed beside, dot-prefixed and carrying no provenance sidecar of its own.
UNDECLARED_SUFFIX = ".observation.json"
UNDECLARED_BODY = (
    '{\n  "observed_at": "2026-08-20T12:00:00Z",\n  "observer": "acquirer-side capture"\n}\n'
)

DELEGATED_ARTIFACT = f"raw/data/{PAYLOAD.name}"
DELEGATED_SIDECAR = f"{DELEGATED_ARTIFACT}.provenance.yml"
PLAIN_NAMED_SIBLING = "raw/data/companion.json"


def undeclared_sibling(relative: str) -> str:
    """The dot-prefixed path this suite plants beside the artifact at ``relative``."""
    artifact = Path(relative)
    return (artifact.parent / f".{artifact.name}{UNDECLARED_SUFFIX}").as_posix()


def plant(workspace: Path, relative: str) -> Path:
    """Write the undeclared delivery and return it, so a control can remove it again."""
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(UNDECLARED_BODY, encoding="utf-8", newline="\n")
    return path


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
    """Render the observed refusal so a red run reads as the diagnosis itself.

    A bare `assertEqual(0, code)` would print `0 != 1` and tell a reader nothing. The
    payload fields below — the refusal message, `unexpected_new_raw_paths`, and the
    `allowed_new_raw_paths` the guard built — are the whole evidence, so they belong in
    the failure text.
    """
    envelope = envelope if isinstance(envelope, dict) else {"raw": envelope}
    details = envelope.get("details") if isinstance(envelope.get("details"), dict) else {}
    return "\n".join(
        [
            f"[{arm} arm]: expected {expected}, observed exit {code}.",
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


class DelegatedUndeclaredCompanionTests(DelegatedWorkspace, unittest.TestCase):
    """The delegated arm: an external acquirer delivers one artifact and one extra file."""

    maxDiff = None

    def assert_the_delivery_is_the_only_record(
        self, workspace: Path, source_id: str, *, because: str
    ) -> None:
        """No record names the planted file — the premise the raw-scope arm rests on.

        Assert it rather than assume it: if the plant ever earned a record the manifest-scope
        guard would refuse first, and the raw-scope guard would be getting credit for a
        refusal it never issued. `because` names what is keeping the record from existing,
        which differs case by case.
        """
        self.assertEqual(
            [source_id],
            [str(record["id"]) for record in manifest_records(workspace)],
            f"{because}; raw tree was {raw_tree_files(workspace)}",
        )

    def test_an_undeclared_sibling_of_a_delivered_artifact_is_refused_by_raw_scope(self):
        """The delegated arm names the extra path and admits only the delivery it fulfilled.

        The undeclared file is written before the delivery is inventoried — the shape an
        acquirer that captures both in one step actually leaves — so the inventory run that
        makes the delivery a record sees the extra file too, and declines to make one for it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)

            undeclared = undeclared_sibling(DELEGATED_ARTIFACT)
            planted_path = plant(workspace, undeclared)
            source_id = self.deliver_for(workspace, request_id)
            self.assert_the_delivery_is_the_only_record(
                workspace,
                source_id,
                because="the delivery's own inventory run saw the planted file and skipped "
                "it for its dot-prefixed name",
            )
            self.fulfil_and_reopen(workspace, request_id, source_id)

            # Snapshotted with the plant already on disk, so the comparison after the
            # refusal says the submission wrote nothing of its own and left the plant
            # exactly where the acquirer has to go and remove it.
            before = self.evidence_bytes(workspace)
            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(
                CONTROLLER.EXIT_INVALID,
                code,
                diagnose(
                    "delegated",
                    f"the undeclared sibling to be refused (exit {CONTROLLER.EXIT_INVALID})",
                    code, envelope, workspace,
                ),
            )
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"], envelope)
            self.assertTrue(envelope["recoverable"], envelope)
            self.assertIn(
                DELEGATED_RAW_SCOPE_REFUSAL,
                envelope["message"],
                diagnose(
                    "delegated",
                    "the raw-scope guard to be the one that refuses",
                    code, envelope, workspace,
                ),
            )
            self.assertNotIn(
                MANIFEST_SCOPE_REFUSAL,
                envelope["message"],
                "a dot-prefixed file has no manifest record, so manifest scope has nothing "
                f"to refuse: {envelope}",
            )
            details = envelope["details"]
            self.assertEqual(
                [undeclared],
                details["unexpected_new_raw_paths"],
                "the refusal must name the undeclared file, and name only it; raw tree was "
                f"{raw_tree_files(workspace)}",
            )
            self.assertEqual(
                [DELEGATED_ARTIFACT, DELEGATED_SIDECAR],
                sorted(details["allowed_new_raw_paths"]),
                "the fulfilled delivery and its sidecar are what the order authorised, and "
                "nothing about the undeclared file may widen that",
            )
            self.assertFalse(
                any(details["raw_scope_violations"].values()),
                f"nothing that existed at issuance was changed; the plant is a new file: {details}",
            )
            self.assertEqual(
                before,
                self.evidence_bytes(workspace),
                "a refused submission writes nothing of its own and removes nothing",
            )

            # The control: the same order with the plant removed is accepted. Without it a
            # fixture refused for some earlier reason would satisfy every assertion above.
            planted_path.unlink()
            code, session = self.submit(workspace, order["action_id"])
            self.assertEqual(0, code, session)
            self.assertEqual("research", session["phase"], session)

    def test_a_plain_named_sibling_written_before_inventory_is_refused_by_manifest_scope(self):
        """Which guard answers is decided by whether a record names the file.

        A plain-named sibling written before an inventory run becomes a manifest record the
        order never fulfilled with, and `require` raises on the first failing check, so the
        manifest-scope guard refuses and names the record. Pinning that here keeps the
        diagnosis an acquirer gets attached to the mistake they actually made.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)

            planted_path = plant(workspace, PLAIN_NAMED_SIBLING)
            # The delivery's own inventory run is what turns the sibling into a record.
            source_id = self.deliver_for(workspace, request_id)
            extra_id = self.source_id_for(workspace, PLAIN_NAMED_SIBLING)
            self.assertNotEqual(source_id, extra_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            before = self.evidence_bytes(workspace)
            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(
                CONTROLLER.EXIT_INVALID,
                code,
                diagnose(
                    "delegated",
                    f"the extra record to be refused (exit {CONTROLLER.EXIT_INVALID})",
                    code, envelope, workspace,
                ),
            )
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"], envelope)
            self.assertTrue(envelope["recoverable"], envelope)
            self.assertIn(
                MANIFEST_SCOPE_REFUSAL,
                envelope["message"],
                diagnose(
                    "delegated",
                    "the manifest-scope guard to refuse first",
                    code, envelope, workspace,
                ),
            )
            self.assertNotIn(
                RAW_SCOPE_REFUSAL,
                envelope["message"],
                "a record naming the file is a manifest-scope failure; reaching raw scope "
                f"would mean the acquirer was pointed at the wrong repair: {envelope}",
            )
            violations = envelope["details"]["manifest_scope_violations"]
            self.assertEqual([extra_id], violations["added_outside_scope"], envelope)
            self.assertEqual([], violations["removed"], envelope)
            self.assertEqual([], violations["changed_outside_scope"], envelope)
            self.assertEqual([source_id], envelope["details"]["fulfilled_source_ids"], envelope)
            self.assertEqual(
                before,
                self.evidence_bytes(workspace),
                "a refused submission writes nothing of its own and removes nothing",
            )

            # The control has to undo both halves of the mistake: the bytes under `raw/`
            # and the record inventory derived from them. Re-running inventory over the
            # repaired tree is how an acquirer does that, and it restores exactly the
            # manifest the order was issued against.
            planted_path.unlink()
            self.run_script(DELEGATED_INVENTORY, ["--report"], workspace)
            self.assertEqual(
                [source_id], [str(record["id"]) for record in manifest_records(workspace)]
            )
            code, session = self.submit(workspace, order["action_id"])
            self.assertEqual(0, code, session)
            self.assertEqual("research", session["phase"], session)

    def test_a_plain_named_sibling_written_after_inventory_is_refused_by_raw_scope(self):
        """The same file, written where no record can name it, falls through to raw scope.

        Nothing about the file changed — only when it was written. The manifest-scope guard
        sees a manifest that matches the order exactly and passes it through, and the
        raw-scope guard is left holding the diagnosis.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)

            source_id = self.deliver_for(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)
            planted_path = plant(workspace, PLAIN_NAMED_SIBLING)
            self.assert_the_delivery_is_the_only_record(
                workspace,
                source_id,
                because="no inventory run followed this plant, so nothing could have made a "
                "record of it",
            )

            before = self.evidence_bytes(workspace)
            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(
                CONTROLLER.EXIT_INVALID,
                code,
                diagnose(
                    "delegated",
                    f"the un-inventoried sibling to be refused (exit {CONTROLLER.EXIT_INVALID})",
                    code, envelope, workspace,
                ),
            )
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"], envelope)
            self.assertTrue(envelope["recoverable"], envelope)
            self.assertIn(
                DELEGATED_RAW_SCOPE_REFUSAL,
                envelope["message"],
                diagnose(
                    "delegated",
                    "the raw-scope guard to be the one that refuses",
                    code, envelope, workspace,
                ),
            )
            self.assertNotIn(
                MANIFEST_SCOPE_REFUSAL,
                envelope["message"],
                f"no record names this file, so manifest scope has nothing to refuse: {envelope}",
            )
            details = envelope["details"]
            self.assertEqual(
                [PLAIN_NAMED_SIBLING],
                details["unexpected_new_raw_paths"],
                "the refusal must name the file the acquirer has to remove; raw tree was "
                f"{raw_tree_files(workspace)}",
            )
            self.assertEqual(
                [DELEGATED_ARTIFACT, DELEGATED_SIDECAR],
                sorted(details["allowed_new_raw_paths"]),
                envelope,
            )
            self.assertFalse(any(details["raw_scope_violations"].values()), details)
            self.assertEqual(
                before,
                self.evidence_bytes(workspace),
                "a refused submission writes nothing of its own and removes nothing",
            )

            planted_path.unlink()
            code, session = self.submit(workspace, order["action_id"])
            self.assertEqual(0, code, session)
            self.assertEqual("research", session["phase"], session)


class ProviderUndeclaredCompanionTests(unittest.TestCase):
    """The provider (non-delegated) and blocked-partial arms, on the shipped harness.

    Bound off the shipped harness class through the module object rather than by
    inheritance: `OrchestrationControllerTests` carries its own large suite, and
    subclassing it would re-run every one of those tests under this file.
    """

    maxDiff = None

    run_module = _toc.OrchestrationControllerTests.run_module
    init_workspace = _toc.OrchestrationControllerTests.init_workspace
    controller = _toc.OrchestrationControllerTests.controller
    json_script = _toc.OrchestrationControllerTests.json_script
    assert_json_script_ok = _toc.OrchestrationControllerTests.assert_json_script_ok
    enable_academic_providers = _toc.OrchestrationControllerTests.enable_academic_providers
    manifest_records = _toc.OrchestrationControllerTests.manifest_records
    write_mock_acquired_paper = _toc.OrchestrationControllerTests.write_mock_acquired_paper
    start = _toc.OrchestrationControllerTests.start
    submit = _toc.OrchestrationControllerTests.submit
    # `DelegatedWorkspace` is deliberately not a TestCase, so borrowing from it binds no
    # tests here either — and this digest is the same question on either arm.
    evidence_bytes = DelegatedWorkspace.evidence_bytes

    # -- walk a real session to a pending provider acquisition order -----------------

    def walk_to_acquisition(self, root: Path) -> tuple[Path, str, str, dict]:
        """research -> discovery -> candidate_review -> acquisition, all through the loop.

        The order has to be a genuine one: the raw-scope guard compares a `raw/` tree
        snapshot taken when the order was ISSUED against the tree at submission, so an
        order fabricated after the delivery would compare the wrong two trees and could
        not reach the guard under test.
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

        _, acquisition_order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
        self.assertEqual("acquisition", acquisition_order["phase"], acquisition_order)
        self.assertNotEqual(
            CONTROLLER.ACQUISITION_MODE_DELEGATED,
            acquisition_order.get("acquisition_mode"),
            "these cases must exercise the provider (non-delegated) arm of the raw-scope guard",
        )
        return target, request_id, candidate_id, acquisition_order

    # -- the delivery -----------------------------------------------------------------

    def write_a_file_shaped_capture(self, target: Path, request_id: str, candidate_id: str) -> str:
        """The bytes of one file-shaped capture, written but not yet inventoried.

        File-shaped on purpose. A directory-shaped bundle's record admits its whole subtree,
        so a dot path written inside one is legitimately attributed to that record and
        proves nothing about the guard this file is measuring.

        Inventorying is a separate step so that a caller can plant the undeclared file
        first, and put `should_skip` between the two rather than merely after them.
        """
        return self.write_mock_acquired_paper(
            target, request_id=request_id, candidate_id=candidate_id
        )

    def inventory_and_assert_the_capture_is_the_only_record(self, target: Path, relative: str) -> str:
        """Run the acquirer's inventory and require it to have seen the capture alone.

        The undeclared file is on disk when this runs, so this is a live assertion about
        `source_inventory.should_skip` rather than a restatement of the writing order: if a
        dot-prefixed sibling ever started earning a record, the manifest-scope guard would
        refuse first and the raw-scope arm below would be getting credit for a refusal it
        never issued.
        """
        inventory = self.assert_json_script_ok(
            INVENTORY, ["--project-root", str(target), "--report", "--format", "json"]
        )
        self.assertEqual("ready_for_normalization", inventory["readiness"], inventory)
        records = self.manifest_records(target)
        self.assertEqual(
            [[relative]],
            [record.get("raw_paths") for record in records],
            "the capture must be the one record the order inherits, and inventory must "
            f"derive it as the file itself; raw tree was {raw_tree_files(target)}",
        )
        return str(records[0]["id"])

    def fulfil(self, target: Path, request_id: str, candidate_id: str, order: dict, source_id: str) -> None:
        """Normalize, fulfil, record the candidate transition and reopen the question."""
        self.assert_json_script_ok(
            NORMALIZE, ["--project-root", str(target), "--source-id", source_id, "--format", "json"]
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
             "--to-state", "fetched", "--reason", "Delivered the academic capture.",
             "--source-id", source_id, "--actor", "acquire-agent", "--run-id", order["run_id"]],
        )
        self.assert_json_script_ok(
            RESOLVE,
            ["--project-root", str(target), "reopen", "--slug", "test-question",
             "--agent-id", "acquire-agent", "--source-id", source_id,
             "--request-id", request_id, "--format", "json"],
        )

    # -- provider arm -----------------------------------------------------------------

    def test_an_undeclared_sibling_is_refused_on_the_provider_arm(self):
        """A completed provider acquisition refuses the extra file with the same diagnosis.

        Same shape as the delegated case, one message apart: this arm's refusal carries no
        `delegated ` prefix, and an operator matching on the delegated wording would miss it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root)

            relative = self.write_a_file_shaped_capture(target, request_id, candidate_id)
            undeclared = undeclared_sibling(relative)
            planted_path = plant(target, undeclared)
            source_id = self.inventory_and_assert_the_capture_is_the_only_record(target, relative)
            self.fulfil(target, request_id, candidate_id, order, source_id)

            before = self.evidence_bytes(target)
            code, payload, stderr = self.submit(
                root, target, order["action_id"],
                summary="Delivered the capture and left an undeclared file beside it.",
                artifacts=[relative, f"{relative}.provenance.yml", "sources/manifest.jsonl"],
            )

            self.assertEqual(
                CONTROLLER.EXIT_INVALID,
                code,
                diagnose(
                    "provider",
                    f"the undeclared sibling to be refused (exit {CONTROLLER.EXIT_INVALID})",
                    code, payload, target,
                ),
            )
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", payload["error_code"], payload)
            self.assertTrue(payload["recoverable"], payload)
            self.assertIn(
                RAW_SCOPE_REFUSAL,
                payload["message"],
                diagnose(
                    "provider",
                    "the raw-scope guard to be the one that refuses",
                    code, payload, target,
                ),
            )
            self.assertNotIn(
                DELEGATED_RAW_SCOPE_REFUSAL,
                payload["message"],
                "this arm's wording is the un-prefixed one, and the substring assertion above "
                f"would also pass on the delegated spelling: {payload}",
            )
            self.assertNotIn(
                MANIFEST_SCOPE_REFUSAL,
                payload["message"],
                f"a dot-prefixed file has no manifest record to be refused by scope: {payload}",
            )
            details = payload["details"]
            self.assertEqual(
                [undeclared],
                details["unexpected_new_raw_paths"],
                "the refusal must name the undeclared file, and name only it; raw tree was "
                f"{raw_tree_files(target)}",
            )
            self.assertEqual(
                [relative, f"{relative}.provenance.yml"],
                sorted(details["allowed_new_raw_paths"]),
                "the fulfilled capture and its sidecar are what the order authorised",
            )
            self.assertFalse(
                any(details["raw_scope_violations"].values()),
                f"nothing that existed at issuance was changed; the plant is a new file: {details}",
            )
            self.assertEqual(
                before,
                self.evidence_bytes(target),
                "a refused submission writes nothing of its own and removes nothing",
            )

            # The control: with the plant removed the same order is accepted, which is what
            # makes the refusal above evidence about this guard rather than about the fixture.
            planted_path.unlink()
            code, session, stderr = self.submit(
                root, target, order["action_id"],
                summary="Removed the undeclared file and resubmitted the same order.",
                artifacts=[relative, f"{relative}.provenance.yml", "sources/manifest.jsonl"],
            )
            self.assertEqual(0, code, (session, stderr))

    # -- blocked-partial arm ------------------------------------------------------------

    def test_an_undeclared_sibling_is_refused_on_the_blocked_partial_arm(self):
        """A partial delivery that leaves an extra file is refused rather than paused.

        `blocked` is how an acquirer reports "I fetched something but could not finish with
        it": the capture is on disk and inventoried, nothing is fulfilled or normalized. The
        arm admits that delivery as correlated residue and refuses everything else by name,
        and the refusal must leave the same order open for the acquirer to repair.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root)

            relative = self.write_a_file_shaped_capture(target, request_id, candidate_id)
            undeclared = undeclared_sibling(relative)
            planted_path = plant(target, undeclared)
            source_id = self.inventory_and_assert_the_capture_is_the_only_record(target, relative)

            before = self.evidence_bytes(target)
            code, payload, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Fetched the capture but could not normalize it; recording a partial delivery.",
                artifacts=[relative, f"{relative}.provenance.yml", "sources/manifest.jsonl"],
            )

            self.assertEqual(
                CONTROLLER.EXIT_INVALID,
                code,
                diagnose(
                    "blocked-partial",
                    f"the undeclared sibling to be refused (exit {CONTROLLER.EXIT_INVALID}) "
                    "rather than admitted as partial-delivery residue",
                    code, payload, target,
                ),
            )
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", payload["error_code"], payload)
            self.assertTrue(payload["recoverable"], payload)
            self.assertIn(
                BLOCKED_RAW_SCOPE_REFUSAL,
                payload["message"],
                diagnose(
                    "blocked-partial",
                    "the blocked-partial raw guard to be the one that refuses",
                    code, payload, target,
                ),
            )
            details = payload["details"]
            self.assertEqual(
                [undeclared],
                details["unexpected_new_raw_paths"],
                "the refusal must name the undeclared file, and name only it: the capture "
                f"itself is correlated residue. Raw tree: {raw_tree_files(target)}",
            )
            self.assertEqual(
                [relative, f"{relative}.provenance.yml"],
                sorted(details["allowed_new_raw_paths"]),
                "the correlated partial delivery is what this arm admits, and nothing wider",
            )
            self.assertFalse(any(details["raw_scope_violations"].values()), details)
            self.assertEqual(
                before,
                self.evidence_bytes(target),
                "a refused submission writes nothing of its own and removes nothing",
            )
            self.assertEqual(
                [source_id],
                [str(record["id"]) for record in self.manifest_records(target)],
                "a refused partial delivery must leave the manifest as it found it",
            )
            session = CONTROLLER.load_session(target, "orch-test")
            self.assertEqual("active", session["status"], session)
            self.assertEqual(
                order["action_id"],
                session["pending_action_id"],
                "the refusal must leave the same order open for the acquirer to remove the "
                f"undeclared file and resubmit, not pause or close the session: {stderr}",
            )

            # The control for this arm is a pause, not an exit 0: a partial delivery that
            # the guard accepts is exactly what `EXIT_PAUSED` records.
            planted_path.unlink()
            code, paused, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Removed the undeclared file and recorded the partial delivery again.",
                artifacts=[relative, f"{relative}.provenance.yml", "sources/manifest.jsonl"],
            )
            self.assertEqual(
                CONTROLLER.EXIT_PAUSED,
                code,
                diagnose(
                    "blocked-partial",
                    f"the repaired partial delivery to pause the session (exit {CONTROLLER.EXIT_PAUSED})",
                    code, paused, target,
                ),
            )
            self.assertEqual("paused", paused["status"], paused)


if __name__ == "__main__":
    unittest.main()
