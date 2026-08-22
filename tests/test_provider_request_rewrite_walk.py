"""What a provider-arm acquirer can do to a scoped request's own ``scope`` mid-order.

``source_requests.py fulfill --require-scope`` compares two declarations: the ``scope``
a request recorded when it was issued, and the ``scope`` the delivered source stamps in
its provenance sidecar. The controller's provider (non-delegated) acquisition arm then
guards the request store against changes outside the order, but it exempts the order's
whole scoped request set from that guard, and it never re-reads a request's ``scope``
after issuance. Nothing therefore compares the ``scope`` a request declared when the
work order was cut against the ``scope`` it declares when the order is submitted.

These cases walk that end to end on the real harness -- research, discovery, candidate
review and acquisition all driven through ``controller next`` / ``controller submit``
against a temporary workspace -- and pin what was actually observed at each step:

* a delivery that contradicts the request's ``scope`` as issued is refused, and the
  order cannot be submitted, so the check is real and the honest route is closed;
* rewriting that request's ``scope`` mid-order to the value the delivery already
  carries makes the same fulfilment succeed, and the submission is admitted;
* deleting the ``scope`` key outright leaves ``--require-scope`` nothing to require, and
  an unstamped delivery is admitted too;
* appending a record outside the order's request scope is still refused, so the store
  guard is live and its exemption really is bounded to the scoped ids;
* the narrower ``mutable_ids`` the delegated arm uses would not have refused the
  rewrite either, measured on this walk's own before/after fingerprints.

The two admissions are recorded on purpose. They are measurements of what the shipped
code does today, not an endorsement of it: naming them ``still_admitted`` is how this
suite keeps a known hole visible and makes closing it a deliberate, reviewable change
rather than an accident. Nothing here proposes or applies a fix.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import tests.test_orchestration_controller as _toc
from tests._script_loader import load_script

ACADEMIC_DOI = _toc.ACADEMIC_DOI
ARXIV_PAYLOAD = _toc.ARXIV_PAYLOAD
CONTROLLER = _toc.CONTROLLER
DISCOVER = _toc.DISCOVER
INVENTORY = _toc.INVENTORY
NORMALIZE = _toc.NORMALIZE
RESOLVE = _toc.RESOLVE
SCRIPTS = _toc.SCRIPTS
SOURCE_REQUESTS = _toc.SOURCE_REQUESTS
openalex_payload = _toc.openalex_payload

QUESTION_CLAIM = load_script("provider_rewrite_walk_question_claim", "question_claim.py")

QUESTION_SLUG = "test-question"
REQUESTS_RELATIVE = "sources/source-requests.jsonl"

#: What the request declares when the work order is cut.
SCOPE_AS_ISSUED = {"facet_id": "supplier_quote"}
#: What the acquirer's delivery stamps instead -- a different value for the same key,
#: so the pairing is a contradiction rather than a silence the default mode tolerates.
SCOPE_AS_DELIVERED = {"facet_id": "benchmark_table"}

BUNDLE_RELATIVE = "raw/papers/arxiv-2601.12345v2"
BUNDLE_README = (
    json.dumps({"process": {"compiler": "pdflatex"}, "texlive_version": "synthetic-2026"}, indent=2) + "\n"
)
BUNDLE_MAIN_TEX = (
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

EMPTY_VIOLATIONS = {"removed": [], "added_outside_scope": [], "changed_outside_scope": []}


class ProviderArmRequestScopeRewrite(unittest.TestCase):
    """Provider-arm acquisition orders whose scoped request is edited mid-order."""

    maxDiff = None

    # Borrowed through the module object so the parent TestCase is never bound in this
    # module's namespace: binding it would make pytest re-collect its whole suite here.
    run_module = _toc.OrchestrationControllerTests.run_module
    init_workspace = _toc.OrchestrationControllerTests.init_workspace
    controller = _toc.OrchestrationControllerTests.controller
    json_script = _toc.OrchestrationControllerTests.json_script
    assert_json_script_ok = _toc.OrchestrationControllerTests.assert_json_script_ok
    enable_academic_providers = _toc.OrchestrationControllerTests.enable_academic_providers
    manifest_records = _toc.OrchestrationControllerTests.manifest_records
    start = _toc.OrchestrationControllerTests.start
    submit = _toc.OrchestrationControllerTests.submit

    # -- durable store helpers -------------------------------------------------------

    def request_records(self, target: Path) -> list[dict]:
        text = (target / REQUESTS_RELATIVE).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def request_record(self, target: Path, request_id: str) -> dict:
        matching = [item for item in self.request_records(target) if item.get("request_id") == request_id]
        self.assertEqual(1, len(matching), matching)
        return matching[0]

    def write_request_records(self, target: Path, records: list[dict]) -> None:
        (target / REQUESTS_RELATIVE).write_text(
            "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records),
            encoding="utf-8",
            newline="\n",
        )

    def set_request_scope(self, target: Path, request_id: str, scope: dict | None) -> None:
        """Rewrite one request's declared scope in place, the way an acquirer would."""
        records = self.request_records(target)
        for record in records:
            if record.get("request_id") != request_id:
                continue
            if scope is None:
                record.pop("scope", None)
            else:
                record["scope"] = dict(scope)
        self.write_request_records(target, records)

    def request_store_fingerprints(self, target: Path) -> dict[str, str]:
        return CONTROLLER.record_fingerprint_snapshot(
            self.request_records(target),
            id_field="request_id",
            label="source-request store",
        )

    def issued_request_fingerprints(self, target: Path, order: dict) -> dict[str, str]:
        """The request-store baseline the controller persisted when it cut this order."""
        hydrated = CONTROLLER.hydrate_integrity_baselines(target, order)
        guards = [
            item
            for item in hydrated.get("required_postconditions", [])
            if isinstance(item, dict) and item.get("check") == "manifest_records_increased"
        ]
        self.assertEqual(1, len(guards), hydrated)
        return guards[0]["source_request_record_fingerprints_before"]

    # -- delivery --------------------------------------------------------------------

    def deliver_bundle(self, target: Path, *, request_id: str, candidate_id: str, scope: dict | None) -> None:
        """Deliver the acquired bundle, stamping its provenance scope when asked."""
        bundle = target / BUNDLE_RELATIVE
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "00README.json").write_text(BUNDLE_README, encoding="utf-8")
        (bundle / "main.tex").write_text(BUNDLE_MAIN_TEX, encoding="utf-8")
        sidecar: dict[str, object] = {
            "origin_url": "https://arxiv.org/e-print/2601.12345v2",
            "retrieved_at": "2026-07-20T00:00:00Z",
            "retrieved_by": "fetch_sources.py/arxiv",
            "license": "CC-BY-4.0",
            "terms_url": "https://info.arxiv.org/help/license/index.html",
            "terms_note": "Mocked offline acquisition uses explicit arXiv provenance.",
            "notes": "Network-free arXiv source fixture: a directory bundle.",
            "request_id": request_id,
            "candidate_id": candidate_id,
            "academic_provider": "arxiv",
            "academic_source_type": "preprint",
            "arxiv_id": "2601.12345v2",
            "doi": ACADEMIC_DOI,
            "title": "Solid Electrolyte Conductivity Survey",
            "authors": ["Ada Example"],
            "published": "2026-01-10T00:00:00Z",
        }
        if scope is not None:
            sidecar["scope"] = dict(scope)
        (target / f"{BUNDLE_RELATIVE}.provenance.yml").write_text(
            yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
        )

    def inventory_and_normalize(self, target: Path) -> str:
        self.assert_json_script_ok(INVENTORY, ["--project-root", str(target), "--report", "--format", "json"])
        records = [
            record for record in self.manifest_records(target) if record.get("raw_paths") == [BUNDLE_RELATIVE]
        ]
        self.assertEqual(1, len(records), records)
        source_id = records[0]["id"]
        self.assert_json_script_ok(
            NORMALIZE, ["--project-root", str(target), "--source-id", source_id, "--format", "json"]
        )
        return source_id

    def source_provenance_scope(self, target: Path, source_id: str) -> dict:
        records = [record for record in self.manifest_records(target) if record.get("id") == source_id]
        self.assertEqual(1, len(records), records)
        return records[0].get("provenance", {}).get("scope", {})

    def fulfill(self, target: Path, request_id: str, source_id: str) -> tuple[int, dict]:
        code, payload, _ = self.json_script(
            SOURCE_REQUESTS,
            [
                "--project-root", str(target), "fulfill",
                "--request-id", request_id,
                "--source-id", source_id,
                "--require-scope",
                "--format", "json",
            ],
        )
        return code, payload

    def finish_acquisition_paperwork(
        self, target: Path, *, request_id: str, candidate_id: str, source_id: str, order: dict
    ) -> None:
        """Everything an acquisition order needs besides the fulfilment itself."""
        self.assert_json_script_ok(
            DISCOVER,
            ["--project-root", str(target), "--format", "json", "candidates", "transition",
             "--candidate-id", candidate_id, "--expected-state", "selected", "--to-state", "fetched",
             "--reason", "Bundle inventoried and normalized.", "--source-id", source_id,
             "--actor", "acquire-agent", "--run-id", order["run_id"]],
        )
        self.assert_json_script_ok(
            RESOLVE,
            ["--project-root", str(target), "reopen", "--slug", QUESTION_SLUG,
             "--agent-id", "acquire-agent", "--source-id", source_id,
             "--request-id", request_id, "--format", "json"],
        )

    def submit_acquisition(self, root: Path, target: Path, order: dict) -> tuple[int, dict]:
        code, payload, _ = self.submit(
            root,
            target,
            order["action_id"],
            summary="Delivered an arXiv bundle directory, inventoried, normalized, fulfilled.",
            artifacts=[BUNDLE_RELATIVE, f"{BUNDLE_RELATIVE}.provenance.yml", "sources/manifest.jsonl"],
        )
        return code, payload

    # -- the walk to a genuine provider-arm order -------------------------------------

    def walk_to_acquisition(self, root: Path, *, scope: dict) -> tuple[Path, str, str, dict]:
        """Drive research, discovery and candidate review until acquisition is issued.

        The request is created with ``--scope`` during research, so the acquisition
        order is cut against a request that already declares what would satisfy it.
        """
        target = self.init_workspace(root, question=True)
        self.enable_academic_providers(target)
        self.start(target)
        _, research_order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
        self.assertEqual("research", research_order["phase"], research_order)

        scope_args: list[str] = []
        for key, value in scope.items():
            scope_args.extend(["--scope", f"{key}={value}"])
        request_report = self.assert_json_script_ok(
            SOURCE_REQUESTS,
            [
                "--project-root", str(target), "add",
                "--kind", "paper",
                "--query-or-identifier",
                "solid electrolyte room-temperature ionic conductivity above 1 mS/cm",
                "--rationale", "The open question needs primary academic evidence.",
                "--priority", "high",
                "--question-slug", QUESTION_SLUG,
                *scope_args,
                "--format", "json",
            ],
        )
        request_id = request_report["request"]["request_id"]
        self.assertEqual(scope, request_report["request"].get("scope"), request_report)
        self.assert_json_script_ok(
            QUESTION_CLAIM,
            ["--project-root", str(target), "claim", "--slug", QUESTION_SLUG,
             "--agent-id", "answer-agent", "--format", "json"],
        )
        self.assert_json_script_ok(
            RESOLVE,
            ["--project-root", str(target), "block", "--slug", QUESTION_SLUG,
             "--agent-id", "answer-agent",
             "--blocked-reason", "No delivered academic evidence is available yet.",
             "--request-id", request_id, "--format", "json"],
        )
        code, envelope, _ = self.submit(
            root, target, research_order["action_id"],
            summary="Recorded the evidence gap and blocked the question on a scoped source request.",
            artifacts=[REQUESTS_RELATIVE, f"wiki/questions/{QUESTION_SLUG}.md"],
        )
        self.assertEqual(0, code, envelope)

        _, discovery_order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
        self.assertEqual("discovery", discovery_order["phase"], discovery_order)
        with (
            mock.patch.object(DISCOVER, "ARXIV_TRANSPORT", lambda _u, _t, _h: ARXIV_PAYLOAD),
            mock.patch.object(DISCOVER, "OPENALEX_TRANSPORT", lambda _u, _t, _h: openalex_payload()),
            mock.patch.object(DISCOVER, "ARXIV_CLOCK", lambda: 0.0),
            mock.patch.object(DISCOVER, "OPENALEX_CLOCK", lambda: 0.0),
            mock.patch.object(DISCOVER, "ARXIV_SLEEP", lambda _s: None),
            mock.patch.object(DISCOVER, "OPENALEX_SLEEP", lambda _s: None),
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
        code, envelope, _ = self.submit(
            root, target, discovery_order["action_id"],
            summary="Mocked academic providers produced one deduplicated candidate.",
            artifacts=["sources/discovery/candidates.jsonl"],
        )
        self.assertEqual(0, code, envelope)

        _, review_order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
        self.assertEqual("candidate_review", review_order["phase"], review_order)
        self.assert_json_script_ok(
            DISCOVER,
            ["--project-root", str(target), "--format", "json", "candidates", "select",
             "--candidate-id", candidate_id, "--request-id", request_id,
             "--reason", "Selected the academic-primary arXiv route.",
             "--actor", "review-agent", "--run-id", review_order["run_id"]],
        )
        code, envelope, _ = self.submit(
            root, target, review_order["action_id"],
            summary="Reviewed and selected the routable academic candidate.",
            artifacts=["sources/discovery/candidates.jsonl"],
        )
        self.assertEqual(0, code, envelope)

        _, order, _ = self.controller(target, "next", "--orchestration-id", "orch-test")
        self.assertEqual("acquisition", order["phase"], order)
        self.assertNotEqual(
            CONTROLLER.ACQUISITION_MODE_DELEGATED,
            order.get("acquisition_mode"),
            "every case here must run the provider (non-delegated) arm",
        )
        self.assertEqual([request_id], order["scope"]["request_ids"], order)
        return target, request_id, candidate_id, order

    # -- what holds ------------------------------------------------------------------

    def test_a_delivery_contradicting_the_request_scope_as_issued_is_still_refused(self):
        """The control: without any edit, the scope check closes the order's only route.

        The request declares one facet, the delivery stamps another for the same key, so
        the pairing is a contradiction. ``fulfill`` refuses, the request stays open, and
        the acquisition order cannot be submitted at all.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root, scope=SCOPE_AS_ISSUED)
            self.deliver_bundle(
                target, request_id=request_id, candidate_id=candidate_id, scope=SCOPE_AS_DELIVERED
            )
            source_id = self.inventory_and_normalize(target)
            self.assertEqual(SCOPE_AS_DELIVERED, self.source_provenance_scope(target, source_id))

            code, envelope = self.fulfill(target, request_id, source_id)
            self.assertEqual(2, code, envelope)
            self.assertEqual("REQUEST_SCOPE_MISMATCH", envelope["error_code"], envelope)
            self.assertEqual(
                f"Request {request_id} cannot be fulfilled by source {source_id}: they disagree about "
                f"scope facet_id (request facet_id=supplier_quote; source facet_id=benchmark_table).",
                envelope["message"],
                envelope,
            )
            self.assertEqual(
                [{"key": "facet_id", "request_value": "supplier_quote", "source_value": "benchmark_table"}],
                envelope["details"]["conflicts"],
                envelope,
            )

            record = self.request_record(target, request_id)
            self.assertEqual("open", record["status"], record)
            self.assertEqual(SCOPE_AS_ISSUED, record["scope"], record)

            code, envelope = self.submit_acquisition(root, target, order)
            self.assertEqual(2, code, envelope)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"], envelope)
            self.assertEqual(
                "acquisition did not fulfill the scoped source request", envelope["message"], envelope
            )

    def test_appending_a_request_record_outside_the_order_scope_is_still_refused(self):
        """The store guard is live, and its exemption really is bounded to scoped ids.

        Same rewrite as the admitted case below, plus one extra request record the order
        never named. The guard that ignores the scoped request's own edit refuses this
        one, naming the appended id -- so what follows is an exemption, not an absence.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root, scope=SCOPE_AS_ISSUED)
            self.deliver_bundle(
                target, request_id=request_id, candidate_id=candidate_id, scope=SCOPE_AS_DELIVERED
            )
            source_id = self.inventory_and_normalize(target)
            self.set_request_scope(target, request_id, SCOPE_AS_DELIVERED)
            code, payload = self.fulfill(target, request_id, source_id)
            self.assertEqual(0, code, payload)

            records = self.request_records(target)
            records.append(
                {
                    "schema_version": "1.0",
                    "request_id": "req-appended01",
                    "kind": "paper",
                    "query_or_identifier": "a request this order never named",
                    "rationale": "Appended to the store after the order was issued.",
                    "priority": "low",
                    "status": "fulfilled",
                    "source_id": source_id,
                    "question_slugs": [],
                    "created_at": "2026-08-22T00:00:00Z",
                    "updated_at": "2026-08-22T00:00:00Z",
                }
            )
            self.write_request_records(target, records)
            self.finish_acquisition_paperwork(
                target, request_id=request_id, candidate_id=candidate_id, source_id=source_id, order=order
            )

            code, envelope = self.submit_acquisition(root, target, order)
            self.assertEqual(2, code, envelope)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"], envelope)
            self.assertEqual(
                "acquisition changed source requests outside the persisted request scope",
                envelope["message"],
                envelope,
            )
            self.assertEqual(
                {"removed": [], "added_outside_scope": ["req-appended01"], "changed_outside_scope": []},
                envelope["details"]["source_request_scope_violations"],
                envelope,
            )

    # -- what is admitted, measured and recorded on purpose ---------------------------

    def test_rewriting_the_scoped_request_scope_mid_order_is_still_admitted(self):
        """MEASURED HOLE, recorded on purpose: the rewrite defeats ``--require-scope``.

        The delivery whose scope the previous case proved incompatible is fulfilled and
        submitted successfully once the request's own ``scope`` is edited to match it.
        No guard fires -- not the scope check, not the request-store guard, not the
        manifest, question-transition or reconciliation guards -- and the falsified
        ``scope`` is what the durable store holds afterwards, so the audit records a
        request that never wanted what it originally asked for.

        This pins today's behaviour. It is a measurement, not an endorsement: closing it
        must be a deliberate change that turns this case red on purpose.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root, scope=SCOPE_AS_ISSUED)
            self.deliver_bundle(
                target, request_id=request_id, candidate_id=candidate_id, scope=SCOPE_AS_DELIVERED
            )
            source_id = self.inventory_and_normalize(target)

            self.set_request_scope(target, request_id, SCOPE_AS_DELIVERED)
            code, payload = self.fulfill(target, request_id, source_id)
            self.assertEqual(0, code, payload)
            self.assertTrue(payload["updated"], payload)
            self.assertEqual(SCOPE_AS_DELIVERED, payload["request"]["scope"], payload)

            self.finish_acquisition_paperwork(
                target, request_id=request_id, candidate_id=candidate_id, source_id=source_id, order=order
            )
            code, session = self.submit_acquisition(root, target, order)
            self.assertEqual(0, code, session)
            self.assertEqual("active", session["status"], session)
            self.assertIsNone(session["pending_action_id"], session)

            record = self.request_record(target, request_id)
            self.assertEqual("fulfilled", record["status"], record)
            self.assertEqual(source_id, record["source_id"], record)
            self.assertEqual(SCOPE_AS_DELIVERED, record["scope"], record)

    def test_deleting_the_scoped_request_scope_mid_order_is_still_admitted(self):
        """MEASURED HOLE, recorded on purpose: deletion is the cheaper defeat.

        A key the delivery never states is what ``--require-scope`` exists to refuse, and
        it does refuse the unstamped delivery while the request still declares the key.
        Removing the key from the request instead of matching it leaves the flag with
        nothing to require: the same unstamped delivery then fulfils and the submission
        is admitted, and the request is left declaring no scope at all.

        Recorded for the same reason as the case above, and worth separating from it: a
        fix that only compared declared values would still let this route through.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root, scope=SCOPE_AS_ISSUED)
            self.deliver_bundle(target, request_id=request_id, candidate_id=candidate_id, scope=None)
            source_id = self.inventory_and_normalize(target)
            self.assertEqual({}, self.source_provenance_scope(target, source_id))

            code, envelope = self.fulfill(target, request_id, source_id)
            self.assertEqual(2, code, envelope)
            self.assertEqual("REQUEST_SCOPE_MISSING", envelope["error_code"], envelope)
            self.assertEqual(
                f"Source {source_id} declares no provenance scope for facet_id; --require-scope needs the "
                f"delivery to state every scope key request {request_id} declares or --match-scope asserts.",
                envelope["message"],
                envelope,
            )
            self.assertEqual(["facet_id"], envelope["details"]["missing_keys"], envelope)

            self.set_request_scope(target, request_id, None)
            code, payload = self.fulfill(target, request_id, source_id)
            self.assertEqual(0, code, payload)
            self.assertNotIn("scope", payload["request"], payload)

            self.finish_acquisition_paperwork(
                target, request_id=request_id, candidate_id=candidate_id, source_id=source_id, order=order
            )
            code, session = self.submit_acquisition(root, target, order)
            self.assertEqual(0, code, session)
            self.assertEqual("active", session["status"], session)

            record = self.request_record(target, request_id)
            self.assertEqual("fulfilled", record["status"], record)
            self.assertNotIn("scope", record, record)

    def test_the_narrower_fulfilled_only_change_guard_would_still_admit_the_rewrite(self):
        """MEASURED: narrowing ``mutable_ids`` alone would not have refused the rewrite.

        The provider arm exempts the order's whole scoped request set from the store
        guard; the delegated arm exempts only the requests this action fulfilled. On the
        exact before/after fingerprints of the admitted rewrite, both exemptions report
        nothing, because the rewritten request *is* the fulfilled one.

        Exempting nothing does report the change -- and would also refuse an honest
        fulfilment, shown here by restoring the request's issued scope and fingerprinting
        the record again: fulfilment necessarily rewrites ``status``, ``source_id`` and
        ``updated_at``, so no whole-record exemption width separates the two. Whatever
        closes this has to compare the declared scope itself.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root, scope=SCOPE_AS_ISSUED)
            before = self.issued_request_fingerprints(target, order)
            self.assertEqual([request_id], sorted(before), before)

            self.deliver_bundle(
                target, request_id=request_id, candidate_id=candidate_id, scope=SCOPE_AS_DELIVERED
            )
            source_id = self.inventory_and_normalize(target)
            self.set_request_scope(target, request_id, SCOPE_AS_DELIVERED)
            code, payload = self.fulfill(target, request_id, source_id)
            self.assertEqual(0, code, payload)
            self.finish_acquisition_paperwork(
                target, request_id=request_id, candidate_id=candidate_id, source_id=source_id, order=order
            )
            code, session = self.submit_acquisition(root, target, order)
            self.assertEqual(0, code, session)

            after = self.request_store_fingerprints(target)
            self.assertNotEqual(before[request_id], after[request_id], (before, after))

            scoped_width = CONTROLLER.fingerprint_scope_violations(
                before, after, mutable_ids={request_id}
            )
            self.assertEqual(EMPTY_VIOLATIONS, scoped_width, (before, after))

            no_exemption = CONTROLLER.fingerprint_scope_violations(before, after, mutable_ids=set())
            self.assertEqual(
                {"removed": [], "added_outside_scope": [], "changed_outside_scope": [request_id]},
                no_exemption,
                (before, after),
            )

            honest = self.request_record(target, request_id)
            honest["scope"] = dict(SCOPE_AS_ISSUED)
            honest_after = CONTROLLER.record_fingerprint_snapshot(
                [honest], id_field="request_id", label="source-request store"
            )
            self.assertEqual(
                {"removed": [], "added_outside_scope": [], "changed_outside_scope": [request_id]},
                CONTROLLER.fingerprint_scope_violations(before, honest_after, mutable_ids=set()),
                (before, honest_after),
            )
