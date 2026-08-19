"""EW-BUG-005: a completed acquisition may create no evidence its fulfilments do not cite.

A delegated acquirer that answers one scoped request in two steps -- a discovery
snapshot plus the quote it led to -- and then delivers *both*, inventories both, and
fulfils with only the quote has created a manifest record this order never authorised.
The constraint is enforced three guards deep (manifest scope, then exact accounting,
then raw scope), and the contract an acquirer actually sees is the **first** refusal:
the manifest-scope guard, naming the record it did not fulfil with.

This is a **tripwire, not a target**. It pins current, correct behaviour and must keep
passing after the CR-19 attribution-predicate fix lands. That fix rewrites how
`allowed_new_raw_paths` is derived (from each fulfilled record's declared `raw_paths`
to what `source_inventory.build_records` attributes to the fulfilled records), which is
raw-path logic sitting *below* the manifest-scope guard. If any of these cases starts
reporting a raw-path message instead of the manifest-scope one, the fix has reordered
the chain and moved the diagnosis an acquirer gets further from its actual mistake.

The stamp state of the extra delivery's sidecar is varied deliberately. Stamping it for
the very same scoped request is the most sympathetic case an acquirer can construct --
"this file *is* part of answering that request" -- and it must still be refused, because
correlation is not fulfilment. Stamping it for another request, or leaving it unstamped,
must reach the same refusal by the same guard rather than a differently-shaped one.

The closing case covers the other half of the advice in the acquisition runbook -- hold
captures outside the raw roots until you mean to inventory them. A delivery that is
never inventoried has no manifest record to be refused by scope, so it falls through to
the raw-scope guard instead, which names the payload *and* its sidecar.
"""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_delegated_acquisition_e2e import (  # noqa: E402
    ACQUIRER,
    CONTROLLER,
    INVENTORY,
    ORCHESTRATION_ID,
    PAYLOAD,
    DelegatedWorkspace,
)

MANIFEST_SCOPE_REFUSAL = (
    "delegated acquisition changed, removed, or added evidence-manifest records "
    "outside fulfilled source scope"
)
RAW_SCOPE_REFUSAL = (
    "delegated acquisition changed raw evidence outside newly fulfilled manifest source scope"
)

DISCOVERY_NAME = "supplier-search-b0abc12345.json"
DISCOVERY_BODY = (
    '{\n  "query": "B0ABC12345",\n  "matched_listing": "ds-1005006543210987",\n'
    '  "results_considered": 14\n}\n'
)
DISCOVERY_RAW_PATH = f"raw/data/{DISCOVERY_NAME}"
DISCOVERY_SIDECAR_RAW_PATH = f"{DISCOVERY_RAW_PATH}.provenance.yml"

# How the extra delivery's sidecar is stamped. `None` for the scoped request id means
# "stamp it with whatever request this order scopes"; the literal id is a request this
# workspace has never heard of; and the absent stamp is evidence correlated to nothing.
SCOPED_STAMP = "scoped-request"
FOREIGN_STAMP = "req-some-other-purpose"
NO_STAMP = None

STAMP_STATES = (
    ("stamped for the scoped request", SCOPED_STAMP),
    ("stamped for a different request", FOREIGN_STAMP),
    ("unstamped", NO_STAMP),
)


class AcquisitionCreationScopeTests(DelegatedWorkspace, unittest.TestCase):
    """Everything a completed acquisition creates must be cited by one of its fulfilments."""

    maxDiff = None

    # -- fixtures -----------------------------------------------------------------

    def deliver_extra(
        self, workspace: Path, stamp: str | None, *, inventory: bool
    ) -> str | None:
        """A second raw file the order will not be fulfilled with.

        `inventory=False` leaves it as bytes on disk with no manifest record, which is
        the state the raw-scope guard exists to catch. Returns the extra record's source
        id when there is one to return.
        """
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        payload = destination / DISCOVERY_NAME
        payload.write_text(DISCOVERY_BODY, encoding="utf-8", newline="\n")
        sidecar = {
            "origin_url": "https://supplier.test/search?q=B0ABC12345",
            "license": "CC-BY-4.0",
            "retrieved_at": "2026-08-18T12:00:00Z",
            "retrieved_by": ACQUIRER,
            "checksum": f"sha256:{hashlib.sha256(payload.read_bytes()).hexdigest()}",
        }
        if stamp is not None:
            sidecar["request_id"] = stamp
        (destination / (DISCOVERY_NAME + ".provenance.yml")).write_text(
            yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
        )
        if not inventory:
            return None
        self.run_script(INVENTORY, ["--report"], workspace)
        return self.source_id_for(workspace, DISCOVERY_RAW_PATH)

    # -- assertions ----------------------------------------------------------------

    def assert_first_refusal(self, workspace: Path, action_id: str, message: str) -> dict:
        """Submit, and require the first guard to refuse to be the one named.

        `require` raises on the first failing check, so the envelope's message *is* the
        first refusal -- which is the whole point of the ordering this pins.
        """
        code, envelope = self.submit(
            workspace, action_id, artifacts=[f"raw/data/{PAYLOAD.name}"]
        )
        self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
        self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"], envelope)
        self.assertIn(message, envelope["message"], envelope)
        # A refusal is a repair request: the action stays pending so the acquirer can fix
        # the workspace and resubmit the same order.
        self.assertTrue(envelope["recoverable"], envelope)
        session = json.loads(
            (workspace / "runs" / "orchestrations" / ORCHESTRATION_ID / "session.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(action_id, session["pending_action_id"])
        return envelope

    # -- the extra manifest record --------------------------------------------------

    def run_extra_record_case(self, tmpdir: Path, stamp: str | None, *, normalize: bool) -> None:
        workspace, request_id = self.make_workspace(tmpdir)
        self.start(workspace)
        order = self.pending_order(workspace)
        sidecar_stamp = request_id if stamp == SCOPED_STAMP else stamp

        if normalize:
            # Deliver first, then let the fulfilment's own `normalize --all` sweep both
            # records: the extra record ends the action inventoried *and* normalized.
            self.deliver_extra(workspace, sidecar_stamp, inventory=False)
            quote_id = self.deliver_for(workspace, request_id)
            extra_id = self.source_id_for(workspace, DISCOVERY_RAW_PATH)
        else:
            quote_id = self.deliver_for(workspace, request_id)
            extra_id = self.deliver_extra(workspace, sidecar_stamp, inventory=True)

        self.assertNotEqual(quote_id, extra_id)
        self.fulfil_and_reopen(workspace, request_id, quote_id)

        envelope = self.assert_first_refusal(
            workspace, order["action_id"], MANIFEST_SCOPE_REFUSAL
        )
        violations = envelope["details"]["manifest_scope_violations"]
        # The payload has to name the record, not merely report that something is wrong:
        # an acquirer holding two new records cannot repair "outside fulfilled scope".
        self.assertEqual([extra_id], violations["added_outside_scope"], envelope)
        # Nothing pre-existing moved -- this is purely a creation-scope failure.
        self.assertEqual([], violations["removed"], envelope)
        self.assertEqual([], violations["changed_outside_scope"], envelope)
        # And the other half of the diagnosis: what the order *did* authorise.
        self.assertEqual([quote_id], envelope["details"]["fulfilled_source_ids"], envelope)

    def test_an_extra_inventoried_record_is_refused_by_manifest_scope(self):
        """Inventoried but not fulfilled with: refused in every stamp state, same guard."""
        for label, stamp in STAMP_STATES:
            with self.subTest(sidecar=label, normalized=False), tempfile.TemporaryDirectory() as tmp:
                self.run_extra_record_case(Path(tmp), stamp, normalize=False)

    def test_an_extra_normalized_record_is_refused_by_the_same_guard(self):
        """Normalizing the extra record does not move the refusal to a normalized-scope one.

        The manifest-scope guard runs before any normalized-output check, so an acquirer
        who did *more* work off-scope still gets told the same thing about the same
        record. Pinning both variants keeps a future reordering from making the
        diagnosis depend on how far past inventory the acquirer got.
        """
        for label, stamp in STAMP_STATES:
            with self.subTest(sidecar=label, normalized=True), tempfile.TemporaryDirectory() as tmp:
                self.run_extra_record_case(Path(tmp), stamp, normalize=True)

    # -- the delivery that never became a record ------------------------------------

    def test_a_delivered_but_uninventoried_file_is_refused_by_raw_scope(self):
        """No manifest record means nothing for manifest scope to refuse -- raw scope catches it.

        This is why the runbook tells acquirers to hold captures outside the raw roots
        until they mean to inventory them: bytes under `raw/` are in scope whether or not
        anything ever made a record of them, and the refusal names the payload and its
        sidecar both, because the guard admits raw paths only in that pair.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)
            quote_id = self.deliver_for(workspace, request_id)
            self.deliver_extra(workspace, request_id, inventory=False)
            self.fulfil_and_reopen(workspace, request_id, quote_id)

            envelope = self.assert_first_refusal(
                workspace, order["action_id"], RAW_SCOPE_REFUSAL
            )
            details = envelope["details"]
            self.assertEqual(
                [DISCOVERY_RAW_PATH, DISCOVERY_SIDECAR_RAW_PATH],
                sorted(details["unexpected_new_raw_paths"]),
                envelope,
            )
            # Nothing pre-existing under raw/ was touched, and the fulfilled record's own
            # delivery is allowed -- so it is absent from the unexpected set by being present
            # in the allowed one.
            self.assertEqual(
                {"removed": [], "added_outside_scope": [], "changed_outside_scope": []},
                details["raw_scope_violations"],
                envelope,
            )
            self.assertIn(f"raw/data/{PAYLOAD.name}", details["allowed_new_raw_paths"])
            self.assertNotIn(DISCOVERY_RAW_PATH, details["allowed_new_raw_paths"])


if __name__ == "__main__":
    unittest.main()
