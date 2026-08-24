"""The claim ledger, and the gate change that tells a caller which order it is inside.

Contingent bookkeeping needs two things before either verb can stop writing through: a
place to record what the acquirer says it did, and a way for the verb to know whether the
order it is inside is one whose bookkeeping the controller will later commit. Neither is
wired into ``fulfill`` or ``reopen`` yet -- these pin the primitives on their own terms.
"""

import json
import tempfile
import unittest
from pathlib import Path

from tests._script_loader import load_script

CLAIMS = load_script("order_claims_under_test", "_order_claims.py")
GATE = load_script("order_claims_gate_under_test", "_delegation_gate.py")


class ClaimLedgerTests(unittest.TestCase):
    maxDiff = None

    def test_claims_are_written_outside_the_orchestration_session_tree(self):
        """A claim under the session tree would read as control tampering.

        The managed host snapshots ``runs/orchestrations/<id>/`` before dispatching a
        worker and verifies it unchanged afterwards, excluding only host-owned runtime
        subtrees. The acquirer files its claim inside exactly that window, so the ledger
        has to live somewhere the tripwire does not watch.
        """
        path = CLAIMS.claims_path(Path("/ws"), "orch-1", "action-0001")

        self.assertEqual(Path("/ws/runs/order-claims/orch-1/action-0001.json"), path)
        self.assertNotIn("orchestrations", path.parts, path)

    def test_an_identifier_that_would_escape_the_claims_directory_is_refused(self):
        for label, oid, action in (
            ("orchestration_id", "../etc", "action-0001"),
            ("action_id", "orch-1", "../../secret"),
            ("orchestration_id", "", "action-0001"),
            ("action_id", "orch-1", "with/slash"),
        ):
            with self.subTest(oid=oid, action=action):
                with self.assertRaises(CLAIMS.OrderClaimError) as caught:
                    CLAIMS.claims_path(Path("/ws"), oid, action)
                self.assertIn(label, caught.exception.message, caught.exception.message)

    def test_a_missing_ledger_reads_as_no_claims_rather_than_an_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            claims = CLAIMS.load_claims(CLAIMS.claims_path(Path(tmpdir), "orch-1", "action-0001"))

            self.assertEqual({}, claims["fulfilments"])
            self.assertEqual({}, claims["reopens"])

    def test_a_fulfilment_and_a_reopen_share_one_document_per_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            CLAIMS.record_fulfilment_claim(
                root, "orch-1", "action-0001",
                request_id="req-1", source_id="src-1", claimed_at="2026-01-01T00:00:00Z",
            )
            CLAIMS.record_reopen_claim(
                root, "orch-1", "action-0001",
                question_slug="needs-price", source_ids=["src-1"], request_ids=["req-1"],
                claimed_at="2026-01-01T00:00:01Z",
            )

            path = CLAIMS.claims_path(root, "orch-1", "action-0001")
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(1, document["schema_version"], document)
            self.assertEqual("orch-1", document["orchestration_id"], document)
            self.assertEqual("action-0001", document["action_id"], document)
            self.assertEqual(
                {"request_id": "req-1", "source_id": "src-1", "claimed_at": "2026-01-01T00:00:00Z"},
                CLAIMS.fulfilment_claim(document, "req-1"),
                document,
            )
            self.assertEqual(
                ["src-1"], CLAIMS.reopen_claim(document, "needs-price")["source_ids"], document
            )

    def test_re_filing_the_same_request_replaces_rather_than_appends(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for stamp in ("2026-01-01T00:00:00Z", "2026-01-01T00:00:05Z"):
                CLAIMS.record_fulfilment_claim(
                    root, "orch-1", "action-0001",
                    request_id="req-1", source_id="src-1", claimed_at=stamp,
                )

            document = CLAIMS.load_claims(CLAIMS.claims_path(root, "orch-1", "action-0001"))
            self.assertEqual(1, len(document["fulfilments"]), document)

    def test_two_actions_of_one_orchestration_keep_separate_ledgers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            CLAIMS.record_fulfilment_claim(
                root, "orch-1", "action-0001",
                request_id="req-1", source_id="src-1", claimed_at="2026-01-01T00:00:00Z",
            )
            CLAIMS.record_fulfilment_claim(
                root, "orch-1", "action-0002",
                request_id="req-2", source_id="src-2", claimed_at="2026-01-01T00:00:00Z",
            )

            first = CLAIMS.load_claims(CLAIMS.claims_path(root, "orch-1", "action-0001"))
            second = CLAIMS.load_claims(CLAIMS.claims_path(root, "orch-1", "action-0002"))
            self.assertEqual(["req-1"], sorted(first["fulfilments"]), first)
            self.assertEqual(["req-2"], sorted(second["fulfilments"]), second)

    def test_an_unreadable_ledger_refuses_rather_than_reporting_no_claims(self):
        """Silently reading damage as "nothing was claimed" would commit nothing and pass."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = CLAIMS.claims_path(Path(tmpdir), "orch-1", "action-0001")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json", encoding="utf-8")

            with self.assertRaises(CLAIMS.OrderClaimError):
                CLAIMS.load_claims(path)

    def test_a_damaged_section_refuses_rather_than_reading_as_no_claims(self):
        """Degrading to "nothing was claimed" would accept a submission committing nothing."""
        for document in (
            {"schema_version": 1, "fulfilments": "garbage", "reopens": {}},
            {"schema_version": 1, "fulfilments": {}, "reopens": ["not", "a", "map"]},
            {"schema_version": 99, "fulfilments": {}, "reopens": {}},
            {"fulfilments": {}, "reopens": {}},
        ):
            with self.subTest(document=document):
                with tempfile.TemporaryDirectory() as tmpdir:
                    path = CLAIMS.claims_path(Path(tmpdir), "orch-1", "action-0001")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(document), encoding="utf-8")

                    with self.assertRaises(CLAIMS.OrderClaimError):
                        CLAIMS.load_claims(path)

    def test_a_ledger_reached_through_a_symlink_refuses(self):
        """The acquirer writes this file; a link here would aim the controller's read."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            elsewhere = root / "elsewhere.json"
            elsewhere.write_text(json.dumps(CLAIMS.empty_claims("orch-1", "action-0001")), encoding="utf-8")
            path = CLAIMS.claims_path(root, "orch-1", "action-0001")
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.symlink_to(elsewhere)
            except (OSError, NotImplementedError):  # pragma: no cover - unprivileged Windows
                self.skipTest("this filesystem does not allow creating a symbolic link")

            with self.assertRaises(CLAIMS.OrderClaimError) as caught:
                CLAIMS.load_claims(path)
            self.assertIn("singly linked regular file", caught.exception.message)

    def test_a_dangling_link_refuses_instead_of_reading_as_no_claims(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = CLAIMS.claims_path(root, "orch-1", "action-0001")
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.symlink_to(root / "does-not-exist.json")
            except (OSError, NotImplementedError):  # pragma: no cover - unprivileged Windows
                self.skipTest("this filesystem does not allow creating a symbolic link")

            with self.assertRaises(CLAIMS.OrderClaimError):
                CLAIMS.load_claims(path)

    def test_the_accessors_tolerate_a_document_whose_sections_are_null(self):
        self.assertIsNone(CLAIMS.fulfilment_claim({"fulfilments": None}, "req-1"))
        self.assertIsNone(CLAIMS.reopen_claim({"reopens": None}, "needs-price"))
        self.assertIsNone(CLAIMS.fulfilment_claim({}, "req-1"))

    def test_a_ledger_holding_a_json_array_refuses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = CLAIMS.claims_path(Path(tmpdir), "orch-1", "action-0001")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("[]", encoding="utf-8")

            with self.assertRaises(CLAIMS.OrderClaimError):
                CLAIMS.load_claims(path)


class SanctioningOrderTests(unittest.TestCase):
    """The gate is the only code that already knows which order a mutation is inside."""

    DELEGATED = {
        "action_id": "action-0001",
        "phase": "acquisition",
        "acquisition_mode": "delegated",
        "scope": {"request_ids": ["req-1"], "question_slugs": ["needs-price"]},
    }
    RESEARCH = {
        "action_id": "action-0007",
        "phase": "research",
        "scope": {"question_slugs": ["needs-price"]},
    }

    def seed(self, root: Path, order: dict, *, orchestration_id: str = "orch-1") -> None:
        session_dir = root / "runs" / "orchestrations" / orchestration_id
        (session_dir / "work-orders").mkdir(parents=True, exist_ok=True)
        (session_dir / "session.json").write_text(
            json.dumps({"status": "active", "pending_action_id": order["action_id"]}), encoding="utf-8"
        )
        (session_dir / "work-orders" / f"{order['action_id']}.json").write_text(
            json.dumps(order), encoding="utf-8"
        )

    def gate(self, root: Path, **kwargs):
        return GATE.require_sanctioned_mutation(
            root, True, error_code="X", subject="subject", remediation="fix it", **kwargs
        )

    def test_the_gate_hands_back_the_order_that_sanctioned_the_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.seed(root, self.DELEGATED)

            entry = self.gate(root, request_id="req-1")

            self.assertEqual("orch-1", entry["orchestration_id"], entry)
            self.assertEqual("action-0001", entry["action_id"], entry)
            self.assertTrue(GATE.is_contingent_acquisition_order(entry["work_order"]), entry)

    def test_an_ungated_workspace_still_learns_which_order_scopes_the_change(self):
        """Identification is not gated on the workspace's acquisition mode; refusal is.

        A workspace that does not delegate is never *refused* a mutation -- that is the
        backward compatibility this gate promises. But a caller still has to be able to
        find out which pending order it is inside, because an order freezing the request
        store is a property of the order, not of the workspace that issued it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.seed(root, self.DELEGATED)

            entry = GATE.require_sanctioned_mutation(
                root, False, request_id="req-1",
                error_code="X", subject="subject", remediation="fix it",
            )

            self.assertEqual("action-0001", entry["action_id"], entry)

    def test_an_ungated_workspace_is_not_refused_an_unsanctioned_change(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.seed(root, self.DELEGATED)

            self.assertIsNone(
                GATE.require_sanctioned_mutation(
                    root, False, request_id="req-nothing-scopes",
                    error_code="X", subject="subject", remediation="fix it",
                )
            )

    def test_no_live_session_reports_no_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(self.gate(Path(tmpdir), request_id="req-1"))

    def test_a_research_order_sanctions_a_reopen_but_is_not_one_the_controller_commits(self):
        """Scope is the authorization, so a research order reopens its own questions.

        It must keep writing straight through: no acquisition submission will follow to
        commit a claim on its behalf, so filing one would strand the reopen forever.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.seed(root, self.RESEARCH)

            entry = self.gate(root, question_slug="needs-price")

            self.assertEqual("action-0007", entry["action_id"], entry)
            self.assertFalse(GATE.is_contingent_acquisition_order(entry["work_order"]), entry)

    def test_a_delegated_acquisition_order_sanctions_a_reopen_and_is_committable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.seed(root, self.DELEGATED)

            entry = self.gate(root, question_slug="needs-price")

            self.assertTrue(GATE.is_contingent_acquisition_order(entry["work_order"]), entry)

    def test_an_acquisition_order_wins_over_a_research_order_scoping_one_question(self):
        """Two live sessions can scope one slug, and sort order must not decide this.

        Returning the research order would write the reopen straight through while a
        delegated acquisition order has that question frozen -- mutating exactly the state
        the freeze exists to hold. Claiming under an order that never commits is visible
        and recoverable; a silent bypass of the freeze is not.
        """
        for research_first in (True, False):
            with self.subTest(research_first=research_first):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    # Session ids chosen so each ordering puts a different one first.
                    research_id = "orch-aaa" if research_first else "orch-zzz"
                    acquisition_id = "orch-zzz" if research_first else "orch-aaa"
                    self.seed(root, self.RESEARCH, orchestration_id=research_id)
                    self.seed(root, self.DELEGATED, orchestration_id=acquisition_id)

                    entry = self.gate(root, question_slug="needs-price")

                    self.assertEqual(acquisition_id, entry["orchestration_id"], entry)
                    self.assertTrue(GATE.is_contingent_acquisition_order(entry["work_order"]), entry)

    def test_ambiguous_orders_are_refused_whatever_the_workspace_mode_says(self):
        """The one refusal that reaches a workspace this gate does not otherwise gate.

        The promise is that a mutation is never refused for *lacking* a sanction, because
        an operator working a workspace by hand is not driving a protocol. Two live
        acquisition orders scoping one subject is the opposite of that.

        Writing through is not the safe answer it looks like: an acquisition order freezes
        the request store, so the write lands in state both orders then refuse as changed
        outside their scope. Refusing here costs one command; writing through costs both
        orders.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.seed(root, self.DELEGATED, orchestration_id="orch-aaa")
            self.seed(root, {**self.DELEGATED, "action_id": "action-0002"}, orchestration_id="orch-zzz")

            for delegated in (False, True):
                with self.subTest(delegated=delegated):
                    with self.assertRaises(GATE.DelegationGateError) as caught:
                        GATE.require_sanctioned_mutation(
                            root, delegated, request_id="req-1",
                            error_code="X", subject="subject", remediation="fix it",
                        )
                    self.assertEqual(
                        ["orch-aaa", "orch-zzz"],
                        caught.exception.details["orchestration_ids"],
                        caught.exception.details,
                    )

    def test_an_unsanctioned_mutation_still_refuses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.seed(root, self.DELEGATED)

            with self.assertRaises(GATE.DelegationGateError) as caught:
                self.gate(root, request_id="req-unscoped")
            self.assertEqual("X", caught.exception.error_code)

    def test_a_malformed_order_is_not_mistaken_for_a_committable_one(self):
        for value in (None, {}, {"phase": "research"}, {"phase": "research", "acquisition_mode": "delegated"}):
            with self.subTest(value=value):
                self.assertFalse(GATE.is_contingent_acquisition_order(value))


if __name__ == "__main__":
    unittest.main()
