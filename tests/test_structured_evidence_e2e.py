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
"""

import contextlib
import importlib.util
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


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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

    def deliver_payload(self, workspace: Path) -> None:
        """A delivery per docs/source-delivery.md: the artifact plus its sidecar."""
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PAYLOAD, destination / PAYLOAD.name)
        shutil.copy2(
            PAYLOAD.with_name(PAYLOAD.name + ".provenance.yml"),
            destination / (PAYLOAD.name + ".provenance.yml"),
        )

    def make_workspace(self, root: Path, *, adapter: bool = True) -> Path:
        workspace = self.init_workspace(root)
        if adapter:
            self.enable_adapter(workspace)
        else:
            config_path = workspace / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["raw"]["source_roots"] = sorted({*config["raw"]["source_roots"], "raw/data"})
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        self.deliver_payload(workspace)
        return workspace

    # -- pipeline stages ---------------------------------------------------------

    def run_inventory(self, workspace: Path) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = INVENTORY.main(["--project-root", str(workspace), "--format", "json"])
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

    def block_the_question(self, workspace: Path) -> str:
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
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = REQUESTS.main(
                [
                    "--project-root", str(workspace),
                    "add", "--kind", "dataset",
                    "--query-or-identifier", "keepa product B0ABC12345",
                    "--rationale", "No price evidence in the workspace.",
                    "--question-slug", QUESTION_SLUG, "--format", "json",
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


if __name__ == "__main__":
    unittest.main()
