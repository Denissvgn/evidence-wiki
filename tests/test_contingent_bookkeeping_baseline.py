"""What an acquisition order makes durable, and when.

Inside a pending delegated acquisition order the acquirer's `fulfill` and `reopen` file
claims that the controller commits when it accepts the submission. Three of these four
tests measure that boundary from the durable side: while the order is pending the request
store and the question page hold what they held at issuance, a refused submission commits
nothing, and an action the acquirer declares failed leaves its request routable rather than
stranded.

They began as measurements of the opposite -- of an acquirer whose bookkeeping went
straight to disk ahead of any verification -- and were rewritten when that stopped being
true. That is what they were for: each one failed when the writes moved behind acceptance,
which is the only evidence that the change did anything.

The file exists because a green suite is not evidence about this. Moving those writes behind
acceptance can pass every other test in the tree while quietly loosening a guard, because a
guard whose subject stops moving stops having anything to watch. These tests keep the
durable bytes themselves in view, so a change to when they are written has to disagree with
a named assertion rather than with silence.

Assertions are on the store and the page, never on a command's return value: the return
value is what the acquirer *claims*, and what this file is about is what the workspace
holds. The fourth test is the control -- the provider arm, which files no claims -- and it
did not move when the other three did.
"""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tests._script_loader import load_script, load_script_uncached
from tests.test_delegated_acquisition_e2e import (
    ACQUIRER,
    CLAIM,
    CONTROLLER,
    INVENTORY,
    NORMALIZE,
    ORCHESTRATION_ID,
    PAYLOAD,
    QUESTION_SLUG,
    REQUESTS,
    RESOLVE,
    DelegatedWorkspace,
)

DISCOVER = load_script("contingent_bookkeeping_discover", "discover_sources.py")
CLAIMS = load_script("contingent_bookkeeping_claims", "_order_claims.py")

#: The provider arm acquires into `raw/papers`, which an initialized workspace already
#: carries as a source root, so the paper below needs no configuration of its own.
PAPER = "raw/papers/solid-electrolyte.html"


def stored_requests(workspace: Path) -> list[dict]:
    """Every source request as the durable store holds it, read fresh off disk."""
    path = workspace / "sources" / "source-requests.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def stored_request(workspace: Path, request_id: str) -> dict:
    for record in stored_requests(workspace):
        if record.get("request_id") == request_id:
            return record
    raise AssertionError(f"no stored request {request_id}")


def claim_ledger(workspace: Path, action_id: str, orchestration_id: str = ORCHESTRATION_ID) -> dict:
    """This action's claim ledger, read from where the acquirer files it."""
    path = workspace / "runs" / "order-claims" / orchestration_id / f"{action_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"fulfilments": {}, "reopens": {}}


def question_fields(workspace: Path, slug: str = QUESTION_SLUG) -> dict:
    """The question page's frontmatter, parsed from the bytes on disk."""
    text = (workspace / "wiki" / "questions" / f"{slug}.md").read_text(encoding="utf-8")
    _, frontmatter, _ = text.split("---\n", 2)
    return yaml.safe_load(frontmatter)


def session_document(workspace: Path) -> dict:
    path = workspace / "runs" / "orchestrations" / ORCHESTRATION_ID / "session.json"
    return json.loads(path.read_text(encoding="utf-8"))


class DelegatedAcquisitionBookkeepingTests(DelegatedWorkspace, unittest.TestCase):
    """The delegated arm, measured at three points: mid-order, after a refusal, after a failure."""

    #: `source_requests.py` stamps to the second, so two writes inside one second are
    #: indistinguishable by their stamps alone. Pinning the fulfilment's clock to a fixed
    #: value makes the rewrite observable without making the assertion depend on how long
    #: the delivery above it happened to take.
    FULFILMENT_STAMP = "2026-12-31T23:59:59Z"

    def pending_acquisition(self, root: Path) -> tuple[Path, str, str]:
        """A workspace whose blocked question has a live delegated acquisition order."""
        workspace, request_id = self.make_workspace(root)
        self.start(workspace)
        order = self.pending_order(workspace)
        self.assertEqual([request_id], order["scope"]["request_ids"], order)
        return workspace, request_id, order["action_id"]

    def test_the_acquirer_makes_nothing_durable_before_the_controller_verifies_it(self):
        """Inside a pending order, `fulfill` and `reopen` file claims and write nothing.

        Both are still ordinary commands an acquirer runs inside its own order, and both
        still succeed. What changed is where the answer goes: into this action's claim
        ledger, which the controller commits when it accepts the submission. Until then the
        request store and the question page hold exactly what they held at issuance, so
        nothing downstream can mistake the acquirer's word for a verified result.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, action_id = self.pending_acquisition(Path(tmpdir))

            issued_record = stored_request(workspace, request_id)
            self.assertEqual("open", issued_record["status"], issued_record)
            self.assertIsNone(issued_record["source_id"], issued_record)
            issued_page = question_fields(workspace)
            self.assertEqual("blocked", issued_page["status"], issued_page)
            self.assertEqual([request_id], issued_page["blocking_request_ids"], issued_page)

            source_id = self.deliver_for(workspace, request_id)
            with mock.patch.object(
                REQUESTS, "timestamp_utc", return_value=self.FULFILMENT_STAMP
            ):
                self.fulfil_and_reopen(workspace, request_id, source_id)

            # Still pending: the controller has not been told the action is over, let alone
            # verified it.
            session = session_document(workspace)
            self.assertEqual(action_id, session["pending_action_id"], session)
            self.assertEqual(0, session["completed_action_count"], session)

            record = stored_request(workspace, request_id)
            self.assertEqual(
                issued_record,
                record,
                msg=f"the store must be byte-identical to issuance while the order is pending: {record}",
            )

            page = question_fields(workspace)
            self.assertEqual(
                issued_page,
                page,
                msg=f"the question page must not move while the order is pending: {page}",
            )

            # The bookkeeping is not lost, it is filed: the ledger is what the controller
            # will read at verification and commit on acceptance.
            claims = claim_ledger(workspace, action_id)
            self.assertEqual(
                {"request_id": request_id, "source_id": source_id, "claimed_at": self.FULFILMENT_STAMP},
                claims["fulfilments"][request_id],
                claims,
            )
            self.assertEqual([QUESTION_SLUG], sorted(claims["reopens"]), claims)

    def test_a_deleted_raw_file_is_named_even_when_derivation_fails(self):
        """A decided tamper verdict must not be preempted by a derivation that raises.

        Whether a raw file that existed at issuance was changed or removed is answered by
        comparing two snapshots, and needs nothing from inventory. Deriving first let a
        derivation that raises travel out ahead of it, and the operator was told to repair
        a raw tree rather than told what they had deleted.

        The two are not independent, which is what makes the wrong answer the likely one
        rather than a coincidence: deleting raw evidence is a plausible reason the
        derivation cannot run at all. Here it is both.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            # A bystander delivered before the session: raw evidence the order's baseline
            # records and that nothing in the order reuses, so deleting it is a plain
            # tamper rather than a reuse question.
            self.deliver_unnormalized_bystander(workspace, "req-a-bystander-nobody-scopes")
            self.start(workspace)
            order = self.pending_order(workspace)
            source_id = self.deliver_for(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            removed = workspace / "raw" / "data" / self.UNNORMALIZED_BYSTANDER_NAME
            self.assertTrue(removed.is_file(), "the fixture must leave raw evidence from before the order")
            relative = removed.relative_to(workspace).as_posix()
            removed.unlink()

            inventory = CONTROLLER.load_sibling_module("source_inventory")
            with mock.patch.object(
                inventory, "build_records", side_effect=RuntimeError("inventory cannot read the tree")
            ):
                code, envelope = self.submit(
                    workspace, order["action_id"], artifacts=[f"raw/data/{PAYLOAD.name}"]
                )

            self.assertNotEqual(0, code, envelope)
            self.assertIn(
                "changed or removed raw evidence that existed when the order was issued",
                envelope["message"],
                envelope,
            )
            self.assertIn(relative, envelope["details"]["raw_scope_violations"]["removed"], envelope)

    def test_a_refused_submission_leaves_no_bookkeeping_behind(self):
        """A refusal now undoes nothing because nothing was done.

        The acquirer fulfils and reopens, then submits work the controller will not accept --
        here because the normalized record the fulfilment rests on is gone by the time the
        postconditions look for it. Which refusal fires is not the point. What the store and
        the page hold afterwards is, and they hold what they held at issuance: the claims
        were never committed, so there is nothing asserting a fulfilment the controller
        declined to verify.

        The claims themselves survive on purpose. A retry of the same action re-files the
        same claim and must stay an idempotent no-op; discarding them here would make the
        retry file a second claim and report that it changed something.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, action_id = self.pending_acquisition(Path(tmpdir))
            source_id = self.deliver_for(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)
            normalized = list((workspace / "sources" / "normalized").glob("*.md"))
            self.assertEqual(1, len(normalized), normalized)
            normalized[0].unlink()

            code, envelope = self.submit(
                workspace, action_id, artifacts=[f"raw/data/{PAYLOAD.name}"]
            )

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"], envelope)
            self.assertEqual(
                "fulfilled source requests do not have normalized evidence",
                envelope["message"],
                envelope,
            )
            self.assertEqual([source_id], envelope["details"]["source_ids"], envelope)

            record = stored_request(workspace, request_id)
            self.assertEqual(
                "open",
                record["status"],
                msg=f"a refused submission must commit no fulfilment: {record}",
            )
            self.assertIsNone(record["source_id"], record)

            page = question_fields(workspace)
            self.assertEqual(
                "blocked",
                page["status"],
                msg=f"a refused submission must commit no reopen: {page}",
            )
            self.assertEqual([request_id], page["blocking_request_ids"], page)

            # The claims are still on file, which is what makes the retry a no-op.
            claims = claim_ledger(workspace, action_id)
            self.assertEqual(source_id, claims["fulfilments"][request_id]["source_id"], claims)

            # And the action is still the pending one, so the workspace is not merely
            # un-rolled-back: it is un-rolled-back *and* still owed the same work.
            session = session_document(workspace)
            self.assertEqual(action_id, session["pending_action_id"], session)

    def test_a_question_page_hand_edited_inside_the_order_is_refused(self):
        """The freeze covers the page this order will reopen, not only the pages it will not.

        A reopen inside a pending order is a claim, and the controller writes the page when
        it accepts the submission -- so the freeze has to hold over every question page for
        the life of the order. Its one exemption is deliberately narrow: a slug whose
        durable page *already equals* the projection the commit would write, which is what
        an interrupted finalization leaves behind. Widen it to the slugs this action
        reopens and the page it is about stops being frozen at all: an acquirer can write
        whatever it likes into the page the order names, and the commit lands its
        frontmatter on top of text no verification ever read.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, action_id = self.pending_acquisition(Path(tmpdir))
            source_id = self.deliver_for(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            # Body prose rather than a lifecycle field, so the frontmatter still reads as it
            # did at issuance: the transition guard ahead of the freeze is satisfied, the
            # page is nowhere near the committed projection that would exempt it, and the
            # frozen bytes are the only thing left to disagree with the edit.
            page = workspace / "wiki" / "questions" / f"{QUESTION_SLUG}.md"
            page.write_text(
                page.read_text(encoding="utf-8") + "\nThe acquirer's own note about the quote.\n",
                encoding="utf-8",
            )

            code, envelope = self.submit(
                workspace, action_id, artifacts=[f"raw/data/{PAYLOAD.name}"]
            )

            self.assertNotEqual(
                0, code, msg=f"a hand-edited question page must cost the order its acceptance: {envelope}"
            )
            self.assertIn(
                "changed a question that was not fully unblocked by this action",
                envelope["message"],
                envelope,
            )
            # The message is shared with the unauthorized-reopen guard above it, so the
            # details key is what says the freeze answered rather than that one.
            self.assertEqual(
                [f"{QUESTION_SLUG}.md"],
                envelope["details"]["question_scope_violations"]["changed_outside_scope"],
                envelope,
            )

            # Refused, so nothing was committed: the page still says blocked and the request
            # is still open. The edit itself stands, because a refusal names it rather than
            # undoing it.
            fields = question_fields(workspace)
            self.assertEqual("blocked", fields["status"], fields)
            self.assertEqual([request_id], fields["blocking_request_ids"], fields)
            record = stored_request(workspace, request_id)
            self.assertEqual("open", record["status"], record)
            self.assertIsNone(record["source_id"], record)
            self.assertTrue(
                page.read_text(encoding="utf-8").endswith(
                    "The acquirer's own note about the quote.\n"
                ),
                msg="a refusal reports the edit; it does not repair the page",
            )

    def test_a_failed_outcome_leaves_the_request_routable(self):
        """The outcome that ends an action without any verification, and now costs nothing.

        `prepare_submission` reaches `verify_action_postconditions` for a completed outcome
        and a different check for a blocked one; `failed` takes neither branch, so no
        evidence check and no scope check runs. That is unchanged and is still asserted
        here.

        What changed is what survives it. The fulfilment was a claim nothing committed, so
        an action the acquirer itself declared a failure leaves the request exactly as
        issued -- open, and therefore visible to routing, which selects on status. The
        request can be re-issued and re-attempted. Before this, it could not be seen again
        by anything, which is the hazard this asserts is gone.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, action_id = self.pending_acquisition(Path(tmpdir))
            source_id = self.deliver_for(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            with mock.patch.object(
                CONTROLLER,
                "verify_action_postconditions",
                wraps=CONTROLLER.verify_action_postconditions,
            ) as verify:
                code, session = self.submit(workspace, action_id, outcome="failed")

            self.assertEqual(
                0,
                verify.call_count,
                msg=f"a failed outcome verified nothing about the action: {session}",
            )
            self.assertEqual(CONTROLLER.EXIT_INVALID, code, session)
            self.assertEqual("failed", session["status"], session)

            record = stored_request(workspace, request_id)
            self.assertEqual(
                "open",
                record["status"],
                msg=f"a failed action must commit nothing it never verified: {record}",
            )
            self.assertIsNone(record["source_id"], record)

            config = CONTROLLER.load_config(workspace)
            routable = [item["request_id"] for item in CONTROLLER.open_requests(workspace, config)]
            self.assertEqual(
                [request_id],
                routable,
                msg=f"the request must stay routable so a later order can retry it: {stored_requests(workspace)}",
            )


class ProviderAcquisitionBookkeepingTests(DelegatedWorkspace, unittest.TestCase):
    """The provider arm, which used to be the control and is now covered by the same freeze.

    These began as the measurement that contingent bookkeeping reached only the delegated
    arm: every field of a scoped request record was mutable here, so a mid-order rewrite
    survived acceptance. An acquisition order freezes the request store whoever executes it
    now, and both cases assert the refusal rather than the admission.
    """

    CANDIDATE_ID = "cand-provider-route"
    ORIGINAL_RATIONALE = "The question cannot be answered from delivered evidence."
    REWRITTEN_RATIONALE = "Rewritten after the fact to fit what arrived."

    def enable_providers(self, workspace: Path) -> None:
        """Declare the academic provider route, which is what makes acquisition reachable."""
        config_path = workspace / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        integrations = config.setdefault("integrations", {})
        integrations["discovery"] = {
            "enabled": True,
            "providers": ["arxiv", "openalex"],
            "candidate_store_path": "sources/discovery/candidates.jsonl",
        }
        integrations["acquisition"] = {
            "enabled": True,
            "providers": ["arxiv", "openalex"],
            "target_root": "raw/papers",
            "max_downloads_per_run": 10,
            "require_license_check": True,
        }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        (workspace / "sources" / "discovery").mkdir(parents=True, exist_ok=True)

    def discover(self, workspace: Path, argv: list[str]) -> dict:
        """`discover_sources.py`, whose `--format` is a global flag rather than a trailing one."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = DISCOVER.main(["--project-root", str(workspace), "--format", "json", *argv])
        self.assertEqual(0, int(code or 0), stderr.getvalue() or stdout.getvalue())
        return json.loads(stdout.getvalue())

    def select_candidate(self, workspace: Path, request_id: str) -> None:
        """A selected acquisition candidate: without one the order has no route to scope."""
        config = DISCOVER.load_config(workspace)
        written = DISCOVER.append_candidates(
            DISCOVER.candidate_store_path(workspace, config),
            [
                {
                    "schema_version": "1.0",
                    "candidate_id": self.CANDIDATE_ID,
                    "request_id": request_id,
                    "source_request_id": request_id,
                    "selected_for_request_id": request_id,
                    "selected_request_id": request_id,
                    "provider": "arxiv",
                    "discovery_providers": ["arxiv"],
                    "source_type": "paper",
                    "paper": {"provider_ids": {"arxiv": "2601.12345v2"}},
                    "lifecycle_schema_version": "2.0",
                    "lifecycle_state": "selected",
                    "status": "selected",
                    "selection_status": "selected",
                    "fetch_status": "planned",
                    "selected_at": "2026-07-21T00:00:00Z",
                    "selected_by": "agent-test",
                    "selection_reason": "Selected as the provider route for this request.",
                    "lifecycle_updated_at": "2026-07-21T00:00:00Z",
                    "lifecycle_updated_by": "agent-test",
                    "lifecycle_reason": "Selected as the provider route for this request.",
                }
            ],
        )
        self.assertEqual([self.CANDIDATE_ID], written)

    def deliver_paper(self, workspace: Path, request_id: str) -> str:
        """Acquire along the scoped route: a provenance-stamped paper, inventoried and normalized."""
        path = workspace / PAPER
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "<html><head><title>Solid Electrolyte Conductivity Survey</title></head>"
            "<body>Room-temperature ionic conductivity exceeds 1 mS/cm for the reported "
            "sulfide family.</body></html>\n",
            encoding="utf-8",
        )
        (workspace / f"{PAPER}.provenance.yml").write_text(
            yaml.safe_dump(
                {
                    "origin_url": "https://arxiv.org/abs/2601.12345v2",
                    "retrieved_at": "2026-07-20T00:00:00Z",
                    "retrieved_by": "fetch_sources.py/arxiv",
                    "license": "CC-BY-4.0",
                    "terms_url": "https://info.arxiv.org/help/license/index.html",
                    "request_id": request_id,
                    "candidate_id": self.CANDIDATE_ID,
                    "academic_provider": "arxiv",
                    "academic_source_type": "preprint",
                    "arxiv_id": "2601.12345v2",
                    "title": "Solid Electrolyte Conductivity Survey",
                    "authors": ["Ada Example"],
                    "published": "2026-01-10T00:00:00Z",
                    "checksum": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.run_script(INVENTORY, ["--report"], workspace)
        self.run_script(NORMALIZE, ["--all"], workspace)
        return self.source_id_for(workspace, PAPER)

    def rewrite_rationale(self, workspace: Path, request_id: str) -> None:
        """Rewrite one scoped request's stated reason, leaving every other field alone."""
        records = stored_requests(workspace)
        rewritten = 0
        for record in records:
            if record["request_id"] == request_id:
                record["rationale"] = self.REWRITTEN_RATIONALE
                rewritten += 1
        self.assertEqual(1, rewritten, records)
        (workspace / "sources" / "source-requests.jsonl").write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )

    def test_a_provider_submission_replayed_after_its_commit_still_accepts(self):
        """The provider arm's interrupted-finalization path, which nothing else covers.

        Logic-identical to the delegated one, and that is exactly why it is worth its own
        case: the two arms reach the same commit through different verification, so a
        change to either arm's tolerance can break this one alone.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.init_workspace(Path(tmpdir))
            self.enable_providers(workspace)
            request_id = self.block_question_on_a_request(workspace)
            self.select_candidate(workspace, request_id)
            self.start(workspace)
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)

            source_id = self.deliver_paper(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)
            self.discover(
                workspace,
                [
                    "candidates", "transition",
                    "--candidate-id", self.CANDIDATE_ID,
                    "--expected-state", "selected",
                    "--to-state", "fetched",
                    "--source-id", source_id,
                    "--reason", "Provenance-backed evidence was inventoried and normalized.",
                    "--actor", ACQUIRER,
                    "--run-id", order["run_id"],
                ],
            )

            before = session_document(workspace)
            artifacts = [PAPER, f"{PAPER}.provenance.yml", "sources/manifest.jsonl"]
            code, accepted = self.submit(workspace, order["action_id"], artifacts=artifacts)
            self.assertEqual(0, code, accepted)
            committed = stored_request(workspace, request_id)
            self.assertEqual("fulfilled", committed["status"], committed)

            # Roll the session back to the instant before the commit's own session write.
            session_path = workspace / "runs" / "orchestrations" / ORCHESTRATION_ID / "session.json"
            session_path.write_text(json.dumps(before, indent=2) + "\n", encoding="utf-8")

            replay_code, replay = self.submit(workspace, order["action_id"], artifacts=artifacts)

            self.assertEqual(0, replay_code, replay)
            self.assertEqual(
                committed,
                stored_request(workspace, request_id),
                msg="the replayed provider commit must be idempotent, not restamped",
            )

    def test_a_blocked_provider_action_that_claimed_a_fulfilment_is_refused(self):
        """The no-change assertions cannot see a claim, so the ledger has to be checked.

        A blocked attempt is one that recorded nothing. Both halves of that used to be
        provable from the workspace: a fulfilment moved the request store and a reopen moved
        the question page, and the two byte-equality checks caught either. Freezing both for
        the duration of the order satisfies those checks instead of disproving them, so a
        blocked submission that filed claims would pass them unread.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.init_workspace(Path(tmpdir))
            self.enable_providers(workspace)
            request_id = self.block_question_on_a_request(workspace)
            self.select_candidate(workspace, request_id)
            self.start(workspace)
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)

            source_id = self.deliver_paper(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            code, envelope = self.submit(
                workspace, order["action_id"], outcome="blocked"
            )

            self.assertNotEqual(0, code, envelope)
            self.assertIn("changed the source-request store", envelope["message"], envelope)
            self.assertEqual(
                [request_id], envelope["details"]["claimed_request_ids"], envelope
            )

    def test_a_blocked_action_that_claims_during_verification_is_refused(self):
        """The ledger read happens before six workspace snapshots, and can go stale.

        Every byte-equality check this arm runs would still pass -- that is the whole
        reason it reads the ledger at all -- so a claim filed while those snapshots run
        would be accepted as "changed nothing" and left uncommitted. The re-read at the
        acceptance boundary is what catches it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.init_workspace(Path(tmpdir))
            self.enable_providers(workspace)
            request_id = self.block_question_on_a_request(workspace)
            self.select_candidate(workspace, request_id)
            self.start(workspace)
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)
            source_id = self.deliver_paper(workspace, request_id)

            original = CONTROLLER.question_file_fingerprint_snapshot

            def claim_while_verifying(*args, **kwargs):
                CLAIMS.record_fulfilment_claim(
                    workspace, ORCHESTRATION_ID, order["action_id"],
                    request_id=request_id, source_id=source_id,
                    claimed_at="2026-01-01T00:00:00Z",
                )
                return original(*args, **kwargs)

            with mock.patch.object(
                CONTROLLER, "question_file_fingerprint_snapshot", claim_while_verifying
            ):
                code, envelope = self.submit(workspace, order["action_id"], outcome="blocked")

            self.assertNotEqual(0, code, envelope)
            self.assertIn("while it was being verified", envelope["message"], envelope)
            self.assertEqual([request_id], envelope["details"]["claimed_request_ids"], envelope)

    def test_a_failed_route_that_claims_during_verification_is_refused(self):
        """The late-claim re-read sat behind the `route_failed` early return.

        A blocked submission whose candidate transitioned to `failed` is still an accepted
        submission -- it returns the orchestration to planning, exit 0 -- so a claim filed
        during verification is left uncommitted exactly as it would be on the retryable
        path. The guard was written for the retryable path and returned before reaching
        this one, which is the difference this test pins.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.init_workspace(Path(tmpdir))
            self.enable_providers(workspace)
            request_id = self.block_question_on_a_request(workspace)
            self.select_candidate(workspace, request_id)
            self.start(workspace)
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)
            source_id = self.deliver_paper(workspace, request_id)

            self.discover(
                workspace,
                [
                    "candidates", "transition",
                    "--candidate-id", self.CANDIDATE_ID,
                    "--expected-state", "selected",
                    "--to-state", "failed",
                    "--reason", "The provider route returned nothing usable.",
                    "--actor", ACQUIRER,
                    "--run-id", order["run_id"],
                ],
            )

            original = CONTROLLER.question_file_fingerprint_snapshot

            def claim_while_verifying(*args, **kwargs):
                CLAIMS.record_fulfilment_claim(
                    workspace, ORCHESTRATION_ID, order["action_id"],
                    request_id=request_id, source_id=source_id,
                    claimed_at="2026-01-01T00:00:00Z",
                )
                return original(*args, **kwargs)

            with mock.patch.object(
                CONTROLLER, "question_file_fingerprint_snapshot", claim_while_verifying
            ):
                code, envelope = self.submit(workspace, order["action_id"], outcome="blocked")

            self.assertNotEqual(0, code, envelope)
            self.assertIn("while it was being verified", envelope["message"], envelope)
            self.assertEqual(
                [request_id], envelope["details"]["claimed_request_ids"], envelope
            )

    def test_a_reopen_claim_for_a_question_this_order_never_scoped_is_refused(self):
        """Without this, one order's paper reopens a question that order never touched.

        Reopen claims are read from the ledger, which the acquirer writes, and the question
        pages are frozen for the duration of the order. So a claim naming a slug outside the
        order's blocked-question baseline moves nothing: every byte-equality check passes,
        the question-file scope guard sees a page that never changed, and the transition
        guard only walks the baseline. Verification would then commit the claim -- the
        unscoped question flips to open and takes on this order's source. The slug set is
        read here or nowhere.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            # The harness's spare question, blocked here on a request of its own. Blocked,
            # so it is not actionable research and the workspace still routes to
            # acquisition; on a request no candidate was ever selected for, so this order
            # never scopes it -- which is what keeps it out of the blocked-question baseline.
            unscoped_slug = "answerable-from-delivered-evidence"
            workspace = self.init_workspace(Path(tmpdir), spare_question=True)
            self.enable_providers(workspace)
            request_id = self.block_question_on_a_request(workspace)
            # The claim/request/block route `block_question_on_a_request` walks, inlined
            # only to lower the priority: two `high` requests created in the same second are
            # ordered by generated request id, so which one the route walk reaches first --
            # and with it whether this order is acquisition or discovery -- would otherwise
            # differ run to run.
            unscoped_request_id = self.run_script(
                REQUESTS,
                [
                    "add", "--kind", "other",
                    "--query-or-identifier", "Competitor pricing for the neighbouring question",
                    "--rationale", "The question cannot be answered from delivered evidence.",
                    "--priority", "low", "--question-slug", unscoped_slug,
                ],
                workspace,
            )["request"]["request_id"]
            self.run_script(
                CLAIM,
                ["claim", "--slug", unscoped_slug, "--agent-id", "research-agent"],
                workspace,
            )
            self.run_script(
                RESOLVE,
                [
                    "block", "--slug", unscoped_slug, "--agent-id", "research-agent",
                    "--blocked-reason", "No competitor quote has been delivered.",
                    "--request-id", unscoped_request_id,
                ],
                workspace,
            )
            self.select_candidate(workspace, request_id)
            self.start(workspace)
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)
            self.assertEqual("acquisition", order["phase"], order)
            self.assertEqual([request_id], order["scope"]["request_ids"], order)
            self.assertEqual([QUESTION_SLUG], order["scope"]["question_slugs"], order)

            source_id = self.deliver_paper(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)
            self.discover(
                workspace,
                [
                    "candidates", "transition",
                    "--candidate-id", self.CANDIDATE_ID,
                    "--expected-state", "selected",
                    "--to-state", "fetched",
                    "--source-id", source_id,
                    "--reason", "Provenance-backed evidence was inventoried and normalized.",
                    "--actor", ACQUIRER,
                    "--run-id", order["run_id"],
                ],
            )
            # The acquirer decides its paper answers the neighbouring question too, and
            # files the reopen for it. Everything else about this submission is in order.
            CLAIMS.record_reopen_claim(
                workspace, ORCHESTRATION_ID, order["action_id"],
                question_slug=unscoped_slug,
                source_ids=[source_id],
                request_ids=[unscoped_request_id],
                claimed_at="2026-01-01T00:00:00Z",
            )

            code, envelope = self.submit(
                workspace,
                order["action_id"],
                artifacts=[PAPER, f"{PAPER}.provenance.yml", "sources/manifest.jsonl"],
            )

            self.assertNotEqual(0, code, envelope)
            self.assertIn("did not scope as blocked", envelope["message"], envelope)
            self.assertEqual([unscoped_slug], envelope["details"]["question_slugs"], envelope)

            # Refused, so nothing was committed: the unscoped page never took the source on,
            # the scoped page is still blocked, and the fulfilment is still only a claim.
            unscoped = question_fields(workspace, unscoped_slug)
            self.assertEqual("blocked", unscoped["status"], unscoped)
            self.assertEqual([], unscoped["source_ids"], unscoped)
            self.assertEqual("blocked", question_fields(workspace)["status"], "the scoped page must not move")
            self.assertEqual("open", stored_request(workspace, request_id)["status"])

    def test_a_fulfilment_leaving_another_question_the_request_names_blocked_is_refused(self):
        """Without it an order closes over a question its own fulfilled request never unblocked.

        The transition guard above walks the baseline only -- the scoped questions this
        order holds the blockers for. A request may name a second question that some other
        request blocks; that question is in no baseline, and no reopen for it would be
        authorized, so this check is the only thing that reads it at all. Delete it and the
        submission is accepted, the fulfilment commits, and the request goes to `fulfilled`
        declaring it unblocked a question the workspace still reports as blocked.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.init_workspace(Path(tmpdir))
            self.enable_providers(workspace)
            self.add_second_question(workspace)

            for slug in (QUESTION_SLUG, self.SECOND_SLUG):
                self.run_script(
                    CLAIM, ["claim", "--slug", slug, "--agent-id", "research-agent"], workspace
                )
            # The scoped request names both questions; a separate request is what actually
            # blocks the second one. That is the shape no baseline covers -- the order
            # scopes both slugs, but only the question whose blocker it holds is in
            # `blocked_questions_before`.
            request_id = self.run_script(
                REQUESTS,
                [
                    "add", "--kind", "other",
                    "--query-or-identifier", "Live supplier quote for B0ABC12345",
                    "--rationale", self.ORIGINAL_RATIONALE,
                    "--priority", "high",
                    "--question-slug", QUESTION_SLUG,
                    "--question-slug", self.SECOND_SLUG,
                ],
                workspace,
            )["request"]["request_id"]
            # Lower priority, so `open_requests` sorts it behind the scoped request
            # whatever second the two were created in: request ids break a same-second tie,
            # and routing would issue discovery for whichever came first.
            other_request_id = self.run_script(
                REQUESTS,
                [
                    "add", "--kind", "other",
                    "--query-or-identifier", "Competitor offer snapshot",
                    "--rationale", self.ORIGINAL_RATIONALE,
                    "--priority", "low",
                    "--question-slug", self.SECOND_SLUG,
                ],
                workspace,
            )["request"]["request_id"]
            for slug, blocker in (
                (QUESTION_SLUG, request_id),
                (self.SECOND_SLUG, other_request_id),
            ):
                self.run_script(
                    RESOLVE,
                    [
                        "block", "--slug", slug, "--agent-id", "research-agent",
                        "--blocked-reason", "No supplier quote has been delivered.",
                        "--request-id", blocker,
                    ],
                    workspace,
                )

            self.select_candidate(workspace, request_id)
            self.start(workspace)
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)
            self.assertEqual([request_id], order["scope"]["request_ids"], order)
            self.assertEqual(
                [QUESTION_SLUG, self.SECOND_SLUG], order["scope"]["question_slugs"], order
            )

            source_id = self.deliver_paper(workspace, request_id)
            # Reopens the one question this order holds the blocker for. The other is
            # outside the baseline, so a reopen claim for it would be refused as unscoped:
            # the acquirer has no route that answers for it, and leaves it blocked.
            self.fulfil_and_reopen(workspace, request_id, source_id)
            self.discover(
                workspace,
                [
                    "candidates", "transition",
                    "--candidate-id", self.CANDIDATE_ID,
                    "--expected-state", "selected",
                    "--to-state", "fetched",
                    "--source-id", source_id,
                    "--reason", "Provenance-backed evidence was inventoried and normalized.",
                    "--actor", ACQUIRER,
                    "--run-id", order["run_id"],
                ],
            )

            code, refused = self.submit(
                workspace,
                order["action_id"],
                artifacts=[PAPER, f"{PAPER}.provenance.yml", "sources/manifest.jsonl"],
            )

            self.assertNotEqual(
                0, code, msg=f"a question the fulfilled request names must not stay blocked: {refused}"
            )
            self.assertIn(
                "questions linked to fulfilled evidence remain blocked",
                refused["message"],
                refused,
            )
            self.assertEqual([self.SECOND_SLUG], refused["details"]["question_slugs"], refused)

            # Refused, so nothing committed: both pages still hold what issuance captured,
            # and the scoped request is still open for another attempt.
            scoped = question_fields(workspace)
            self.assertEqual("blocked", scoped["status"], scoped)
            second = question_fields(workspace, self.SECOND_SLUG)
            self.assertEqual("blocked", second["status"], second)
            self.assertEqual([other_request_id], second["blocking_request_ids"], second)
            record = stored_request(workspace, request_id)
            self.assertEqual("open", record["status"], record)
            self.assertIsNone(record["source_id"], record)

    def test_a_provider_claim_pairing_a_contradicting_source_scope_is_refused(self):
        """Writing this arm's ledger by hand must not get past the check `fulfill` applies.

        `check_fulfill_scope` lives in the CLI, so it is reached only by a caller that uses
        the CLI. The ledger is a file the acquirer writes, so a claim put there directly
        would pair a request with a source whose declared scope contradicts it and never
        meet that check at all. The delegated arm re-runs the predicate and has its own
        case for it; this arm reaches the same commit through separate verification, so
        without this the provider copy of the re-run could be deleted and stay green --
        a provider acquirer could fulfil a facet-X request with facet-Y evidence.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.init_workspace(Path(tmpdir))
            self.enable_providers(workspace)
            request_id = self.block_question_on_a_request(workspace)
            # Declared before issuance, so the frozen baseline carries it and the store is
            # never edited inside the order -- the contradiction is in the claim alone.
            records = stored_requests(workspace)
            for record in records:
                if record["request_id"] == request_id:
                    record["scope"] = {"facet_id": "a-facet-the-paper-does-not-carry"}
            (workspace / "sources" / "source-requests.jsonl").write_text(
                "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
                encoding="utf-8",
            )
            self.select_candidate(workspace, request_id)
            self.start(workspace)
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)

            source_id = self.deliver_paper(workspace, request_id)
            # Stamped after the delivery because `deliver_paper` writes the sidecar itself,
            # and re-inventoried because the manifest record is where the pairing check
            # reads a source's declared scope.
            sidecar_path = workspace / f"{PAPER}.provenance.yml"
            sidecar = yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
            sidecar["scope"] = {"facet_id": "a-facet-the-request-did-not-ask-for"}
            sidecar_path.write_text(yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8")
            self.run_script(INVENTORY, ["--report"], workspace)

            # Filed straight into the ledger: `fulfill` refuses this pairing itself, so the
            # only way an acquirer gets it as far as submission is by skipping the command.
            CLAIMS.record_fulfilment_claim(
                workspace, ORCHESTRATION_ID, order["action_id"],
                request_id=request_id, source_id=source_id, claimed_at="2026-01-01T00:00:00Z",
            )
            # Claimed too, so the question-transition guard is satisfied and the scope
            # contradiction is the only thing left for verification to answer.
            CLAIMS.record_reopen_claim(
                workspace, ORCHESTRATION_ID, order["action_id"],
                question_slug=QUESTION_SLUG, source_ids=[source_id], request_ids=[request_id],
                claimed_at="2026-01-01T00:00:00Z",
            )
            # Everything else the order asked for is done, so nothing earlier can answer.
            self.discover(
                workspace,
                [
                    "candidates", "transition",
                    "--candidate-id", self.CANDIDATE_ID,
                    "--expected-state", "selected",
                    "--to-state", "fetched",
                    "--source-id", source_id,
                    "--reason", "Provenance-backed evidence was inventoried and normalized.",
                    "--actor", ACQUIRER,
                    "--run-id", order["run_id"],
                ],
            )

            code, envelope = self.submit(
                workspace,
                order["action_id"],
                artifacts=[PAPER, f"{PAPER}.provenance.yml", "sources/manifest.jsonl"],
            )

            self.assertNotEqual(0, code, envelope)
            self.assertIn(
                "acquisition claimed a fulfilment whose scope contradicts the request",
                envelope["message"],
                envelope,
            )
            self.assertEqual(
                [{"request_id": request_id, "source_id": source_id}],
                [
                    {"request_id": failure["request_id"], "source_id": failure["source_id"]}
                    for failure in envelope["details"]["scope_pairing_failures"]
                ],
                envelope,
            )

            # Refused, so the request is still open and the question still blocked: the
            # contradiction never became a commit.
            record = stored_request(workspace, request_id)
            self.assertEqual("open", record["status"], record)
            self.assertIsNone(record["source_id"], record)
            fields = question_fields(workspace)
            self.assertEqual("blocked", fields["status"], fields)

    def test_a_scoped_requests_record_may_not_be_rewritten_mid_order(self):
        """No part of a scoped request record, not only its scope, survives a mid-order edit.

        The provider arm's request-scope guard used to admit every request id the order
        named, so any field of that record -- including the rationale saying why the
        evidence was wanted -- could be restated to match whatever turned up, and the
        submission was still accepted.

        The store is frozen for the duration of an acquisition order now, and the guard
        exempts only a record already carrying its own committed claim. The rewrite is
        refused, and refused on the record rather than on the field, which is why a
        rationale is a fair test of it: nothing here is scope-aware.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.init_workspace(Path(tmpdir))
            self.enable_providers(workspace)
            request_id = self.block_question_on_a_request(workspace)
            self.select_candidate(workspace, request_id)
            self.assertEqual(
                self.ORIGINAL_RATIONALE,
                stored_request(workspace, request_id)["rationale"],
                stored_requests(workspace),
            )

            session = self.start(workspace)
            self.assertEqual("providers", session["acquisition_mode"], session)
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)
            self.assertEqual("acquisition", order["phase"], order)
            self.assertEqual([request_id], order["scope"]["request_ids"], order)
            self.assertEqual([self.CANDIDATE_ID], order["scope"]["candidate_ids"], order)

            source_id = self.deliver_paper(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)
            self.discover(
                workspace,
                [
                    "candidates", "transition",
                    "--candidate-id", self.CANDIDATE_ID,
                    "--expected-state", "selected",
                    "--to-state", "fetched",
                    "--source-id", source_id,
                    "--reason", "Provenance-backed evidence was inventoried and normalized.",
                    "--actor", ACQUIRER,
                    "--run-id", order["run_id"],
                ],
            )
            self.rewrite_rationale(workspace, request_id)

            code, accepted = self.submit(
                workspace,
                order["action_id"],
                artifacts=[PAPER, f"{PAPER}.provenance.yml", "sources/manifest.jsonl"],
            )

            self.assertNotEqual(
                0, code, msg=f"a mid-order rewrite must cost the order its acceptance: {accepted}"
            )
            self.assertIn("changed source requests outside", accepted["message"], accepted)
            self.assertEqual(
                [request_id],
                accepted["details"]["source_request_scope_violations"]["changed_outside_scope"],
                accepted,
            )

            # Refused, so nothing was committed: the fulfilment is still only a claim. The
            # rewrite itself stands, because a refusal names an edit rather than undoing it.
            record = stored_request(workspace, request_id)
            self.assertEqual("open", record["status"], record)
            self.assertIsNone(record["source_id"], record)
            self.assertEqual(self.REWRITTEN_RATIONALE, record["rationale"], record)



class ClaimIdempotencyTests(DelegatedWorkspace, unittest.TestCase):
    """The re-filing contract a downstream consumer told us its replay path depends on.

    They call `fulfill` inside their submit path, immediately before the driver call, so a
    retried node re-files *and* re-submits. Every assertion here is about that shape: an
    identical re-fulfil has to stay an idempotent no-op, and it has to stay one across a
    refusal and across `resume`, because a second claim -- or an `updated` that flipped
    false to true -- would be a silent semantic change on exactly their path.
    """

    def pending_acquisition(self, root: Path) -> tuple[Path, str, str]:
        workspace, request_id = self.make_workspace(root)
        self.start(workspace)
        order = self.pending_order(workspace)
        return workspace, request_id, order["action_id"]

    def fulfil(self, workspace: Path, request_id: str, source_id: str) -> dict:
        return self.run_script(
            REQUESTS, ["fulfill", "--request-id", request_id, "--source-id", source_id], workspace
        )

    def fulfil_refused(self, workspace: Path, request_id: str, source_id: str) -> tuple[int, dict]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = REQUESTS.main(
                [
                    "--project-root", str(workspace),
                    "fulfill", "--request-id", request_id, "--source-id", source_id,
                    "--format", "json",
                ]
            )
        return int(code or 0), json.loads(stderr.getvalue() or stdout.getvalue())

    def test_an_identical_re_fulfil_is_a_no_op_leaving_one_ledger_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, action_id = self.pending_acquisition(Path(tmpdir))
            source_id = self.deliver_for(workspace, request_id)

            first = self.fulfil(workspace, request_id, source_id)
            second = self.fulfil(workspace, request_id, source_id)

            self.assertTrue(first["updated"], first)
            self.assertTrue(first["contingent"], first)
            self.assertFalse(second["updated"], second)
            self.assertTrue(second["contingent"], second)
            claims = claim_ledger(workspace, action_id)
            self.assertEqual([request_id], sorted(claims["fulfilments"]), claims)
            # Compared whole, not field by field. Both are second-resolution stamps, so an
            # assertion on `updated_at` alone passes whenever the two calls land inside one
            # second and fails when they straddle a boundary -- which is how a real
            # disagreement between the two branches hid here.
            self.assertEqual(
                first["request"],
                second["request"],
                msg="a replay must report exactly what the call it replays reported",
            )

    def test_re_fulfilling_with_a_different_source_is_refused_against_the_claim(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, action_id = self.pending_acquisition(Path(tmpdir))
            source_id = self.deliver_for(workspace, request_id)
            self.fulfil(workspace, request_id, source_id)

            code, envelope = self.fulfil_refused(workspace, request_id, "another-source")

            self.assertNotEqual(0, code, envelope)
            self.assertEqual("REQUEST_ALREADY_FULFILLED", envelope["error_code"], envelope)
            self.assertTrue(envelope["recoverable"], envelope)
            # Refused against the claim: the durable record never said fulfilled at all.
            self.assertEqual("open", stored_request(workspace, request_id)["status"])
            self.assertEqual(
                source_id, claim_ledger(workspace, action_id)["fulfilments"][request_id]["source_id"]
            )

    def test_a_second_reopen_adds_to_the_claim_rather_than_replacing_it(self):
        """Re-filing must not drop what the first reopen contributed.

        A reopen recomputes its source list from the question page, and the page is frozen
        inside the order -- so a second call starts from the same unchanged frontmatter and
        would overwrite the first claim with a narrower one. That loss was impossible while
        the page moved, because the second call was refused as not reopenable. A per-request
        loop over a multi-source question is exactly the shape that would hit it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, action_id = self.pending_acquisition(Path(tmpdir))
            source_id = self.deliver_for(workspace, request_id)
            self.fulfil(workspace, request_id, source_id)

            # Stand in for an earlier pass of a per-request loop: a claim this action
            # already filed, naming a source the frozen page does not carry.
            earlier = "src-from-an-earlier-pass"
            CLAIMS.record_reopen_claim(
                workspace,
                ORCHESTRATION_ID,
                action_id,
                question_slug=QUESTION_SLUG,
                source_ids=[earlier],
                request_ids=["req-from-an-earlier-pass"],
                claimed_at="2026-01-01T00:00:00Z",
            )

            second = self.run_script(
                RESOLVE,
                [
                    "reopen", "--slug", QUESTION_SLUG, "--agent-id", ACQUIRER,
                    "--source-id", source_id, "--request-id", request_id,
                ],
                workspace,
            )

            self.assertTrue(second["contingent"], second)
            claim = claim_ledger(workspace, action_id)["reopens"][QUESTION_SLUG]
            self.assertEqual(
                sorted([earlier, source_id]),
                sorted(claim["source_ids"]),
                msg=f"the earlier pass's source must survive the second reopen: {claim}",
            )
            self.assertIn("req-from-an-earlier-pass", claim["request_ids"], claim)

    def test_the_projection_reports_the_record_the_commit_will_write(self):
        """What `fulfill` shows must be what lands, not a stamp the commit never writes.

        The commit writes `status` and `source_id` and nothing else, so that a replay after
        an interrupted finalization can revert exactly that delta and prove the rest of the
        record never moved. A report projecting a fresh `updated_at` would be describing a
        record the controller is not going to write.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, action_id = self.pending_acquisition(Path(tmpdir))
            issued = stored_request(workspace, request_id)
            source_id = self.deliver_for(workspace, request_id)

            reported = self.fulfil(workspace, request_id, source_id)["request"]

            self.assertEqual(
                {**issued, "status": "fulfilled", "source_id": source_id},
                reported,
                msg=f"the projection must differ from the issued record only by the commit's delta: {reported}",
            )

    def test_a_claim_read_by_a_fresh_process_is_still_the_same_claim(self):
        """A claim held in memory would make the replay depend on which process replays it.

        The consumer's retry can arrive after a lease renewal or a resumed session, so the
        second `fulfill` is not guaranteed to run in the process that filed the first. This
        replays through a separately executed copy of the command, holding none of the
        first one's state, and requires the same no-op answer.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, action_id = self.pending_acquisition(Path(tmpdir))
            source_id = self.deliver_for(workspace, request_id)
            self.fulfil(workspace, request_id, source_id)

            successor = load_script_uncached("bookkeeping_successor_requests", "source_requests.py")
            self.assertIsNot(REQUESTS, successor)
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = successor.main(
                    [
                        "--project-root", str(workspace),
                        "fulfill", "--request-id", request_id, "--source-id", source_id,
                        "--format", "json",
                    ]
                )
            self.assertEqual(0, int(code or 0), stderr.getvalue())
            replayed = json.loads(stdout.getvalue())

            self.assertFalse(replayed["updated"], replayed)
            self.assertTrue(replayed["contingent"], replayed)
            self.assertEqual([request_id], sorted(claim_ledger(workspace, action_id)["fulfilments"]))
            self.assertEqual("open", stored_request(workspace, request_id)["status"])


class CommitReplayTests(DelegatedWorkspace, unittest.TestCase):
    """Finalization is replayable, so the commit has to survive being interrupted."""

    def test_a_submission_replayed_after_its_commit_still_accepts(self):
        """A crash between the commit and the session write must not wedge the order.

        `finalize_pending_submission` writes the session last, so a process that dies after
        the wet pass has committed leaves the bookkeeping durable and the submission still
        pending. The replay's *dry* pass then reads a store that no longer matches the
        frozen baseline. It is admitted only because the scope guard tolerates exactly the
        ids whose durable record already equals its own claim -- status and source id both.
        Tolerating status alone would admit a request relinked to some other source, which
        is the one thing the relink refusal exists to stop.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)
            action_id = order["action_id"]
            source_id = self.deliver_for(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            before = session_document(workspace)
            code, envelope = self.submit(workspace, action_id)
            self.assertEqual(0, code, envelope)
            committed = stored_request(workspace, request_id)
            self.assertEqual("fulfilled", committed["status"], committed)
            self.assertEqual(source_id, committed["source_id"], committed)

            # Roll the session back to the instant before the commit's own session write,
            # leaving the committed bookkeeping exactly where the crash would have left it.
            session_path = workspace / "runs" / "orchestrations" / ORCHESTRATION_ID / "session.json"
            session_path.write_text(json.dumps(before, indent=2) + "\n", encoding="utf-8")

            replay_code, replay = self.submit(workspace, action_id)

            self.assertEqual(0, replay_code, replay)
            replayed = stored_request(workspace, request_id)
            self.assertEqual(
                committed,
                replayed,
                msg=f"the replayed commit must be idempotent, not restamped: {replayed}",
            )

    def test_a_replay_does_not_license_editing_the_rest_of_the_record(self):
        """The replay allowance is for the commit's own delta, not for the record.

        Exempting the request id outright would let anything on that record differ from the
        frozen baseline once `status` and `source_id` happened to match -- so an edit made
        in the window between the crash and the replay would be admitted, and the commit
        would then skip the record as already done. The allowance reverts the delta and
        requires the result to fingerprint back to issuance.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            action_id = self.pending_order(workspace)["action_id"]
            source_id = self.deliver_for(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            before = session_document(workspace)
            code, envelope = self.submit(workspace, action_id)
            self.assertEqual(0, code, envelope)

            # Crash: the commit landed, the session write did not. Then a field the commit
            # never touches is edited before the replay.
            path = workspace / "sources" / "source-requests.jsonl"
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
            for record in records:
                if record["request_id"] == request_id:
                    record["rationale"] = "rewritten after the commit"
            path.write_text(
                "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records),
                encoding="utf-8",
            )
            session_path = workspace / "runs" / "orchestrations" / ORCHESTRATION_ID / "session.json"
            session_path.write_text(json.dumps(before, indent=2) + "\n", encoding="utf-8")

            replay_code, replay = self.submit(workspace, action_id)

            self.assertNotEqual(0, replay_code, replay)
            self.assertIn("outside the fulfilled request scope", replay["message"], replay)


class LedgerIsNotTrustedTests(DelegatedWorkspace, unittest.TestCase):
    """The ledger is acquirer-writable, so verification treats it as a claim, not a fact."""

    def pending_acquisition(self, root: Path) -> tuple[Path, str, str]:
        workspace, request_id = self.make_workspace(root)
        self.start(workspace)
        return workspace, request_id, self.pending_order(workspace)["action_id"]

    def test_a_claim_naming_a_source_whose_scope_contradicts_the_request_is_refused(self):
        """Writing the ledger by hand must not get past the check `fulfill` applies.

        `check_fulfill_scope` lives in the CLI, so it is reached only by a caller that uses
        the CLI. The ledger is a file the acquirer writes, so a claim put there directly
        would pair a request with a source whose declared scope contradicts it and never
        meet that check at all. Verification re-runs the same predicate.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            # Declared before issuance, so the frozen baseline carries it and the store is
            # never edited inside the order -- the contradiction is in the claim alone.
            path = workspace / "sources" / "source-requests.jsonl"
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
            for record in records:
                if record["request_id"] == request_id:
                    record["scope"] = {"facet_id": "a-facet-the-delivery-does-not-carry"}
            path.write_text(
                "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records),
                encoding="utf-8",
            )
            self.start(workspace)
            action_id = self.pending_order(workspace)["action_id"]
            source_id = self.deliver_for(workspace, request_id, scope={"facet_id": "a-different-facet"})
            CLAIMS.record_fulfilment_claim(
                workspace, ORCHESTRATION_ID, action_id,
                request_id=request_id, source_id=source_id, claimed_at="2026-01-01T00:00:00Z",
            )
            # Claimed too, so the question-transition guard is satisfied and the scope
            # contradiction is the only thing left for verification to answer.
            CLAIMS.record_reopen_claim(
                workspace, ORCHESTRATION_ID, action_id,
                question_slug=QUESTION_SLUG, source_ids=[source_id], request_ids=[request_id],
                claimed_at="2026-01-01T00:00:00Z",
            )

            code, envelope = self.submit(workspace, action_id, artifacts=[f"raw/data/{PAYLOAD.name}"])

            self.assertNotEqual(0, code, envelope)
            self.assertIn("scope contradicts the request", envelope["message"], envelope)
            self.assertEqual(
                request_id,
                envelope["details"]["scope_pairing_failures"][0]["request_id"],
                envelope,
            )

    def test_a_claim_naming_a_request_this_order_does_not_scope_is_refused(self):
        """A claim may only name a request the work order asked for.

        The scope is the whole authorization: it is what routing decided this acquirer is
        allowed to close, and what the operator sees on the order. The ledger is a file the
        acquirer writes, so a claim put there directly names any request in the store,
        scoped or not, and never passes through the routing decision at all. Without this
        the commit would fulfil a request nobody ordered -- overwriting the source id an
        earlier order already recorded against it -- on the word of the acquirer alone.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            elsewhere = self.add_request(workspace, "Delivered under an order that is already closed")
            bystander = self.deliver_unnormalized_bystander(workspace, elsewhere)
            # Fulfilled before the session exists, so routing leaves it out of the order's
            # scope while the store still holds it: an *open* request with no question
            # behind it is still scoped, and a scoped id is not the attack.
            self.run_script(
                REQUESTS, ["fulfill", "--request-id", elsewhere, "--source-id", bystander], workspace
            )
            self.start(workspace)
            order = self.pending_order(workspace)
            action_id = order["action_id"]
            self.assertEqual([request_id], order["scope"]["request_ids"], order)
            served = stored_request(workspace, elsewhere)

            source_id = self.deliver_for(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)
            CLAIMS.record_fulfilment_claim(
                workspace, ORCHESTRATION_ID, action_id,
                request_id=elsewhere, source_id=source_id, claimed_at="2026-01-01T00:00:00Z",
            )

            code, envelope = self.submit(workspace, action_id, artifacts=[f"raw/data/{PAYLOAD.name}"])

            self.assertNotEqual(0, code, envelope)
            self.assertIn("a request this order does not scope", envelope["message"], envelope)
            self.assertEqual([elsewhere], envelope["details"]["request_ids"], envelope)
            self.assertEqual(
                served,
                stored_request(workspace, elsewhere),
                msg="the unscoped request must hold the source the earlier order recorded",
            )
            self.assertEqual("open", stored_request(workspace, request_id)["status"])

    def test_a_forged_reopen_source_is_refused_before_it_reaches_the_page(self):
        """A claim cannot attach evidence the manifest has no record of.

        `reopen` validates every `--source-id` against the manifest before it will touch a
        page. The ledger is a file the acquirer writes, so a claim put there directly skips
        that command -- and naming the real source *plus* an arbitrary id would satisfy the
        expected-ids check, which only asks that the expected set is a subset of what was
        claimed. The commit would then write the arbitrary id into the frontmatter.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, action_id = self.pending_acquisition(Path(tmpdir))
            source_id = self.deliver_for(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            # The real source, plus one the workspace has never seen.
            CLAIMS.record_reopen_claim(
                workspace, ORCHESTRATION_ID, action_id,
                question_slug=QUESTION_SLUG,
                source_ids=[source_id, "raw:forged-by-the-acquirer"],
                request_ids=[request_id],
                claimed_at="2026-01-01T00:00:00Z",
            )

            code, envelope = self.submit(workspace, action_id, artifacts=[f"raw/data/{PAYLOAD.name}"])

            self.assertNotEqual(0, code, envelope)
            self.assertIn("the reopen command would refuse", envelope["message"], envelope)
            self.assertEqual(
                [
                    {
                        "question_slug": QUESTION_SLUG,
                        "source_id": "raw:forged-by-the-acquirer",
                        "reason": "not_in_manifest",
                    }
                ],
                envelope["details"]["unvalidated_reopen_sources"],
                envelope,
            )
            self.assertEqual("blocked", question_fields(workspace)["status"], "the page must not move")

    def test_a_reopen_claim_naming_an_unnormalized_source_is_refused(self):
        """Manifest membership is only half of what `reopen` requires of a source id.

        `transition_reopen` gates every `--source-id` on two things: the manifest holds the
        record, *and* normalization has produced one (`SOURCE_NOT_NORMALIZED`). The
        controller's stand-in for that command originally rebuilt only the membership half,
        so an inventoried-but-un-normalized record -- which is in the manifest, and is the
        ordinary state of a delivery a prior order never finished -- went onto a durable
        page as evidence nothing has read.

        The bystander here is exactly that state, and the CLI refuses the same edit.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, action_id = self.pending_acquisition(Path(tmpdir))
            source_id = self.deliver_for(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)
            stray = self.deliver_unnormalized_bystander(
                workspace, "req-a-bystander-nobody-scopes"
            )

            CLAIMS.record_reopen_claim(
                workspace, ORCHESTRATION_ID, action_id,
                question_slug=QUESTION_SLUG,
                source_ids=[source_id, stray],
                request_ids=[request_id],
                claimed_at="2026-01-01T00:00:00Z",
            )

            code, envelope = self.submit(workspace, action_id, artifacts=[f"raw/data/{PAYLOAD.name}"])

            self.assertNotEqual(0, code, envelope)
            self.assertIn("the reopen command would refuse", envelope["message"], envelope)
            self.assertEqual(
                [
                    {
                        "question_slug": QUESTION_SLUG,
                        "source_id": stray,
                        "reason": "not_normalized",
                    }
                ],
                envelope["details"]["unvalidated_reopen_sources"],
                envelope,
            )
            self.assertEqual("blocked", question_fields(workspace)["status"], "the page must not move")

    def test_a_blocked_delegated_action_that_claims_during_verification_is_refused(self):
        """The delegated blocked arm reads the ledger once, at the top, and then never again.

        Its sibling on the provider arm re-reads at the acceptance boundary for a reason
        that applies identically here: the store and the pages are frozen, so every
        byte-equality check between the two reads is satisfied by a claim rather than
        disproved by one, and a claim filed in that window rides out on an accepted
        "changed nothing" submission with nothing to commit it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, action_id = self.pending_acquisition(Path(tmpdir))
            # Nothing is delivered: a blocked delegated action that changed the workspace is
            # refused for that instead, and the claim would never be the thing under test.
            original = CONTROLLER.raw_tree_snapshot

            def claim_while_verifying(*args, **kwargs):
                CLAIMS.record_fulfilment_claim(
                    workspace, ORCHESTRATION_ID, action_id,
                    request_id=request_id, source_id="raw:claimed-after-the-read",
                    claimed_at="2026-01-01T00:00:00Z",
                )
                return original(*args, **kwargs)

            with mock.patch.object(CONTROLLER, "raw_tree_snapshot", claim_while_verifying):
                code, envelope = self.submit(workspace, action_id, outcome="blocked")

            self.assertNotEqual(0, code, envelope)
            self.assertIn("while it was being verified", envelope["message"], envelope)
            self.assertEqual(
                [request_id], envelope["details"]["claimed_request_ids"], envelope
            )

    def test_a_page_hand_written_to_the_projection_cannot_smuggle_extra_sources(self):
        """The replay exemption compared the page against a projection built from itself.

        A page whose durable state already equals what the commit would produce is exempted
        from the question-file freeze, so an interrupted finalization can be replayed. The
        exactness test guarding that exemption projected `durable | claimed` and then asked
        whether `durable` equalled it -- which is true of *any* superset of the claimed
        sources. An acquirer could reopen through the CLI, hand-write the page open with
        extra source ids the manifest never saw, and have the whole file exempted.

        The projection is built from the frozen baseline the work order recorded, so the
        extra ids are a difference from it rather than part of it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, action_id = self.pending_acquisition(Path(tmpdir))
            source_id = self.deliver_for(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            # The claim is filed and the page is still blocked. Hand-write it to the state a
            # committed reopen leaves behind -- plus two ids nothing delivered.
            page = workspace / "wiki" / "questions" / f"{QUESTION_SLUG}.md"
            text = page.read_text(encoding="utf-8")
            self.assertIn("status: blocked", text, "the page must still be frozen at this point")
            forged = ["raw:forged-by-hand-a", "raw:forged-by-hand-b"]
            lines = [
                line for line in text.splitlines(keepends=True)
                if not line.startswith("blocking_request_ids:") and not line.startswith("- req-")
            ]
            text = "".join(lines).replace("status: blocked", "status: open", 1)
            head, frontmatter, body = text.split("---\n", 2)
            parsed = yaml.safe_load(frontmatter)
            parsed["source_ids"] = sorted({source_id, *forged})
            parsed.pop("blocking_request_ids", None)
            parsed.pop("blocked_reason", None)
            page.write_text(
                head + "---\n" + yaml.safe_dump(parsed, sort_keys=False) + "---\n" + body,
                encoding="utf-8",
            )

            code, envelope = self.submit(workspace, action_id, artifacts=[f"raw/data/{PAYLOAD.name}"])

            self.assertNotEqual(0, code, envelope)
            self.assertIn("question", envelope["message"], envelope)
            # The refusal does not restore the page -- the operator is told to -- but it
            # must commit nothing, so the fulfilment the claim describes stays uncommitted.
            self.assertEqual("open", stored_request(workspace, request_id)["status"])

    def test_an_unreadable_ledger_refuses_both_arms_rather_than_reading_as_no_claims(self):
        """A ledger the controller cannot read must never be reported as "no claims".

        The acquirer writes this file, so the acquirer can also damage it, and degrading an
        unreadable read to an empty document is the fail-open this whole mechanism exists to
        remove. It lands differently on each arm, so both are walked here: the completed arm
        would accept having committed nothing, and the blocked arm -- whose byte-equality
        checks are all satisfied by a store and a page the order freezes anyway -- would
        pause for replay with a real fulfilment and a real reopen still on file.
        """
        for outcome in ("completed", "blocked"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmpdir:
                workspace, request_id = self.make_workspace(Path(tmpdir))
                # Delivered and normalized before issuance, so the order's evidence
                # baseline already covers it and nothing the acquirer does inside the order
                # moves a durable byte. The ledger is then the only thing left to read, and
                # an arm that reads it as empty has nothing left to disagree with.
                source_id = self.deliver_before_the_order(workspace, request_id)
                self.start(workspace)
                action_id = self.pending_order(workspace)["action_id"]
                self.fulfil_and_reopen(workspace, request_id, source_id)

                ledger = (
                    workspace / "runs" / "order-claims" / ORCHESTRATION_ID / f"{action_id}.json"
                )
                ledger.write_text("{ truncated", encoding="utf-8")

                code, envelope = self.submit(workspace, action_id, outcome=outcome)

                self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
                self.assertEqual("ORCHESTRATION_STATE_UNREADABLE", envelope["error_code"], envelope)
                self.assertIn("claims could not be read", envelope["message"], envelope)
                # Not merely refused: the action is still owed. A blocked submission that
                # read the damage as "nothing was claimed" would pause the session here.
                session = session_document(workspace)
                self.assertEqual("active", session["status"], session)
                self.assertEqual(action_id, session["pending_action_id"], session)
                self.assertEqual("open", stored_request(workspace, request_id)["status"])
                self.assertEqual("blocked", question_fields(workspace)["status"])

    def test_a_ledger_carrying_more_claims_than_the_bound_is_refused_rather_than_projected(self):
        """An unbounded ledger is one input an acquirer alone decides the size of.

        Every other input this controller believes is bounded before it is walked -- the
        raw tree, the manifest, the attempt audit -- and the claim ledger is the only one
        the acquirer itself writes. Without the entry bound a submission hands verification
        a claim set of the acquirer's choosing, and the projection, the pairing checks and
        the commit all walk it; the refusal is what keeps the cost of a submission set by
        the order rather than by the attempt.

        The bound is what is under test, not the number: `MAX_SCOPE_GUARD_ENTRIES` is read
        from the controller rather than spelled out, so raising the limit moves the fixture
        with it instead of quietly making this pass for the wrong reason.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, action_id = self.pending_acquisition(Path(tmpdir))
            source_id = self.deliver_for(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            # Written straight to the file rather than through `record_fulfilment_claim`:
            # the ledger is the acquirer's to write, and filing one claim at a time is a
            # rule of the CLI, not of the document the controller reads.
            path = workspace / "runs" / "order-claims" / ORCHESTRATION_ID / f"{action_id}.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            padding = {
                f"req-padding-{index}": {
                    "request_id": f"req-padding-{index}",
                    "source_id": source_id,
                    "claimed_at": "2026-01-01T00:00:00Z",
                }
                for index in range(CONTROLLER.MAX_SCOPE_GUARD_ENTRIES)
            }
            document["fulfilments"].update(padding)
            path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
            claimed = len(document["fulfilments"]) + len(document["reopens"])

            code, envelope = self.submit(workspace, action_id, artifacts=[f"raw/data/{PAYLOAD.name}"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope["message"])
            self.assertEqual("ORCHESTRATION_SCOPE_EXCEEDED", envelope["error_code"], envelope["message"])
            self.assertEqual(
                f"delegated acquisition claimed {claimed} bookkeeping entries, "
                f"over the {CONTROLLER.MAX_SCOPE_GUARD_ENTRIES} limit",
                envelope["message"],
            )
            # Not recoverable: an over-large ledger is not something a retry gets past.
            self.assertFalse(envelope["recoverable"], envelope["message"])

            record = stored_request(workspace, request_id)
            self.assertEqual("open", record["status"], msg=f"nothing may be committed: {record}")
            self.assertIsNone(record["source_id"], record)
            self.assertEqual("blocked", question_fields(workspace)["status"], "the page must not move")

    def test_a_ledger_entry_the_projection_could_not_read_is_refused_rather_than_crashing(self):
        """Without the entry-shape check a malformed claim takes verification down with it.

        The load-time section check only asks that `fulfilments` and `reopens` are mappings,
        so whatever sits *inside* them reaches the projection unread. A `source_ids` of
        `null` then makes the projection iterate `None` and a bare `TypeError` leaves the
        controller -- no envelope, no error code, no remediation, and no verdict the acquirer
        can act on -- where every other unreadable-ledger path fails closed. The same hole
        hands an entry that is not an object at all, and one carrying no `source_id`, to code
        that assumes both. The guard's four refusals are pinned together because dropping any
        one of them reopens that path for the shape it was the only one watching.
        """

        def null_source_ids(document, request_id):
            document["reopens"][QUESTION_SLUG]["source_ids"] = None

        def no_source_id(document, request_id):
            document["fulfilments"][request_id].pop("source_id")

        def entry_is_not_an_object(document, request_id):
            document["fulfilments"][request_id] = "fulfilled, honest"

        def empty_key(document, request_id):
            # A JSON object's keys are always strings, so an empty one is the only key this
            # guard's key check is still reachable by from a file the acquirer wrote.
            document["reopens"][""] = document["reopens"].pop(QUESTION_SLUG)

        for damage, expected in (
            (null_source_ids, "reopens entry '{slug}' has a non-string-list source_ids"),
            (no_source_id, "fulfilments entry '{request_id}' has no source_id string"),
            (entry_is_not_an_object, "has a non-object fulfilments entry '{request_id}'"),
            (empty_key, "has a non-string reopens key"),
        ):
            with self.subTest(damage=damage.__name__), tempfile.TemporaryDirectory() as tmpdir:
                workspace, request_id, action_id = self.pending_acquisition(Path(tmpdir))
                source_id = self.deliver_for(workspace, request_id)
                self.fulfil_and_reopen(workspace, request_id, source_id)

                # Edited on disk rather than filed through `record_*_claim`: the ledger is a
                # file the acquirer owns, and nothing between it and the controller re-reads
                # what the acquirer put there.
                ledger = workspace / "runs" / "order-claims" / ORCHESTRATION_ID / f"{action_id}.json"
                document = json.loads(ledger.read_text(encoding="utf-8"))
                damage(document, request_id)
                ledger.write_text(json.dumps(document), encoding="utf-8")

                code, envelope = self.submit(workspace, action_id, artifacts=[f"raw/data/{PAYLOAD.name}"])

                self.assertNotEqual(0, code, envelope)
                self.assertEqual("ORCHESTRATION_STATE_UNREADABLE", envelope["error_code"], envelope)
                self.assertIn(
                    expected.format(slug=QUESTION_SLUG, request_id=request_id),
                    envelope["message"],
                    envelope,
                )
                self.assertEqual("blocked", question_fields(workspace)["status"], "the page must not move")
                self.assertEqual(
                    "open", stored_request(workspace, request_id)["status"], "the store must not move"
                )

    def test_a_claim_filed_after_verification_began_refuses_rather_than_being_dropped(self):
        """A late claim must not be silently left uncommitted by an accepted submission."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, action_id = self.pending_acquisition(Path(tmpdir))
            source_id = self.deliver_for(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            original = CONTROLLER.commit_delegated_bookkeeping

            def file_a_late_claim(*args, **kwargs):
                CLAIMS.record_fulfilment_claim(
                    workspace, ORCHESTRATION_ID, action_id,
                    request_id="req-filed-late", source_id=source_id,
                    claimed_at="2026-01-01T00:00:00Z",
                )
                return original(*args, **kwargs)

            with mock.patch.object(CONTROLLER, "commit_delegated_bookkeeping", file_a_late_claim):
                code, envelope = self.submit(workspace, action_id, artifacts=[f"raw/data/{PAYLOAD.name}"])

            self.assertNotEqual(0, code, envelope)
            self.assertIn("changed while the submission was being verified", envelope["message"], envelope)


if __name__ == "__main__":
    unittest.main()
