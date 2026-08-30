"""A raw companion is admitted exactly when the sidecar beside its capture declares it.

A delivery may name companion files in its provenance sidecar -- a schema the payload is
keyed on, an observation record captured alongside it -- and inventory resolves each
declaration against the tree into ``provenance.companion_paths``. This file measures the
raw-scope guards' half of that contract, on all three acquisition arms:

  * delegated                 ``verify_delegated_acquisition_postconditions``
  * provider, non-delegated   ``verify_action_postconditions``
  * blocked partial delivery  ``verify_blocked_action_postconditions``

Both verdicts live here, and they are about the SAME FILE. Every plant in this suite is
written by one helper with one body under one name; what differs between a case that is
accepted and a case that is refused is whether the sidecar beside the capture names it.
A suite that planted one file for the green cases and a different one for the refusals
would be measuring the file, not the declaration.

  * DECLARED and resolved onto the record: admitted, and the action carrying it completes;
  * UNDECLARED: refused, naming the path the acquirer has to remove;
  * DECLARED under one name, delivered under another: refused, naming the delivered one.
    A declaration is not a wildcard, and a name inventory could not resolve admits nothing.

The plants are DOT-PREFIXED, which is what makes any of this reachable at raw scope rather
than one guard earlier. ``source_inventory.should_skip`` drops any path with a dot-prefixed
component before the raw walk ever sees it, so such a file never becomes a manifest record
of its own -- a declaration is the only route it has onto a record at all;
``orchestration_controller.raw_tree_snapshot`` consults no skip predicate and lstats every
regular file under each raw root. The snapshot sees it, inventory does not, and the
raw-scope guard is the one left to decide.

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

Admission is per-record and per-directory, and the negative half pins both edges. A
companion belongs to the capture whose sidecar named it: it is not admitted beside a
source the action never touched, and a companion already in the order's baseline is raw
evidence like any other -- editing its bytes mid-order lands in ``changed_outside_scope``,
which is a different leg of the guard from the one a declaration widens.

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

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tests.test_orchestration_controller as _toc  # noqa: E402
from tests.test_delegated_acquisition_e2e import (  # noqa: E402
    ACQUIRER,
    PAYLOAD,
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
# The companion half of the inventory-agreement check, spelled the same way on each arm and
# for the same reason: the provider spelling is the bare one, the other two prefix it.
COMPANION_MISMATCH_REFUSAL = (
    "acquisition manifest companion_paths do not match inventory-derived attribution"
)
DELEGATED_COMPANION_MISMATCH_REFUSAL = f"delegated {COMPANION_MISMATCH_REFUSAL}"
BLOCKED_COMPANION_MISMATCH_REFUSAL = f"blocked {COMPANION_MISMATCH_REFUSAL}"

#: What the companion looks like: an extra capture named after the artifact it was observed
#: beside, dot-prefixed and carrying no provenance sidecar of its own. One shape for the
#: whole suite -- the declared cases and the undeclared ones plant this same file, so the
#: verdicts they earn differ by the declaration alone.
UNDECLARED_SUFFIX = ".observation.json"
UNDECLARED_BODY = (
    '{\n  "observed_at": "2026-08-20T12:00:00Z",\n  "observer": "acquirer-side capture"\n}\n'
)
#: A second companion-shaped name, for the case where a sidecar declares one name and the
#: delivery writes another.
ALTERNATE_SUFFIX = ".appendix.json"
ALTERNATE_BODY = (
    '{\n  "appendix": "supplementary capture",\n  "observer": "acquirer-side capture"\n}\n'
)
#: Different bytes under the SAME name, for the case where a companion already in the
#: order's baseline is edited while the order is open.
EDITED_BODY = (
    '{\n  "observed_at": "2026-08-21T09:30:00Z",\n  "observer": "acquirer-side capture"\n}\n'
)

DELEGATED_ARTIFACT = f"raw/data/{PAYLOAD.name}"
DELEGATED_SIDECAR = f"{DELEGATED_ARTIFACT}.provenance.yml"
PLAIN_NAMED_SIBLING = "raw/data/companion.json"


def companion_beside(relative: str, suffix: str = UNDECLARED_SUFFIX) -> str:
    """The dot-prefixed path a companion of ``relative`` must occupy to be declarable.

    Dot-prefixed and carrying the capture's own file name: the leading dot is what keeps
    the raw walk from inventorying it as a source of its own, and the capture's name is
    what stops one capture's companion from being read as a neighbour's. A declaration
    naming anything else is refused by inventory before a guard ever sees it.
    """
    artifact = Path(relative)
    return (artifact.parent / f".{artifact.name}{suffix}").as_posix()


def undeclared_sibling(relative: str) -> str:
    """The dot-prefixed path this suite plants beside the artifact at ``relative``."""
    return companion_beside(relative)


def write_raw_file(workspace: Path, relative: str, body: str) -> Path:
    """Write one file under ``raw/`` and return it, so a control can remove it again."""
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def plant(workspace: Path, relative: str) -> Path:
    """Write the undeclared delivery and return it, so a control can remove it again."""
    return write_raw_file(workspace, relative, UNDECLARED_BODY)


def manifest_records(workspace: Path) -> list[dict]:
    path = workspace / "sources" / "manifest.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def resolved_companions(workspace: Path, source_id: str) -> list[str]:
    """``provenance.companion_paths`` for one record: what inventory RESOLVED, not what was asked.

    The declared ``companions`` beside it is what the delivery requested and is never acted
    on, so a green case that read that key would pass without inventory having admitted
    anything -- and the guard under test reads the resolved key.
    """
    for record in manifest_records(workspace):
        if str(record.get("id")) == source_id:
            provenance = record.get("provenance")
            if not isinstance(provenance, dict):
                return []
            return [path for path in provenance.get("companion_paths", []) if isinstance(path, str)]
    raise AssertionError(f"no manifest record {source_id} under {workspace}")


def declare_companions_in_sidecar(workspace: Path, relative: str, declared: list[str]) -> None:
    """Add a ``companions:`` list to a capture's already-written provenance sidecar.

    Written onto the sidecar the shipped fixtures produce rather than through a new
    delivery helper, so the only difference between a declared case and its undeclared
    twin is this one key.
    """
    sidecar_path = workspace / f"{relative}.provenance.yml"
    sidecar = yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
    sidecar["companions"] = list(declared)
    sidecar_path.write_text(yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8")


def source_id_for(workspace: Path, relative: str) -> str:
    """The id of the record whose ``raw_paths`` names ``relative``."""
    for record in manifest_records(workspace):
        raw_paths = record.get("raw_paths")
        if isinstance(raw_paths, list) and relative in raw_paths:
            return str(record["id"])
    raise AssertionError(f"no manifest record names {relative} under {workspace}")


def rewrite_manifest_record(workspace: Path, source_id: str, **fields: object) -> None:
    """Replace fields on one manifest record in place, leaving every other record alone."""
    path = workspace / "sources" / "manifest.jsonl"
    rewritten: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if str(record.get("id")) == source_id:
            record.update(fields)
        rewritten.append(json.dumps(record))
    path.write_text("".join(f"{line}\n" for line in rewritten), encoding="utf-8")


def rewrite_companion_paths(workspace: Path, source_id: str, companion_paths: list[str]) -> None:
    """Replace one record's RESOLVED companion list, leaving the rest of its provenance alone."""
    for record in manifest_records(workspace):
        if str(record.get("id")) == source_id:
            provenance = dict(record.get("provenance") or {})
            provenance["companion_paths"] = list(companion_paths)
            rewrite_manifest_record(workspace, source_id, provenance=provenance)
            return
    raise AssertionError(f"no manifest record {source_id} under {workspace}")


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


class DelegatedDeclaredCompanionTests(DelegatedWorkspace, unittest.TestCase):
    """The delegated arm: the same extra file, this time named by the sidecar delivering it."""

    maxDiff = None

    def deliver_declaring(
        self,
        workspace: Path,
        request_id: str,
        *,
        declared: list[str],
        written: dict[str, str],
    ) -> str:
        """`DelegatedWorkspace.deliver_for` with a `companions:` list and files beside it.

        Local to this file rather than a `companions` keyword on the shared harness:
        nothing else in the suite delivers one, and a keyword no other caller passes is a
        seam every reader of that harness would have to step over.

        `written` is written BEFORE the inventory run, which is the only order that can
        work: inventory resolves each declared name against the tree and drops what is not
        there, so an acquirer capturing the artifact and its companion has to leave both
        behind before it runs. `declared` and `written` are separate arguments precisely so
        a caller can make them disagree.
        """
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        payload = destination / PAYLOAD.name
        shutil.copy2(PAYLOAD, payload)
        sidecar = yaml.safe_load(
            PAYLOAD.with_name(PAYLOAD.name + ".provenance.yml").read_text(encoding="utf-8")
        )
        sidecar["retrieved_by"] = ACQUIRER
        sidecar["request_id"] = request_id
        sidecar["checksum"] = f"sha256:{hashlib.sha256(payload.read_bytes()).hexdigest()}"
        sidecar["companions"] = list(declared)
        (destination / (PAYLOAD.name + ".provenance.yml")).write_text(
            yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
        )
        for relative, body in written.items():
            write_raw_file(workspace, relative, body)
        self.run_script(DELEGATED_INVENTORY, ["--report"], workspace)
        source_id = self.source_id_for(workspace, DELEGATED_ARTIFACT)
        self.run_script(DELEGATED_NORMALIZE, ["--source-id", source_id], workspace)
        return source_id

    def test_a_declared_companion_is_admitted_and_the_delegated_order_completes(self):
        """The file the undeclared case is refused for, declared, closes the same order.

        Byte for byte the plant of `test_an_undeclared_sibling_...`: same path, same body,
        same moment. The sidecar naming it is the only difference, and the accepted
        submission here against the refusal there is the whole measurement.

        `companion_paths` is asserted before the submission, because a declaration that
        inventory dropped would leave the record admitting nothing and the acceptance below
        would be recording an empty delivery rather than an admitted companion.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)

            companion = companion_beside(DELEGATED_ARTIFACT)
            source_id = self.deliver_declaring(
                workspace,
                request_id,
                declared=[PurePosixPath(companion).name],
                written={companion: UNDECLARED_BODY},
            )
            self.assertEqual(
                [companion],
                resolved_companions(workspace, source_id),
                "inventory must have resolved the declaration onto the record; without that "
                f"this order admits nothing extra. Raw tree: {raw_tree_files(workspace)}",
            )
            self.assertEqual(
                [source_id],
                [str(record["id"]) for record in manifest_records(workspace)],
                "the companion must not have earned a record of its own; a declaration is "
                "its only route onto one",
            )
            self.fulfil_and_reopen(workspace, request_id, source_id)

            code, session = self.submit(workspace, order["action_id"])
            self.assertEqual(
                0,
                code,
                diagnose(
                    "delegated",
                    "the declared companion to be admitted (exit 0)",
                    code, session, workspace,
                ),
            )
            self.assertEqual("research", session["phase"], session)
            self.assertIn(
                companion,
                raw_tree_files(workspace),
                "an accepted submission leaves the companion where the acquirer delivered it",
            )

    def test_a_companion_delivered_under_a_name_the_sidecar_did_not_declare_is_refused(self):
        """A declaration admits one name, not a shape of names.

        The sidecar names `<capture>.observation.json`; the delivery writes
        `<capture>.appendix.json` instead. Both are companion-shaped, both sit beside the
        capture, and neither the declared name nor the delivered one reaches the record:
        inventory resolves declarations against the tree and the declared file is not
        there, so `companion_paths` is empty and the delivered file is admitted by nothing.

        That is the case a guard keyed on "looks like a companion" rather than on the
        resolved list would wave through, so the allowed set is asserted alongside the
        refusal: neither name may appear in it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)

            declared = companion_beside(DELEGATED_ARTIFACT)
            delivered = companion_beside(DELEGATED_ARTIFACT, ALTERNATE_SUFFIX)
            source_id = self.deliver_declaring(
                workspace,
                request_id,
                declared=[PurePosixPath(declared).name],
                written={delivered: ALTERNATE_BODY},
            )
            self.assertEqual(
                [],
                resolved_companions(workspace, source_id),
                "a declared name that is not on disk resolves to nothing, so the record "
                f"admits neither file. Raw tree: {raw_tree_files(workspace)}",
            )
            self.fulfil_and_reopen(workspace, request_id, source_id)

            before = self.evidence_bytes(workspace)
            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(
                CONTROLLER.EXIT_INVALID,
                code,
                diagnose(
                    "delegated",
                    f"the undeclared name to be refused (exit {CONTROLLER.EXIT_INVALID})",
                    code, envelope, workspace,
                ),
            )
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"], envelope)
            self.assertIn(
                DELEGATED_RAW_SCOPE_REFUSAL,
                envelope["message"],
                diagnose(
                    "delegated",
                    "the raw-scope guard to be the one that refuses",
                    code, envelope, workspace,
                ),
            )
            details = envelope["details"]
            self.assertEqual(
                [delivered],
                details["unexpected_new_raw_paths"],
                "the refusal must name the file that was actually written, not the one the "
                f"sidecar asked for; raw tree was {raw_tree_files(workspace)}",
            )
            self.assertEqual(
                [DELEGATED_ARTIFACT, DELEGATED_SIDECAR],
                sorted(details["allowed_new_raw_paths"]),
                "neither the declared-but-absent name nor the delivered one may be admitted: "
                "the record resolved no companion at all",
            )
            self.assertEqual(
                before,
                self.evidence_bytes(workspace),
                "a refused submission writes nothing of its own and removes nothing",
            )

            # The control: the same order, the same unresolved declaration, with only the
            # undeclared file removed. Accepted -- so the refusal above is about the file
            # and not about the sidecar carrying a name inventory could not resolve.
            (workspace / delivered).unlink()
            code, session = self.submit(workspace, order["action_id"])
            self.assertEqual(0, code, session)
            self.assertEqual("research", session["phase"], session)


class ProviderHarness:
    """Session, delivery and fulfilment drivers for the provider and blocked-partial arms.

    Bound off the shipped harness class through the module object rather than by
    inheritance, and held on a plain class rather than a `TestCase`:
    `OrchestrationControllerTests` carries its own large suite, and subclassing it would
    re-run every one of those tests under this file.
    """

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
        not reach the guard under test.

        `between_actions`, when given, is called with `(target, request_id, candidate_id)`
        in the gap the completed candidate review leaves — after that submission, before
        the acquisition order is issued. That gap is the only place a caller can put
        evidence that must be BOTH pre-existing at issuance and stamped for the request and
        candidate this order will scope, because the candidate id does not exist until
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


class ProviderUndeclaredCompanionTests(ProviderHarness, unittest.TestCase):
    """The provider (non-delegated) and blocked-partial arms refuse the undeclared file."""

    maxDiff = None

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


class ProviderDeclaredCompanionTests(ProviderHarness, unittest.TestCase):
    """The provider and blocked-partial arms admit a declared companion, and only that.

    The blocked cases here are the ones that reach the arm's OTHER admission site. A
    completed acquisition and a blocked one that appends a record both admit through
    inventory attribution; a blocked delivery that continues an order whose correlated
    record ALREADY EXISTED admits through the arm's own per-record re-add, which consults
    attribution not at all. A companion admitted in the first place and refused in the
    second would make the same delivery submittable as a completion and not as the partial
    delivery that precedes it, so the pre-existing-record cases are pinned here separately.
    """

    maxDiff = None

    #: A second capture the order neither scopes nor fulfils: correlated to nothing, its
    #: sidecar declaring a companion of its own.
    BYSTANDER = "raw/papers/prior-review.html"
    BYSTANDER_BODY = (
        "<html><head><title>Prior Review</title></head>"
        "<body>Held in this workspace before the order and fulfilled by nothing in it.</body>"
        "</html>\n"
    )

    # -- deliveries -------------------------------------------------------------------

    def deliver_declaring_a_companion(
        self, target: Path, request_id: str, candidate_id: str
    ) -> tuple[str, str]:
        """One file-shaped capture, its sidecar naming a companion, and the companion itself.

        The companion is byte-identical to what the undeclared cases plant beside the same
        capture, and lands in the same place at the same moment. The `companions:` key on
        the sidecar is the only difference between this delivery and that one.
        """
        relative = self.write_a_file_shaped_capture(target, request_id, candidate_id)
        companion = companion_beside(relative)
        declare_companions_in_sidecar(target, relative, [PurePosixPath(companion).name])
        write_raw_file(target, companion, UNDECLARED_BODY)
        return relative, companion

    def deliver_a_bystander_declaring_a_companion(self, target: Path) -> tuple[str, str]:
        """A capture correlated to nothing, whose sidecar declares a companion of its own.

        No `request_id` and no `candidate_id`, which is what evidence acquired for an
        earlier purpose looks like. The order below neither scopes it nor fulfils it, so
        whatever its record declares is outside every allowed set the order builds.
        """
        path = write_raw_file(target, self.BYSTANDER, self.BYSTANDER_BODY)
        companion = companion_beside(self.BYSTANDER)
        (target / f"{self.BYSTANDER}.provenance.yml").write_text(
            yaml.safe_dump(
                {
                    "origin_url": "https://example.test/reviews/prior-review",
                    "retrieved_at": "2026-07-01T00:00:00Z",
                    "retrieved_by": "fetch_sources.py/arxiv",
                    "license": "CC-BY-4.0",
                    "terms_url": "https://info.arxiv.org/help/license/index.html",
                    "checksum": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
                    "companions": [PurePosixPath(companion).name],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        write_raw_file(target, companion, UNDECLARED_BODY)
        return self.BYSTANDER, companion

    def order_over_a_pre_existing_capture(
        self, root: Path, *, companion_at_issuance: bool
    ) -> tuple[Path, dict, str, str, str]:
        """An order whose one correlated record, and the companion it declares, pre-date it.

        Delivered and inventoried in the gap `walk_to_acquisition` leaves between the
        completed candidate review and the order, which is the only point where evidence can
        be BOTH pre-existing at issuance and stamped for the request and candidate the order
        will scope. What that buys the cases below is a correlated record the action under
        test did not append — the state in which the blocked arm admits through its own
        per-record re-add rather than through inventory attribution.

        `companion_at_issuance` decides which of the two questions the caller is asking.
        Kept means the companion is in the order's baseline, so moving its bytes is a change
        to evidence the order was issued against. Removed after the inventory run means the
        record NAMES a companion the baseline does not contain — the only arrangement in
        which re-supplying it is a new raw path, and the state a transfer that dropped one
        file of a capture leaves behind.

        Returns `(target, order, capture, companion, source_id)`.
        """
        delivered: dict[str, str] = {}

        def deliver_between_actions(workspace: Path, request_id: str, candidate_id: str) -> None:
            relative, companion = self.deliver_declaring_a_companion(
                workspace, request_id, candidate_id
            )
            source_id = self.inventory_and_assert_the_capture_is_the_only_record(
                workspace, relative
            )
            self.assertEqual(
                [companion],
                resolved_companions(workspace, source_id),
                "the pre-existing record must carry the resolved companion; a record that "
                "declares nothing makes every case below a test of the undeclared path",
            )
            delivered.update(relative=relative, companion=companion, source_id=source_id)
            if not companion_at_issuance:
                (workspace / companion).unlink()

        target, _, _, order = self.walk_to_acquisition(
            root, between_actions=deliver_between_actions
        )
        self.assertEqual(
            [delivered["source_id"]],
            [str(record["id"]) for record in self.manifest_records(target)],
            "the capture's record must be the manifest state the order inherits, so the "
            "action under test appends no correlated record of its own",
        )
        return (
            target,
            order,
            delivered["relative"],
            delivered["companion"],
            delivered["source_id"],
        )

    # -- provider arm -----------------------------------------------------------------

    def test_a_declared_companion_is_admitted_and_the_provider_order_completes(self):
        """The file the undeclared provider case is refused for, declared, completes the order.

        Same capture, same plant, same moment as
        `test_an_undeclared_sibling_is_refused_on_the_provider_arm`; the sidecar naming it
        is the only difference. `companion_paths` is asserted first, so an acceptance here
        cannot come from a declaration inventory quietly dropped.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root)

            relative, companion = self.deliver_declaring_a_companion(
                target, request_id, candidate_id
            )
            source_id = self.inventory_and_assert_the_capture_is_the_only_record(target, relative)
            self.assertEqual(
                [companion],
                resolved_companions(target, source_id),
                "inventory must have resolved the declaration onto the record; raw tree was "
                f"{raw_tree_files(target)}",
            )
            self.fulfil(target, request_id, candidate_id, order, source_id)

            code, payload, stderr = self.submit(
                root, target, order["action_id"],
                summary="Delivered the capture and the companion its sidecar declares.",
                artifacts=[
                    relative,
                    f"{relative}.provenance.yml",
                    companion,
                    "sources/manifest.jsonl",
                ],
            )
            self.assertEqual(
                0,
                code,
                diagnose(
                    "provider",
                    "the declared companion to be admitted (exit 0)",
                    code, payload, target,
                ),
            )
            self.assertIn(
                companion,
                raw_tree_files(target),
                f"an accepted submission leaves the companion on disk: {stderr}",
            )

    # -- blocked-partial arm ------------------------------------------------------------

    def test_a_late_delivered_companion_of_a_pre_existing_record_is_admitted(self):
        """A partial delivery may supply the companion a correlated record already names.

        This is the arm's second admission site and the only case that reaches it. The
        correlated record was in the manifest before the order was issued, so the action
        appends nothing and inventory attribution answers an empty question; everything
        admitted here comes from the arm's own per-record re-add over the correlated
        records. A companion admitted through attribution and not through that re-add would
        make this exact delivery submittable as a completed acquisition and refused as the
        partial one that precedes it.

        Nothing is re-inventoried inside the order, deliberately: the manifest already names
        the companion, and re-deriving it would change a pre-existing record, which this arm
        refuses several checks earlier for reasons that have nothing to do with companions.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, order, relative, companion, source_id = self.order_over_a_pre_existing_capture(
                root, companion_at_issuance=False
            )
            self.assertNotIn(
                companion,
                raw_tree_files(target),
                "the companion must be absent from the order's baseline, or re-supplying it "
                "is not a new raw path and this case measures nothing",
            )

            write_raw_file(target, companion, UNDECLARED_BODY)

            code, payload, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Re-supplied the declared companion but could not finish the capture.",
                artifacts=[relative, f"{relative}.provenance.yml", companion],
            )
            self.assertEqual(
                CONTROLLER.EXIT_PAUSED,
                code,
                diagnose(
                    "blocked-partial",
                    "the declared companion to be admitted as correlated residue and the "
                    f"session to pause (exit {CONTROLLER.EXIT_PAUSED})",
                    code, payload, target,
                ),
            )
            self.assertEqual("paused", payload["status"], payload)
            self.assertEqual(
                [source_id],
                [str(record["id"]) for record in self.manifest_records(target)],
                f"the partial delivery appends no record of its own: {stderr}",
            )

    def test_editing_a_companion_the_order_was_issued_against_is_refused(self):
        """A companion in the baseline is raw evidence, and raw evidence is immutable.

        Declaring a companion widens what an action may ADD. It says nothing about what an
        action may change, and the two are different legs of the same guard: a companion
        already present at issuance is compared by digest like every other raw entry, and
        moving its bytes lands in `changed_outside_scope`.

        `added_outside_scope` is asserted empty on purpose rather than left unmentioned. It
        is empty by construction on every arm — each passes its own set of new entries as
        the allowed additions — so a test that looked there for this refusal would pass
        against a guard that had stopped checking anything at all.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, order, relative, companion, source_id = self.order_over_a_pre_existing_capture(
                root, companion_at_issuance=True
            )
            self.assertIn(
                companion,
                raw_tree_files(target),
                "the companion must be in the order's baseline for an edit to be a change",
            )

            write_raw_file(target, companion, EDITED_BODY)

            code, payload, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Re-observed the capture and rewrote its companion; recording a partial delivery.",
                artifacts=[relative, f"{relative}.provenance.yml", companion],
            )
            self.assertEqual(
                CONTROLLER.EXIT_INVALID,
                code,
                diagnose(
                    "blocked-partial",
                    f"the edited companion to be refused (exit {CONTROLLER.EXIT_INVALID})",
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
                    "the raw-scope guard to be the one that refuses",
                    code, payload, target,
                ),
            )
            violations = payload["details"]["raw_scope_violations"]
            self.assertEqual(
                [companion],
                violations["changed_outside_scope"],
                "the refusal must name the companion whose bytes moved; raw tree was "
                f"{raw_tree_files(target)}",
            )
            self.assertEqual([], violations["removed"], payload)
            self.assertEqual(
                [],
                violations["added_outside_scope"],
                "every new entry is passed as an allowed addition, so this leg is empty "
                "whatever the guard decides; the verdict above is the one that counts",
            )
            self.assertEqual(
                [],
                payload["details"]["unexpected_new_raw_paths"],
                "nothing was added: the companion already existed and was rewritten",
            )

            # The control: restoring the bytes the order was issued against pauses the same
            # partial delivery, so the refusal above is about the edit and not the fixture.
            write_raw_file(target, companion, UNDECLARED_BODY)
            code, paused, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Restored the companion and recorded the partial delivery again.",
                artifacts=[relative, f"{relative}.provenance.yml", companion],
            )
            self.assertEqual(
                CONTROLLER.EXIT_PAUSED,
                code,
                diagnose(
                    "blocked-partial",
                    f"the restored companion to pause the session (exit {CONTROLLER.EXIT_PAUSED})",
                    code, paused, target,
                ),
            )
            self.assertEqual(
                [source_id],
                [str(record["id"]) for record in self.manifest_records(target)],
                f"neither submission appends a record: {stderr}",
            )

    SMUGGLED = "raw/papers/second-capture.html"
    SMUGGLED_BODY = (
        "<html><head><title>Second Capture</title></head>"
        "<body>Named by a companion list and accounted for by no record.</body></html>\n"
    )

    def pre_existing_record_rewritten(
        self, root: Path, rewrite: Callable[[Path, str, str, str], None]
    ) -> tuple[Path, dict, str, str]:
        """An order over a pre-existing correlated record whose manifest entry was rewritten.

        `raw_paths` and `provenance.companion_paths` on a record this arm did not append are
        manifest text and nothing re-derives them: the equality check against inventory
        covers only records the action itself appended. So a record whose entry disagrees
        with where its companion actually sits — hand-edited, or stale after a move — is a
        state the guard has to answer rather than one it can assume away, and `rewrite`
        builds each such state after the inventory run that produced the honest record.

        `rewrite` is called with `(workspace, source_id, capture, companion)` in the
        between-actions gap. Returns `(target, order, capture, companion)`.
        """
        built: dict[str, str] = {}

        def deliver_between_actions(workspace: Path, request_id: str, candidate_id: str) -> None:
            capture, companion = self.deliver_declaring_a_companion(
                workspace, request_id, candidate_id
            )
            source_id = self.inventory_and_assert_the_capture_is_the_only_record(
                workspace, capture
            )
            self.assertEqual(
                [companion],
                resolved_companions(workspace, source_id),
                "the rewrite below must start from a record that honestly resolved its "
                "companion, or it is editing something other than the state under test",
            )
            (workspace / companion).unlink()
            rewrite(workspace, source_id, capture, companion)
            built.update(capture=capture, companion=companion, source_id=source_id)

        target, _, _, order = self.walk_to_acquisition(
            root, between_actions=deliver_between_actions
        )
        return target, order, built["capture"], built["companion"]

    def test_a_companion_is_not_admitted_beside_a_path_its_record_does_not_name(self):
        """A companion list belongs to one capture, not to every path its record declares.

        A record carries one companion list — whatever its primary sidecar resolved — and may
        declare several raw paths. Pairing that list with each of them in turn would admit a
        dot-file beside a capture no sidecar ever named it for, so admission is scoped to the
        directory of the path the companion was claimed for.

        Two captures may share a file name in different raw roots, which is what makes the
        directory the load-bearing half of that answer rather than a restatement of the
        naming rule: here the record's declared path is `raw/web/<name>`, the companion still
        sits at `raw/papers/.<name>.…`, and the companion's name matches the declared capture
        exactly. Only where it sits separates the two.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            relocated: dict[str, str] = {}

            def relocate(workspace: Path, source_id: str, capture: str, companion: str) -> None:
                moved = f"raw/web/{PurePosixPath(capture).name}"
                write_raw_file(workspace, moved, self.BYSTANDER_BODY)
                rewrite_manifest_record(workspace, source_id, raw_paths=[moved])
                self.assertEqual(
                    [companion],
                    resolved_companions(workspace, source_id),
                    "the companion list must survive the move; the whole question is what "
                    "happens when it no longer sits beside a path the record names",
                )
                relocated["moved"] = moved

            target, order, _, companion = self.pre_existing_record_rewritten(root, relocate)
            moved = relocated["moved"]
            write_raw_file(target, companion, UNDECLARED_BODY)

            code, payload, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Wrote a companion beside a capture the record no longer declares.",
                artifacts=[moved],
            )
            self.assertEqual(
                CONTROLLER.EXIT_INVALID,
                code,
                diagnose(
                    "blocked-partial",
                    "a companion outside the directory of every declared path to be refused "
                    f"(exit {CONTROLLER.EXIT_INVALID})",
                    code, payload, target,
                ),
            )
            self.assertIn(
                BLOCKED_RAW_SCOPE_REFUSAL,
                payload["message"],
                diagnose(
                    "blocked-partial",
                    "the raw-scope guard to be the one that refuses",
                    code, payload, target,
                ),
            )
            details = payload["details"]
            self.assertEqual(
                [companion],
                details["unexpected_new_raw_paths"],
                "the refusal must name the file written outside every declared path's "
                f"directory; raw tree was {raw_tree_files(target)}",
            )
            self.assertEqual(
                [moved, f"{moved}.provenance.yml"],
                sorted(details["allowed_new_raw_paths"]),
                "the record contributes the path it declares and that path's sidecar; a "
                "companion list cannot reach out of that directory",
            )

            # The control: with the plant removed the same partial delivery pauses, so the
            # refusal above is about where the file was written and not about the fixture.
            (target / companion).unlink()
            code, paused, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Removed the misplaced companion and recorded the partial delivery again.",
                artifacts=[moved],
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

    def test_a_companion_path_naming_an_ordinary_file_beside_the_capture_is_refused(self):
        """What a companion list buys is one companion, not one file of the acquirer's choice.

        The allowance a declaration earns is a dot-prefixed file named after the capture:
        dot-prefixed so the raw walk never reads it as a source, and named after its capture
        so it belongs to that one. A guard that admitted whatever `companion_paths` contained
        would inherit those properties from a manifest field rather than checking them, and
        this arm treats a pre-existing record's manifest entry as acquirer-written text
        everywhere else.

        So the list here names an ordinary, plain-named capture in the same directory. It is
        source-shaped: delivered, it would be raw evidence that no record accounts for and
        that a later inventory run would turn into a record the order never fulfilled with.
        The guard must refuse it on the name alone.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            def name_an_ordinary_file(
                workspace: Path, source_id: str, capture: str, _companion: str
            ) -> None:
                rewrite_companion_paths(workspace, source_id, [self.SMUGGLED])
                self.assertEqual(
                    PurePosixPath(self.SMUGGLED).parent,
                    PurePosixPath(capture).parent,
                    "the named file must sit in the capture's own directory, so the refusal "
                    "is earned by its name rather than by where it is",
                )

            target, order, capture, _ = self.pre_existing_record_rewritten(
                root, name_an_ordinary_file
            )
            write_raw_file(target, self.SMUGGLED, self.SMUGGLED_BODY)

            code, payload, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Delivered a second capture named by the record's companion list.",
                artifacts=[capture, f"{capture}.provenance.yml", self.SMUGGLED],
            )
            self.assertEqual(
                CONTROLLER.EXIT_INVALID,
                code,
                diagnose(
                    "blocked-partial",
                    "a plain-named file in a companion list to be refused (exit "
                    f"{CONTROLLER.EXIT_INVALID})",
                    code, payload, target,
                ),
            )
            self.assertIn(
                BLOCKED_RAW_SCOPE_REFUSAL,
                payload["message"],
                diagnose(
                    "blocked-partial",
                    "the raw-scope guard to be the one that refuses",
                    code, payload, target,
                ),
            )
            details = payload["details"]
            self.assertEqual(
                [self.SMUGGLED],
                details["unexpected_new_raw_paths"],
                f"the refusal must name the delivered file; raw tree was {raw_tree_files(target)}",
            )
            self.assertEqual(
                [capture, f"{capture}.provenance.yml"],
                sorted(details["allowed_new_raw_paths"]),
                "a companion list may widen admission by a companion and by nothing else",
            )

            # The control: with the plant removed the same partial delivery pauses.
            (target / self.SMUGGLED).unlink()
            code, paused, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Removed the second capture and recorded the partial delivery again.",
                artifacts=[capture, f"{capture}.provenance.yml"],
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

    def test_a_companion_materialising_beside_an_untouched_source_is_refused(self):
        """Admission is per-record: another source's declaration authorises nothing here.

        A second capture sits in this workspace from before the order, correlated to
        nothing, and its sidecar declares a companion exactly as the scoped capture's does.
        The order neither scopes it nor fulfils it, so its record contributes to no allowed
        set — and a companion appearing beside it while the order is open is a file this
        action has no business writing.

        The failure this pins is a guard that collected companions from every record it
        could see rather than from the records the order admits. That reads as harmless —
        the declaration is genuine, the naming rule is satisfied, inventory resolved it —
        and it would hand an acquisition write access to a dot-file beside every capture in
        the workspace.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            planted: dict[str, str] = {}

            def deliver_between_actions(workspace: Path, request_id: str, candidate_id: str) -> None:
                relative = self.write_a_file_shaped_capture(workspace, request_id, candidate_id)
                bystander, companion = self.deliver_a_bystander_declaring_a_companion(workspace)
                self.assert_json_script_ok(
                    INVENTORY, ["--project-root", str(workspace), "--report", "--format", "json"]
                )
                bystander_id = source_id_for(workspace, bystander)
                self.assertEqual(
                    [companion],
                    resolved_companions(workspace, bystander_id),
                    "the untouched source must genuinely declare this companion, or the "
                    "refusal below is only the ordinary undeclared one",
                )
                self.assertEqual(
                    [],
                    resolved_companions(workspace, source_id_for(workspace, relative)),
                    "the scoped capture must declare nothing, so nothing it owns could "
                    "account for the planted file",
                )
                planted.update(capture=relative, bystander=bystander, companion=companion)
                # Removed after the inventory that recorded it, so re-writing it during the
                # order is a NEW raw path rather than a change to one already present.
                (workspace / companion).unlink()

            target, _, _, order = self.walk_to_acquisition(
                root, between_actions=deliver_between_actions
            )
            capture = planted["capture"]
            companion = planted["companion"]

            write_raw_file(target, companion, UNDECLARED_BODY)

            code, payload, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Wrote beside an earlier capture and stopped; recording a partial delivery.",
                artifacts=[capture, f"{capture}.provenance.yml"],
            )
            self.assertEqual(
                CONTROLLER.EXIT_INVALID,
                code,
                diagnose(
                    "blocked-partial",
                    "the companion of an untouched source to be refused (exit "
                    f"{CONTROLLER.EXIT_INVALID})",
                    code, payload, target,
                ),
            )
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", payload["error_code"], payload)
            self.assertIn(
                BLOCKED_RAW_SCOPE_REFUSAL,
                payload["message"],
                diagnose(
                    "blocked-partial",
                    "the raw-scope guard to be the one that refuses",
                    code, payload, target,
                ),
            )
            details = payload["details"]
            self.assertEqual(
                [companion],
                details["unexpected_new_raw_paths"],
                "the refusal must name the planted file, and name only it; raw tree was "
                f"{raw_tree_files(target)}",
            )
            self.assertNotIn(
                companion,
                details["allowed_new_raw_paths"],
                "the untouched source's declaration must widen nothing this order admits",
            )
            self.assertFalse(any(details["raw_scope_violations"].values()), details)
            session = CONTROLLER.load_session(target, "orch-test")
            self.assertEqual(
                order["action_id"],
                session["pending_action_id"],
                "the refusal must leave the same order open for the acquirer to repair: "
                f"{stderr}",
            )

            # The control: with the plant removed the same partial delivery pauses.
            (target / companion).unlink()
            code, paused, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Removed the file written beside the earlier capture and resubmitted.",
                artifacts=[capture, f"{capture}.provenance.yml"],
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


def record_provenance(workspace: Path, source_id: str) -> dict:
    """One record's whole ``provenance`` block, so a case can assert a key is ABSENT.

    ``resolved_companions`` reads a missing ``companion_paths`` and an empty one as the
    same ``[]``, which is the right answer for a guard and the wrong one for the case
    that has to show a workspace declaring nothing anywhere.
    """
    for record in manifest_records(workspace):
        if str(record.get("id")) == source_id:
            provenance = record.get("provenance")
            return provenance if isinstance(provenance, dict) else {}
    raise AssertionError(f"no manifest record {source_id} under {workspace}")


class DelegatedStaleCompanionRecordTests(DelegatedWorkspace, unittest.TestCase):
    """The delegated arm: a companion delivered after the inventory run that recorded its capture.

    Every other declared case in this file writes the companion BEFORE inventory, which is
    the order that produces an honest record: inventory resolves the declaration against a
    tree that contains the file, and ``provenance.companion_paths`` names it. Reverse those
    two moments and the record is left saying the delivery has no companion while the tree
    says it has one.

    Nothing about the file changes -- same name, same body, same directory, same
    declaration. What changes is whether the manifest accounts for it, and the record is
    where the accounting lives: ``raw_fingerprint`` covers the companions the record
    carries, so a companion no record carries is raw evidence whose edits re-trigger no
    normalization, which is the whole point of declaring it.

    The verification passes could not see that. The completed arms answer raw scope from
    ``derived_raw_attribution``, which re-runs inventory at submission time over the tree as
    it now stands and resolves the declaration a SECOND time -- so the late companion is
    admitted, by a derivation whose answer is thrown away rather than by the record that
    persists. ``raw_attribution_mismatches`` compares ``raw_paths`` and nothing else, and
    ``raw_paths`` is unaffected by a companion, so it passed the delivery through.

    So the equality check is asked of the other list too, and asked at the same moment: with
    the ``raw_paths`` check, before the raw-scope subtraction. Every case here asserts that
    it was the mismatch check and not the subtraction that refused -- an acquirer told
    "you wrote a stray file" would delete the evidence, and the true repair is to re-run
    inventory so the record accounts for it.
    """

    maxDiff = None

    # The one delivery helper the declared cases use, bound rather than re-written: what
    # separates these cases from those is the `written` argument alone, and a second
    # delivery body would let that difference drift into something else. Bound as an
    # unbound function off the class, which collects no tests from it.
    deliver_declaring = DelegatedDeclaredCompanionTests.deliver_declaring

    def assert_the_refusal_names_the_late_companion(
        self, payload: dict, source_id: str, companion: str, workspace: Path
    ) -> None:
        """The refusal is the mismatch check, and it names the record and the file."""
        self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", payload["error_code"], payload)
        self.assertTrue(payload["recoverable"], payload)
        self.assertIn(
            DELEGATED_COMPANION_MISMATCH_REFUSAL,
            payload["message"],
            diagnose(
                "delegated",
                "the companion-agreement check to be the one that refuses",
                CONTROLLER.EXIT_INVALID, payload, workspace,
            ),
        )
        self.assertNotIn(
            RAW_SCOPE_REFUSAL,
            payload["message"],
            "the acquirer must be told the manifest is stale, not that it wrote a stray "
            "file: the raw-scope subtraction admits this companion and would refuse the "
            "delivery for something else, or not at all",
        )
        mismatches = payload["details"]["raw_companion_mismatches"]
        self.assertEqual(
            [source_id],
            sorted(mismatches),
            f"the refusal must name the record whose companions are stale: {payload}",
        )
        self.assertEqual(
            [],
            mismatches[source_id]["declared_companion_paths"],
            "the persisted record resolved no companion, which is exactly the state the "
            "check exists to surface",
        )
        self.assertEqual(
            [companion],
            mismatches[source_id]["derived_companion_paths"],
            f"inventory over the delivered tree now resolves the companion: {payload}",
        )
        self.assertEqual(
            [companion],
            mismatches[source_id]["derived_not_declared"],
            "the payload must name the file the record does not account for",
        )
        self.assertEqual(
            [],
            mismatches[source_id]["declared_not_derived"],
            "nothing the record claims went missing; the disagreement is one-sided",
        )
        self.assertIn(
            "source_inventory.py --report",
            payload["remediation"],
            "the remediation must be the repair the control below performs",
        )

    def test_a_companion_delivered_after_the_inventory_run_is_refused_and_repaired(self):
        """Refused for the stale record, then accepted once inventory records the companion.

        The refusal and its repair are one case on purpose. The remediation this check
        prints tells the acquirer to re-run inventory, and a refusal test alone cannot say
        whether that is true advice -- if re-running inventory did not clear it, the guard
        would be a wall rather than a check. So the same action id is resubmitted after the
        one command the remediation names, and has to close the order.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)

            companion = companion_beside(DELEGATED_ARTIFACT)
            source_id = self.deliver_declaring(
                workspace,
                request_id,
                declared=[PurePosixPath(companion).name],
                written={},
            )
            self.assertEqual(
                [],
                resolved_companions(workspace, source_id),
                "the record must be written while the companion is absent, or this case is "
                f"the ordinary declared one. Raw tree: {raw_tree_files(workspace)}",
            )
            self.fulfil_and_reopen(workspace, request_id, source_id)

            # The acquirer's second capture, landing after the run that recorded the first.
            write_raw_file(workspace, companion, UNDECLARED_BODY)
            before = self.evidence_bytes(workspace)

            code, payload = self.submit(workspace, order["action_id"])
            self.assertEqual(
                CONTROLLER.EXIT_INVALID,
                code,
                diagnose(
                    "delegated",
                    f"the stale record to be refused (exit {CONTROLLER.EXIT_INVALID}) rather "
                    "than admitted by a derivation whose answer is discarded",
                    code, payload, workspace,
                ),
            )
            self.assert_the_refusal_names_the_late_companion(
                payload, source_id, companion, workspace
            )
            self.assertEqual(
                before,
                self.evidence_bytes(workspace),
                "a refused submission writes nothing of its own and removes nothing",
            )

            # The control: the remediation, performed. Nothing about the delivery changes --
            # the companion stays exactly where the acquirer wrote it -- and the record now
            # accounts for it.
            self.run_script(DELEGATED_INVENTORY, ["--report"], workspace)
            self.assertEqual(
                [companion],
                resolved_companions(workspace, source_id),
                "re-running inventory must be what puts the companion on the record; if it "
                "does not, the remediation this guard prints is not the repair",
            )
            self.run_script(DELEGATED_NORMALIZE, ["--source-id", source_id], workspace)
            code, session = self.submit(workspace, order["action_id"])
            self.assertEqual(
                0,
                code,
                diagnose(
                    "delegated",
                    "the repaired record to close the order (exit 0)",
                    code, session, workspace,
                ),
            )
            self.assertEqual("research", session["phase"], session)
            self.assertIn(
                companion,
                raw_tree_files(workspace),
                "the repair is a record that accounts for the delivery, never a delivery "
                "deleted to satisfy a guard",
            )

    def test_a_delivery_declaring_no_companion_is_unaffected_on_the_delegated_arm(self):
        """The opt-in guarantee: a workspace that declares nothing earns no new refusal.

        The shipped acquirer loop, whose sidecar carries no ``companions:`` key at all, so
        the record carries no ``companion_paths`` and inventory resolves none. Both sides of
        the comparison are empty and the order closes exactly as it did before the check
        existed. Asserted as the ABSENCE of the key rather than as an empty list, because an
        empty list is a state a delivery has to opt into.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)

            source_id = self.acquire(workspace, request_id)
            provenance = record_provenance(workspace, source_id)
            self.assertNotIn(
                "companions",
                provenance,
                "the shipped delivery must declare nothing; a fixture that declares makes "
                "this case a test of the declared path",
            )
            self.assertNotIn(
                "companion_paths",
                provenance,
                "and inventory must have resolved nothing onto it, so neither side of the "
                "companion comparison exists",
            )

            code, session = self.submit(workspace, order["action_id"])
            self.assertEqual(
                0,
                code,
                diagnose(
                    "delegated",
                    "a delivery declaring no companion to close the order (exit 0)",
                    code, session, workspace,
                ),
            )
            self.assertEqual("research", session["phase"], session)


class ProviderStaleCompanionRecordTests(ProviderHarness, unittest.TestCase):
    """The same late companion on the provider and blocked-partial arms.

    Both arms are here because the check has to be on all three or it becomes the
    inconsistency it was added to close: a delivery refused as a completed acquisition and
    accepted as the partial one that precedes it tells an acquirer nothing it can act on.

    The blocked arm reaches this through the record it APPENDS. Its companion comparison
    covers exactly the records the action appended, which is the set its ``raw_paths``
    comparison covers and for the same reason: a correlated record that pre-dates the order
    is manifest text nothing re-derives, and comparing it against a fresh derivation would
    refuse the partial delivery that re-supplies a companion such a record already names --
    a delivery ``ProviderDeclaredCompanionTests`` pins as admitted, and which stays so.

    A pre-existing correlated record whose own companion list is stale is therefore refused
    on that arm by the raw-scope subtraction, which names the file rather than the record.
    Nothing is admitted that should not be, and the difference is what the acquirer is told;
    the reasoning is at the check itself.
    """

    maxDiff = None

    def deliver_declaring_an_absent_companion(
        self, target: Path, request_id: str, candidate_id: str
    ) -> tuple[str, str]:
        """One file-shaped capture whose sidecar names a companion that is not on disk yet.

        The delivery `ProviderDeclaredCompanionTests` makes minus one write, so what
        separates a refused case here from an accepted one there is the moment the companion
        lands and nothing else.
        """
        relative = self.write_a_file_shaped_capture(target, request_id, candidate_id)
        companion = companion_beside(relative)
        declare_companions_in_sidecar(target, relative, [PurePosixPath(companion).name])
        return relative, companion

    def inventory_with_the_companion_absent(self, target: Path, relative: str) -> str:
        """Run the acquirer's inventory over a tree the declared companion is missing from.

        Not `inventory_and_assert_the_capture_is_the_only_record`: that helper requires
        `ready_for_normalization`, and a declaration inventory cannot resolve is precisely
        what makes this record review-required. The warning is asserted in its place, so the
        fixture stays pinned to the state under test -- a record inventory wrote while the
        companion was absent, having said out loud that it was.
        """
        inventory = self.assert_json_script_ok(
            INVENTORY, ["--project-root", str(target), "--report", "--format", "json"]
        )
        self.assertEqual("needs_review", inventory["readiness"], inventory)
        self.assertTrue(
            any(
                "provenance companion does not exist" in str(warning)
                for warning in inventory["warnings"]
            ),
            f"inventory must report the declaration it could not resolve: {inventory}",
        )
        records = self.manifest_records(target)
        self.assertEqual(
            [[relative]],
            [record.get("raw_paths") for record in records],
            "the capture must be the one record the order inherits, and inventory must "
            f"derive it as the file itself; raw tree was {raw_tree_files(target)}",
        )
        return str(records[0]["id"])

    def assert_the_refusal_names_the_late_companion(
        self,
        arm: str,
        message: str,
        raw_scope_needle: str,
        payload: dict,
        source_id: str,
        companion: str,
        target: Path,
    ) -> None:
        """The refusal is the mismatch check, and it names the record and the file."""
        self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", payload["error_code"], payload)
        self.assertTrue(payload["recoverable"], payload)
        self.assertIn(
            message,
            payload["message"],
            diagnose(
                arm,
                "the companion-agreement check to be the one that refuses",
                CONTROLLER.EXIT_INVALID, payload, target,
            ),
        )
        self.assertNotIn(
            raw_scope_needle,
            payload["message"],
            "the acquirer must be told the manifest is stale, not that it wrote a stray file",
        )
        mismatches = payload["details"]["raw_companion_mismatches"]
        self.assertEqual([source_id], sorted(mismatches), payload)
        self.assertEqual([], mismatches[source_id]["declared_companion_paths"], payload)
        self.assertEqual([companion], mismatches[source_id]["derived_companion_paths"], payload)
        self.assertEqual(
            [companion],
            mismatches[source_id]["derived_not_declared"],
            "the payload must name the file the record does not account for",
        )
        self.assertEqual([], mismatches[source_id]["declared_not_derived"], payload)
        self.assertIn("source_inventory.py --report", payload["remediation"], payload)

    def test_a_companion_delivered_after_the_inventory_run_is_refused_on_the_provider_arm(self):
        """Refused for the stale record, then accepted once inventory records the companion."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root)

            relative, companion = self.deliver_declaring_an_absent_companion(
                target, request_id, candidate_id
            )
            source_id = self.inventory_with_the_companion_absent(target, relative)
            self.assertEqual(
                [],
                resolved_companions(target, source_id),
                "the record must be written while the companion is absent, or this case is "
                f"the ordinary declared one. Raw tree: {raw_tree_files(target)}",
            )
            self.fulfil(target, request_id, candidate_id, order, source_id)

            write_raw_file(target, companion, UNDECLARED_BODY)
            before = self.evidence_bytes(target)

            code, payload, stderr = self.submit(
                root, target, order["action_id"],
                summary="Delivered the capture, then the companion its sidecar declares.",
                artifacts=[relative, f"{relative}.provenance.yml", companion, "sources/manifest.jsonl"],
            )
            self.assertEqual(
                CONTROLLER.EXIT_INVALID,
                code,
                diagnose(
                    "provider",
                    f"the stale record to be refused (exit {CONTROLLER.EXIT_INVALID})",
                    code, payload, target,
                ),
            )
            self.assert_the_refusal_names_the_late_companion(
                "provider", COMPANION_MISMATCH_REFUSAL, RAW_SCOPE_REFUSAL,
                payload, source_id, companion, target,
            )
            self.assertEqual(
                before,
                self.evidence_bytes(target),
                "a refused submission writes nothing of its own and removes nothing",
            )

            # The control: the remediation, performed, with the delivery left alone.
            self.assert_json_script_ok(
                INVENTORY, ["--project-root", str(target), "--report", "--format", "json"]
            )
            self.assertEqual(
                [companion],
                resolved_companions(target, source_id),
                "re-running inventory must be what puts the companion on the record",
            )
            self.assert_json_script_ok(
                NORMALIZE,
                ["--project-root", str(target), "--source-id", source_id, "--format", "json"],
            )
            code, session, stderr = self.submit(
                root, target, order["action_id"],
                summary="Re-ran inventory so the record accounts for the companion.",
                artifacts=[relative, f"{relative}.provenance.yml", companion, "sources/manifest.jsonl"],
            )
            self.assertEqual(
                0,
                code,
                diagnose(
                    "provider",
                    "the repaired record to close the order (exit 0)",
                    code, session, target,
                ),
            )
            self.assertIn(
                companion,
                raw_tree_files(target),
                f"the repair is a record that accounts for the delivery, not a deletion: {stderr}",
            )

    def test_a_companion_delivered_after_the_inventory_run_is_refused_on_the_blocked_partial_arm(self):
        """The same stale record, submitted as a partial delivery, earns the same refusal.

        The correlated record is appended by this action, so the arm's companion comparison
        covers it -- the same set its ``raw_paths`` comparison covers. A partial delivery is
        accepted with a pause rather than an exit 0, so the control asserts that instead.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root)

            relative, companion = self.deliver_declaring_an_absent_companion(
                target, request_id, candidate_id
            )
            source_id = self.inventory_with_the_companion_absent(target, relative)
            self.assertEqual(
                [],
                resolved_companions(target, source_id),
                "the record must be written while the companion is absent, or this case is "
                f"the ordinary declared one. Raw tree: {raw_tree_files(target)}",
            )

            write_raw_file(target, companion, UNDECLARED_BODY)
            before = self.evidence_bytes(target)

            code, payload, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Captured the artifact and its companion but could not finish.",
                artifacts=[relative, f"{relative}.provenance.yml", companion, "sources/manifest.jsonl"],
            )
            self.assertEqual(
                CONTROLLER.EXIT_INVALID,
                code,
                diagnose(
                    "blocked-partial",
                    f"the stale record to be refused (exit {CONTROLLER.EXIT_INVALID}) rather "
                    "than paused, so the two arms agree about the same delivery",
                    code, payload, target,
                ),
            )
            self.assert_the_refusal_names_the_late_companion(
                "blocked-partial", BLOCKED_COMPANION_MISMATCH_REFUSAL, BLOCKED_RAW_SCOPE_REFUSAL,
                payload, source_id, companion, target,
            )
            self.assertEqual(
                before,
                self.evidence_bytes(target),
                "a refused submission writes nothing of its own and removes nothing",
            )
            session = CONTROLLER.load_session(target, "orch-test")
            self.assertEqual(
                order["action_id"],
                session["pending_action_id"],
                f"the refusal must leave the same order open for the acquirer to repair: {stderr}",
            )

            # The control: the remediation, performed, and the partial delivery pauses.
            self.assert_json_script_ok(
                INVENTORY, ["--project-root", str(target), "--report", "--format", "json"]
            )
            self.assertEqual(
                [companion],
                resolved_companions(target, source_id),
                "re-running inventory must be what puts the companion on the record",
            )
            code, paused, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Re-ran inventory so the record accounts for the companion.",
                artifacts=[relative, f"{relative}.provenance.yml", companion, "sources/manifest.jsonl"],
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

    def test_a_delivery_declaring_no_companion_is_unaffected_on_the_provider_arm(self):
        """The opt-in guarantee on the provider arm: no declaration, no new refusal."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root)

            relative = self.write_a_file_shaped_capture(target, request_id, candidate_id)
            source_id = self.inventory_and_assert_the_capture_is_the_only_record(target, relative)
            provenance = record_provenance(target, source_id)
            self.assertNotIn("companions", provenance, provenance)
            self.assertNotIn("companion_paths", provenance, provenance)
            self.fulfil(target, request_id, candidate_id, order, source_id)

            code, session, stderr = self.submit(
                root, target, order["action_id"],
                summary="Delivered one capture that declares no companion.",
                artifacts=[relative, f"{relative}.provenance.yml", "sources/manifest.jsonl"],
            )
            self.assertEqual(
                0,
                code,
                diagnose(
                    "provider",
                    "a delivery declaring no companion to close the order (exit 0)",
                    code, session, target,
                ),
            )

    def test_a_delivery_declaring_no_companion_is_unaffected_on_the_blocked_partial_arm(self):
        """The opt-in guarantee on the blocked-partial arm, whose acceptance is a pause."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root)

            relative = self.write_a_file_shaped_capture(target, request_id, candidate_id)
            source_id = self.inventory_and_assert_the_capture_is_the_only_record(target, relative)
            provenance = record_provenance(target, source_id)
            self.assertNotIn("companions", provenance, provenance)
            self.assertNotIn("companion_paths", provenance, provenance)

            code, paused, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Fetched one capture that declares no companion and stopped.",
                artifacts=[relative, f"{relative}.provenance.yml", "sources/manifest.jsonl"],
            )
            self.assertEqual(
                CONTROLLER.EXIT_PAUSED,
                code,
                diagnose(
                    "blocked-partial",
                    "a partial delivery declaring no companion to pause the session "
                    f"(exit {CONTROLLER.EXIT_PAUSED})",
                    code, paused, target,
                ),
            )
            self.assertEqual("paused", paused["status"], paused)

if __name__ == "__main__":
    unittest.main()
