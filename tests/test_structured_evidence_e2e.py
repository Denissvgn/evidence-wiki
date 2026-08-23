"""CR-2 end to end: a structured payload becomes citable, quotable evidence.

Every other CR-2 suite tests one leg. This one walks the whole chain a host actually
depends on, in a workspace built the way an operator builds one:

    deliver JSON + sidecar -> inventory -> a normalized record exists
      -> verify against the contract -> reopen the blocked question
      -> ground a claim in a facet value -> lint

The legs are load-bearing in sequence, not individually: a record is what opens the
reopen gate, and the facet headings are what make a value quotable at all. A regression
in any one of them shows up here as a broken chain rather than as a passing unit test
about a stage nobody can reach.

Two ways to get that record, and CR-2 promises both work on identical terms:

- `StructuredEvidenceChainTests` — this package runs a configured adapter (AC1).
- `ForeignRecordChainTests` — an external tool wrote the record by hand and no adapter
  exists anywhere (AC2), plus one mutation per contract violation family showing verify
  and lint each name it (AC3).

CR-4 then names the request that starts the chain. Before it, a workspace whose evidence
is JSON had to file every request as `kind: other` — the request said nothing about what
was wanted and nothing about what would satisfy it:

- `StructuredDataRequestKindTests` — the same chain opened with the built-in
  `structured_data` kind and a `--scope facet_id=…` mapping, delivered with a sidecar
  stating the same scope, and closed through `fulfill` and `reopen`. Plus the refusal
  that gives the scope its meaning: a delivery scoped to a different facet cannot fulfil
  the request.

CR-7 then fixes what all of the above still could not say. Every chain here ends in a
*quote*, and a quote against structured evidence is checked by substring containment: it
proves the cited record contains a sentence, not that the claim's value is the one the
record states. CR-7 adds anchor form — an RFC 6901 pointer into a hash-bound
structured-view sidecar, checked by canonical equality — plus the write path that stops
hosts hand-editing question frontmatter:

- `AnchorGroundingChainTests` — the whole CR-7 loop against an adapter that emits
  `structured`: sidecar written and bound, `grounding set`, `answer --require-grounding
  --grounding-file`, `verify_quotes --write`, and the reporting surfaces (lint counts,
  `export_answers`, the controller's answered-slug filter) agreeing about what happened.
- `NativeTableAnchorTests` — the same loop with no adapter anywhere. A plain CSV
  delivery, anchored past the 20-row body sample and past the 80-character cell cap:
  content that was citable but permanently unquotable before CR-7, which is the gap
  `normalized-source-format.md` documents. Plus the fail-closed half — a table that
  cannot be addressed faithfully emits no sidecar and says why.
- `AnchorGroundedWorkspaceShipsTests` — the cross-unit proof that anchor form is a
  first-class way to ground a *publishable* answer, not merely a verifiable one.
- `AnchorFailurePathTests` — the refusals, because fail-closed is the whole point of
  replacing containment with equality.
- `CrSevenAcceptanceCriteriaTests` — one named test per CR-7 acceptance criterion.
"""

import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from tests._script_loader import load_script as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
ADAPTER_FIXTURE = FIXTURES / "normalizer-adapter"
STUB_ADAPTER = ADAPTER_FIXTURE / "stub_adapter.py"
PAYLOAD = ADAPTER_FIXTURE / "keepa-b0abc123.json"
PROFILE_FIXTURE_PATH = FIXTURES / "workspace-init-profile.yml"

QUESTION_SLUG = "needs-price-evidence"
ADAPTER_NAME = "stub-normalize"
ADAPTER_VERSION = "1.0.0"

STRUCTURED_KIND = "structured_data"
# Both are top-level keys of the fixture payload, so either is a facet the adapter
# really renders — the mis-pairing below is between two answers that both exist.
REQUESTED_FACET = "supplier_quote"
DELIVERED_OTHER_FACET = "price_history_median_90d"


INIT = load_script_module("e2e_structured_init", "init_research_workspace.py")
INVENTORY = load_script_module("e2e_structured_inventory", "source_inventory.py")
NORMALIZE = load_script_module("e2e_structured_normalize", "normalize_sources.py")
VERIFY_CONTRACT = load_script_module("e2e_structured_normalize_verify", "normalize_verify.py")
CLAIM = load_script_module("e2e_structured_question_claim", "question_claim.py")
RESOLVE = load_script_module("e2e_structured_question_resolve", "question_resolve.py")
REQUESTS = load_script_module("e2e_structured_source_requests", "source_requests.py")
VERIFY_QUOTES = load_script_module("e2e_structured_verify_quotes", "verify_quotes.py")
LINT = load_script_module("e2e_structured_lint", "lint.py")
STATUS = load_script_module("e2e_structured_workspace_status", "workspace_status.py")
REQUEST_SCOPE = load_script_module("e2e_structured_request_scope", "_request_scope.py")
EXPORT = load_script_module("e2e_structured_export_answers", "export_answers.py")
READINESS = load_script_module("e2e_structured_publication_readiness", "publication_readiness.py")
CONTROLLER = load_script_module("e2e_structured_controller", "orchestration_controller.py")

FACET_KEY = REQUEST_SCOPE.FACET_SCOPE_KEY



@contextlib.contextmanager
def stub_environment(**values: str):
    """Steer the reference adapter's behaviour for one call."""
    original = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, previous in original.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


class StructuredEvidenceWorkspace:
    """Workspace scaffolding and pipeline drivers shared by both acceptance paths.

    Deliberately not a TestCase: subclassing one that carries tests would re-run the
    whole parent suite under every child class.
    """

    # -- workspace construction --------------------------------------------------

    def init_workspace(self, root: Path) -> Path:
        target = root / "structured-evidence-workspace"
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text(encoding="utf-8"))
        profile["workspace_init"]["target_path"] = str(target)
        profile["workspace_init"]["questions"] = [
            {
                "id": QUESTION_SLUG,
                "question": "What does the supplier quote for B0ABC12345?",
                "priority": "high",
            }
        ]
        profile_path = root / "profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            INIT.main(["--profile", str(profile_path)])
        return target

    def enable_adapter(self, workspace: Path) -> None:
        """What an operator does by hand: scan `raw/data`, map the kind to a command."""
        config_path = workspace / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["raw"]["source_roots"] = sorted({*config["raw"]["source_roots"], "raw/data"})
        config["normalization"] = {
            "adapters": [
                {
                    "kinds": ["structured_data"],
                    "provider": "command",
                    "command": [sys.executable, str(STUB_ADAPTER)],
                    "name": ADAPTER_NAME,
                    "version": ADAPTER_VERSION,
                }
            ]
        }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def deliver_payload(self, workspace: Path, *, scope: dict[str, str] | None = None) -> None:
        """A delivery per docs/source-delivery.md: the artifact plus its sidecar.

        `scope` is CR-4's addition to that contract: the deliverer states what the
        artifact answers, so fulfilment compares it instead of pairing by position. It
        is appended to the fixture's own sidecar text rather than re-serialized, which
        keeps `origin_url`/`retrieved_at`/`license` byte-identical to the unscoped
        delivery every other test in this module makes.
        """
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PAYLOAD, destination / PAYLOAD.name)
        sidecar = PAYLOAD.with_name(PAYLOAD.name + ".provenance.yml").read_text(encoding="utf-8")
        if scope:
            # The fixture ends with a newline today; guaranteeing the boundary means a
            # future fixture that does not cannot glue `scope:` onto its last line. And
            # safe_dump quotes a value containing ':', '#', or a leading '*' rather than
            # letting YAML reparse it as something else — hand-rolled lines would not.
            if not sidecar.endswith("\n"):
                sidecar += "\n"
            sidecar += yaml.safe_dump({"scope": scope}, sort_keys=True, allow_unicode=True)
        (destination / (PAYLOAD.name + ".provenance.yml")).write_text(sidecar, encoding="utf-8")

    def make_workspace(
        self, root: Path, *, adapter: bool = True, scope: dict[str, str] | None = None
    ) -> Path:
        workspace = self.init_workspace(root)
        if adapter:
            self.enable_adapter(workspace)
        else:
            config_path = workspace / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["raw"]["source_roots"] = sorted({*config["raw"]["source_roots"], "raw/data"})
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        self.deliver_payload(workspace, scope=scope)
        return workspace

    # -- pipeline stages ---------------------------------------------------------

    def run_inventory(self, workspace: Path, *extra: str) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = INVENTORY.main(["--project-root", str(workspace), "--format", "json", *extra])
        self.assertEqual(0, code)

    def run_normalize(self, workspace: Path, *extra: str) -> tuple[int, dict, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = NORMALIZE.main(
                ["--project-root", str(workspace), "--all", "--format", "json", *extra]
            )
        raw = stdout.getvalue()
        return int(code or 0), (json.loads(raw) if raw.strip() else {}), stderr.getvalue()

    def run_contract_verify(self, workspace: Path, *extra: str) -> tuple[int, dict, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = VERIFY_CONTRACT.main(["--project-root", str(workspace), *extra])
        raw = stdout.getvalue()
        return int(code or 0), (json.loads(raw) if raw.strip() else {}), stderr.getvalue()

    def run_quote_verify(self, workspace: Path) -> tuple[int, dict, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = VERIFY_QUOTES.main(
                ["--project-root", str(workspace), "--slug", QUESTION_SLUG, "--format", "json"]
            )
        raw = stdout.getvalue()
        return int(code or 0), (json.loads(raw) if raw.strip() else {}), stderr.getvalue()

    def run_lint(self, workspace: Path) -> dict:
        return LINT.run_checks(workspace, LINT.load_config(workspace))

    # -- question lifecycle ------------------------------------------------------

    def block_the_question(
        self,
        workspace: Path,
        *,
        kind: str = "dataset",
        scope: dict[str, str] | None = None,
    ) -> str:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                0,
                CLAIM.main(
                    [
                        "--project-root", str(workspace),
                        "claim", "--slug", QUESTION_SLUG,
                        "--agent-id", "research-agent", "--format", "json",
                    ]
                ),
            )
        scope_argv = [
            argument for key in sorted(scope or {}) for argument in ("--scope", f"{key}={scope[key]}")
        ]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = REQUESTS.main(
                [
                    "--project-root", str(workspace),
                    "add", "--kind", kind,
                    "--query-or-identifier", "keepa product B0ABC12345",
                    "--rationale", "No price evidence in the workspace.",
                    "--question-slug", QUESTION_SLUG, *scope_argv, "--format", "json",
                ]
            )
        self.assertEqual(0, code, stdout.getvalue())
        request_id = json.loads(stdout.getvalue())["request"]["request_id"]

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = RESOLVE.main(
                [
                    "--project-root", str(workspace),
                    "block", "--slug", QUESTION_SLUG,
                    "--agent-id", "research-agent",
                    "--blocked-reason", "Needs a supplier price snapshot.",
                    "--request-id", request_id, "--format", "json",
                ]
            )
        self.assertEqual(0, code)
        return request_id

    def request_record(self, workspace: Path, request_id: str) -> dict:
        """Read one request back from the store, as a consumer of the CLI would."""
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = REQUESTS.main(["--project-root", str(workspace), "list", "--format", "json"])
        self.assertEqual(0, int(code or 0), stdout.getvalue())
        matched = [
            record
            for record in json.loads(stdout.getvalue())["requests"]
            if record.get("request_id") == request_id
        ]
        self.assertEqual(1, len(matched), stdout.getvalue())
        return matched[0]

    def run_fulfill(
        self, workspace: Path, request_id: str, source_id: str, *extra: str
    ) -> tuple[int, dict]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = REQUESTS.main(
                [
                    "--project-root", str(workspace),
                    "fulfill", "--request-id", request_id,
                    "--source-id", source_id, *extra, "--format", "json",
                ]
            )
        return int(code or 0), json.loads(stdout.getvalue() or stderr.getvalue())

    def question_status(self, workspace: Path) -> str:
        page = (workspace / "wiki" / "questions" / f"{QUESTION_SLUG}.md").read_text(encoding="utf-8")
        return yaml.safe_load(page.split("---\n", 2)[1])["status"]

    def run_reopen(self, workspace: Path, source_id: str, request_id: str) -> tuple[int, dict]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = RESOLVE.main(
                [
                    "--project-root", str(workspace),
                    "reopen", "--slug", QUESTION_SLUG,
                    "--agent-id", "delivery-agent",
                    "--source-id", source_id,
                    "--request-id", request_id, "--format", "json",
                ]
            )
        payload = json.loads(stdout.getvalue() or stderr.getvalue())
        return int(code or 0), payload

    def ground_the_question(
        self,
        workspace: Path,
        source_id: str,
        *,
        quote: str,
        # The facet heading the adapter emitted, not a page title: this is the anchor
        # CR-2 promises a structured source can carry.
        location_hint: str = "supplier_quote",
    ) -> None:
        """Anchor a claim to a facet section of the rendered record."""
        page = workspace / "wiki" / "questions" / f"{QUESTION_SLUG}.md"
        head, frontmatter_text, body = page.read_text(encoding="utf-8").split("---\n", 2)
        frontmatter = yaml.safe_load(frontmatter_text)
        frontmatter["source_ids"] = [source_id]
        frontmatter["grounding"] = [
            {
                "claim": "The supplier quotes 23.99 EUR for B0ABC12345.",
                "source_id": source_id,
                "quote": quote,
                "location_hint": location_hint,
            }
        ]
        page.write_text(
            head + "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n" + body,
            encoding="utf-8",
        )

    # -- helpers -----------------------------------------------------------------

    def manifest(self, workspace: Path) -> dict[str, dict]:
        path = workspace / "sources" / "manifest.jsonl"
        return {
            record["id"]: record
            for record in (
                json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
            )
        }

    def structured_record(self, workspace: Path) -> dict:
        candidates = [
            record for record in self.manifest(workspace).values() if record["kind"] == "structured_data"
        ]
        self.assertEqual(1, len(candidates), self.manifest(workspace))
        return candidates[0]

    def normalized_path(self, workspace: Path, source_id: str) -> Path:
        return workspace / "sources" / "normalized" / f"{NORMALIZE.safe_source_id(source_id)}.md"

    def about_the_source(
        self, issues: list[dict], source_id: str, record_path: Path, workspace: Path
    ) -> list[dict]:
        label = record_path.relative_to(workspace).as_posix()
        return [
            issue
            for issue in issues
            if issue.get("source_id") == source_id or label in issue.get("files", [])
        ]


class StructuredEvidenceChainTests(StructuredEvidenceWorkspace, unittest.TestCase):
    """The CR-2 AC1 path, run once per test against a freshly built workspace."""

    def test_delivered_payload_becomes_quotable_evidence_end_to_end(self):
        """CR-2 AC1 verbatim, one stage at a time, each asserted before the next runs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            request_id = self.block_the_question(workspace)

            # 1. Inventory classifies the payload and fingerprints it.
            self.run_inventory(workspace)
            manifest_record = self.structured_record(workspace)
            source_id = manifest_record["id"]
            self.assertTrue(manifest_record["raw_fingerprint"].startswith("sha256:"))
            self.assertEqual(["raw/data/keepa-b0abc123.json"], manifest_record["raw_paths"])
            self.assertEqual(
                "https://api.keepa.test/product/B0ABC12345",
                manifest_record["provenance"]["origin_url"],
            )

            # 2. The reopen gate is shut until the source is normalized. This is the
            #    failure CR-2 exists to remove, so the chain must show it closed first.
            code, payload = self.run_reopen(workspace, source_id, request_id)
            self.assertEqual(2, code)
            self.assertEqual("SOURCE_NOT_NORMALIZED", payload["error_code"])

            # 3. Normalization runs the adapter and writes the record.
            code, report, stderr = self.run_normalize(workspace)
            self.assertEqual(0, code, stderr)
            self.assertEqual(1, report["summary"]["methods"]["adapter"])
            record_path = self.normalized_path(workspace, source_id)
            self.assertTrue(record_path.is_file())

            # 4. The record verifies against the published contract.
            code, verify_report, stderr = self.run_contract_verify(workspace)
            self.assertEqual(VERIFY_CONTRACT.EXIT_OK, code, stderr)
            entry = next(
                record for record in verify_report["records"] if record["source_id"] == source_id
            )
            self.assertEqual("verified", entry["result"])
            self.assertEqual("external", entry["origin"])
            self.assertEqual(1.0, entry["rendered_coverage"]["ratio"])

            # 5. The gate now opens.
            code, payload = self.run_reopen(workspace, source_id, request_id)
            self.assertEqual(0, code, payload)
            self.assertEqual("open", payload["status"])
            self.assertIn(source_id, payload["source_ids"])

            # 6. A value inside a facet section grounds a claim by containment.
            self.ground_the_question(workspace, source_id, quote="supplier_quote: 23.99 EUR")
            code, quotes, stderr = self.run_quote_verify(workspace)
            self.assertEqual(0, code, stderr)
            grounding = quotes["questions"][0]["grounding"][0]
            self.assertEqual("verified", grounding["result"])
            self.assertEqual("section", grounding["anchor"]["type"])
            self.assertTrue(quotes["questions"][0]["all_verified"])

            # 7. Lint treats the adapter's record exactly as it treats a native one.
            results = self.run_lint(workspace)
            about_source = self.about_the_source(results["issues"], source_id, record_path, workspace)

            # 8. The workspace stays orchestratable. This is the controller's own
            #    postcondition (`fresh_workspace_status` -> ORCHESTRATION_WORKSPACE_UNSAFE),
            #    checked at the surface it reads rather than inferred from lint.
            status = STATUS.build_status_document(workspace)

        self.assertEqual([], [issue for issue in results["issues"] if issue["severity"] == "HIGH"])
        self.assertEqual(1, results["stats"]["sources_foreign_normalized"])
        self.assertEqual(0, results["stats"]["normalized_contract_violations"])
        # One LOW finding remains, and it is not about the record: writing the wiki
        # source note is the agent-driven ingest step, which is deliberately outside
        # this deterministic chain. Pinning it exactly is what keeps the assertion
        # honest — a new finding about the source would break this test rather than
        # hide inside a severity filter.
        self.assertEqual(
            ["normalized_missing_source_note"], [issue["category"] for issue in about_source]
        )
        self.assertTrue(status["workspace_health"]["materially_valid"], status["workspace_health"])
        self.assertNotEqual("attention_required", status["readiness"]["verdict"])

    # -- the same delivery without an adapter ------------------------------------

    def test_without_an_adapter_the_payload_is_classified_but_never_normalized(self):
        """The backward-compatibility half of AC1: configuring nothing changes nothing.

        The kind is still recognised — that is a better manifest, not new behaviour —
        but no command runs, no record appears, and the reopen gate stays shut.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir), adapter=False)
            request_id = self.block_the_question(workspace)
            self.run_inventory(workspace)
            source_id = self.structured_record(workspace)["id"]

            code, report, stderr = self.run_normalize(workspace)
            self.assertEqual(0, code, stderr)
            self.assertEqual(0, report["summary"]["methods"].get("adapter", 0))
            self.assertFalse(self.normalized_path(workspace, source_id).exists())

            code, payload = self.run_reopen(workspace, source_id, request_id)

        self.assertEqual(2, code)
        self.assertEqual("SOURCE_NOT_NORMALIZED", payload["error_code"])

    # -- a capped rendering is still a working chain ------------------------------

    def test_a_capped_rendering_still_grounds_what_it_did_render(self):
        """Coverage below 1.0 is a reporting fact, not a broken chain.

        The dropped facets are citable but not quotable — exactly the loss D1's
        `rendered_coverage` exists to make visible — while a facet that *was* rendered
        grounds a claim as normally.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.run_inventory(workspace)
            source_id = self.structured_record(workspace)["id"]

            with stub_environment(EW_STUB_CAP="2"):
                code, _, stderr = self.run_normalize(workspace)
            self.assertEqual(0, code, stderr)

            code, verify_report, stderr = self.run_contract_verify(workspace)
            self.assertEqual(VERIFY_CONTRACT.EXIT_OK, code, stderr)
            coverage = verify_report["records"][0]["rendered_coverage"]
            self.assertEqual(0.5, coverage["ratio"])
            self.assertEqual(
                ["price_history_median_90d", "offer_count"], coverage["capped_sections"]
            )

            self.ground_the_question(workspace, source_id, quote="supplier_quote: 23.99 EUR")
            code, quotes, stderr = self.run_quote_verify(workspace)
            self.assertEqual(0, code, stderr)
            self.assertEqual("verified", quotes["questions"][0]["grounding"][0]["result"])

            # And the dropped facet has no section to anchor to, so a claim on it fails
            # closed. That is the honest outcome the coverage block predicted, not a
            # silent pass — and the reason `capped_sections` names it up front.
            self.ground_the_question(
                workspace, source_id, quote="offer_count: 7", location_hint="offer_count"
            )
            code, quotes, _ = self.run_quote_verify(workspace)

        self.assertEqual(1, code)
        self.assertEqual("anchor_not_found", quotes["questions"][0]["grounding"][0]["result"])


FOREIGN_NORMALIZER_NAME = "external-tool"


def hand_written_record(source_id: str, fingerprint: str) -> str:
    """A record an external tool wrote by hand, targeting the published contract.

    This is the CR's originating shape: the host renders the payload itself and writes
    the record directly, with no adapter configured and nothing of this package in the
    loop except the format. `normalized_format` is what makes that legible — without
    it a foreign record is indistinguishable from a legacy native one.
    """
    return f"""---
type: normalized_source
normalized_format: 1
source_id: {source_id}
source_kind: structured_data
status: content_extracted
evidence_usable: true
created: 2026-08-08
updated: 2026-08-08
raw_paths:
  - raw/data/keepa-b0abc123.json
manifest_path: sources/manifest.jsonl
normalizer:
  name: {FOREIGN_NORMALIZER_NAME}
  version: 1
parse_warnings: []
title: Keepa snapshot B0ABC12345
raw_fingerprint: {fingerprint}
rendered_coverage:
  total_values: 4
  rendered_values: 4
  ratio: 1.0
  sections:
    - heading: supplier_quote
      total: 1
      rendered: 1
---

# Keepa snapshot B0ABC12345

## Citation Metadata

- URL: https://api.keepa.test/product/B0ABC12345

## Abstract

Supplier price snapshot for B0ABC12345.

## Outline

- supplier_quote

## Extracted Text

### asin

- asin: B0ABC12345

### supplier_quote

- supplier_quote: 23.99 EUR

## Figures and Tables

- None recorded.

## Links

- None recorded.

## Raw Source Paths

- `raw/data/keepa-b0abc123.json`

## Parse Warnings

- None recorded.
"""


class ForeignRecordChainTests(StructuredEvidenceWorkspace, unittest.TestCase):
    """CR-2 AC2/AC3: a hand-written record is evidence on the same terms, or named.

    No adapter is configured anywhere in this class. Everything here is reachable by a
    host that only targets the format document — which is the point of versioning it.
    """

    def make_foreign_workspace(self, root: Path) -> tuple[Path, str, Path]:
        workspace = self.make_workspace(root, adapter=False)
        self.run_inventory(workspace)
        manifest_record = self.structured_record(workspace)
        source_id = manifest_record["id"]
        record_path = self.normalized_path(workspace, source_id)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            hand_written_record(source_id, manifest_record["raw_fingerprint"]), encoding="utf-8"
        )
        return workspace, source_id, record_path

    # -- AC2: same terms as a native record --------------------------------------

    def test_hand_written_record_is_evidence_on_the_same_terms(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, source_id, record_path = self.make_foreign_workspace(Path(tmpdir))
            request_id = self.block_the_question(workspace)

            # Verified by the same validator that judges this package's own output.
            code, report, stderr = self.run_contract_verify(workspace)
            self.assertEqual(VERIFY_CONTRACT.EXIT_OK, code, stderr)
            # The hand-written record is the only one in the workspace, which is what
            # lets every case below read `records[0]`.
            self.assertEqual(1, report["counts"]["records"])
            entry = report["records"][0]
            self.assertEqual("verified", entry["result"])
            self.assertEqual("external", entry["origin"])
            self.assertEqual({"name": FOREIGN_NORMALIZER_NAME, "version": 1}, entry["normalizer"])

            # The reopen gate reads "a record exists", not "we wrote it".
            code, payload = self.run_reopen(workspace, source_id, request_id)
            self.assertEqual(0, code, payload)
            self.assertEqual("open", payload["status"])

            # Grounding is containment against the body, so a facet value quotes.
            self.ground_the_question(workspace, source_id, quote="supplier_quote: 23.99 EUR")
            code, quotes, stderr = self.run_quote_verify(workspace)
            self.assertEqual(0, code, stderr)
            self.assertEqual("verified", quotes["questions"][0]["grounding"][0]["result"])

            results = self.run_lint(workspace)
            about_source = self.about_the_source(results["issues"], source_id, record_path, workspace)

        self.assertEqual([], [issue for issue in results["issues"] if issue["severity"] == "HIGH"])
        self.assertEqual(1, results["stats"]["sources_foreign_normalized"])
        self.assertEqual(0, results["stats"]["normalized_contract_violations"])
        # Same residue as the adapter path: only the agent-driven ingest step is absent.
        self.assertEqual(
            ["normalized_missing_source_note"], [issue["category"] for issue in about_source]
        )

    def test_normalization_leaves_a_record_it_did_not_produce_alone(self):
        """The host owns records for kinds nothing here normalizes.

        With no adapter mapped, `structured_data` is not eligible, so a full
        `--all` run must not rewrite or delete the host's work. This is the boundary
        B4 drew, asserted from the outside: byte-identical after the run.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, _, record_path = self.make_foreign_workspace(Path(tmpdir))
            before = record_path.read_bytes()

            code, _, stderr = self.run_normalize(workspace)
            after = record_path.read_bytes()

        self.assertEqual(0, code, stderr)
        self.assertEqual(before, after)

    # -- AC3: every violation family is named ------------------------------------

    # One mutation per violation family in `_normalized_contract`, each expressed as a
    # plausible mistake rather than a synthetic one: a writer that forgot the version
    # handle, reordered a section, copied a stale fingerprint, and so on.
    MUTATIONS: tuple[tuple[str, str, object], ...] = (
        (
            "no frontmatter at all",
            "NORMALIZED_CONTRACT_FRONTMATTER_MISSING",
            lambda text: text.split("---\n", 2)[2],
        ),
        (
            "unknown status value",
            "NORMALIZED_CONTRACT_FRONTMATTER_INVALID",
            lambda text: text.replace("status: content_extracted", "status: done"),
        ),
        (
            "missing the format version",
            "NORMALIZED_CONTRACT_FORMAT_VERSION_UNSUPPORTED",
            lambda text: text.replace("normalized_format: 1\n", ""),
        ),
        (
            "required sections out of order",
            "NORMALIZED_CONTRACT_SECTIONS_INVALID",
            lambda text: text.replace(
                "## Abstract\n\nSupplier price snapshot for B0ABC12345.\n\n## Outline\n\n- supplier_quote\n",
                "## Outline\n\n- supplier_quote\n\n## Abstract\n\nSupplier price snapshot for B0ABC12345.\n",
            ),
        ),
        (
            "fingerprint disagrees with the manifest",
            "NORMALIZED_CONTRACT_MANIFEST_MISMATCH",
            lambda text: re.sub(r"raw_fingerprint: .*", "raw_fingerprint: sha256:stale", text),
        ),
        (
            "warning declared but not restated in the body",
            "NORMALIZED_CONTRACT_WARNINGS_INCONSISTENT",
            lambda text: text.replace("parse_warnings: []", "parse_warnings:\n  - truncated series"),
        ),
        (
            "coverage ratio contradicts its own counts",
            "NORMALIZED_CONTRACT_RENDERED_COVERAGE_INVALID",
            lambda text: text.replace("  ratio: 1.0", "  ratio: 0.25"),
        ),
    )

    def test_verify_names_every_violation_family_with_its_stable_code(self):
        for label, expected_code, mutate in self.MUTATIONS:
            with self.subTest(mutation=label):
                with tempfile.TemporaryDirectory() as tmpdir:
                    workspace, _, record_path = self.make_foreign_workspace(Path(tmpdir))
                    record_path.write_text(
                        mutate(record_path.read_text(encoding="utf-8")), encoding="utf-8"
                    )
                    code, report, stderr = self.run_contract_verify(workspace)

                self.assertEqual(VERIFY_CONTRACT.EXIT_NOT_VERIFIED, code, stderr)
                entry = report["records"][0]
                self.assertEqual("invalid", entry["result"])
                # Exactly this family and no other: each mutation is scoped to one
                # breach, so a validator that started over- or under-reporting shows up
                # here rather than hiding behind a membership check.
                self.assertEqual(
                    [expected_code], [violation["code"] for violation in entry["violations"]]
                )

    def test_lint_names_every_violation_family_it_can_attribute(self):
        for label, expected_code, mutate in self.MUTATIONS:
            with self.subTest(mutation=label):
                with tempfile.TemporaryDirectory() as tmpdir:
                    workspace, source_id, record_path = self.make_foreign_workspace(Path(tmpdir))
                    record_path.write_text(
                        mutate(record_path.read_text(encoding="utf-8")), encoding="utf-8"
                    )
                    results = self.run_lint(workspace)

                findings = [
                    issue
                    for issue in results["issues"]
                    if issue["category"] == "normalized_record_contract_violation"
                ]
                if expected_code == "NORMALIZED_CONTRACT_FRONTMATTER_MISSING":
                    # A record with no frontmatter names no producing tool, so lint
                    # cannot tell a foreign record from a stray Markdown file and does
                    # not police it. `normalize_verify.py` still refuses it, which is
                    # where the AC3 guarantee lives. Asserted rather than skipped: the
                    # asymmetry is a decision, and it should break if it changes.
                    self.assertEqual([], findings)
                    continue
                self.assertEqual(1, len(findings), results["issues"])
                self.assertEqual(expected_code, findings[0]["code"])
                self.assertEqual("MEDIUM", findings[0]["severity"])
                self.assertIn("normalize_verify.py", findings[0]["recommendation"])
                # MEDIUM, never HIGH: a malformed record must not freeze orchestration.
                self.assertEqual(0, results["stats"]["issue_counts"]["HIGH"])

    def test_a_violating_record_cannot_quietly_ground_a_claim(self):
        """Lint is deliberately not the gate, so the gate has to be checked separately.

        `normalized_record_contract_violation` is MEDIUM precisely because the
        load-bearing checks fail closed on their own. This asserts that they do: a
        record whose body no longer carries the facet cannot verify a quote against it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, source_id, record_path = self.make_foreign_workspace(Path(tmpdir))
            record_path.write_text(
                record_path.read_text(encoding="utf-8").replace(
                    "### supplier_quote\n\n- supplier_quote: 23.99 EUR\n", ""
                ),
                encoding="utf-8",
            )
            self.ground_the_question(workspace, source_id, quote="supplier_quote: 23.99 EUR")
            code, quotes, _ = self.run_quote_verify(workspace)

        self.assertEqual(1, code)
        self.assertEqual("anchor_not_found", quotes["questions"][0]["grounding"][0]["result"])


class StructuredDataRequestKindTests(StructuredEvidenceWorkspace, unittest.TestCase):
    """CR-4: the request that starts the chain finally names what it wants.

    CR-2 made a JSON payload citable. What it could not fix is the request that asks for
    one: with `paper`/`dataset`/`web`/`code`/`other` as the whole vocabulary, a workspace
    whose evidence is JSON filed `kind: other` — a bucket that says nothing — and paired
    the delivery back to the request by position afterwards. CR-4 supplies both halves,
    and this walks the loop with both stated: a `structured_data` request carrying
    `scope: {facet_id: …}`, and a delivery whose sidecar declares a facet of its own.

    The scope is only worth carrying if it can refuse something, so the refusal is here
    too — with both facets drawn from the payload's own keys, making the delivery a
    source that genuinely answers *something*, recorded against the wrong request. That
    is the mis-pairing the change request reports; a malformed sidecar would not be.
    """

    def scoped_workspace(self, root: Path, *, delivered_facet: str) -> tuple[Path, str, str]:
        """A blocked `structured_data` request, plus a delivery scoped to `delivered_facet`."""
        workspace = self.make_workspace(root, scope={FACET_KEY: delivered_facet})
        request_id = self.block_the_question(
            workspace, kind=STRUCTURED_KIND, scope={FACET_KEY: REQUESTED_FACET}
        )
        self.run_inventory(workspace, "--report")
        return workspace, request_id, self.structured_record(workspace)["id"]

    def test_delivered_sidecar_scope_survives_values_yaml_would_reinterpret(self):
        """The helper writes a sidecar the workspace parses, not one that merely looks right.

        Scope values are opaque strings the deliverer chooses, so they can carry ':',
        '#', or a leading '*' — each of which YAML reads as structure when written bare.
        A sidecar that silently parsed to a *different* scope than the test intended
        would make every scope assertion in this module meaningless.
        """
        awkward = {
            "facet_id": "price: current",
            "candidate": "*acme #1",
            "note": "yes",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir), scope=awkward)

            written = (workspace / "raw" / "data" / (PAYLOAD.name + ".provenance.yml")).read_text(
                encoding="utf-8"
            )
            document = yaml.safe_load(written)

            self.assertEqual(awkward, document["scope"])
            # "yes" must stay the string the deliverer wrote, not YAML 1.1's boolean.
            self.assertIsInstance(document["scope"]["note"], str)
            # The fixture's own fields are untouched, so a scoped delivery differs from
            # an unscoped one only by the scope block.
            original = yaml.safe_load(
                PAYLOAD.with_name(PAYLOAD.name + ".provenance.yml").read_text(encoding="utf-8")
            )
            self.assertEqual(original, {key: value for key, value in document.items() if key != "scope"})

            # And the workspace agrees: inventory round-trips it onto the manifest record.
            self.run_inventory(workspace, "--report")
            self.assertEqual(awkward, self.structured_record(workspace)["provenance"]["scope"])

    def test_a_structured_data_request_survives_the_whole_delivery_loop(self):
        """blocked question -> scoped request -> delivery -> normalize -> fulfil -> reopen."""
        scope = {FACET_KEY: REQUESTED_FACET}
        # 1. No pack is installed anywhere in this module, so the kind can only be
        #    accepted by `add` because it is reserved — which is the whole point of
        #    making it a built-in rather than something each pack redeclares.
        self.assertIn(STRUCTURED_KIND, REQUESTS.REQUEST_KINDS)
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, source_id = self.scoped_workspace(
                Path(tmpdir), delivered_facet=REQUESTED_FACET
            )
            opened = self.request_record(workspace, request_id)
            self.assertEqual(STRUCTURED_KIND, opened["kind"])
            self.assertEqual(scope, opened["scope"])
            self.assertEqual("blocked", self.question_status(workspace))

            # 2. Inventory files the payload under the same string the request used, and
            #    round-trips the sidecar scope onto the manifest record. Both matter: the
            #    shared kind string is what lets a CR-2 adapter declaring
            #    `kinds: [structured_data]` cover a `structured_data` request at all, and
            #    the manifest is where fulfilment reads the delivery's side of the scope.
            manifest_record = self.structured_record(workspace)
            self.assertEqual(STRUCTURED_KIND, manifest_record["kind"])
            self.assertEqual(scope, manifest_record["provenance"]["scope"])

            # 3. The configured adapter turns the payload into a normalized record.
            code, report, stderr = self.run_normalize(workspace)
            self.assertEqual(0, code, stderr)
            self.assertEqual(1, report["summary"]["methods"]["adapter"])
            self.assertTrue(self.normalized_path(workspace, source_id).is_file())

            # 4. Fulfilment: the two scopes agree, so the request closes against this
            #    source — matched, not merely unchecked. The contradicting case below is
            #    what tells those two apart.
            code, fulfilled = self.run_fulfill(workspace, request_id, source_id)
            self.assertEqual(0, code, fulfilled)
            self.assertEqual("fulfilled", fulfilled["request"]["status"])
            self.assertEqual(source_id, fulfilled["request"]["source_id"])
            self.assertEqual(STRUCTURED_KIND, fulfilled["request"]["kind"])

            # 5. ...and the question it blocked reopens. This is the step that used to
            #    end the chain at SOURCE_NOT_NORMALIZED for structured evidence, and the
            #    reason the loop is worth walking rather than unit-testing in pieces.
            code, reopened = self.run_reopen(workspace, source_id, request_id)
            self.assertEqual(0, code, reopened)
            self.assertEqual("open", reopened["status"])
            self.assertIn(source_id, reopened["source_ids"])
            self.assertEqual("open", self.question_status(workspace))

            final = self.request_record(workspace, request_id)

        # 6. Carried verbatim, never mapped: the kind and the scope that paired it read
        #    back off the store unchanged after every mutation the loop performed.
        self.assertEqual(STRUCTURED_KIND, final["kind"])
        self.assertEqual(scope, final["scope"])
        self.assertEqual("fulfilled", final["status"])
        self.assertEqual(source_id, final["source_id"])

    def test_a_delivery_for_another_facet_contradicts_the_request_it_would_close(self):
        """The refusal's precondition, asserted against durable state rather than assumed.

        Whether `fulfill` can act on this yet is the next assertion's problem; that the
        two sides genuinely disagree — as the shipped matcher reads them, out of the
        request store and the inventoried manifest record that `fulfill` itself reads —
        is this one's, and it holds independently of any flag.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, source_id = self.scoped_workspace(
                Path(tmpdir), delivered_facet=DELIVERED_OTHER_FACET
            )
            conflicts, absences = REQUEST_SCOPE.scope_match(
                self.request_record(workspace, request_id)["scope"],
                REQUESTS.source_provenance_scope(
                    workspace, REQUESTS.load_config(workspace), source_id
                ),
            )

        # A conflict, not an absence: the delivery stated a facet, it is simply not the
        # one asked for. The distinction is the whole layering — a conflict always
        # refuses, an absence only under `--require-scope`.
        self.assertEqual([FACET_KEY], conflicts)
        self.assertEqual([], absences)

    def test_a_delivery_scoped_to_another_facet_is_refused_at_fulfill(self):
        """CR-4 acceptance: a `facet_id=X` request cannot be closed by a `facet_id=Y` source.

        Normalization is deliberately absent — fulfilment gates on manifest membership,
        so leaving it out proves the refusal is the scope check and not a missing record.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, source_id = self.scoped_workspace(
                Path(tmpdir), delivered_facet=DELIVERED_OTHER_FACET
            )
            code, payload = self.run_fulfill(workspace, request_id, source_id)
            refused = self.request_record(workspace, request_id)

        self.assertEqual(REQUESTS.EXIT_INVALID, code, payload)
        self.assertEqual("REQUEST_SCOPE_MISMATCH", payload["error_code"])
        # Refused before the write, not rolled back after it: the request is still open
        # and unlinked, so the source that does answer it can still close it.
        self.assertEqual("open", refused["status"])
        self.assertIsNone(refused["source_id"])


# --------------------------------------------------------------------------------------
# CR-7: structured grounding anchors, and the supported write path for grounding
# --------------------------------------------------------------------------------------

STRUCTURED_ADAPTER_NAME = "stub-normalize-structured"

# Written into the temp workspace rather than added to
# `tests/fixtures/normalizer-adapter/stub_adapter.py`: several suites pin that adapter
# emitting *no* structured view, which is what makes "a source without a sidecar cannot
# use anchor form" testable at all. A second adapter is the honest way to add a second
# behaviour.
STRUCTURED_ADAPTER_SOURCE = r'''#!/usr/bin/env python3
"""A conforming adapter that also emits `structured` — the CR-7 half of the protocol.

Renders the same facet body the reference stub renders, and beside it the complete
facet-shaped structured view an anchor resolves pointers against.
"""

import json
import os
import sys


def main() -> int:
    request = json.loads(sys.stdin.read())
    project_root = request["project_root"]
    payload = {}
    for relative in request["raw_paths"]:
        with open(os.path.join(project_root, relative), encoding="utf-8") as handle:
            payload = json.load(handle)
        break

    sections = []
    outline = []
    coverage = []
    for key, value in payload.items():
        outline.append([3, key])
        sections.append("### " + key + "\n\n- " + key + ": " + str(value) + "\n")
        coverage.append({"heading": key, "total": 1, "rendered": 1})

    # Facet-shaped, and deliberately richer than the body: `supplier_quote` is one string
    # in the provider payload and a three-field facet here. That asymmetry is why anchors
    # resolve against the normalizer's structured view rather than the raw bytes — the
    # pointer reads `supplier_quote/price`, not whatever shape the provider happened to
    # ship. It also gives the sidecar one value of every scalar type to compare.
    structured = {
        "asin": payload["asin"],
        "supplier_quote": {
            "price": payload["supplier_quote"],
            "currency": payload["supplier_quote"].split()[-1],
            "in_stock": True,
        },
        "price_history_median_90d": payload["price_history_median_90d"],
        "offer_count": payload["offer_count"],
    }

    result = {
        "schema_version": "1.0",
        "document_type": "normalizer_adapter_result",
        "adapter": {"name": "stub-normalize-structured", "version": "1.0.0"},
        "status": "content_extracted",
        "title": "Structured rendering of " + request["manifest_record"]["id"],
        "abstract": "Structured payload rendered beside its structured view.",
        "outline": outline,
        "body_markdown": "\n".join(sections),
        "rendered_coverage": {
            "total_values": len(coverage),
            "rendered_values": len(coverage),
            "ratio": 1.0,
            "sections": coverage,
        },
        "warnings": [],
        "structured": structured,
    }
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

ANSWER_AGENT = "answer-agent"
VERIFIER_AGENT = "verifier-agent"

# The CR's own worked example, carried verbatim through every CR-7 case here.
ANCHOR_CLAIM = "Current supplier price is 23.99 EUR"
ANCHOR_POINTER = "supplier_quote/price"
ANCHOR_EXPECTED = "23.99 EUR"
# The quote-form half of a mixed block, and the backward-compatibility case: a value the
# adapter rendered into a facet section of the body, checked by containment as always.
QUOTE_CLAIM = "The record lists seven offers."
QUOTE_TEXT = "offer_count: 7"
QUOTE_HINT = "offer_count"


def canonical_grounding_block(source_id: str) -> str:
    """The bytes a compliant hand edit writes for the anchor entry, stated not rendered.

    "Byte-identical to a compliant hand edit" is only checkable if the test says what the
    hand edit is, so this is a literal rather than a call into the serializer under test.
    Every free-text field is double-quoted; `source_id` is quoted because an inventoried
    id carries a `:`, which YAML would otherwise read as the start of a mapping.
    """
    return (
        "grounding:\n"
        f'  - claim: "{ANCHOR_CLAIM}"\n'
        f'    source_id: "{source_id}"\n'
        "    anchor:\n"
        f'      pointer: "{ANCHOR_POINTER}"\n'
        f'      expected: "{ANCHOR_EXPECTED}"\n'
    )


class AnchorGroundingWorkspace(StructuredEvidenceWorkspace):
    """CR-7 drivers layered on the CR-2 ones: an emitting adapter and the write path.

    Same reason as the parent for not being a TestCase, and same in-process discipline:
    every stage is a `MODULE.main([...])` call under redirected streams, so a refusal is
    an exit code and an envelope rather than a subprocess to interpret.
    """

    # -- workspace construction --------------------------------------------------

    def enable_structured_adapter(self, root: Path, workspace: Path) -> Path:
        """Configure the emitting adapter, written outside the workspace it normalizes."""
        adapter = root / "structured-adapter.py"
        adapter.write_text(STRUCTURED_ADAPTER_SOURCE, encoding="utf-8")
        config_path = workspace / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["raw"]["source_roots"] = sorted({*config["raw"]["source_roots"], "raw/data"})
        config["normalization"] = {
            "adapters": [
                {
                    "kinds": [STRUCTURED_KIND],
                    "provider": "command",
                    "command": [sys.executable, str(adapter)],
                    "name": STRUCTURED_ADAPTER_NAME,
                    "version": ADAPTER_VERSION,
                }
            ]
        }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return adapter

    def make_anchor_workspace(self, root: Path) -> tuple[Path, str]:
        """A normalized structured source with a bound sidecar, and its question open."""
        workspace = self.init_workspace(root)
        self.enable_structured_adapter(root, workspace)
        self.deliver_payload(workspace)
        self.run_inventory(workspace)
        source_id = self.structured_record(workspace)["id"]
        code, _, stderr = self.run_normalize(workspace)
        self.assertEqual(0, code, stderr)
        return workspace, source_id

    # -- the artefacts under test ------------------------------------------------

    def question_page(self, workspace: Path) -> Path:
        return workspace / "wiki" / "questions" / f"{QUESTION_SLUG}.md"

    def page_digest(self, workspace: Path) -> str:
        """The whole page, hashed. A refusal must leave these bytes alone."""
        return hashlib.sha256(self.question_page(workspace).read_bytes()).hexdigest()

    def structured_sidecar(self, workspace: Path, source_id: str) -> Path:
        """Where the verifier itself looks for the sidecar, never a path restated here."""
        path, _ = VERIFY_QUOTES.structured_view_path(
            workspace, VERIFY_QUOTES.load_config(workspace), source_id
        )
        return path

    def record_frontmatter(self, workspace: Path, source_id: str) -> dict:
        text = self.normalized_path(workspace, source_id).read_text(encoding="utf-8")
        return yaml.safe_load(text.split("---\n", 2)[1])

    def record_body(self, workspace: Path, source_id: str) -> str:
        return self.normalized_path(workspace, source_id).read_text(encoding="utf-8").split("---\n", 2)[2]

    # -- grounding files ---------------------------------------------------------

    def anchor_entry(self, source_id: str, *, expected: str = ANCHOR_EXPECTED, pointer: str = ANCHOR_POINTER) -> dict:
        return {
            "claim": ANCHOR_CLAIM,
            "source_id": source_id,
            "anchor": {"pointer": pointer, "expected": expected},
        }

    def quote_entry(self, source_id: str) -> dict:
        return {
            "claim": QUOTE_CLAIM,
            "source_id": source_id,
            "quote": QUOTE_TEXT,
            "location_hint": QUOTE_HINT,
        }

    def grounding_file(self, root: Path, entries: list[dict], name: str = "grounding.yml") -> Path:
        """A host's own file, dumped by a host's own YAML writer.

        Deliberately not written in the canonical form: the point of the write path is
        that whatever shape the host hands over, the page ends up canonical.
        """
        path = root / name
        path.write_text(yaml.safe_dump({"grounding": entries}, sort_keys=False), encoding="utf-8")
        return path

    # -- pipeline stages ---------------------------------------------------------

    def run_resolve(self, workspace: Path, *args: str) -> tuple[int, dict, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = RESOLVE.main(["--project-root", str(workspace), *args, "--format", "json"])
        return int(code or 0), json.loads(stdout.getvalue() or stderr.getvalue()), stderr.getvalue()

    def claim_question(self, workspace: Path, agent_id: str = ANSWER_AGENT) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = CLAIM.main(
                [
                    "--project-root", str(workspace),
                    "claim", "--slug", QUESTION_SLUG,
                    "--agent-id", agent_id, "--format", "json",
                ]
            )
        self.assertEqual(0, code, stdout.getvalue())

    def run_grounding_set(
        self, workspace: Path, grounding_file: Path, *, agent_id: str = ANSWER_AGENT, extra: tuple[str, ...] = ()
    ) -> tuple[int, dict, str]:
        return self.run_resolve(
            workspace,
            "grounding", "set", "--slug", QUESTION_SLUG,
            "--from-file", str(grounding_file), "--agent-id", agent_id, *extra,
        )

    def write_answer_page(self, workspace: Path, source_id: str) -> str:
        page = workspace / "wiki" / "synthesis" / "supplier-price.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            "---\n"
            "type: synthesis\n"
            "created: 2026-08-09\n"
            "updated: 2026-08-09\n"
            "source_ids:\n"
            f"  - {source_id}\n"
            "summary: The supplier quotes 23.99 EUR for B0ABC12345.\n"
            "---\n\n"
            "# Supplier Price\n\nThe supplier quotes 23.99 EUR for B0ABC12345.\n",
            encoding="utf-8",
        )
        return page.relative_to(workspace).as_posix()

    def run_answer(
        self,
        workspace: Path,
        source_id: str,
        *,
        answer_page: str | None = None,
        grounding_file: Path | None = None,
        require_grounding: bool = True,
        extra: tuple[str, ...] = (),
    ) -> tuple[int, dict, str]:
        argv = [
            "answer", "--slug", QUESTION_SLUG, "--agent-id", ANSWER_AGENT,
            "--answer-page", answer_page or self.write_answer_page(workspace, source_id),
            "--source-id", source_id,
        ]
        if require_grounding:
            argv.append("--require-grounding")
        if grounding_file is not None:
            argv.extend(["--grounding-file", str(grounding_file)])
        return self.run_resolve(workspace, *argv, *extra)

    def run_quote_verify_write(self, workspace: Path) -> tuple[int, dict, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = VERIFY_QUOTES.main(
                [
                    "--project-root", str(workspace), "--slug", QUESTION_SLUG,
                    "--format", "json", "--write", "--verified-by", VERIFIER_AGENT,
                ]
            )
        raw = stdout.getvalue()
        return int(code or 0), (json.loads(raw) if raw.strip() else {}), stderr.getvalue()

    def run_readiness(self, workspace: Path) -> tuple[int, dict, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = READINESS.main(["--project-root", str(workspace), "--format", "json"])
        return int(code or 0), json.loads(stdout.getvalue()), stderr.getvalue()

    # -- helpers -----------------------------------------------------------------

    def high_findings(self, results: dict) -> list[str]:
        return sorted(issue["category"] for issue in results["issues"] if issue["severity"] == "HIGH")

    def grounding_results(self, report: dict) -> list[dict]:
        return report["questions"][0]["grounding"]


class AnchorGroundingChainTests(AnchorGroundingWorkspace, unittest.TestCase):
    """The whole CR-7 loop, one stage at a time, each asserted before the next runs."""

    def test_structured_delivery_becomes_anchor_grounded_evidence_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, source_id = self.make_anchor_workspace(root)

            # 1. The normalizer wrote the sidecar and the record binds it by digest. The
            #    binding is the whole trust story: without it the sidecar is an
            #    unattested file that happens to sit next to a record.
            sidecar = self.structured_sidecar(workspace, source_id)
            self.assertTrue(sidecar.is_file())
            frontmatter = self.record_frontmatter(workspace, source_id)
            binding = frontmatter["structured_view"]
            self.assertEqual(sidecar.name, Path(binding["path"]).name)
            self.assertEqual(
                hashlib.sha256(sidecar.read_bytes()).hexdigest(),
                binding["content_hash"].removeprefix("sha256:"),
            )
            view = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual("23.99 EUR", view["supplier_quote"]["price"])

            # 2. The contract validator treats the sidecar as part of the record.
            code, verify_report, stderr = self.run_contract_verify(workspace)
            self.assertEqual(VERIFY_CONTRACT.EXIT_OK, code, stderr)
            entry = next(
                record for record in verify_report["records"] if record["source_id"] == source_id
            )
            self.assertEqual("verified", entry["result"])
            self.assertIs(True, entry["structured_view"]["declared"])
            self.assertIs(True, entry["structured_view"]["verified"])

            # 3. `grounding set` is the supported write path: the block lands canonical,
            #    and the envelope says plainly that nothing was verified by writing it.
            self.claim_question(workspace)
            grounding_file = self.grounding_file(root, [self.anchor_entry(source_id)])
            code, written, stderr = self.run_grounding_set(workspace, grounding_file)
            self.assertEqual(0, code, stderr)
            self.assertEqual({"quote": 0, "anchor": 1}, written["by_form"])
            self.assertEqual("not_performed", written["verification"])
            self.assertIn("verify_quotes.py", written["remediation"])
            self.assertIn(
                canonical_grounding_block(source_id),
                self.question_page(workspace).read_text(encoding="utf-8"),
            )

            # 4. Answering with the same file verifies the file's own entries and lands
            #    grounding and status in one write, reporting the split it recorded.
            #    (That the verification comes *first* is what `AnchorFailurePathTests`
            #    and criterion 1 below pin, where there is a refusal to observe.)
            code, answered, stderr = self.run_answer(workspace, source_id, grounding_file=grounding_file)
            self.assertEqual(0, code, stderr)
            self.assertEqual("answered", answered["status"])
            self.assertEqual({"quote": 0, "anchor": 1}, answered["by_form"])

            # 5. The standalone verifier stamps the page it just checked.
            code, verified, stderr = self.run_quote_verify_write(workspace)
            self.assertEqual(0, code, stderr)
            result = self.grounding_results(verified)[0]
            self.assertEqual("verified", result["result"])
            self.assertEqual("anchor", result["form"])
            self.assertEqual("/supplier_quote/price", result["pointer"])
            self.assertEqual("23.99 EUR", result["resolved"])
            self.assertEqual("structured_anchor_evidence", result["policy"])
            self.assertIn(
                self.structured_sidecar(workspace, source_id).name,
                " ".join(result["artifacts"]),
            )
            page_frontmatter = yaml.safe_load(
                self.question_page(workspace).read_text(encoding="utf-8").split("---\n", 2)[1]
            )
            self.assertEqual(VERIFIER_AGENT, page_frontmatter["verified_by"])

            # 6. Every reporting surface agrees about what the workspace holds.
            results = self.run_lint(workspace)
            export = EXPORT.build_export(workspace, None)
            status = STATUS.build_status_document(workspace)
            controller_slugs = CONTROLLER.answered_grounded_slugs(export)

        self.assertEqual([], self.high_findings(results))
        self.assertEqual(0, results["stats"]["grounding_entries_quote"])
        self.assertEqual(1, results["stats"]["grounding_entries_anchor"])
        exported = export["questions"][0]
        # The page's own entry, unsummarized, beside the verifier's verdict on it.
        self.assertEqual(
            {"pointer": ANCHOR_POINTER, "expected": ANCHOR_EXPECTED}, exported["grounding"][0]["anchor"]
        )
        self.assertEqual("anchor", exported["grounding"][0]["form"])
        self.assertTrue(exported["grounding_verification"]["all_verified"])
        self.assertEqual({"quote": 0, "anchor": 1}, exported["grounding_verification"]["by_form"])
        # The controller reaches the verifier by "this answered question declared
        # grounding", never by what form the grounding took — so an anchor-only question
        # is scheduled for verification exactly as a quoted one is.
        self.assertEqual([QUESTION_SLUG], controller_slugs)
        self.assertTrue(status["workspace_health"]["materially_valid"], status["workspace_health"])

    def test_the_persisted_quote_report_has_one_schema_whether_or_not_anything_was_grounded(self):
        """The controller's empty-report template and a real report are the same document.

        The controller persists `runs/<id>/evaluation/quote-verification.json` from one of
        two branches. A host reading `counts.by_form` to measure its migration must not
        have to discover that the file sometimes omits it and read the omission as "no
        anchors" rather than "no questions".
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, source_id = self.make_anchor_workspace(root)
            self.claim_question(workspace)
            grounding_file = self.grounding_file(
                root, [self.anchor_entry(source_id), self.quote_entry(source_id)]
            )
            code, _, stderr = self.run_answer(workspace, source_id, grounding_file=grounding_file)
            self.assertEqual(0, code, stderr)
            code, real, verify_stderr = self.run_quote_verify(workspace)
            empty = CONTROLLER.empty_quote_verification_report()

        self.assertEqual(0, code, verify_stderr)
        self.assertEqual(sorted(empty["counts"]), sorted(real["counts"]), verify_stderr)
        self.assertEqual(sorted(empty["counts"]["by_form"]), sorted(real["counts"]["by_form"]))
        self.assertEqual({"quote": 0, "anchor": 0}, empty["counts"]["by_form"])
        self.assertEqual({"quote": 1, "anchor": 1}, real["counts"]["by_form"])


TABLE_COLUMNS = ("date", "price", "note")
TABLE_ROWS = NORMALIZE.TABLE_SAMPLE_ROWS * 2
# Inside the rendered sample, so the body shows this row — ellipsized. Outside it lives
# whole in the structured view, which is the cell-cap half of the quotability gap.
TABLE_LONG_CELL_ROW = 3
# Past the sample, so the body never shows this row at all: the row-cap half.
TABLE_DEEP_ROW = NORMALIZE.TABLE_SAMPLE_ROWS + 15
# No commas: this is a real cell in a real comma-delimited file, and a quoted field would
# make the fixture about `csv` quoting rather than about the rendering cap.
TABLE_LONG_NOTE = (
    "supplier confirmed the quoted price by email and stated that no rebate or "
    "promotional credit applies to this order line"
)
TABLE_START = datetime.date(2026, 7, 1)


def table_row(index: int) -> tuple[str, str, str]:
    date = (TABLE_START + datetime.timedelta(days=index)).isoformat()
    note = TABLE_LONG_NOTE if index == TABLE_LONG_CELL_ROW else f"routine daily quote {index}"
    return date, f"{20 + index / 100:.2f}", note


def price_history_csv(rows: int = TABLE_ROWS) -> str:
    lines = [",".join(TABLE_COLUMNS)]
    lines.extend(",".join(table_row(index)) for index in range(rows))
    return "\n".join(lines) + "\n"


class NativeTableAnchorTests(AnchorGroundingWorkspace, unittest.TestCase):
    """No adapter anywhere: the package itself is the second producer of the sidecar.

    `normalize_table_record` already streams every row of a CSV, then renders 20 of them
    with cells ellipsized at 80 characters. Everything past those caps has been citable
    and permanently unquotable — the gap `normalized-source-format.md` names and CR-7 was
    filed to close. These tests are that closure stated as a workspace fact rather than a
    documentation promise, in both directions: what the caps drop is anchorable, and a
    table that cannot be addressed faithfully is not anchorable at all.
    """

    def make_table_workspace(self, root: Path, text: str) -> tuple[Path, str]:
        workspace = self.init_workspace(root)
        config_path = workspace / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        # No `normalization` key at all, and none added: no adapter is configured
        # anywhere, so whatever the record carries, the native tabular path produced.
        self.assertNotIn("normalization", config)
        config["raw"]["source_roots"] = sorted({*config["raw"]["source_roots"], "raw/data"})
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "price-history.csv").write_text(text, encoding="utf-8")
        (destination / "price-history.csv.provenance.yml").write_text(
            "origin_url: https://api.keepa.test/history/B0ABC12345\n"
            "license: CC-BY-4.0\n"
            "retrieved_at: 2026-08-08T12:00:00Z\n"
            "retrieved_by: fixture-agent/keepa\n",
            encoding="utf-8",
        )
        self.run_inventory(workspace)
        table_records = [
            record for record in self.manifest(workspace).values() if record["kind"] == "table"
        ]
        self.assertEqual(1, len(table_records), self.manifest(workspace))
        code, report, stderr = self.run_normalize(workspace)
        self.assertEqual(0, code, stderr)
        self.assertEqual(0, report["summary"]["methods"].get("adapter", 0))
        self.assertEqual(1, report["summary"]["methods"]["tables"])
        return workspace, table_records[0]["id"]

    def ground_and_verify(
        self, workspace: Path, root: Path, source_id: str, entries: list[dict], *, name: str = "grounding.yml"
    ) -> tuple[int, dict, str]:
        """Write the block through the supported path, then ask the verifier about it."""
        grounding_file = self.grounding_file(root, entries, name=name)
        code, _, stderr = self.run_grounding_set(workspace, grounding_file)
        self.assertEqual(0, code, stderr)
        return self.run_quote_verify(workspace)

    def test_a_plain_csv_anchors_rows_and_cells_the_rendered_body_can_never_quote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, source_id = self.make_table_workspace(root, price_history_csv())
            self.claim_question(workspace)

            # The sidecar holds every row, verbatim: no 20-row sample, no 80-character
            # ellipsis, no type inference. The body holds neither of the two rows below.
            view = json.loads(self.structured_sidecar(workspace, source_id).read_text(encoding="utf-8"))
            self.assertEqual(list(TABLE_COLUMNS), view["columns"])
            self.assertEqual(TABLE_ROWS, len(view["rows"]))
            deep_date, deep_price, _ = table_row(TABLE_DEEP_ROW)
            body = self.record_body(workspace, source_id)
            self.assertGreater(len(TABLE_LONG_NOTE), NORMALIZE.TABLE_MAX_CELL_CHARS)
            self.assertNotIn(deep_price, body)
            self.assertNotIn(TABLE_LONG_NOTE, body)
            # The long cell *is* in the body, but only as the truncation the reader sees;
            # a quote of what the file says would not be found there.
            self.assertIn(TABLE_LONG_NOTE[: NORMALIZE.TABLE_MAX_CELL_CHARS - 1] + "…", body)

            deep_entry = {
                "claim": f"The supplier quoted {deep_price} on {deep_date}.",
                "source_id": source_id,
                "anchor": {"pointer": f"rows/{TABLE_DEEP_ROW}/price", "expected": deep_price},
            }
            long_cell_entry = {
                "claim": "The supplier stated that no rebate applies.",
                "source_id": source_id,
                "anchor": {"pointer": f"rows/{TABLE_LONG_CELL_ROW}/note", "expected": TABLE_LONG_NOTE},
            }
            code, report, stderr = self.ground_and_verify(
                workspace, root, source_id, [deep_entry, long_cell_entry]
            )
            results = self.grounding_results(report)

            # The same two values in quote form, which is all the workspace had before
            # CR-7: both refuse, because the rendered body is where a quote is looked for
            # and neither value survived the rendering.
            unquotable_code, unquotable, unquotable_stderr = self.ground_and_verify(
                workspace,
                root,
                source_id,
                [
                    {"claim": deep_entry["claim"], "source_id": source_id, "quote": f"{deep_date} | {deep_price}"},
                    {"claim": long_cell_entry["claim"], "source_id": source_id, "quote": TABLE_LONG_NOTE},
                ],
                name="quotes.yml",
            )

        self.assertEqual(0, code, stderr)
        self.assertEqual(["verified", "verified"], [result["result"] for result in results])
        self.assertEqual(deep_price, results[0]["resolved"])
        self.assertEqual(f"/rows/{TABLE_DEEP_ROW}/price", results[0]["pointer"])
        self.assertEqual(TABLE_LONG_NOTE, results[1]["resolved"])
        self.assertEqual(VERIFY_QUOTES.EXIT_NOT_VERIFIED, unquotable_code, unquotable_stderr)
        self.assertEqual(
            ["quote_not_found", "quote_not_found"],
            [result["result"] for result in self.grounding_results(unquotable)],
        )

    def test_a_table_that_cannot_be_addressed_faithfully_emits_no_sidecar_and_says_why(self):
        """Fail-closed, and nothing else about the record changes.

        A duplicate column name makes `rows/41/price` ambiguous and a ragged row makes
        every row index a guess, so neither table earns a structured view. What that
        costs is exactly nothing a reader had: the record still renders, still verifies
        against the contract, and still grounds a quote from its sample — the behaviour
        every tabular source had before CR-7.
        """
        cases = {
            "duplicate header": (
                "date,price,price\n2026-07-01,20.00,21.00\n",
                "the header repeats column name(s): price",
            ),
            "ragged row": (
                "date,price,note\n2026-07-01,20.00\n2026-07-02,20.01,ok\n",
                "1 row(s) do not match the 3-column header",
            ),
            "empty column name": (
                "date,,note\n2026-07-01,20.00,ok\n",
                "the header has an empty column name",
            ),
        }
        for label, (text, reason) in cases.items():
            with self.subTest(table=label):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    workspace, source_id = self.make_table_workspace(root, text)
                    frontmatter = self.record_frontmatter(workspace, source_id)
                    sidecar_exists = self.structured_sidecar(workspace, source_id).exists()
                    body = self.record_body(workspace, source_id)
                    code, contract, contract_stderr = self.run_contract_verify(workspace)
                    self.claim_question(workspace)
                    quote_code, quotes, quote_stderr = self.ground_and_verify(
                        workspace,
                        root,
                        source_id,
                        [
                            {
                                "claim": "The first quoted day is 2026-07-01.",
                                "source_id": source_id,
                                "quote": "2026-07-01",
                            }
                        ],
                    )

                self.assertFalse(sidecar_exists)
                # Null, not a partial block: the record template writes every field, and
                # a `structured_view` carrying only a path would still be a declaration
                # the reader would then have to decide the meaning of.
                self.assertIsNone(frontmatter["structured_view"])
                self.assertIn(
                    f"raw/data/price-history.csv: no structured view emitted: {reason}",
                    frontmatter["parse_warnings"],
                )
                # Otherwise the record is what it always was: the same columns line and
                # the same rendered sample a reader could already quote from.
                self.assertIn("Columns (3): ", body)
                self.assertIn("Sample rows (first ", body)
                self.assertEqual(VERIFY_CONTRACT.EXIT_OK, code, contract_stderr)
                self.assertEqual("verified", contract["records"][0]["result"])
                self.assertEqual(0, quote_code, quote_stderr)
                self.assertEqual("verified", self.grounding_results(quotes)[0]["result"])

    def test_an_anchor_against_a_table_with_no_sidecar_refuses_per_entry(self):
        """The other side of the same rule: no sidecar, no anchor form — never a fallback.

        A record without a structured view is not resolved against its raw bytes or its
        rendered body; the entry fails with the result that names what is missing.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, source_id = self.make_table_workspace(
                root, "date,price,price\n2026-07-01,20.00,21.00\n"
            )
            self.claim_question(workspace)
            entry = {
                "claim": "The supplier quoted 20.00 on 2026-07-01.",
                "source_id": source_id,
                "anchor": {"pointer": "rows/0/price", "expected": "20.00"},
            }
            code, report, stderr = self.ground_and_verify(workspace, root, source_id, [entry])

        self.assertEqual(VERIFY_QUOTES.EXIT_NOT_VERIFIED, code, stderr)
        self.assertEqual("structured_view_missing", self.grounding_results(report)[0]["result"])


class AnchorGroundedWorkspaceShipsTests(AnchorGroundingWorkspace, unittest.TestCase):
    """An anchor-grounded answer is publishable, not merely verifiable.

    Lint (CR-7 T9) and the readiness gate (T10) landed on separate branches, and until
    both were merged an anchor-grounded question tripped lint's HIGH
    `question_grounding_missing` — which readiness reads as `no_ship`. So no workspace
    grounded by anchor could reach `ship`, and neither branch could hold the test that
    said so. This is that test: the two halves connected, in a real workspace.

    `coverage_required: true` is load-bearing rather than decoration — it is the only
    condition under which lint raises that HIGH finding at all, so a workspace without it
    would pass this test even with the defect present.
    """

    def publishable_workspace(self, root: Path) -> tuple[Path, str]:
        """The delivery loop end to end, stopping just before the question is answered."""
        workspace = self.init_workspace(root)
        self.enable_structured_adapter(root, workspace)
        self.deliver_payload(workspace)
        request_id = self.block_the_question(workspace, kind=STRUCTURED_KIND)
        self.run_inventory(workspace)
        source_id = self.structured_record(workspace)["id"]
        code, _, stderr = self.run_normalize(workspace)
        self.assertEqual(0, code, stderr)

        # The reviewed candidate the request selected, which is what lets the coverage
        # facet's source policy pass locally instead of parking for manual review.
        candidates = workspace / "sources" / "discovery" / "candidates.jsonl"
        candidates.parent.mkdir(parents=True, exist_ok=True)
        candidates.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "candidate_id": "cand-keepa-supplier-quote",
                    "provider": "search",
                    "url": "https://api.keepa.test/product/B0ABC12345",
                    "title": "Keepa supplier quote snapshot",
                    "source_type": "api",
                    "trust_tier": "official_primary",
                    "official_source": True,
                    "recommended_action": "fetch",
                    "status": "fetched",
                    "selected_for_request_id": request_id,
                    "fetched_source_id": source_id,
                    "evidence_path": "vendor_product_spec",
                    "source_policy": "official_vendor",
                    "freshness_policy": "no_staleness_check",
                    "identity_policy": "none",
                    "reasoning": {"risk_flags": []},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        code, reopened = self.run_reopen(workspace, source_id, request_id)
        self.assertEqual(0, code, reopened)

        coverage = workspace / "sources" / "coverage" / f"{QUESTION_SLUG}.yml"
        coverage.parent.mkdir(parents=True, exist_ok=True)
        coverage.write_text(
            yaml.safe_dump(
                {
                    "schema_version": "1.0",
                    "question_slug": QUESTION_SLUG,
                    "created_at": "2026-08-09T00:00:00Z",
                    "updated_at": "2026-08-09T00:00:00Z",
                    "coverage_profile": "supplier-price",
                    "coverage_verdict": "pending",
                    "required_facets": [
                        {
                            "facet_id": "supplier-quote",
                            "description": "Current supplier quote for the ASIN.",
                            "required": True,
                            "evidence_path": "vendor_product_spec",
                            "source_policy": "official_vendor",
                            "freshness_policy": "no_staleness_check",
                            "identity_policy": "none",
                            "min_sources": 1,
                            "accepted_source_ids": [source_id],
                            "blocking_request_ids": [],
                            "facet_verdict": "pending",
                        }
                    ],
                    "optional_facets": [],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        source_note = workspace / "wiki" / "sources" / "keepa-supplier-quote.md"
        source_note.parent.mkdir(parents=True, exist_ok=True)
        source_note.write_text(
            "---\ntype: source\ncreated: 2026-08-09\nupdated: 2026-08-09\nsource_ids:\n"
            f"  - {source_id}\n---\n\n# Keepa supplier quote\n\nDelivered price snapshot.\n",
            encoding="utf-8",
        )
        self.claim_question(workspace)
        return workspace, source_id

    def test_an_anchor_grounded_answer_passes_lint_and_reaches_a_shippable_verdict(self):
        forms = {
            "anchor only": lambda source_id: [self.anchor_entry(source_id)],
            "mixed quote and anchor": lambda source_id: [
                self.anchor_entry(source_id),
                self.quote_entry(source_id),
            ],
        }
        for label, build_entries in forms.items():
            with self.subTest(grounding=label):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    workspace, source_id = self.publishable_workspace(root)
                    entries = build_entries(source_id)
                    grounding_file = self.grounding_file(root, entries)
                    code, answered, answer_stderr = self.run_answer(
                        workspace, source_id, grounding_file=grounding_file, extra=("--require-coverage",)
                    )
                    self.assertEqual(0, code, answer_stderr)
                    code, _, verify_stderr = self.run_quote_verify_write(workspace)
                    self.assertEqual(0, code, verify_stderr)

                    results = self.run_lint(workspace)
                    readiness_code, readiness, readiness_stderr = self.run_readiness(workspace)
                    page = yaml.safe_load(
                        self.question_page(workspace).read_text(encoding="utf-8").split("---\n", 2)[1]
                    )

                anchors = sum(1 for entry in entries if "anchor" in entry)
                self.assertEqual("answered", answered["status"], answer_stderr)
                # Without this the HIGH finding under test cannot fire at all, and the
                # assertions below would pass on a workspace that never exercised it.
                self.assertIs(True, page["coverage_required"])
                # Zero HIGH findings, and named rather than filtered: the finding this
                # test exists for would otherwise hide behind a severity count.
                self.assertEqual([], self.high_findings(results))
                self.assertEqual(anchors, results["stats"]["grounding_entries_anchor"])
                self.assertEqual(
                    len(entries) - anchors, results["stats"]["grounding_entries_quote"]
                )
                self.assertEqual(READINESS.VERDICT_SHIP, readiness["verdict"], readiness["reasons"])
                self.assertEqual(READINESS.EXIT_READY, readiness_code, readiness_stderr)
                self.assertEqual([], readiness["reasons"]["grounding"])


class AnchorFailurePathTests(AnchorGroundingWorkspace, unittest.TestCase):
    """Fail-closed is the point: an anchor that cannot be proved must prove nothing."""

    def test_a_mismatched_anchor_refuses_the_answer_before_any_byte_of_the_page_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, source_id = self.make_anchor_workspace(root)
            self.claim_question(workspace)
            grounding_file = self.grounding_file(
                root, [self.anchor_entry(source_id, expected="19.99 EUR")]
            )
            before = self.page_digest(workspace)
            code, payload, stderr = self.run_answer(workspace, source_id, grounding_file=grounding_file)
            after = self.page_digest(workspace)

        self.assertEqual(RESOLVE.EXIT_INVALID, code, stderr)
        self.assertEqual("GROUNDING_ANCHOR_INVALID", payload["error_code"])
        failure = payload["details"]["failures"][0]
        self.assertEqual("anchor_value_mismatch", failure["result"])
        self.assertEqual("23.99 EUR", failure["resolved"])
        self.assertEqual("/supplier_quote/price", failure["pointer"])
        # Not "the status is unchanged" — the whole file, byte for byte. A refusal that
        # rewrote the page identically apart from an `updated:` stamp would still be a
        # write, and the fail-closed order exists to prevent writes.
        self.assertEqual(before, after)

    def test_a_forged_sidecar_is_refused_by_the_hash_binding_before_any_value_is_compared(self):
        """The tamper is chosen to *succeed* if the binding were dropped.

        The forged sidecar states exactly what the anchor expects, so a verifier that
        compared first and checked provenance later would report `verified`. Refusing
        with `structured_view_corrupt` is what makes an anchor evidence about the record
        rather than about a file that happens to sit beside it.
        """
        forged_price = "19.99 EUR"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, source_id = self.make_anchor_workspace(root)
            self.claim_question(workspace)
            grounding_file = self.grounding_file(
                root, [self.anchor_entry(source_id, expected=forged_price)]
            )
            code, _, stderr = self.run_grounding_set(workspace, grounding_file)
            self.assertEqual(0, code, stderr)

            sidecar = self.structured_sidecar(workspace, source_id)
            forged = json.loads(sidecar.read_text(encoding="utf-8"))
            forged["supplier_quote"]["price"] = forged_price
            sidecar.write_text(json.dumps(forged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            # The premise, checked rather than assumed: the file on disk now states
            # exactly what the anchor expects, so equality alone would pass it.
            self.assertEqual(
                forged_price,
                json.loads(sidecar.read_text(encoding="utf-8"))["supplier_quote"]["price"],
            )

            code, report, verify_stderr = self.run_quote_verify(workspace)
            result = self.grounding_results(report)[0]
            # And the workspace-level validator names the same breach in its own words,
            # so the tamper is visible to an operator who never runs verification.
            contract_code, contract, contract_stderr = self.run_contract_verify(workspace)

        self.assertEqual(VERIFY_QUOTES.EXIT_NOT_VERIFIED, code, verify_stderr)
        self.assertEqual("structured_view_corrupt", result["result"])
        # No comparison happened: nothing was resolved to compare against.
        self.assertIsNone(result["resolved"])
        self.assertIn("hashes to", result["message"])
        self.assertEqual(VERIFY_CONTRACT.EXIT_NOT_VERIFIED, contract_code, contract_stderr)
        self.assertEqual(
            ["NORMALIZED_CONTRACT_STRUCTURED_VIEW_INVALID"],
            [violation["code"] for violation in contract["records"][0]["violations"]],
        )

    def test_a_declared_but_deleted_sidecar_refuses_rather_than_falling_back(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, source_id = self.make_anchor_workspace(root)
            self.claim_question(workspace)
            grounding_file = self.grounding_file(root, [self.anchor_entry(source_id)])
            code, _, stderr = self.run_grounding_set(workspace, grounding_file)
            self.assertEqual(0, code, stderr)
            self.structured_sidecar(workspace, source_id).unlink()

            code, report, verify_stderr = self.run_quote_verify(workspace)
            result = self.grounding_results(report)[0]

        self.assertEqual(VERIFY_QUOTES.EXIT_NOT_VERIFIED, code, verify_stderr)
        self.assertEqual("structured_view_missing", result["result"])
        # The value is in the rendered body and in the raw payload, and neither is
        # consulted: one resolution root, or an anchor stops saying what it proved.
        self.assertIsNone(result["resolved"])

    def test_a_pointer_into_a_subtree_is_a_different_mistake_than_a_wrong_value(self):
        """`supplier_quote` is a real facet; it is simply not a field.

        Reported apart from a mismatch because the repairs differ: one edit extends the
        pointer, the other corrects the claim.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, source_id = self.make_anchor_workspace(root)
            self.claim_question(workspace)
            cases = {
                "supplier_quote": "anchor_target_not_scalar",
                "supplier_quote/list_price": "anchor_pointer_not_found",
            }
            observed = {}
            for pointer in cases:
                grounding_file = self.grounding_file(
                    root, [self.anchor_entry(source_id, pointer=pointer)], name=f"{pointer.replace('/', '-')}.yml"
                )
                code, _, stderr = self.run_grounding_set(workspace, grounding_file)
                self.assertEqual(0, code, stderr)
                code, report, stderr = self.run_quote_verify(workspace)
                observed[pointer] = (code, self.grounding_results(report)[0]["result"])

        self.assertEqual(
            {pointer: (VERIFY_QUOTES.EXIT_NOT_VERIFIED, result) for pointer, result in cases.items()},
            observed,
        )


class CrSevenAcceptanceCriteriaTests(AnchorGroundingWorkspace, unittest.TestCase):
    """One named test per CR-7 acceptance criterion, so criterion and test read as one.

    The chain tests above walk the loop; these state the promises the CR was accepted on,
    each in a name a reader can match to the change request without cross-referencing a
    backlog. Overlap with the chain is deliberate — a criterion nobody can point at a test
    for is a criterion nobody checked.
    """

    def test_criterion_1_an_anchor_that_resolves_and_matches_verifies_and_a_failing_one_names_the_entry_before_terminal_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, source_id = self.make_anchor_workspace(root)
            self.claim_question(workspace)

            # Resolves and matches -> verified, with the pointer it walked and the value
            # it found, so the report says what was proved rather than that it passed.
            good = self.grounding_file(root, [self.anchor_entry(source_id)], name="good.yml")
            code, _, stderr = self.run_grounding_set(workspace, good)
            self.assertEqual(0, code, stderr)
            code, report, stderr = self.run_quote_verify(workspace)
            self.assertEqual(0, code, stderr)
            verified = self.grounding_results(report)[0]

            # Non-resolving and mismatched, each a stable code naming the failing entry,
            # and each refused before the terminal status is written.
            refusals = {}
            for label, entry in {
                "mismatch": self.anchor_entry(source_id, expected="19.99 EUR"),
                "not_found": self.anchor_entry(source_id, pointer="supplier_quote/rrp"),
            }.items():
                failing = self.grounding_file(root, [entry], name=f"{label}.yml")
                before = self.page_digest(workspace)
                code, payload, stderr = self.run_answer(workspace, source_id, grounding_file=failing)
                refusals[label] = {
                    "code": code,
                    "error_code": payload["error_code"],
                    "failure": payload["details"]["failures"][0],
                    "page_unchanged": before == self.page_digest(workspace),
                    "status": self.question_status(workspace),
                }

        self.assertEqual("verified", verified["result"])
        self.assertEqual("anchor", verified["form"])
        self.assertEqual("/supplier_quote/price", verified["pointer"])
        self.assertEqual(ANCHOR_EXPECTED, verified["resolved"])
        for label, refusal in refusals.items():
            with self.subTest(refusal=label):
                self.assertEqual(RESOLVE.EXIT_INVALID, refusal["code"])
                self.assertEqual("GROUNDING_ANCHOR_INVALID", refusal["error_code"])
                # Named, not merely counted: the failure carries the claim and the source
                # of the entry a host has to go and repair.
                self.assertEqual(ANCHOR_CLAIM, refusal["failure"]["claim"])
                self.assertEqual(source_id, refusal["failure"]["source_id"])
                # Before terminal state, in both senses: no status transition happened,
                # and no byte of the page moved.
                self.assertEqual("in_progress", refusal["status"])
                self.assertTrue(refusal["page_unchanged"])
        self.assertEqual(
            {"mismatch": "anchor_value_mismatch", "not_found": "anchor_pointer_not_found"},
            {label: refusal["failure"]["result"] for label, refusal in refusals.items()},
        )

    def test_criterion_2_the_grounding_file_writes_the_canonical_block_and_refuses_a_different_claim_holder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, source_id = self.make_anchor_workspace(root)
            grounding_file = self.grounding_file(root, [self.anchor_entry(source_id)])
            # The host's file is not already canonical, so "the page ends up canonical"
            # is a statement about the write path rather than about the input.
            self.assertNotIn(
                canonical_grounding_block(source_id), grounding_file.read_text(encoding="utf-8")
            )

            # Unclaimed and unheld, the write needs the explicit flag; held by someone
            # else, no flag helps. Grounding is a mutation of a claimed question, and the
            # write path is under exactly the claim discipline every other mutation is.
            unclaimed_code, unclaimed, _ = self.run_grounding_set(workspace, grounding_file)
            self.claim_question(workspace, agent_id="holding-agent")
            before_refusals = self.page_digest(workspace)
            held_code, held, _ = self.run_grounding_set(workspace, grounding_file)
            stolen_code, stolen, _ = self.run_grounding_set(
                workspace, grounding_file, extra=("--allow-unclaimed",)
            )
            page_after_refusals = self.page_digest(workspace)

            # Under the holder's own id, the block lands byte-identical to what a
            # compliant hand edit writes — the host's own YAML dumper never reaches the
            # page, so its key order and quoting cannot become the workspace's.
            code, written, stderr = self.run_grounding_set(
                workspace, grounding_file, agent_id="holding-agent"
            )
            self.assertEqual(0, code, stderr)
            page = self.question_page(workspace).read_text(encoding="utf-8")
            reloaded = yaml.safe_load(page.split("---\n", 2)[1])["grounding"]

        self.assertEqual(RESOLVE.EXIT_INVALID, unclaimed_code)
        self.assertEqual("QUESTION_NOT_CLAIMED", unclaimed["error_code"])
        self.assertEqual(RESOLVE.EXIT_CONFLICT, held_code)
        self.assertEqual("CLAIM_HELD", held["error_code"])
        # `--allow-unclaimed` covers an unheld question, never another agent's hold.
        self.assertEqual(RESOLVE.EXIT_CONFLICT, stolen_code)
        self.assertEqual("CLAIM_HELD", stolen["error_code"])
        self.assertEqual(before_refusals, page_after_refusals)
        self.assertIn(canonical_grounding_block(source_id), page)
        # And the canonical bytes reload as the entry the host handed over, unchanged.
        self.assertEqual([self.anchor_entry(source_id)], reloaded)
        self.assertEqual({"quote": 0, "anchor": 1}, written["by_form"])

    def test_criterion_3_lint_counts_grounding_by_anchor_apart_from_grounding_by_quote(self):
        mixtures = {
            "quote only": (0, 1),
            "anchor only": (1, 0),
            "both forms": (1, 1),
        }
        for label, (anchors, quotes) in mixtures.items():
            with self.subTest(mixture=label):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    workspace, source_id = self.make_anchor_workspace(root)
                    self.claim_question(workspace)
                    entries = [self.anchor_entry(source_id)] * anchors + [self.quote_entry(source_id)] * quotes
                    grounding_file = self.grounding_file(root, entries)
                    code, _, stderr = self.run_answer(
                        workspace, source_id, grounding_file=grounding_file
                    )
                    self.assertEqual(0, code, stderr)
                    results = self.run_lint(workspace)
                    summary = LINT.format_grounding_summary(results["stats"])

                self.assertEqual(anchors, results["stats"]["grounding_entries_anchor"])
                self.assertEqual(quotes, results["stats"]["grounding_entries_quote"])
                # The counts are what a workspace measures its own migration with, so the
                # line lint writes to log.md has to name both forms, never one total.
                self.assertEqual(f"quote={quotes} anchor={anchors}", summary)

    def test_criterion_4_quote_form_is_verified_exactly_as_it_was_before_anchors_existed(self):
        """Backward compatibility, including which code a quote failure still raises.

        A host that switched on `GROUNDING_QUOTE_INVALID` before CR-7 must keep seeing it
        for a quote failure; `GROUNDING_ANCHOR_INVALID` is additive, never a rename.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, source_id = self.make_anchor_workspace(root)

            # The pre-CR-7 shape, hand-written onto the page exactly as a host wrote it
            # then — no anchor key anywhere near it.
            self.ground_the_question(workspace, source_id, quote=QUOTE_TEXT, location_hint=QUOTE_HINT)
            code, report, stderr = self.run_quote_verify(workspace)
            result = self.grounding_results(report)[0]

            self.claim_question(workspace)
            failing = self.grounding_file(
                root,
                [{"claim": QUOTE_CLAIM, "source_id": source_id, "quote": "offer_count: 8"}],
            )
            refusal_code, refusal, refusal_stderr = self.run_answer(
                workspace, source_id, grounding_file=failing
            )

        self.assertEqual(0, code, stderr)
        self.assertEqual("verified", result["result"])
        self.assertEqual("quote", result["form"])
        self.assertEqual("retained_quote_evidence", result["policy"])
        # A quote entry still reports the body locator it was checked against, and still
        # carries no pointer: the two forms report what each actually cited.
        self.assertEqual("section", result["anchor"]["type"])
        self.assertNotIn("pointer", result)
        self.assertEqual({"quote": 1, "anchor": 0}, report["counts"]["by_form"])
        self.assertEqual(RESOLVE.EXIT_INVALID, refusal_code, refusal_stderr)
        self.assertEqual("GROUNDING_QUOTE_INVALID", refusal["error_code"])
        self.assertEqual("quote_not_found", refusal["details"]["failures"][0]["result"])


if __name__ == "__main__":
    unittest.main()
