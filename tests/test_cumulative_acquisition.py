"""Acquisition keeps full question dependencies and exact replayed page bytes."""

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from tests import test_contingent_bookkeeping_baseline as bookkeeping
from tests.test_delegated_acquisition_e2e import (
    ACQUIRER,
    CLAIM,
    CONTROLLER,
    INVENTORY,
    NORMALIZE,
    ORCHESTRATION_ID,
    QUESTION_SLUG,
    REQUESTS,
    RESOLVE,
    DelegatedWorkspace,
)


class CumulativeAcquisitionTests(DelegatedWorkspace, unittest.TestCase):
    enable_providers = bookkeeping.ProviderAcquisitionBookkeepingTests.enable_providers
    discover = bookkeeping.ProviderAcquisitionBookkeepingTests.discover

    def workspace_with_blockers(self, root, delegated):
        workspace = self.init_workspace(root)
        self.configure(workspace, delegated=delegated)
        if not delegated:
            self.enable_providers(workspace)
        self.run_script(CLAIM, ["claim", "--slug", QUESTION_SLUG, "--agent-id", ACQUIRER], workspace)
        requests = []
        for query in ("Conductivity observations", "Stability observations"):
            result = self.run_script(REQUESTS, [
                "add", "--kind", "other", "--query-or-identifier", query,
                "--rationale", "Both observations are required.", "--priority", "high",
                "--question-slug", QUESTION_SLUG,
            ], workspace)
            requests.append(result["request"]["request_id"])
        requests = [record["request_id"] for record in CONTROLLER.open_requests(
            workspace, yaml.safe_load((workspace / "research.yml").read_text())
        )]
        self.run_script(RESOLVE, [
            "block", "--slug", QUESTION_SLUG, "--agent-id", ACQUIRER,
            "--blocked-reason", "Both observations are required.",
            "--request-id", requests[0], "--request-id", requests[1],
        ], workspace)
        return workspace, requests

    def select(self, workspace, request_id, number):
        self.CANDIDATE_ID = f"cand-observation-{number}"
        bookkeeping.ProviderAcquisitionBookkeepingTests.select_candidate(self, workspace, request_id)
        path = workspace / "sources/discovery/candidates.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        for record in records:
            if record["candidate_id"] == self.CANDIDATE_ID:
                record["paper"]["provider_ids"]["arxiv"] = f"2601.1234{number}v2"
        path.write_text("".join(json.dumps(record) + "\n" for record in records))

    def deliver(self, workspace, request_id, number, order, delegated):
        relative = f"raw/papers/observation-{number}.html"
        path = workspace / relative
        path.write_text(f"<html><title>Observation {number}</title><body>"
                        f"The measured conductivity was {number} mS/cm.</body></html>\n")
        provenance = {
            "origin_url": f"https://arxiv.org/abs/2601.1234{number}v2",
            "retrieved_at": "2026-07-20T00:00:00Z", "retrieved_by": ACQUIRER,
            "license": "CC-BY-4.0", "terms_url": "https://info.arxiv.org/help/license/index.html",
            "request_id": request_id, "academic_provider": "arxiv",
            "academic_source_type": "preprint", "arxiv_id": f"2601.1234{number}v2",
            "title": f"Observation {number}", "authors": ["Ada Example"],
            "published": "2026-01-10T00:00:00Z",
            "checksum": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
        }
        if not delegated:
            provenance["candidate_id"] = self.CANDIDATE_ID
        (workspace / (relative + ".provenance.yml")).write_text(yaml.safe_dump(provenance))
        self.run_script(INVENTORY, ["--report"], workspace)
        source_id = self.source_id_for(workspace, relative)
        self.run_script(NORMALIZE, ["--source-id", source_id], workspace)
        self.run_script(REQUESTS, ["fulfill", "--request-id", request_id, "--source-id", source_id], workspace)
        if not delegated:
            self.discover(workspace, [
                "candidates", "transition", "--candidate-id", self.CANDIDATE_ID,
                "--expected-state", "selected", "--to-state", "fetched", "--source-id", source_id,
                "--reason", "Delivered and normalized.", "--actor", ACQUIRER, "--run-id", order["run_id"],
            ])
        return source_id, relative

    def reopen(self, workspace, request_id, source_id):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = RESOLVE.main([
                "--project-root", str(workspace),
                "reopen", "--slug", QUESTION_SLUG, "--agent-id", ACQUIRER,
                "--request-id", request_id, "--source-id", source_id,
                "--format", "json",
            ])
        return int(code or 0), json.loads(stdout.getvalue() or stderr.getvalue())

    def test_two_orders_retain_partial_progress_and_reopen_with_every_source(self):
        for delegated in (True, False):
            with self.subTest(delegated=delegated), tempfile.TemporaryDirectory() as directory:
                workspace, requests = self.workspace_with_blockers(Path(directory), delegated)
                if not delegated:
                    self.select(workspace, requests[0], 1)
                self.start(workspace)
                first = self.pending_order(workspace)
                source_one, artifact_one = self.deliver(workspace, requests[0], 1, first, delegated)
                if delegated:
                    self.record_failure(workspace, requests[1], "provider_throttled", first["action_id"])
                code, result = self.submit(workspace, first["action_id"], artifacts=[artifact_one])
                self.assertEqual(0, code, result)
                fields = bookkeeping.question_fields(workspace)
                self.assertEqual("blocked", fields["status"])
                self.assertEqual(requests, fields["blocking_request_ids"])
                self.assertEqual("fulfilled", bookkeeping.stored_request(workspace, requests[0])["status"])
                if not delegated:
                    self.select(workspace, requests[1], 2)
                second = self.pending_order(workspace)
                self.assertEqual([requests[1]], second["scope"]["request_ids"])
                source_two, artifact_two = self.deliver(workspace, requests[1], 2, second, delegated)
                code, result = self.reopen(workspace, requests[1], source_two)
                self.assertEqual(0, code, result)
                code, result = self.submit(workspace, second["action_id"], artifacts=[artifact_two])
                self.assertEqual(0, code, result)
                fields = bookkeeping.question_fields(workspace)
                self.assertEqual("open", fields["status"])
                self.assertNotIn("blocking_request_ids", fields)
                self.assertEqual({source_one, source_two}, set(fields["source_ids"]))

    def test_premature_reopen_does_not_modify_the_page_or_claims(self):
        for delegated in (True, False):
            with self.subTest(delegated=delegated), tempfile.TemporaryDirectory() as directory:
                workspace, requests = self.workspace_with_blockers(Path(directory), delegated)
                if not delegated:
                    self.select(workspace, requests[0], 1)
                self.start(workspace)
                order = self.pending_order(workspace)
                source, _ = self.deliver(workspace, requests[0], 1, order, delegated)
                before_claims = bookkeeping.claim_ledger(workspace, order["action_id"])
                before_page = self.question_frontmatter(workspace, QUESTION_SLUG)
                code, result = self.reopen(workspace, requests[0], source)
                self.assertEqual(2, code, result)
                self.assertEqual("QUESTION_BLOCKERS_UNFULFILLED", result["error_code"])
                self.assertEqual(before_claims, bookkeeping.claim_ledger(workspace, order["action_id"]))
                self.assertEqual(before_page, self.question_frontmatter(workspace, QUESTION_SLUG))

    def test_replayed_commit_rejects_unrelated_page_edits(self):
        for delegated in (True, False):
            for edit in ("body", "frontmatter", "answer_page"):
                with self.subTest(delegated=delegated, edit=edit), tempfile.TemporaryDirectory() as directory:
                    workspace = self.init_workspace(Path(directory))
                    self.configure(workspace, delegated=delegated)
                    if not delegated:
                        self.enable_providers(workspace)
                    request_id = self.block_question_on_a_request(workspace)
                    if not delegated:
                        self.select(workspace, request_id, 1)
                    self.start(workspace)
                    order = self.pending_order(workspace)
                    source, artifact = self.deliver(workspace, request_id, 1, order, delegated)
                    code, result = self.reopen(workspace, request_id, source)
                    self.assertEqual(0, code, result)
                    session_path = workspace / "runs/orchestrations" / ORCHESTRATION_ID / "session.json"
                    before = session_path.read_bytes()
                    code, result = self.submit(workspace, order["action_id"], artifacts=[artifact])
                    self.assertEqual(0, code, result)
                    session_path.write_bytes(before)
                    page = workspace / "wiki/questions" / f"{QUESTION_SLUG}.md"
                    text = page.read_text()
                    if edit == "body":
                        text += "\nUnapproved replacement evidence.\n"
                    else:
                        field = "unrelated: changed" if edit == "frontmatter" else "answer_page: wiki/synthesis/forged.md"
                        text = text.replace("---\n", f"---\n{field}\n", 1)
                    page.write_text(text)
                    code, result = self.submit(workspace, order["action_id"], artifacts=[artifact])
                    self.assertNotEqual(0, code, result)
                    self.assertIn("question_scope_violations", result["details"])
                    self.assertEqual(text, page.read_text())

    def test_changed_or_missing_fulfilled_sibling_is_refused_by_command_and_controller(self):
        for delegated in (True, False):
            for mutation in ("changed", "missing"):
                with self.subTest(delegated=delegated, mutation=mutation), tempfile.TemporaryDirectory() as directory:
                    workspace, requests = self.workspace_with_blockers(Path(directory), delegated)
                    if not delegated:
                        self.select(workspace, requests[0], 1)
                    self.start(workspace)
                    first = self.pending_order(workspace)
                    source_one, artifact_one = self.deliver(workspace, requests[0], 1, first, delegated)
                    if delegated:
                        self.record_failure(workspace, requests[1], "provider_throttled", first["action_id"])
                    code, result = self.submit(workspace, first["action_id"], artifacts=[artifact_one])
                    self.assertEqual(0, code, result)
                    if not delegated:
                        self.select(workspace, requests[1], 2)
                    second = self.pending_order(workspace)
                    source_two, artifact_two = self.deliver(workspace, requests[1], 2, second, delegated)
                    request_path = workspace / "sources/source-requests.jsonl"
                    records = [json.loads(line) for line in request_path.read_text().splitlines()]
                    for record in records:
                        if record["request_id"] == requests[0]:
                            record["rationale"] = "Replaced outside the pending order."
                    if mutation == "missing":
                        records = [record for record in records if record["request_id"] != requests[0]]
                    request_path.write_text("".join(json.dumps(record) + "\n" for record in records))
                    before_claims = bookkeeping.claim_ledger(workspace, second["action_id"])
                    before_page = self.question_frontmatter(workspace, QUESTION_SLUG)
                    code, result = self.reopen(workspace, requests[1], source_two)
                    self.assertEqual(2, code, result)
                    self.assertEqual("QUESTION_BLOCKERS_UNFULFILLED", result["error_code"])
                    self.assertEqual([requests[0]], result["details"]["untrusted_request_ids"])
                    self.assertEqual(before_claims, bookkeeping.claim_ledger(workspace, second["action_id"]))
                    bookkeeping.CLAIMS.record_reopen_claim(
                        workspace, ORCHESTRATION_ID, second["action_id"], question_slug=QUESTION_SLUG,
                        source_ids=[source_one, source_two], request_ids=[requests[1]],
                        claimed_at="2026-09-10T00:00:00Z",
                    )
                    before_requests = request_path.read_bytes()
                    code, result = self.submit(workspace, second["action_id"], artifacts=[artifact_two])
                    self.assertNotEqual(0, code, result)
                    refusal = (
                        "workspace health or HIGH validation findings changed"
                        if mutation == "missing" else "sibling source requests"
                    )
                    self.assertIn(refusal, result["message"])
                    self.assertEqual(before_page, self.question_frontmatter(workspace, QUESTION_SLUG))
                    self.assertEqual(before_requests, request_path.read_bytes())

    def test_incomplete_or_inconsistent_saved_baseline_cannot_be_replayed(self):
        for delegated in (True, False):
            with self.subTest(delegated=delegated), tempfile.TemporaryDirectory() as directory:
                workspace, requests = self.workspace_with_blockers(Path(directory), delegated)
                if not delegated:
                    self.select(workspace, requests[0], 1)
                self.start(workspace)
                order = CONTROLLER.hydrate_integrity_baselines(workspace, self.pending_order(workspace))
                CONTROLLER.require_acquisition_evidence_baselines(order)
                before_page = self.question_frontmatter(workspace, QUESTION_SLUG)
                for mutation in ("missing_blockers", "missing_page", "body", "links", "scope"):
                    with self.subTest(mutation=mutation):
                        changed = copy.deepcopy(order)
                        guards = {item["check"]: item for item in changed["required_postconditions"]}
                        baseline = guards["linked_blocked_questions_reopened"]["blocked_questions_before"][QUESTION_SLUG]
                        if mutation == "missing_blockers":
                            baseline.pop("page_blocking_request_ids")
                        elif mutation == "missing_page":
                            baseline.pop("page_before")
                        elif mutation == "body":
                            baseline["page_before"] += "\nChanged baseline text.\n"
                        elif mutation == "links":
                            baseline["page_blocking_request_ids"] = ["unissued-request"]
                        else:
                            changed["scope"]["question_slugs"] = []
                        with self.assertRaises(CONTROLLER.OrchestrationControllerError) as raised:
                            CONTROLLER.require_acquisition_evidence_baselines(changed)
                        self.assertEqual("ORCHESTRATION_ACQUISITION_BASELINE_UNAVAILABLE", raised.exception.error_code)
                        self.assertFalse(raised.exception.recoverable)
                self.assertEqual(before_page, self.question_frontmatter(workspace, QUESTION_SLUG))
