"""SHAPE 2 probe: one inventory-derived attribution predicate for all three raw-scope arms.

Evidence suite for the consolidated CR-18/CR-19 backlog. Under the unified predicate,
``allowed_new_raw_paths`` is "the files inventory, re-run over the delivered tree,
attributes to the admitted record set", and each NEW record's ``raw_paths`` must equal
(pinned order) what inventory derives for its id.

What each case asserts:
- CR-19 bundle deliveries (delegated, provider, blocked-partial, documented arXiv
  command) now complete;
- the CR-18 §4 hand-append smuggle now refuses, naming the mismatch;
- inventory-derived multi-raw_paths records (paper+PDF pairing, same-URL link merge)
  still pass;
- the planted-marker residue is still ADMITTED (measured honestly: an inventory
  semantics question, not a controller one);
- the EW-BUG-005 §2 control still refuses FIRST at the manifest-scope guard.
"""

import hashlib
import io
import json
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import tests.test_orchestration_controller as _toc  # noqa: E402
from tests.test_delegated_acquisition_e2e import (  # noqa: E402
    ACQUIRER,
    LATEX_BUNDLE_FIXTURE,
    PAYLOAD,
    QUESTION_SLUG,
    DelegatedWorkspace,
    synthetic_pdf,
)
from tests.test_delegated_acquisition_e2e import (
    CONTROLLER as D_CONTROLLER,
)
from tests.test_delegated_acquisition_e2e import (
    INVENTORY as D_INVENTORY,
)
from tests.test_delegated_acquisition_e2e import (
    NORMALIZE as D_NORMALIZE,
)
from tests.test_delegated_acquisition_e2e import (
    REQUESTS as D_REQUESTS,
)
from tests.test_delegated_acquisition_e2e import (
    RESOLVE as D_RESOLVE,
)

# Referenced through the module object: importing the harness TestCase into this
# namespace would make pytest re-collect the whole parent suite inside this file.
ACADEMIC_DOI = _toc.ACADEMIC_DOI
ARXIV_PAYLOAD = _toc.ARXIV_PAYLOAD
CONTROLLER = _toc.CONTROLLER
DISCOVER = _toc.DISCOVER
INVENTORY = _toc.INVENTORY
NORMALIZE = _toc.NORMALIZE
RESOLVE = _toc.RESOLVE
SCRIPTS = _toc.SCRIPTS
SOURCE_REQUESTS = _toc.SOURCE_REQUESTS
load_script_module = _toc.load_script_module
openalex_payload = _toc.openalex_payload

FETCH = load_script_module("shape2_fetch_sources", SCRIPTS / "fetch_sources.py")

MISMATCH_MESSAGE = "raw_paths do not match inventory-derived attribution"
MANIFEST_GUARD_MESSAGE = (
    "changed, removed, or added evidence-manifest records outside fulfilled source scope"
)

DISCOVERY_NAME = "supplier-search-b0abc12345.json"
DISCOVERY_BODY = (
    '{\n  "query": "B0ABC12345",\n  "matched_listing": "ds-1005006543210987",\n'
    '  "results_considered": 14\n}\n'
)

BUNDLE_NAME = "arxiv-2601.12345v2"
BUNDLE_RELATIVE = f"raw/papers/{BUNDLE_NAME}"
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


class DelegatedArmShape2(DelegatedWorkspace, unittest.TestCase):
    """Delegated-arm cases, on the shipped e2e harness."""

    maxDiff = None

    # -- fixtures -----------------------------------------------------------------

    def sidecar(self, workspace: Path, relative: str, request_id: str | None, origin: str) -> None:
        target = workspace / relative
        body: dict[str, object] = {
            "origin_url": origin,
            "license": "CC-BY-4.0",
            "retrieved_at": "2026-08-17T12:00:00Z",
            "retrieved_by": ACQUIRER,
        }
        if request_id is not None:
            body["request_id"] = request_id
        if target.is_file():
            body["checksum"] = f"sha256:{hashlib.sha256(target.read_bytes()).hexdigest()}"
        (workspace / f"{relative}.provenance.yml").write_text(
            yaml.safe_dump(body, sort_keys=False), encoding="utf-8"
        )

    def deliver_discovery(self, workspace: Path, request_id: str, *, inventory: bool = True):
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        payload = destination / DISCOVERY_NAME
        payload.write_text(DISCOVERY_BODY, encoding="utf-8", newline="\n")
        self.sidecar(
            workspace,
            f"raw/data/{DISCOVERY_NAME}",
            request_id,
            "https://supplier.test/search?q=B0ABC12345",
        )
        if inventory:
            self.run_script(D_INVENTORY, ["--report"], workspace)
            return self.source_id_for(workspace, f"raw/data/{DISCOVERY_NAME}")
        return None

    def deliver_bundle(
        self, workspace: Path, request_id: str, *, extra_files: dict[str, str] | None = None
    ) -> str:
        relative = f"raw/papers/{LATEX_BUNDLE_FIXTURE.name}"
        shutil.copytree(LATEX_BUNDLE_FIXTURE, workspace / relative)
        for name, body in (extra_files or {}).items():
            (workspace / relative / name).write_text(body, encoding="utf-8")
        self.sidecar(workspace, relative, request_id, "https://example.org/bundle")
        self.run_script(D_INVENTORY, ["--report"], workspace)
        return relative

    def complete_and_submit(
        self, workspace: Path, order: dict, request_id: str, source_id: str
    ) -> tuple[int, dict]:
        self.run_script(
            D_REQUESTS,
            ["fulfill", "--request-id", request_id, "--source-id", source_id],
            workspace,
        )
        self.run_script(D_NORMALIZE, ["--source-id", source_id], workspace)
        self.run_script(
            D_RESOLVE,
            [
                "reopen", "--slug", QUESTION_SLUG, "--agent-id", ACQUIRER,
                "--source-id", source_id, "--request-id", request_id,
            ],
            workspace,
        )
        return self.submit(workspace, order["action_id"])

    # -- (v) single-file happy path -------------------------------------------------

    def test_single_file_happy_path_still_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)
            source_id = self.deliver_for(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)
            code, envelope = self.submit(workspace, order["action_id"])
            self.assertEqual(0, code, envelope)

    # -- (v) EW-BUG-005 §2 control: manifest guard still fires FIRST -----------------

    def test_extra_unfulfilled_record_still_refused_at_manifest_guard_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)
            quote_id = self.deliver_for(workspace, request_id)
            self.deliver_discovery(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, quote_id)
            code, envelope = self.submit(workspace, order["action_id"])
            self.assertNotEqual(0, code, envelope)
            self.assertIn(MANIFEST_GUARD_MESSAGE, envelope.get("message", ""), envelope)
            self.assertNotIn(MISMATCH_MESSAGE, envelope.get("message", ""), envelope)

    # -- (ii) the CR-18 §4 hand-append smuggle must now refuse -----------------------

    def test_hand_appended_raw_path_now_refused_naming_the_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)
            quote_id = self.deliver_for(workspace, request_id)
            # Deliver the second file WITHOUT inventorying it, then hand-append its
            # path to the fulfilled record's raw_paths (one record, two raw files).
            self.deliver_discovery(workspace, request_id, inventory=False)
            manifest = workspace / "sources" / "manifest.jsonl"
            lines = []
            for line in manifest.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record.get("id") == quote_id:
                    record["raw_paths"].append(f"raw/data/{DISCOVERY_NAME}")
                lines.append(json.dumps(record))
            manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.fulfil_and_reopen(workspace, request_id, quote_id)
            code, envelope = self.submit(workspace, order["action_id"])
            self.assertNotEqual(0, code, envelope)
            self.assertIn(MISMATCH_MESSAGE, envelope.get("message", ""), envelope)
            mismatches = envelope.get("details", {}).get("raw_attribution_mismatches", {})
            self.assertIn(quote_id, mismatches, envelope)
            self.assertEqual(
                [f"raw/data/{PAYLOAD.name}", f"raw/data/{DISCOVERY_NAME}"],
                mismatches[quote_id]["declared_raw_paths"],
            )
            self.assertEqual(
                [f"raw/data/{PAYLOAD.name}"],
                mismatches[quote_id]["derived_raw_paths"],
            )
            # The two lists say they disagree; this says which declared path is the one
            # inventory accounts for nowhere, which is the path the operator must remove.
            self.assertEqual(
                [f"raw/data/{DISCOVERY_NAME}"],
                mismatches[quote_id]["declared_not_derived"],
                envelope,
            )

    # -- (i) CR-19: a directory-shaped bundle delivered in-order now completes -------

    def test_delegated_bundle_in_order_now_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)
            relative = self.deliver_bundle(workspace, request_id)
            source_id = self.source_id_for(workspace, relative)
            code, envelope = self.complete_and_submit(workspace, order, request_id, source_id)
            self.assertEqual(0, code, envelope)

    # -- (iv) planted marker: measured honestly, still ADMITTED ----------------------

    def test_planted_files_inside_the_bundle_prefix_are_still_admitted(self):
        """SHAPE 2 does NOT close the planted-marker residue.

        Inventory attributes every file under a bundle prefix to the one record with no
        member enumeration, so 'the files inventory attributes to this record' and
        'anything under the prefix' remain the same set. Closing it needs an
        inventory-level member manifest (CR-19 shape (b)), not a controller change.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)
            relative = self.deliver_bundle(
                workspace,
                request_id,
                extra_files={
                    "offer.json": json.dumps({"price": "12.50 EUR"}) + "\n",
                    "freight.json": json.dumps({"carrier": "ACME"}) + "\n",
                },
            )
            source_id = self.source_id_for(workspace, relative)
            code, envelope = self.complete_and_submit(workspace, order, request_id, source_id)
            self.assertEqual(0, code, envelope)  # asserted-and-documented accepted risk

    # -- (iii) inventory-derived multi-raw_paths records still pass ------------------

    def test_paper_pdf_pairing_inside_the_order_still_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)
            relative = f"raw/papers/{LATEX_BUNDLE_FIXTURE.name}"
            shutil.copytree(LATEX_BUNDLE_FIXTURE, workspace / relative)
            self.sidecar(workspace, relative, request_id, "https://example.org/bundle")
            pdf_rel = "raw/papers/2601.00002v1.pdf"
            (workspace / pdf_rel).write_bytes(
                synthetic_pdf(["Synthetic benchmark paper.", "The unit price is 12.50 EUR."])
            )
            self.sidecar(workspace, pdf_rel, request_id, "https://example.org/bundle.pdf")
            self.run_script(D_INVENTORY, ["--report"], workspace)
            merged = [
                record
                for line in (workspace / "sources" / "manifest.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
                for record in [json.loads(line)]
                if len(record.get("raw_paths", [])) > 1
            ]
            self.assertTrue(merged, "expected the paper+PDF pairing to merge into one record")
            source_id = merged[0]["id"]
            code, envelope = self.complete_and_submit(workspace, order, request_id, source_id)
            self.assertEqual(0, code, envelope)

    def test_same_url_link_merge_attribution_equality_holds(self):
        """The link-merge multi-raw_paths record derives byte-identically.

        Unit-level rather than e2e: fulfilling a link record inside an order is refused
        by an unrelated pre-existing quality gate ("normalized evidence has unusable
        extraction status 'stubbed'"), which fires before any raw-scope logic and is not
        changed by SHAPE 2. What SHAPE 2 must guarantee is that the derived raw_paths
        equals the manifest's for the merged record, and that both files plus sidecars
        are attributed to it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            links = workspace / "raw" / "links"
            links.mkdir(parents=True, exist_ok=True)
            url = "https://example.test/supplier/B0ABC12345"
            for name in ("offer.url", "freight.url"):
                (links / name).write_text(f"URL={url}\n", encoding="utf-8", newline="\n")
                self.sidecar(workspace, f"raw/links/{name}", request_id, url)
            self.run_script(D_INVENTORY, ["--report"], workspace)
            merged = [
                record
                for line in (workspace / "sources" / "manifest.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
                for record in [json.loads(line)]
                if len(record.get("raw_paths", [])) > 1
            ]
            self.assertTrue(merged, "expected the same-URL link merge to produce one record")
            config = yaml.safe_load((workspace / "research.yml").read_text(encoding="utf-8"))
            from tests.test_delegated_acquisition_e2e import CONTROLLER as D_CONTROLLER
            attribution = D_CONTROLLER.derived_raw_attribution(workspace, config)
            entry = attribution[merged[0]["id"]]
            self.assertEqual(merged[0]["raw_paths"], entry["raw_paths"])
            self.assertEqual(
                {
                    "raw/links/offer.url",
                    "raw/links/offer.url.provenance.yml",
                    "raw/links/freight.url",
                    "raw/links/freight.url.provenance.yml",
                },
                entry["files"],
            )


class ProviderArmShape2(unittest.TestCase):
    """Provider and blocked-partial arms, on the shipped controller harness."""

    maxDiff = None

    # Borrowed via the module object so the parent TestCase is never bound in this
    # module's namespace (pytest would re-collect its whole suite here).
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

    def write_directory_bundle(
        self,
        target: Path,
        *,
        request_id: str,
        candidate_id: str,
        extra_files: dict[str, str] | None = None,
    ) -> str:
        bundle = target / BUNDLE_RELATIVE
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "00README.json").write_text(README_JSON, encoding="utf-8")
        (bundle / "main.tex").write_text(MAIN_TEX, encoding="utf-8")
        for name, body in (extra_files or {}).items():
            (bundle / name).write_text(body, encoding="utf-8")
        sidecar = {
            "origin_url": "https://arxiv.org/e-print/2601.12345v2",
            "retrieved_at": "2026-07-20T00:00:00Z",
            "retrieved_by": "fetch_sources.py/arxiv",
            "license": "CC-BY-4.0",
            "terms_url": "https://info.arxiv.org/help/license/index.html",
            "terms_note": "Mocked offline acquisition uses explicit arXiv provenance.",
            "notes": "Network-free arXiv --format source fixture: a directory bundle.",
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
        (target / f"{BUNDLE_RELATIVE}.provenance.yml").write_text(
            yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
        )
        return BUNDLE_RELATIVE

    def walk_to_acquisition(self, root: Path) -> tuple[Path, str, str, dict]:
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
            load_script_module("shape2_question_claim", SCRIPTS / "question_claim.py"),
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
            "these cases must run the provider (non-delegated) arm",
        )
        return target, request_id, candidate_id, acquisition_order

    def fulfil_bundle(self, target: Path, request_id: str, candidate_id: str, order: dict) -> str:
        self.assert_json_script_ok(
            INVENTORY, ["--project-root", str(target), "--report", "--format", "json"]
        )
        records = self.manifest_records(target)
        bundle_record = next(r for r in records if r["raw_paths"] == [BUNDLE_RELATIVE])
        source_id = bundle_record["id"]
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
             "--candidate-id", candidate_id, "--expected-state", "selected", "--to-state", "fetched",
             "--reason", "Bundle inventoried and normalized.", "--source-id", source_id,
             "--actor", "acquire-agent", "--run-id", order["run_id"]],
        )
        self.assert_json_script_ok(
            RESOLVE,
            ["--project-root", str(target), "reopen", "--slug", "test-question",
             "--agent-id", "acquire-agent", "--source-id", source_id,
             "--request-id", request_id, "--format", "json"],
        )
        return source_id

    # -- (i) provider arm: directory bundle now completes ----------------------------

    def test_provider_arm_directory_bundle_now_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root)
            self.write_directory_bundle(target, request_id=request_id, candidate_id=candidate_id)
            self.fulfil_bundle(target, request_id, candidate_id, order)
            code, payload, stderr = self.submit(
                root, target, order["action_id"],
                summary="Delivered an arXiv bundle directory, inventoried, normalized, fulfilled.",
                artifacts=[BUNDLE_RELATIVE, f"{BUNDLE_RELATIVE}.provenance.yml",
                           "sources/manifest.jsonl"],
            )
            self.assertEqual(0, code, (payload, stderr))

    # -- (i) the documented first-party arXiv --format source flow ------------------

    def test_documented_arxiv_source_flow_inside_an_order_now_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root)

            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w:gz") as tar:
                for name, body in (("main.tex", MAIN_TEX), ("sections/intro.tex", "Intro evidence.\n")):
                    payload = body.encode("utf-8")
                    info = tarfile.TarInfo(name=name)
                    info.size = len(payload)
                    tar.addfile(info, io.BytesIO(payload))
            archive_bytes = archive.getvalue()

            def transport(url, _timeout):
                if "export.arxiv.org/api/query" in url:
                    return ARXIV_PAYLOAD
                return archive_bytes

            original = (FETCH.ARXIV_TRANSPORT, FETCH.ARXIV_CLOCK, FETCH.ARXIV_SLEEP, FETCH.ARXIV_LAST_REQUEST_AT)
            FETCH.ARXIV_TRANSPORT = transport
            FETCH.ARXIV_CLOCK = lambda: 0.0
            FETCH.ARXIV_SLEEP = lambda _seconds: None
            FETCH.ARXIV_LAST_REQUEST_AT = None
            try:
                code, download, stderr = self.json_script(
                    FETCH,
                    ["--project-root", str(target), "--format", "json",
                     "arxiv", "download", "--id", "2601.12345v2", "--format", "source",
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

            self.assert_json_script_ok(
                INVENTORY, ["--project-root", str(target), "--report", "--format", "json"]
            )
            records = self.manifest_records(target)
            self.assertEqual(1, len(records), records)
            self.assertEqual([raw_relative], records[0]["raw_paths"])
            source_id = records[0]["id"]
            self.assert_json_script_ok(
                NORMALIZE, ["--project-root", str(target), "--all", "--format", "json"]
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
                 "--source-id", source_id, "--actor", "acquire-agent", "--run-id", order["run_id"]],
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
            self.assertEqual(0, code, (payload, stderr))

    # -- (i) blocked-partial arm: bundle delivery pauses instead of refusing ---------

    def test_blocked_partial_arm_directory_bundle_now_pauses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root)
            raw_relative = self.write_directory_bundle(
                target, request_id=request_id, candidate_id=candidate_id
            )
            self.assert_json_script_ok(
                INVENTORY, ["--project-root", str(target), "--report", "--format", "json"]
            )
            records = self.manifest_records(target)
            self.assertEqual(1, len(records), records)
            code, payload, stderr = self.submit(
                root, target, order["action_id"],
                outcome="blocked",
                summary="Downloaded the bundle but could not normalize it; partial delivery.",
                artifacts=[raw_relative, f"{raw_relative}.provenance.yml", "sources/manifest.jsonl"],
            )
            self.assertEqual(CONTROLLER.EXIT_PAUSED, code, (payload, stderr))
            self.assertEqual("paused", payload["phase"])

    # -- (v) provider control: stray file outside the prefix still refused -----------

    def test_stray_file_outside_the_bundle_prefix_still_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root)
            self.write_directory_bundle(target, request_id=request_id, candidate_id=candidate_id)
            stray = target / "raw" / "papers" / "stray-unrelated.txt"
            stray.write_text("an extra delivery\n", encoding="utf-8")
            self.fulfil_bundle(target, request_id, candidate_id, order)
            code, payload, _ = self.submit(
                root, target, order["action_id"], summary="Delivered a bundle plus a stray file."
            )
            self.assertNotEqual(0, code, payload)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", payload["error_code"])
            self.assertIn(MANIFEST_GUARD_MESSAGE, payload["message"])

    # -- (iv) provider arm planted marker: still admitted, measured ------------------

    def test_provider_planted_files_inside_the_bundle_prefix_still_admitted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target, request_id, candidate_id, order = self.walk_to_acquisition(root)
            self.write_directory_bundle(
                target,
                request_id=request_id,
                candidate_id=candidate_id,
                extra_files={
                    "offer.json": json.dumps({"price": "12.50 EUR"}) + "\n",
                    "freight.json": json.dumps({"carrier": "ACME"}) + "\n",
                },
            )
            self.fulfil_bundle(target, request_id, candidate_id, order)
            code, payload, stderr = self.submit(
                root, target, order["action_id"],
                summary="Delivered a bundle carrying unrelated planted members.",
            )
            self.assertEqual(0, code, (payload, stderr))  # accepted risk, asserted


class AttributionInvariantsShape2(DelegatedWorkspace, unittest.TestCase):
    """The properties the predicate's soundness and cost both rest on.

    Both were measured while the design was chosen but lived only in scratch probes.
    They are tests because the backlog names them as the vehicles for two standing
    claims: that derivation is deterministic (the controller now depends on it, so a
    future non-deterministic inventory change becomes a submit-breaking bug), and that
    one submit costs exactly one derivation pass despite verifying up to three times.

    Saving that cost has a property of its own, the same mechanism read from the other
    side: the key an answer is memoised under has to be sufficient for the tree that
    answer describes, or the saving is a wrong answer returned quickly.
    """

    maxDiff = None

    def write_sidecar(self, workspace: Path, relative: str, request_id: str) -> None:
        target = workspace / relative
        body: dict[str, object] = {
            "origin_url": "https://example.org/bundle",
            "license": "CC-BY-4.0",
            "retrieved_at": "2026-08-17T12:00:00Z",
            "retrieved_by": ACQUIRER,
            "request_id": request_id,
        }
        if target.is_file():
            body["checksum"] = f"sha256:{hashlib.sha256(target.read_bytes()).hexdigest()}"
        (workspace / f"{relative}.provenance.yml").write_text(
            yaml.safe_dump(body, sort_keys=False), encoding="utf-8"
        )

    def test_inventory_derivation_is_deterministic(self):
        """Repeated derivation over one unchanged tree returns byte-identical attribution.

        Pinned-order list equality is what `raw_attribution_mismatches` compares, so a
        derivation whose ordering wandered between verification passes would refuse a
        legitimate delivery. Exercised over the shape whose ordering is least obvious --
        a directory-shaped bundle, whose members are enumerated by a recursive walk --
        beside an ordinary single-file delivery.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            self.pending_order(workspace)
            self.deliver_for(workspace, request_id)
            bundle_relative = f"raw/papers/{LATEX_BUNDLE_FIXTURE.name}"
            shutil.copytree(LATEX_BUNDLE_FIXTURE, workspace / bundle_relative)
            self.write_sidecar(workspace, bundle_relative, request_id)

            config = D_CONTROLLER.load_config(workspace)
            runs = [
                D_CONTROLLER.derived_raw_attribution(workspace, config)
                for _ in range(3)
            ]

            def comparable(attribution):
                return {
                    source_id: {
                        "raw_paths": entry["raw_paths"],
                        "files": sorted(entry["files"]),
                    }
                    for source_id, entry in sorted(attribution.items())
                }

            first = comparable(runs[0])
            self.assertTrue(first, "the fixture must derive at least one record")
            for index, run in enumerate(runs[1:], start=2):
                self.assertEqual(
                    first,
                    comparable(run),
                    f"derivation pass {index} disagreed with pass 1; the controller "
                    "compares raw_paths by pinned order, so non-deterministic "
                    "derivation would refuse legitimate deliveries",
                )

    # -- an empty directory the raw fingerprint cannot see still busts the memo -------

    def test_an_added_empty_directory_is_not_answered_from_the_memo(self):
        """A tree that gained an empty directory must be derived again, not recalled.

        `raw_tree_snapshot` records an entry per regular *file* and none for a directory,
        so its fingerprint cannot tell these two trees apart. `source_inventory` can: an
        empty `.git/` is one of the markers that makes a directory a local repository, so
        the same tree derives a different record set on either side of one `mkdir`. A memo
        keyed on the fingerprint alone would answer the second question with the first
        tree's answer.

        The memo is only live inside `derivation_verdict_memo()`; outside it the cache is
        None and the memo is a no-op, so the context is entered here explicitly.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            self.pending_order(workspace)
            self.deliver_for(workspace, request_id)
            # A checkout the workspace reads as ordinary raw files until a marker makes
            # it a repository. Both settings are operator config the shipped template
            # documents and the harness workspace leaves off.
            config_path = workspace / "research.yml"
            document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            document["raw"]["source_roots"] = sorted({*document["raw"]["source_roots"], "raw/code"})
            document.setdefault("integrations", {}).setdefault("codebase_analysis", {})["enabled"] = True
            config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

            checkout = workspace / "raw" / "code" / "conductivity-tools"
            checkout.mkdir(parents=True)
            (checkout / "measure.py").write_text("value = 1\n", encoding="utf-8")
            self.write_sidecar(workspace, "raw/code/conductivity-tools/measure.py", request_id)

            config = D_CONTROLLER.load_config(workspace)
            before_fingerprint = D_CONTROLLER.raw_tree_snapshot(workspace, config)["fingerprint"]
            with D_CONTROLLER.derivation_verdict_memo():
                before = D_CONTROLLER.derived_raw_attribution(
                    workspace, config, memo_key=before_fingerprint
                )
                (checkout / ".git").mkdir()
                after_fingerprint = D_CONTROLLER.raw_tree_snapshot(workspace, config)["fingerprint"]
                after = D_CONTROLLER.derived_raw_attribution(
                    workspace, config, memo_key=before_fingerprint
                )

            self.assertEqual(
                before_fingerprint,
                after_fingerprint,
                "the empty directory must leave the raw-tree fingerprint byte-identical, "
                "or this case says nothing about what that fingerprint cannot see",
            )

            def derived_paths(attribution):
                return {source_id: entry["raw_paths"] for source_id, entry in attribution.items()}

            self.assertNotEqual(
                derived_paths(before),
                derived_paths(after),
                "derivation after the empty directory returned the attribution memoised "
                "before it; the memoised answer is stale",
            )
            self.assertIn(
                ["raw/code/conductivity-tools"],
                list(derived_paths(after).values()),
                "inventory must read the empty .git/ as the marker that makes the "
                "directory one local-repository record",
            )

    def test_one_submit_costs_exactly_one_derivation_pass(self):
        """`submit` verifies up to three times; derivation must run once, not three times.

        Each pass re-walks and re-hashes every raw file, so an unmemoised predicate
        would triple that cost on every acquisition. The memo is keyed by the raw-tree
        fingerprint, so this also asserts the key is stable across the passes of one
        submit.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)
            source_id = self.deliver_for(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            # The harness submits through the delegated suite's controller instance, and
            # each controller module caches its own sibling modules -- patching the other
            # instance's source_inventory would count zero calls and silently pass.
            source_inventory = D_CONTROLLER.load_sibling_module("source_inventory")
            original = source_inventory.build_records
            calls: list[int] = []

            def counting_build_records(*args, **kwargs):
                calls.append(1)
                return original(*args, **kwargs)

            with mock.patch.object(source_inventory, "build_records", counting_build_records):
                code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(0, code, envelope)
            self.assertEqual(
                1,
                len(calls),
                "one accepted submit must derive attribution exactly once; "
                f"observed {len(calls)} build_records call(s)",
            )


if __name__ == "__main__":
    unittest.main()
