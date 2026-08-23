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
    """The provider arm, whose request-scope guard treats every scoped record as mutable.

    Separate from the delegated cases above because it is a different verification arm with a
    different scope rule, reached by a workspace that declares acquisition providers instead of
    an acquirer agent. The delegated arm makes only the *fulfilled* scoped requests mutable;
    this one makes all of them mutable, whatever became of them.
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

    def test_a_scoped_requests_record_stays_rewritten_after_the_order_is_accepted(self):
        """Measures how much of a scoped request record an order may rewrite: all of it.

        The provider arm's request-scope guard admits every request id the order names, so a
        record's own fields -- including the rationale that says why the evidence was wanted --
        can be edited mid-order and the submission is still accepted. The rewrite here is the
        retrospective kind: the reason is restated to match what turned up. Nothing in the
        acceptance path compares it against what the order was issued for, and the rewritten
        text is what the store holds once the action is over. Pinned as a measurement of a
        defect; only the durability of the rewrite is measured here.
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

            self.assertEqual(
                0,
                code,
                msg=f"the rewritten record did not cost the order its acceptance: {accepted}",
            )
            self.assertEqual(order["action_id"], accepted["last_completed_action_id"], accepted)
            self.assertEqual("research", accepted["phase"], accepted)

            record = stored_request(workspace, request_id)
            self.assertEqual(
                self.REWRITTEN_RATIONALE,
                record["rationale"],
                msg=f"the mid-order rewrite is what the store holds afterwards: {record}",
            )
            self.assertEqual("fulfilled", record["status"], record)
            self.assertEqual(source_id, record["source_id"], record)
            self.assertEqual(1, len(stored_requests(workspace)), stored_requests(workspace))



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
            self.assertEqual(
                first["request"]["updated_at"],
                second["request"]["updated_at"],
                msg="a replay must report the claim it found, not a fresh stamp",
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
