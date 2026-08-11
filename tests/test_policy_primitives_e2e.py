"""CR-9 end to end: a domain pack's own declarations decide its own evidence policies.

Before CR-9 every ``pack:<pack-name>/<policy-id>`` policy evaluated to ``manual_review``,
including checks that were entirely deterministic over data the workspace already held.
The other CR-9 suites test one leg each — the primitives in isolation, the evaluator seam,
``pack validate``, the capability contract. This one walks what a host actually depends on,
in a workspace built the way an operator builds one:

    declare the pack -> deliver a quote with its provenance and structured view
      -> evaluate coverage -> answer under --require-coverage --require-grounding
      -> record the reviews the pack still asks for -> publication readiness

The legs are load-bearing in sequence rather than individually. A rule verdict that never
reaches the facet rollup changes no coverage verdict; a coverage verdict that never reaches
``answer`` still parks the question with a person; and a question parked with a person is
exactly the outcome CR-9 exists to stop producing for checks a subtraction can settle.

Three shapes of pack policy live in the one fixture workspace, because the difference
between them has to be the declaration and nothing else:

- ``quote-is-current`` — every policy on its facet carries a rule, so it answers with no
  human in the loop and its workspace reaches ``ship``.
- ``supplier-is-approved`` — one policy whose rules all pass but which sets
  ``manual_review_required``, beside one the pack never gave a rule at all. Both park the
  question, and both are cleared by CR-1's recorded-review path.
- ``archived-quote-is-current`` — the same rules over a 50-hour-old delivery, which the
  pack's ``max_age: 48h`` bound refuses until the quote is redelivered.

The fixture is ``tests/fixtures/policy-primitives-workspace``, staged over a workspace the
real initializer built. **It ships ages, never instants**: each provenance sidecar declares
``retrieved_at_hours_ago`` and :meth:`PolicyPrimitivesWorkspace.deliver` turns that into a
``retrieved_at`` against the caller's clock. A fixture that baked the timestamp would begin
failing on its own two days after it was written, which would report the passage of time
and a real regression as the same red test.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "policy-primitives-workspace"
PROFILE_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "workspace-init-profile.yml"

#: The fixture's own key, stripped from the staged workspace once it has been spent.
AGE_KEY = "retrieved_at_hours_ago"
#: The `domain_pack` fragment merged into the initializer's research.yml, then deleted.
OVERLAY_NAME = "research-overlay.yml"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INIT = load_script_module("e2e_primitives_init", "init_research_workspace.py")
CLAIM = load_script_module("e2e_primitives_claim", "question_claim.py")
RESOLVE = load_script_module("e2e_primitives_resolve", "question_resolve.py")
COVERAGE = load_script_module("e2e_primitives_coverage", "coverage_manifest.py")
VERIFY_QUOTES = load_script_module("e2e_primitives_verify_quotes", "verify_quotes.py")
LINT = load_script_module("e2e_primitives_lint", "lint.py")
STATUS = load_script_module("e2e_primitives_status", "workspace_status.py")
READINESS = load_script_module("e2e_primitives_readiness", "publication_readiness.py")
EXPORT = load_script_module("e2e_primitives_export", "export_answers.py")
MCP = load_script_module("e2e_primitives_mcp", "serve_mcp.py")
# The reason prefixes are switched on by hosts, so they are read off the module that owns
# them rather than restated as prose a rewording would silently invalidate.
PRIMITIVES = load_script_module("e2e_primitives_module", "_policy_primitives.py")

PACK_NAME = "market-data"
PROVIDER_POLICY = f"pack:{PACK_NAME}/quote-provider"
FRESHNESS_POLICY = f"pack:{PACK_NAME}/quote-48h"
IDENTITY_POLICY = f"pack:{PACK_NAME}/sku-matches-candidate"
#: Rule-backed *and* flagged: every declared check passes and a person is still asked.
REVIEWED_POLICY = f"pack:{PACK_NAME}/reviewed-quote-provider"
#: Declared in the vocabulary with no rule at all — the pre-CR-9 outcome, kept reachable.
DEFINITION_ONLY_POLICY = f"pack:{PACK_NAME}/supplier-contract-in-force"
ALL_PACK_POLICIES = (
    PROVIDER_POLICY,
    FRESHNESS_POLICY,
    IDENTITY_POLICY,
    REVIEWED_POLICY,
    DEFINITION_ONLY_POLICY,
)

RULE_SLUG = "quote-is-current"
REVIEW_SLUG = "supplier-is-approved"
STALE_SLUG = "archived-quote-is-current"
ALL_SLUGS = (RULE_SLUG, REVIEW_SLUG, STALE_SLUG)

FRESH_SOURCE = "quote:supplier-fresh"
STALE_SOURCE = "quote:supplier-archived"
SOURCE_FOR_SLUG = {RULE_SLUG: FRESH_SOURCE, REVIEW_SLUG: FRESH_SOURCE, STALE_SLUG: STALE_SOURCE}

CANDIDATE_SKU = "B0ABC12345"
ALLOWED_PROVIDER = "aliexpress-ds"
#: The two ages the fixture ships, restated here because the acceptance criteria are
#: written in them. `test_the_fixture_ships_ages_rather_than_instants` keeps the two honest.
FRESH_AGE_HOURS = 2.0
STALE_AGE_HOURS = 50.0
#: The bound the pack declares. Also restated, and also pinned against the declaration.
MAX_AGE_HOURS = 48

ANSWER_AGENT = "agent-a"
REVIEWER = "ops-principal"
REVIEW_REF = "approval-queue-9"
VERIFIER = "verifier-agent"

#: The result envelope CR-9 promised not to widen: a rule verdict is an ordinary policy
#: result, so a host that reads one reads the other with no new branch.
POLICY_RESULT_KEYS = {"policy", "verdict", "source_ids", "reasons", "remediation"}


class PolicyPrimitivesWorkspace:
    """Staging and drivers shared by every scenario below.

    Deliberately not a TestCase: subclassing one that carries tests would re-run the whole
    parent suite under every child class.
    """

    # -- workspace construction --------------------------------------------------

    def init_workspace(self, root: Path, target: Path) -> None:
        """Build the workspace the shipped initializer builds, and nothing else.

        The fixture supplies evidence and declarations, never structure: a hand-written
        workspace is missing `AGENTS.md`, `scripts/` and the rest, which
        `_workspace_health` reports as `publication_blocked` — so no fixture-only workspace
        could ever reach the ship verdict this suite has to be able to observe.
        """
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text(encoding="utf-8"))
        profile["workspace_init"]["target_path"] = str(target)
        profile_path = root / "profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = INIT.main(["--profile", str(profile_path)])
        self.assertEqual(0, int(code or 0))

    def apply_overlay(self, workspace: Path, *, escalation_scope: str | None) -> None:
        """Merge the pack declaration into the initializer's config, then drop the fragment.

        Merged rather than replaced: everything except `domain_pack` stays exactly what the
        shipped starter writes, so a starter change surfaces here as a failure instead of
        being masked by a fixture copy of research.yml.
        """
        overlay_path = workspace / OVERLAY_NAME
        overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
        overlay_path.unlink()
        config_path = workspace / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config.update(overlay)
        if escalation_scope is not None:
            config["review"] = {"escalation_scope": escalation_scope, "max_pending_review_hours": 168}
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def keep_only(self, workspace: Path, slugs: tuple[str, ...]) -> None:
        """Drop the questions a scenario is not about, with their manifests and answers.

        Publication readiness is a workspace-wide verdict, so a scenario that has to reach
        one can only carry questions it intends to resolve.
        """
        for directory, suffix in (
            ("wiki/questions", ".md"),
            ("wiki/outputs", ".md"),
            ("sources/coverage", ".yml"),
        ):
            for path in sorted((workspace / directory).glob(f"*{suffix}")):
                if path.stem in ALL_SLUGS and path.stem not in slugs:
                    path.unlink()

    def stage_workspace(
        self,
        root: Path,
        *,
        slugs: tuple[str, ...] = ALL_SLUGS,
        escalation_scope: str | None = None,
    ) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        workspace = root / "policy-primitives-workspace"
        self.init_workspace(root, workspace)
        shutil.copytree(FIXTURE, workspace, dirs_exist_ok=True)
        self.apply_overlay(workspace, escalation_scope=escalation_scope)
        self.keep_only(workspace, slugs)
        for record in self.manifest_records(workspace):
            self.deliver(workspace, record["id"])
        return workspace

    # -- deliveries --------------------------------------------------------------

    def manifest_records(self, workspace: Path) -> list[dict[str, Any]]:
        path = workspace / "sources" / "manifest.jsonl"
        return [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]

    def deliver(self, workspace: Path, source_id: str, *, age_hours: float | None = None) -> str:
        """Stamp one delivery's `retrieved_at` from an age, and return the instant written.

        Called once per source while staging, spending the fixture's declared age, and
        again by hand when a scenario redelivers a quote — which is the only repair a
        `max_age` failure has.
        """
        records = self.manifest_records(workspace)
        record = next(item for item in records if item["id"] == source_id)
        sidecar = workspace / f"{record['raw_paths'][0]}.provenance.yml"
        document = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
        declared = document.pop(AGE_KEY, None)
        hours = declared if age_hours is None else age_hours
        self.assertIsNotNone(hours, f"{source_id} has no declared age and none was supplied")
        stamp = (datetime.now(timezone.utc) - timedelta(hours=float(hours))).strftime("%Y-%m-%dT%H:%M:%SZ")
        document["retrieved_at"] = stamp
        sidecar.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        record["detected_at"] = stamp
        (workspace / "sources" / "manifest.jsonl").write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in records), encoding="utf-8"
        )
        return stamp

    def provenance_sidecar(self, workspace: Path, source_id: str) -> Path:
        record = next(item for item in self.manifest_records(workspace) if item["id"] == source_id)
        return workspace / f"{record['raw_paths'][0]}.provenance.yml"

    # -- command drivers ---------------------------------------------------------

    def run_module(self, module, argv: list[str]) -> tuple[int, dict[str, Any], str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(argv)
        raw = stdout.getvalue().strip() or stderr.getvalue().strip()
        return int(code or 0), (json.loads(raw) if raw else {}), stderr.getvalue()

    def run_evaluate(self, workspace: Path, slug: str) -> tuple[int, dict[str, Any], str]:
        return self.run_module(
            COVERAGE,
            ["--project-root", str(workspace), "evaluate", "--slug", slug, "--format", "json"],
        )

    def run_claim(self, workspace: Path, slug: str, agent_id: str = ANSWER_AGENT) -> None:
        code, payload, stderr = self.run_module(
            CLAIM,
            [
                "--project-root", str(workspace), "claim", "--slug", slug,
                "--agent-id", agent_id, "--format", "json",
            ],
        )
        self.assertEqual(0, code, stderr or payload)

    def run_answer(self, workspace: Path, slug: str, *extra: str) -> tuple[int, dict[str, Any], str]:
        return self.run_module(
            RESOLVE,
            [
                "--project-root", str(workspace), "answer", "--slug", slug,
                "--agent-id", ANSWER_AGENT,
                "--answer-page", f"wiki/outputs/{slug}.md",
                "--source-id", SOURCE_FOR_SLUG[slug],
                "--require-coverage", "--require-grounding", *extra, "--format", "json",
            ],
        )

    def claim_and_answer(self, workspace: Path, slug: str, *extra: str) -> dict[str, Any]:
        self.run_claim(workspace, slug)
        code, payload, stderr = self.run_answer(workspace, slug, *extra)
        self.assertEqual(0, code, stderr or payload)
        return payload

    def run_review(
        self, workspace: Path, slug: str, policy: str, *, verdict: str = "accepted"
    ) -> tuple[int, dict[str, Any], str]:
        return self.run_module(
            RESOLVE,
            [
                "--project-root", str(workspace), "review", "--slug", slug,
                "--policy", policy, "--verdict", verdict,
                "--reviewed-by", REVIEWER, "--review-ref", REVIEW_REF, "--format", "json",
            ],
        )

    def run_verify_quotes(self, workspace: Path, *slugs: str) -> tuple[int, dict[str, Any], str]:
        arguments = [argument for slug in slugs for argument in ("--slug", slug)]
        return self.run_module(
            VERIFY_QUOTES,
            [
                "--project-root", str(workspace), *arguments, "--format", "json",
                "--write", "--verified-by", VERIFIER,
            ],
        )

    def run_lint(self, workspace: Path) -> dict[str, Any]:
        return LINT.run_checks(workspace, LINT.load_config(workspace))

    def status_document(self, workspace: Path) -> dict[str, Any]:
        return STATUS.build_status_document(workspace)

    def run_readiness(self, workspace: Path) -> tuple[int, dict[str, Any], str]:
        return self.run_module(READINESS, ["--project-root", str(workspace), "--format", "json"])

    def mcp_tool(self, workspace: Path, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        server = MCP.ResearchWikiMcpServer(workspace)
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        result = response["result"]
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    # -- readers -----------------------------------------------------------------

    def facet_results(self, payload: dict[str, Any], index: int = 0) -> list[dict[str, Any]]:
        return payload["policy_results"]["facets"][index]["policy_results"]

    def verdicts(self, payload: dict[str, Any], index: int = 0) -> dict[str, str]:
        return {result["policy"]: result["verdict"] for result in self.facet_results(payload, index)}

    def written_manifest(self, workspace: Path, slug: str) -> dict[str, Any]:
        path = workspace / "sources" / "coverage" / f"{slug}.yml"
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def question_frontmatter(self, workspace: Path, slug: str) -> dict[str, Any]:
        text = (workspace / "wiki" / "questions" / f"{slug}.md").read_text(encoding="utf-8")
        return yaml.safe_load(text.split("---\n", 2)[1])

    def high_findings(self, results: dict[str, Any]) -> list[str]:
        return sorted(issue["category"] for issue in results["issues"] if issue["severity"] == "HIGH")

    def reason_text(self, results: list[dict[str, Any]]) -> str:
        return "\n".join(reason for result in results for reason in result["reasons"])


class PolicyPrimitivesFixtureTests(PolicyPrimitivesWorkspace, unittest.TestCase):
    """Guards on the fixture itself, so a broken fixture reports as a broken fixture.

    Everything downstream reads these files. A sidecar edited without recomputing its
    digest, or an age quietly turned into an instant, would otherwise surface as a fleet of
    unrelated failures in the scenarios below.
    """

    def test_the_fixture_ships_ages_rather_than_instants(self):
        """No committed delivery states *when* it happened, only *how old* it is."""
        ages = {}
        for name, source_id in (("fresh", FRESH_SOURCE), ("archived", STALE_SOURCE)):
            with self.subTest(source=name):
                path = FIXTURE / "raw" / "data" / f"supplier-quote-{name}.json.provenance.yml"
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertIn(AGE_KEY, document)
                self.assertNotIn("retrieved_at", document)
                ages[source_id] = float(document[AGE_KEY])
                # The provider identity a rule may read, and the agent path it may not.
                self.assertEqual(ALLOWED_PROVIDER, document["provider_registration"]["id"])
                self.assertEqual(f"fixture-agent/{ALLOWED_PROVIDER}", document["retrieved_by"])

        # No delivery instant on the manifest either; the staging helper writes it.
        for record in (
            json.loads(line)
            for line in (FIXTURE / "sources" / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ):
            self.assertNotIn("detected_at", record)

        # The ages the acceptance criteria are written in, and the bound they straddle.
        self.assertEqual({FRESH_SOURCE: FRESH_AGE_HOURS, STALE_SOURCE: STALE_AGE_HOURS}, ages)
        overlay = yaml.safe_load((FIXTURE / OVERLAY_NAME).read_text(encoding="utf-8"))
        rule = overlay["domain_pack"]["policy_rules"][FRESHNESS_POLICY]
        self.assertEqual(MAX_AGE_HOURS, rule["all_of"][0]["max_age"]["hours"])
        self.assertLess(FRESH_AGE_HOURS, MAX_AGE_HOURS)
        self.assertGreater(STALE_AGE_HOURS, MAX_AGE_HOURS)

    def test_every_structured_view_hashes_to_the_digest_its_record_binds(self):
        """The binding is what makes a sidecar the record's own evidence.

        Edit a sidecar without recomputing this digest and every `record/...` rule fails
        closed with `structured_view_corrupt`. Naming the mistake here saves the next
        author reading that failure back through four scenarios.
        """
        normalized = FIXTURE / "sources" / "normalized"
        for record_path in sorted(normalized.glob("*.md")):
            with self.subTest(record=record_path.name):
                frontmatter = yaml.safe_load(record_path.read_text(encoding="utf-8").split("---\n", 2)[1])
                binding = frontmatter["structured_view"]
                sidecar = FIXTURE / binding["path"]
                self.assertTrue(sidecar.is_file(), binding["path"])
                data = sidecar.read_bytes()
                self.assertEqual(f"sha256:{hashlib.sha256(data).hexdigest()}", binding["content_hash"])
                # And the bytes are the canonical rendering the writer would emit, so a
                # reformatting pass over the fixture is a visible change, not a silent one.
                payload = json.loads(data.decode("utf-8"))
                canonical = json.dumps(
                    payload, ensure_ascii=False, indent=2, sort_keys=True, separators=(",", ": ")
                ) + "\n"
                self.assertEqual(canonical.encode("utf-8"), data)

    def test_the_declaration_the_fixture_ships_parses(self):
        """One rule per policy the manifests use, and exactly one policy without one."""
        overlay = yaml.safe_load((FIXTURE / OVERLAY_NAME).read_text(encoding="utf-8"))
        domain_pack = overlay["domain_pack"]
        self.assertEqual([], PRIMITIVES.declaration_errors(domain_pack))
        rules = PRIMITIVES.pack_policy_rules(overlay)
        self.assertEqual(
            sorted([PROVIDER_POLICY, FRESHNESS_POLICY, IDENTITY_POLICY, REVIEWED_POLICY]),
            sorted(rules),
        )
        self.assertTrue(rules[REVIEWED_POLICY].manual_review_required)
        self.assertFalse(rules[IDENTITY_POLICY].manual_review_required)
        declared = {
            policy
            for section in domain_pack["policy_vocabularies"].values()
            for policy in section
        }
        self.assertEqual({DEFINITION_ONLY_POLICY}, declared - set(rules))


class CoverageEvaluationChainTests(PolicyPrimitivesWorkspace, unittest.TestCase):
    """`coverage_manifest.py evaluate`: the rule verdicts, and the manifest they rewrite."""

    def test_rule_verdicts_reach_the_report_and_the_manifest_on_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.stage_workspace(Path(tmpdir))

            code, payload, stderr = self.run_evaluate(workspace, RULE_SLUG)
            self.assertEqual(0, code, stderr)

            results = self.facet_results(payload)
            # Order and envelope both: a rule verdict is an ordinary policy result, so a
            # host that already reads one needs no new branch to read this.
            self.assertEqual([PROVIDER_POLICY, FRESHNESS_POLICY, IDENTITY_POLICY], [r["policy"] for r in results])
            self.assertEqual(["pass", "pass", "pass"], [r["verdict"] for r in results])
            for result in results:
                self.assertEqual(POLICY_RESULT_KEYS, set(result))
                self.assertIsNone(result["remediation"])

            # Each reason names the source and the field the check actually read, across
            # all three documents a rule may resolve against.
            reasons = self.reason_text(results)
            self.assertIn(f"{FRESH_SOURCE} provenance/retrieved_at", reasons)
            self.assertIn(f"max_age allows {MAX_AGE_HOURS}h.", reasons)
            self.assertIn(f"{FRESH_SOURCE} record/supplier_quote/sku", reasons)
            self.assertIn("question/metadata/candidate_sku", reasons)
            # `one_of_provenance` reads no field, so its reason carries no ref segment at
            # all — the source id runs straight into the sentence.
            self.assertIn(
                f"{FRESH_SOURCE} was delivered by provider '{ALLOWED_PROVIDER}', "
                "which one_of_provenance allows.",
                reasons,
            )
            # A passing rule-backed policy carries no failure prefix and no review at all.
            for prefix in PRIMITIVES.RULE_REASON_PREFIXES:
                self.assertNotIn(prefix, reasons)
            self.assertNotIn("manual_review", json.dumps(payload["policy_results"]))

            # The report is not the artifact: the manifest on disk carries the derived
            # verdicts, which is what every later reader of this workspace sees.
            self.assertEqual("pass", payload["coverage_verdict"])
            written = self.written_manifest(workspace, RULE_SLUG)
            self.assertEqual("pass", written["coverage_verdict"])
            self.assertEqual("pass", written["required_facets"][0]["facet_verdict"])

    def test_a_definition_only_policy_still_asks_for_a_person_beside_a_flagged_one(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.stage_workspace(Path(tmpdir))

            code, payload, stderr = self.run_evaluate(workspace, REVIEW_SLUG)

            self.assertEqual(0, code, stderr)
            self.assertEqual(
                {
                    REVIEWED_POLICY: "manual_review",
                    FRESHNESS_POLICY: "pass",
                    DEFINITION_ONLY_POLICY: "manual_review",
                },
                self.verdicts(payload),
            )
            results = {result["policy"]: result for result in self.facet_results(payload)}
            # The flagged policy says its mechanical half is settled; the definition-only
            # one has nothing mechanical to say. The two sentences are different on
            # purpose — a reader has to be able to tell "not automatable" from "not yet".
            self.assertTrue(
                results[REVIEWED_POLICY]["reasons"][0].startswith(
                    f"Domain-pack policy {REVIEWED_POLICY} satisfied every declared rule check, "
                    "but its definition still requires a recorded domain review: "
                ),
                results[REVIEWED_POLICY]["reasons"][0],
            )
            self.assertTrue(
                results[DEFINITION_ONLY_POLICY]["reasons"][0].startswith(
                    f"Domain-pack policy {DEFINITION_ONLY_POLICY} requires recorded domain review: "
                ),
                results[DEFINITION_ONLY_POLICY]["reasons"][0],
            )
            # The flagged policy's own checks are reported beside the request for review,
            # so recording it is a decision about the one thing a rule could not settle.
            flagged = self.reason_text([results[REVIEWED_POLICY]])
            self.assertIn(f"{FRESH_SOURCE} provenance/origin_url", flagged)
            self.assertIn(f"{FRESH_SOURCE} record/supplier_quote/unit_price_eur", flagged)
            # manual_review never blocks a facet; only a fail does.
            self.assertEqual("pass", payload["coverage_verdict"])

    def test_a_stale_delivery_blocks_the_facet_until_the_quote_is_redelivered(self):
        """The reopen-loop shape: refused, repaired, and passing, in one workspace."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.stage_workspace(Path(tmpdir))

            code, blocked, stderr = self.run_evaluate(workspace, STALE_SLUG)
            self.assertEqual(0, code, stderr)
            self.assertEqual("blocked", blocked["coverage_verdict"])
            self.assertEqual("blocked", blocked["manifest"]["required_facets"][0]["facet_verdict"])
            self.assertEqual(
                {PROVIDER_POLICY: "pass", FRESHNESS_POLICY: "fail", IDENTITY_POLICY: "pass"},
                self.verdicts(blocked),
            )
            freshness = next(
                result for result in self.facet_results(blocked) if result["policy"] == FRESHNESS_POLICY
            )
            self.assertTrue(
                all(
                    reason.startswith(f"{PRIMITIVES.REASON_STALE}: {STALE_SOURCE} provenance/retrieved_at")
                    for reason in freshness["reasons"]
                ),
                freshness["reasons"],
            )
            # Greppable per policy: the remediation names the declaration to look at.
            self.assertIn(f"domain_pack.policy_rules[{FRESHNESS_POLICY}]", freshness["remediation"])
            # The blocked verdict is on disk too, so a later reader is not told "pending".
            self.assertEqual("blocked", self.written_manifest(workspace, STALE_SLUG)["coverage_verdict"])

            # Redelivering the same quote inside the window is the whole repair: no
            # manifest edit, no policy change, no human.
            self.deliver(workspace, STALE_SOURCE, age_hours=1)
            code, passed, stderr = self.run_evaluate(workspace, STALE_SLUG)

            self.assertEqual(0, code, stderr)
            self.assertEqual("pass", passed["coverage_verdict"])
            self.assertEqual(["pass", "pass", "pass"], [r["verdict"] for r in self.facet_results(passed)])
            self.assertEqual("pass", self.written_manifest(workspace, STALE_SLUG)["coverage_verdict"])

    def test_the_fetching_agent_path_is_never_read_as_the_delivering_provider(self):
        """`retrieved_by` names who fetched, not where the evidence came from.

        With the registration removed and `retrieved_by` set to the allowed id verbatim,
        the delivery carries the allowlisted string and nothing else. Admitting the field
        would therefore pass it, so the `fail` below is evidence the field is not read.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.stage_workspace(Path(tmpdir))
            sidecar = self.provenance_sidecar(workspace, FRESH_SOURCE)
            document = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
            # The premise, checked rather than assumed. `retrieved_by` is set to the bare
            # allowed id, not the usual `fixture-agent/<id>` path: provider matching is
            # equality, so a path-shaped value would fail this rule whether or not the
            # field were admitted, and the test would pass without proving anything. With
            # the value spelled exactly as the allowlist spells it, the only thing keeping
            # the verdict at `fail` is that `retrieved_by` is not read at all.
            document["retrieved_by"] = ALLOWED_PROVIDER
            document.pop("provider_registration")
            sidecar.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

            code, payload, stderr = self.run_evaluate(workspace, RULE_SLUG)

            self.assertEqual(0, code, stderr)
            self.assertEqual(
                {PROVIDER_POLICY: "fail", FRESHNESS_POLICY: "pass", IDENTITY_POLICY: "fail"},
                self.verdicts(payload),
            )
            provider = next(
                result for result in self.facet_results(payload) if result["policy"] == PROVIDER_POLICY
            )
            self.assertTrue(
                provider["reasons"][0].startswith(
                    f"{PRIMITIVES.REASON_PROVENANCE_NOT_ALLOWED}: {FRESH_SOURCE}"
                ),
                provider["reasons"][0],
            )
            self.assertEqual("blocked", payload["coverage_verdict"])


class AnsweringChainTests(PolicyPrimitivesWorkspace, unittest.TestCase):
    """`answer --require-coverage --require-grounding`, and where it routes."""

    def test_an_all_primitive_question_answers_with_no_human_in_the_loop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.stage_workspace(Path(tmpdir))

            payload = self.claim_and_answer(workspace, RULE_SLUG)

            self.assertEqual("answered", payload["status"])
            frontmatter = self.question_frontmatter(workspace, RULE_SLUG)
            self.assertEqual("answered", frontmatter["status"])
            self.assertIs(True, frontmatter["coverage_required"])
            self.assertIs(True, frontmatter["grounding_required"])
            # Not "human_review_required is false" — the keys are absent entirely, which
            # is the difference between a review that was cleared and one never opened.
            self.assertEqual(
                [], [key for key in frontmatter if key.startswith("human_review")], frontmatter
            )
            # The `question_field` rule reads bytes the resolution writer must not disturb.
            self.assertEqual({"candidate_sku": CANDIDATE_SKU}, frontmatter["metadata"])

    def test_a_flagged_and_a_definition_only_policy_both_park_the_question(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.stage_workspace(Path(tmpdir))

            payload = self.claim_and_answer(workspace, REVIEW_SLUG)

            self.assertEqual("human_review", payload["status"])
            frontmatter = self.question_frontmatter(workspace, REVIEW_SLUG)
            self.assertEqual("human_review", frontmatter["status"])
            self.assertIs(True, frontmatter["human_review_required"])
            # Only the two policies that asked for a person; the freshness rule settled
            # itself and is not in the queue.
            self.assertEqual(
                sorted([REVIEWED_POLICY, DEFINITION_ONLY_POLICY]),
                sorted(frontmatter["human_review_policies"]),
            )
            self.assertIn("human_review_requested_at", frontmatter)

    def test_recording_both_reviews_answers_the_question_and_clears_the_safety_gate(self):
        """CR-9 composed with CR-1: the reviews the pack still asks for close normally.

        `publication_readiness.py` needed no change for any of this, and the assertions
        below are all against its unmodified output. It does not reach `ship` here, and
        that is a property of the readiness gate rather than of CR-9: `classify_export`
        raises `attention` for any facet policy whose verdict is not `pass`, and an
        accepted review does not change the verdict the policy returns. The same was true
        of a definition-only pack policy before CR-9 — see the sibling test for the ship
        verdict a workspace reaches when every policy on it carries a rule.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.stage_workspace(Path(tmpdir), slugs=(RULE_SLUG, REVIEW_SLUG))
            self.claim_and_answer(workspace, RULE_SLUG)
            self.claim_and_answer(workspace, REVIEW_SLUG)
            code, parked, stderr = self.run_readiness(workspace)
            self.assertEqual(READINESS.EXIT_NOT_READY, code, stderr)
            self.assertEqual(READINESS.VERDICT_NO_SHIP, parked["verdict"])
            self.assertTrue(
                any("pending required human review" in reason for reason in parked["reasons"]["safety"]),
                parked["reasons"]["safety"],
            )

            # One accepted policy is not two: the question stays parked until every
            # policy that asked for a person has an answer.
            code, first, stderr = self.run_review(workspace, REVIEW_SLUG, REVIEWED_POLICY)
            self.assertEqual(0, code, stderr)
            self.assertEqual("human_review", first["status"])
            self.assertEqual([DEFINITION_ONLY_POLICY], first["pending_policies"])

            code, second, stderr = self.run_review(workspace, REVIEW_SLUG, DEFINITION_ONLY_POLICY)
            self.assertEqual(0, code, stderr)
            self.assertEqual("answered", second["status"])
            self.assertEqual([], second["pending_policies"])

            frontmatter = self.question_frontmatter(workspace, REVIEW_SLUG)
            self.assertEqual("answered", frontmatter["status"])
            self.assertEqual("approved", frontmatter["human_review_status"])
            self.assertIs(True, frontmatter["human_review_approved"])
            self.assertEqual(
                [(REVIEWED_POLICY, REVIEW_REF), (DEFINITION_ONLY_POLICY, REVIEW_REF)],
                [(entry["policy"], entry["review_ref"]) for entry in frontmatter["human_reviews"]],
            )

            code, reviewed, stderr = self.run_readiness(workspace)

            self.assertEqual([], reviewed["reasons"]["safety"], stderr)
            self.assertEqual(READINESS.VERDICT_ATTENTION, reviewed["verdict"], reviewed["reasons"])
            # And the only thing still holding it there is the pair of policies a person
            # decided — nothing about the rule-backed question, and nothing about grounding.
            self.assertEqual(
                {REVIEWED_POLICY, DEFINITION_ONLY_POLICY},
                {
                    policy
                    for policy in ALL_PACK_POLICIES
                    if any(policy in reason for reason in reviewed["reasons"]["coverage"])
                },
            )
            self.assertEqual([], reviewed["reasons"]["grounding"])

    def test_a_workspace_decided_entirely_by_rules_reaches_a_shippable_verdict(self):
        """The outcome CR-9 was filed for, stated as a publication verdict.

        Every policy on this workspace's one facet is a pack policy, and before CR-9 each
        of them evaluated to `manual_review` — which `classify_export` reads as attention,
        so this workspace could not have reached `ship` at all.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.stage_workspace(Path(tmpdir), slugs=(RULE_SLUG,))
            self.claim_and_answer(workspace, RULE_SLUG)

            code, verified, stderr = self.run_verify_quotes(workspace, RULE_SLUG)
            self.assertEqual(0, code, stderr)
            grounding = verified["questions"][0]["grounding"][0]
            self.assertEqual("verified", grounding["result"])
            self.assertEqual("anchor", grounding["form"])

            results = self.run_lint(workspace)
            code, readiness, stderr = self.run_readiness(workspace)
            status = self.status_document(workspace)

        self.assertEqual([], self.high_findings(results))
        self.assertEqual("complete", status["readiness"]["verdict"], status["readiness"]["reasons"])
        self.assertEqual(READINESS.VERDICT_SHIP, readiness["verdict"], readiness["reasons"])
        self.assertEqual(READINESS.EXIT_READY, code, stderr)
        self.assertEqual([], readiness["reasons"]["coverage"])
        self.assertEqual([], readiness["reasons"]["safety"])


class ScopedReviewPairingTests(PolicyPrimitivesWorkspace, unittest.TestCase):
    """CR-1's `review.escalation_scope: question` paired with CR-9's rule verdicts.

    Under the default workspace scope one parked question freezes everything, so the
    automation CR-9 adds would be invisible from the workspace verdict. Under question
    scope the parked question is counted rather than fatal, and the rule-backed question
    resolves beside it — which is the combination `workspace-status.md` documents.
    """

    def test_one_parked_review_leaves_the_rule_backed_question_answerable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.stage_workspace(
                Path(tmpdir), slugs=(RULE_SLUG, REVIEW_SLUG), escalation_scope="question"
            )

            parked = self.claim_and_answer(workspace, REVIEW_SLUG)
            self.assertEqual("human_review", parked["status"])

            # The parked question does not stop the other one being claimed and answered.
            answered = self.claim_and_answer(workspace, RULE_SLUG)
            self.assertEqual("answered", answered["status"])

            document = self.status_document(workspace)
            readiness = document["readiness"]

            # Answers nobody has reviewed are not finished work: in_progress, never
            # complete, and never the workspace-wide attention_required freeze.
            self.assertEqual("in_progress", readiness["verdict"], readiness["reasons"])
            self.assertEqual(1, readiness["questions_awaiting_review"])
            self.assertEqual([REVIEW_SLUG], document["questions"]["human_review_slugs"])
            self.assertEqual(0, document["questions"]["actionable"])
            self.assertEqual(0, int(document["lint"]["issue_counts"].get("HIGH", 0)))

    def test_the_default_scope_still_freezes_the_workspace_on_the_same_evidence(self):
        """The scope changes the blast radius, never whether the review happens."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.stage_workspace(Path(tmpdir), slugs=(RULE_SLUG, REVIEW_SLUG))
            self.assertNotIn(
                "review", yaml.safe_load((workspace / "research.yml").read_text(encoding="utf-8"))
            )

            self.claim_and_answer(workspace, REVIEW_SLUG)
            self.claim_and_answer(workspace, RULE_SLUG)
            readiness = self.status_document(workspace)["readiness"]

            self.assertEqual("attention_required", readiness["verdict"])
            self.assertEqual(1, readiness["questions_awaiting_review"])


class McpSurfaceTests(PolicyPrimitivesWorkspace, unittest.TestCase):
    """The same verdicts over MCP, which is how a host reads this workspace."""

    def test_export_and_status_tools_report_the_rule_verdicts_and_the_review_queue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.stage_workspace(
                Path(tmpdir), slugs=(RULE_SLUG, REVIEW_SLUG), escalation_scope="question"
            )
            self.claim_and_answer(workspace, RULE_SLUG)
            self.claim_and_answer(workspace, REVIEW_SLUG)

            export = self.mcp_tool(workspace, "export_answers")
            status = self.mcp_tool(workspace, "workspace_status")

        questions = {question["slug"]: question for question in export["questions"]}
        answered = questions[RULE_SLUG]
        self.assertEqual("answered", answered["status"])
        self.assertEqual("pass", answered["coverage_status"])
        self.assertEqual(
            {PROVIDER_POLICY: "pass", FRESHNESS_POLICY: "pass", IDENTITY_POLICY: "pass"},
            {
                result["policy"]: result["verdict"]
                for facet in answered["coverage_facets"]
                for result in facet["policy_results"]
            },
        )
        self.assertFalse(answered["human_review"]["required"])

        parked = questions[REVIEW_SLUG]
        self.assertEqual("human_review", parked["status"])
        self.assertTrue(parked["human_review"]["pending"])
        self.assertEqual(
            sorted([REVIEWED_POLICY, DEFINITION_ONLY_POLICY]),
            sorted(parked["human_review"]["policies"]),
        )
        self.assertEqual("in_progress", status["readiness"]["verdict"])
        self.assertEqual(1, status["readiness"]["questions_awaiting_review"])


class CrNineAcceptanceCriteriaTests(PolicyPrimitivesWorkspace, unittest.TestCase):
    """One named test per CR-9 acceptance criterion, so criterion and test read as one.

    Overlap with the chains above is deliberate: a criterion nobody can point at a test for
    is a criterion nobody checked.
    """

    def test_criterion_1_a_max_age_48h_policy_fails_a_fifty_hour_source_and_passes_a_fresh_one(self):
        """Same rule, same manifest shape, same workspace — only the delivery's age differs.

        And no human is involved on either side: neither evaluation names a review, and
        both are recorded in the manifest the workspace keeps.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.stage_workspace(Path(tmpdir))
            observed = {}
            for label, slug in (("50 hours old", STALE_SLUG), ("2 hours old", RULE_SLUG)):
                code, payload, stderr = self.run_evaluate(workspace, slug)
                self.assertEqual(0, code, stderr)
                freshness = next(
                    result for result in self.facet_results(payload) if result["policy"] == FRESHNESS_POLICY
                )
                observed[label] = {
                    "verdict": freshness["verdict"],
                    "coverage_verdict": payload["coverage_verdict"],
                    "written_verdict": self.written_manifest(workspace, slug)["coverage_verdict"],
                    "facet_verdict": self.written_manifest(workspace, slug)["required_facets"][0][
                        "facet_verdict"
                    ],
                    "mentions_review": "manual_review" in json.dumps(payload["policy_results"]),
                    "reasons": freshness["reasons"],
                }

        self.assertEqual("fail", observed["50 hours old"]["verdict"])
        self.assertEqual("pass", observed["2 hours old"]["verdict"])
        # Recorded, not merely reported: the evaluation is what the manifest now says.
        self.assertEqual("blocked", observed["50 hours old"]["written_verdict"])
        self.assertEqual("blocked", observed["50 hours old"]["facet_verdict"])
        self.assertEqual("pass", observed["2 hours old"]["written_verdict"])
        self.assertEqual("pass", observed["2 hours old"]["facet_verdict"])
        for label, record in observed.items():
            with self.subTest(delivery=label):
                self.assertEqual(record["coverage_verdict"], record["written_verdict"])
                self.assertFalse(record["mentions_review"])
        self.assertTrue(
            all(
                reason.startswith(PRIMITIVES.REASON_STALE)
                for reason in observed["50 hours old"]["reasons"]
            ),
            observed["50 hours old"]["reasons"],
        )
        self.assertTrue(
            all(
                not reason.startswith(PRIMITIVES.RULE_REASON_PREFIXES)
                for reason in observed["2 hours old"]["reasons"]
            ),
            observed["2 hours old"]["reasons"],
        )

    def test_criterion_2_a_policy_mixing_primitives_and_manual_review_required_needs_both(self):
        """Neither half alone decides it: the rules must pass *and* a review be recorded.

        The failing half is produced by taking the registration away, which is the one
        thing that makes the flagged policy's `one_of_provenance` leaf fail while leaving
        its other leaves and the review flag exactly as declared.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            # Rules pass: the policy asks for a person, and a recorded review satisfies it.
            satisfied = self.stage_workspace(root / "satisfied", slugs=(REVIEW_SLUG,))
            code, payload, stderr = self.run_evaluate(satisfied, REVIEW_SLUG)
            self.assertEqual(0, code, stderr)
            self.assertEqual("manual_review", self.verdicts(payload)[REVIEWED_POLICY])
            parked = self.claim_and_answer(satisfied, REVIEW_SLUG)
            self.assertEqual("human_review", parked["status"])
            code, first, stderr = self.run_review(satisfied, REVIEW_SLUG, REVIEWED_POLICY)
            self.assertEqual(0, code, stderr)
            self.assertEqual([DEFINITION_ONLY_POLICY], first["pending_policies"])

            # A failing rule is never redeemed by the review flag: the policy fails, and
            # a failing policy blocks the facet rather than queueing it for a person.
            broken = self.stage_workspace(root / "broken", slugs=(REVIEW_SLUG,))
            sidecar = self.provenance_sidecar(broken, FRESH_SOURCE)
            document = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
            document.pop("provider_registration")
            sidecar.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

            code, refused, stderr = self.run_evaluate(broken, REVIEW_SLUG)
            self.assertEqual(0, code, stderr)
            flagged = next(
                result for result in self.facet_results(refused) if result["policy"] == REVIEWED_POLICY
            )

        self.assertEqual("fail", flagged["verdict"])
        self.assertEqual("blocked", refused["coverage_verdict"])
        self.assertTrue(
            flagged["reasons"][0].startswith(PRIMITIVES.REASON_PROVENANCE_NOT_ALLOWED),
            flagged["reasons"][0],
        )
        # Failing, not parked: the request for a human review is not in this result at all.
        self.assertNotIn("requires a recorded domain review", json.dumps(flagged))
        self.assertIn(f"domain_pack.policy_rules[{REVIEWED_POLICY}]", flagged["remediation"])

    def test_criterion_3_require_coverage_no_longer_routes_a_fully_primitive_question_to_review(self):
        """The regression this criterion protects, shown against the pre-CR-9 behaviour.

        The comparison workspace is the same question with the same evidence and the same
        policy ids; only the `policy_rules` block is removed. That is the workspace CR-9
        inherited, and it routes to `human_review` on all three policies.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            with_rules = self.stage_workspace(root / "with-rules", slugs=(RULE_SLUG,))
            answered = self.claim_and_answer(with_rules, RULE_SLUG)
            answered_frontmatter = self.question_frontmatter(with_rules, RULE_SLUG)

            without_rules = self.stage_workspace(root / "without-rules", slugs=(RULE_SLUG,))
            config_path = without_rules / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["domain_pack"].pop("policy_rules")
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            parked = self.claim_and_answer(without_rules, RULE_SLUG)
            parked_frontmatter = self.question_frontmatter(without_rules, RULE_SLUG)

        self.assertEqual("answered", answered["status"])
        self.assertIs(True, answered_frontmatter["coverage_required"])
        self.assertEqual(
            [], [key for key in answered_frontmatter if key.startswith("human_review")], answered_frontmatter
        )

        self.assertEqual("human_review", parked["status"])
        self.assertEqual(
            sorted([PROVIDER_POLICY, FRESHNESS_POLICY, IDENTITY_POLICY]),
            sorted(parked_frontmatter["human_review_policies"]),
        )


if __name__ == "__main__":
    unittest.main()
