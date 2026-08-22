"""What an acquisition order has already made durable before the controller verifies it.

Every test in this file is a *measurement* of what the shipped code does today, not an
endorsement of it. Three of the four record behaviour that is a defect: an acquirer working
inside a pending order writes its fulfilment into `sources/source-requests.jsonl` and
rewrites the question page immediately, so a submission the controller refuses -- or one
that never reaches verification at all -- leaves that bookkeeping standing with nothing
downstream able to tell it from a verified result.

The file exists because a green suite is not evidence about this. A change that moves those
writes behind acceptance can pass every other test in the tree while quietly loosening a
guard, if the guard's subject stops moving and the guard therefore stops having anything to
watch. These tests keep the writes themselves in view: each one reads the durable bytes back
off disk and states what they say, so anything that changes when they are written has to
disagree with a named assertion rather than with silence.

Assertions are on the store and the page, never on a command's return value: the return
value is what the acquirer *claims*, and what this file is about is what survives it.
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

from tests._script_loader import load_script
from tests.test_delegated_acquisition_e2e import (
    ACQUIRER,
    CONTROLLER,
    INVENTORY,
    NORMALIZE,
    ORCHESTRATION_ID,
    PAYLOAD,
    QUESTION_SLUG,
    REQUESTS,
    DelegatedWorkspace,
)

DISCOVER = load_script("contingent_bookkeeping_discover", "discover_sources.py")

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

    def test_the_acquirer_writes_the_fulfilment_and_reopens_the_page_before_the_controller_sees_anything(self):
        """Measures when the acquirer's bookkeeping becomes durable: before any submission.

        `source_requests.py fulfill` and `question_resolve.py reopen` are ordinary commands an
        acquirer runs inside its own order, and both write through to disk the moment they are
        called. Nothing has been submitted at the point these assertions read the store and the
        page, and the order is still pending -- so the workspace already records a fulfilled
        request and a reopened question on the acquirer's word alone. Pinned as a measurement;
        the two tests below are what that costs.
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
            self.assertEqual("fulfilled", record["status"], record)
            self.assertEqual(source_id, record["source_id"], record)
            self.assertEqual(self.FULFILMENT_STAMP, record["updated_at"], record)
            self.assertEqual(
                {
                    **issued_record,
                    "status": "fulfilled",
                    "source_id": source_id,
                    "updated_at": self.FULFILMENT_STAMP,
                },
                record,
                msg=f"those three fields are the whole of what the store now says: {record}",
            )

            page = question_fields(workspace)
            self.assertEqual("open", page["status"], page)
            self.assertEqual([source_id], page["source_ids"], page)
            self.assertNotIn("blocking_request_ids", page, page)
            self.assertNotIn("blocked_reason", page, page)

    def test_a_refused_submission_leaves_the_fulfilment_standing_and_the_question_reopened(self):
        """Measures what a refusal undoes: nothing.

        The acquirer fulfils and reopens, then submits work the controller will not accept --
        here because the normalized record the fulfilment rests on is gone by the time the
        postconditions look for it. Which refusal fires is not the measurement. What the store
        and the page hold *afterwards* is, and they hold exactly what the acquirer wrote: a
        fulfilled request pointing at a source the controller has just declined to verify, and
        a question no longer blocked on it. Pinned as a measurement of a defect.
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
                "fulfilled",
                record["status"],
                msg=f"a refused submission left the fulfilment standing: {record}",
            )
            self.assertEqual(source_id, record["source_id"], record)

            page = question_fields(workspace)
            self.assertEqual(
                "open",
                page["status"],
                msg=f"a refused submission left the question reopened: {page}",
            )
            self.assertEqual([source_id], page["source_ids"], page)

            # And the action is still the pending one, so the workspace is not merely
            # un-rolled-back: it is un-rolled-back *and* still owed the same work.
            session = session_document(workspace)
            self.assertEqual(action_id, session["pending_action_id"], session)

    def test_a_failed_outcome_strands_the_fulfilled_request_where_routing_can_no_longer_see_it(self):
        """Measures the outcome that ends an action without any verification at all.

        `prepare_submission` reaches `verify_action_postconditions` for a completed outcome and
        a different check for a blocked one; `failed` takes neither branch, so no evidence check
        and no scope check runs over what the acquirer already wrote. The fulfilment therefore
        survives an action the acquirer itself declared a failure -- and because routing selects
        open requests by status, the request is now invisible to every later order. It cannot be
        re-issued, re-attempted, or seen again. Pinned as a measurement of a defect.
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
                "fulfilled",
                record["status"],
                msg=f"the failed action left its fulfilment in the store: {record}",
            )
            self.assertEqual(source_id, record["source_id"], record)

            config = CONTROLLER.load_config(workspace)
            routable = [item["request_id"] for item in CONTROLLER.open_requests(workspace, config)]
            self.assertEqual(
                [],
                routable,
                msg=f"the stranded request is invisible to routing: {stored_requests(workspace)}",
            )
            self.assertNotIn(request_id, routable, stored_requests(workspace))


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


if __name__ == "__main__":
    unittest.main()
