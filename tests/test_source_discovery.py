"""Contract tests for the source-discovery schema documentation (E31-T01) and
the trust-tier reasoning policy (E31-T02).

These tests assert that `docs/source-discovery.md` describes every required
`source_candidate` field, names the durable candidate store, states that
candidates are not evidence until fetched into `raw/` with provenance, and is
cross-linked from the acquisition, delivery, and orchestrator-handoff docs.

The E31-T02 tests additionally assert that the doc defines the five trust tiers,
the four ranking rules, and the five reasoning fields, and they validate the
machine-readable tier-example fixtures against that policy — including that an
official legal source outranks a higher-provider-ranked generic result.
"""

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "workspace-template" / "docs"
SOURCE_DISCOVERY_DOC = DOCS / "source-discovery.md"
ACQUISITION_DOC = DOCS / "acquisition.md"
SOURCE_DELIVERY_DOC = DOCS / "source-delivery.md"
HANDOFF_DOC = DOCS / "orchestrator-handoff.md"
README = REPO_ROOT / "README.md"

# The required `source_candidate` fields from E31-T01 plus the E31/E32 origin
# clarification. `request_id`, `seed_source_id`, and `discovery_run_id` are the
# three origin links; exactly one must be populated.
REQUIRED_CANDIDATE_FIELDS = (
    "schema_version",
    "candidate_id",
    "request_id",
    "seed_source_id",
    "discovery_run_id",
    "discovered_at",
    "discovered_by",
    "provider",
    "url",
    "title",
    "source_type",
    "trust_tier",
    "relevance_score",
    "trust_score",
    "official_source",
    "jurisdiction",
    "license",
    "terms_url",
    "rationale",
    "recommended_action",
    "network_io_executed",
    "evidence_path",
    "source_policy",
    "freshness_policy",
    "identity_policy",
    "selected_for_request_id",
    "selected_at",
)


class SourceDiscoverySchemaDocTests(unittest.TestCase):
    def test_source_discovery_doc_exists(self):
        self.assertTrue(
            SOURCE_DISCOVERY_DOC.is_file(),
            "workspace-template/docs/source-discovery.md is missing",
        )

    def test_doc_describes_every_required_candidate_field(self):
        doc = SOURCE_DISCOVERY_DOC.read_text()
        missing = [
            field
            for field in REQUIRED_CANDIDATE_FIELDS
            if f"`{field}`" not in doc
        ]
        self.assertEqual(
            [],
            missing,
            f"source-discovery.md does not describe required field(s): {missing}",
        )

    def test_doc_names_the_durable_candidate_store(self):
        doc = SOURCE_DISCOVERY_DOC.read_text()
        self.assertIn("sources/discovery/candidates.jsonl", doc)
        # The store lives under sources/discovery/, not directly under raw/.
        self.assertIn("sources/discovery/", doc)

    def test_doc_pins_the_candidate_schema_version(self):
        doc = SOURCE_DISCOVERY_DOC.read_text()
        self.assertIn("`source_candidate`", doc)
        self.assertIn('"schema_version": "1.0"', doc)

    def test_doc_documents_recommended_action_values(self):
        doc = SOURCE_DISCOVERY_DOC.read_text()
        for value in ("`fetch`", "`review`", "`reject`"):
            with self.subTest(value=value):
                self.assertIn(value, doc)

    def test_doc_states_candidates_are_not_evidence_until_fetched(self):
        doc = SOURCE_DISCOVERY_DOC.read_text()
        self.assertIn("Candidates are not evidence", doc)
        self.assertIn(".provenance.yml", doc)
        # The transition to evidence runs through raw/ with provenance.
        self.assertIn("`raw/`", doc)

    def test_doc_explains_network_io_executed_for_read_only_runs(self):
        doc = SOURCE_DISCOVERY_DOC.read_text()
        self.assertIn("`network_io_executed`", doc)
        self.assertIn("network_io_executed: false", doc)

    def test_doc_describes_academic_expansion_quality_gates(self):
        doc = SOURCE_DISCOVERY_DOC.read_text()
        self.assertIn("`quality_gates`", doc)
        self.assertIn("`author_identity`", doc)
        self.assertIn("`companion_repository`", doc)
        self.assertIn("`repository_link_origin`", doc)

    def test_doc_cross_links_back_to_related_contracts(self):
        doc = SOURCE_DISCOVERY_DOC.read_text()
        for link in (
            "(acquisition.md)",
            "(source-delivery.md)",
            "(orchestrator-handoff.md)",
        ):
            with self.subTest(link=link):
                self.assertIn(link, doc)

    def test_related_docs_cross_link_to_source_discovery(self):
        for path in (ACQUISITION_DOC, SOURCE_DELIVERY_DOC, HANDOFF_DOC):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), f"{path} is missing")
                self.assertIn("(source-discovery.md)", path.read_text())

    def test_readme_documentation_map_lists_source_discovery(self):
        self.assertIn(
            "workspace-template/docs/source-discovery.md",
            README.read_text(),
        )


# --- E31-T02: trust-tier and reasoning policy ----------------------------

# Trust tiers, ordered best (rank 1) to worst (rank 5). The ordering is the
# policy authority for "outranks": a lower rank number is more trustworthy.
TRUST_TIERS = (
    "official_primary",
    "primary_non_official",
    "secondary_reputable",
    "secondary_unknown",
    "unsafe_or_unusable",
)
TIER_RANK = {tier: index for index, tier in enumerate(TRUST_TIERS)}

RECOMMENDED_ACTIONS = ("fetch", "review", "reject")

ALLOWED_SOURCE_TYPES = (
    "paper",
    "code_repository",
    "dataset",
    "project_page",
    "supplemental_material",
    "publisher_page",
    "official_legal",
    "web_page",
    "standards_registry_entry",
    "harmonised_standard_reference",
    "product_requirement_guidance",
    "geospatial_standard_register_entry",
)

ALLOWED_EVIDENCE_PATHS = (
    "legal_current_figure",
    "academic_method_existence",
    "github_implementation",
    "standards_registry_reference",
    "product_requirement_profile",
    "vendor_product_spec",
)

ALLOWED_SOURCE_POLICIES = (
    "official_primary",
    "primary_or_official",
    "academic_indexed",
    "openalex_or_arxiv",
    "canonical_repository",
    "official_vendor",
    "domain_pack_allowed",
    "manual_review_required",
    "official_standards_registry",
    "standards_body_primary",
)

ALLOWED_FRESHNESS_POLICIES = (
    "current_legal_figure",
    "current_product_spec",
    "current_standard_reference",
    "current_product_requirement",
    "publication_identity",
    "release_snapshot",
    "no_staleness_check",
    "manual_review",
)

ALLOWED_IDENTITY_POLICIES = (
    "citation_id_resolves",
    "origin_url_matches_candidate",
    "repo_ref_resolves",
    "official_domain_match",
    "standard_designation_matches_registry",
    "registry_entry_matches_product_requirement",
    "none",
)

DELIVERY_FAILURE_CODES = (
    "tls_verification_failed",
    "http_error",
    "javascript_required",
    "official_error_page",
    "not_found",
    "content_too_sparse",
    "license_or_terms_unknown",
    "robots_or_terms_blocked",
    "manual_review_required",
)

REASONING_FIELDS = (
    "matched_query_terms",
    "authority_reason",
    "freshness_reason",
    "scope_reason",
    "risk_flags",
)

TIER_EXAMPLES_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "discovery" / "tier-examples.jsonl"
)
CANDIDATE_SCHEMA_EXAMPLES_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "discovery" / "candidate-schema-examples.jsonl"
)

# The request shared by the legal official-vs-generic ranking example.
LEGAL_RANKING_REQUEST_ID = "req-legal-emissions-1"


def load_tier_examples():
    """Parse the tier-example fixture into candidate records."""
    records = []
    for lineno, line in enumerate(
        TIER_EXAMPLES_FIXTURE.read_text().splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:  # pragma: no cover - failure path
            raise AssertionError(
                f"{TIER_EXAMPLES_FIXTURE.name}:{lineno} is not valid JSON: {exc}"
            ) from exc
    return records


def policy_sort_key(candidate):
    """Ranking key derived from the documented policy.

    Trust tier dominates (Ranking Rule 1: official sources first), then a known
    official source, then trust and relevance scores. Lower tuples rank first,
    so the candidate that should be handed to the planner first sorts to index 0.
    """
    return (
        TIER_RANK[candidate["trust_tier"]],
        0 if candidate.get("official_source") is True else 1,
        -candidate["trust_score"],
        -candidate["relevance_score"],
    )


class TrustTierPolicyDocTests(unittest.TestCase):
    """E31-T02: the doc must define the trust-tier and reasoning policy."""

    def setUp(self):
        self.doc = SOURCE_DISCOVERY_DOC.read_text()

    def test_doc_defines_every_trust_tier(self):
        missing = [tier for tier in TRUST_TIERS if f"`{tier}`" not in self.doc]
        self.assertEqual(
            [], missing, f"source-discovery.md does not define tier(s): {missing}"
        )

    def test_doc_defines_every_reasoning_field(self):
        missing = [
            field for field in REASONING_FIELDS if f"`{field}`" not in self.doc
        ]
        self.assertEqual(
            [],
            missing,
            f"source-discovery.md does not define reasoning field(s): {missing}",
        )

    def test_doc_states_the_four_ranking_rules(self):
        # Each rule is asserted by a distinctive, stable phrase.
        lowered = self.doc.lower()
        # Rule 1: legal/regulatory official-first.
        self.assertIn("official-first", lowered)
        self.assertIn("legal or regulatory", lowered)
        # Rule 2: exact identifier match beats fuzzy text match.
        self.assertIn("exact", lowered)
        self.assertIn("doi", lowered)
        self.assertIn("owner/repo", lowered)
        self.assertIn("fuzzy", lowered)
        # Rule 3: unknown officialness requires review before download.
        self.assertIn("official_source: null", self.doc)
        self.assertIn("review before download", lowered)
        # Rule 4: terms/license uncertainty is suggest-not-ingest.
        self.assertIn("silently ingested", lowered)

    def test_doc_explains_official_legal_beats_generic_result(self):
        # The acceptance criterion: the policy explains why an official legal
        # source beats a high-ranked generic search result.
        lowered = self.doc.lower()
        self.assertIn("official legal source", lowered)
        self.assertIn("outranks", lowered)


class TierExampleFixtureTests(unittest.TestCase):
    """E31-T02: validate the tier-example fixtures against the policy."""

    @classmethod
    def setUpClass(cls):
        cls.records = load_tier_examples()

    def test_fixture_exists_and_is_non_empty(self):
        self.assertTrue(
            TIER_EXAMPLES_FIXTURE.is_file(),
            f"{TIER_EXAMPLES_FIXTURE} is missing",
        )
        self.assertTrue(self.records, "tier-examples.jsonl has no records")

    def test_every_required_schema_field_is_present(self):
        for record in self.records:
            cid = record.get("candidate_id", "<no candidate_id>")
            for field in REQUIRED_CANDIDATE_FIELDS + ("reasoning",):
                with self.subTest(candidate=cid, field=field):
                    self.assertIn(field, record)

    def test_exactly_one_origin_link_is_set(self):
        for record in self.records:
            with self.subTest(candidate=record["candidate_id"]):
                populated = [
                    field
                    for field in ("request_id", "seed_source_id", "discovery_run_id")
                    if record.get(field) is not None
                ]
                self.assertEqual(
                    1,
                    len(populated),
                    "exactly one of request_id / seed_source_id / discovery_run_id must be set",
                )

    def test_enumerated_fields_use_documented_values(self):
        for record in self.records:
            cid = record["candidate_id"]
            with self.subTest(candidate=cid, field="trust_tier"):
                self.assertIn(record["trust_tier"], TRUST_TIERS)
            with self.subTest(candidate=cid, field="recommended_action"):
                self.assertIn(record["recommended_action"], RECOMMENDED_ACTIONS)
            with self.subTest(candidate=cid, field="source_type"):
                self.assertIn(record["source_type"], ALLOWED_SOURCE_TYPES)
            with self.subTest(candidate=cid, field="official_source"):
                self.assertIn(record["official_source"], (True, False, None))

    def test_scores_are_within_unit_interval(self):
        for record in self.records:
            for field in ("relevance_score", "trust_score"):
                with self.subTest(candidate=record["candidate_id"], field=field):
                    self.assertGreaterEqual(record[field], 0.0)
                    self.assertLessEqual(record[field], 1.0)

    def test_reasoning_object_is_complete(self):
        for record in self.records:
            cid = record["candidate_id"]
            reasoning = record["reasoning"]
            for field in REASONING_FIELDS:
                with self.subTest(candidate=cid, field=field):
                    self.assertIn(field, reasoning)
            with self.subTest(candidate=cid, field="matched_query_terms"):
                self.assertIsInstance(reasoning["matched_query_terms"], list)
                self.assertTrue(reasoning["matched_query_terms"])
            with self.subTest(candidate=cid, field="risk_flags"):
                self.assertIsInstance(reasoning["risk_flags"], list)
            for field in ("authority_reason", "freshness_reason", "scope_reason"):
                with self.subTest(candidate=cid, field=field):
                    self.assertIsInstance(reasoning[field], str)
                    self.assertTrue(reasoning[field].strip())

    def test_every_tier_has_at_least_one_example(self):
        present = {record["trust_tier"] for record in self.records}
        missing = [tier for tier in TRUST_TIERS if tier not in present]
        self.assertEqual(
            [], missing, f"tier-examples.jsonl is missing tier(s): {missing}"
        )

    def test_unknown_officialness_requires_review(self):
        # Ranking Rule 3: official_source null cannot be recommended for fetch.
        for record in self.records:
            if record["official_source"] is None:
                with self.subTest(candidate=record["candidate_id"]):
                    self.assertEqual(record["recommended_action"], "review")

    def test_terms_or_license_uncertainty_is_not_auto_fetch(self):
        # Ranking Rule 4: uncertain terms/license is suggest-not-ingest.
        uncertain_flags = {"license_uncertain", "terms_uncertain"}
        for record in self.records:
            flags = set(record["reasoning"]["risk_flags"])
            if flags & uncertain_flags:
                with self.subTest(candidate=record["candidate_id"]):
                    self.assertNotEqual(record["recommended_action"], "fetch")

    def test_unsafe_tier_is_rejected(self):
        for record in self.records:
            if record["trust_tier"] == "unsafe_or_unusable":
                with self.subTest(candidate=record["candidate_id"]):
                    self.assertEqual(record["recommended_action"], "reject")

    def test_official_legal_source_outranks_generic_competitor(self):
        # Acceptance criterion: the official legal source beats a higher-ranked
        # generic search result for the same request.
        contenders = [
            record
            for record in self.records
            if record.get("request_id") == LEGAL_RANKING_REQUEST_ID
        ]
        self.assertGreaterEqual(
            len(contenders),
            2,
            "expected an official + generic pair sharing the legal request id",
        )

        winner, *_ = sorted(contenders, key=policy_sort_key)
        self.assertEqual(winner["trust_tier"], "official_primary")
        self.assertIs(winner["official_source"], True)

        generic = [
            record
            for record in contenders
            if record["trust_tier"] != "official_primary"
        ]
        self.assertTrue(generic, "expected a non-official competitor")
        competitor = generic[0]

        # The official source wins on trust tier despite the competitor having a
        # *higher* provider/lexical relevance score — rank, not relevance, decides.
        self.assertGreater(
            competitor["relevance_score"],
            winner["relevance_score"],
            "fixture should prove tier beats raw relevance",
        )
        self.assertLess(
            TIER_RANK[winner["trust_tier"]],
            TIER_RANK[competitor["trust_tier"]],
        )
        self.assertEqual(competitor["recommended_action"], "reject")


class CandidateSchemaExampleFixtureTests(unittest.TestCase):
    """E42-T01: validate examples for the unified candidate schema."""

    @classmethod
    def setUpClass(cls):
        cls.records = []
        for lineno, line in enumerate(
            CANDIDATE_SCHEMA_EXAMPLES_FIXTURE.read_text().splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                cls.records.append(json.loads(line))
            except json.JSONDecodeError as exc:  # pragma: no cover - failure path
                raise AssertionError(
                    f"{CANDIDATE_SCHEMA_EXAMPLES_FIXTURE.name}:{lineno} is not valid JSON: {exc}"
                ) from exc

    def test_fixture_exists_and_covers_acceptance_examples(self):
        self.assertTrue(CANDIDATE_SCHEMA_EXAMPLES_FIXTURE.is_file())
        labels = {record["schema_example"] for record in self.records}
        self.assertEqual(
            {
                "openalex_work",
                "arxiv_paper",
                "github_repo",
                "official_regulation",
                "vendor_product_page",
                "vendor_product_js_failure",
                "dataset",
                "standards_registry_entry",
            },
            labels,
        )

    def test_examples_carry_policy_and_selection_fields(self):
        for record in self.records:
            with self.subTest(candidate=record["candidate_id"]):
                for field in REQUIRED_CANDIDATE_FIELDS:
                    self.assertIn(field, record)
                self.assertIn(record["evidence_path"], ALLOWED_EVIDENCE_PATHS)
                self.assertIn(record["source_policy"], ALLOWED_SOURCE_POLICIES)
                self.assertIn(record["freshness_policy"], ALLOWED_FRESHNESS_POLICIES)
                self.assertIn(record["identity_policy"], ALLOWED_IDENTITY_POLICIES)
                self.assertIsNone(record["selected_for_request_id"])
                self.assertIsNone(record["selected_at"])

    def test_academic_examples_carry_provider_neutral_paper_metadata(self):
        academic = [
            record for record in self.records
            if record["schema_example"] in {"openalex_work", "arxiv_paper"}
        ]
        self.assertEqual(2, len(academic))
        for record in academic:
            with self.subTest(candidate=record["candidate_id"]):
                self.assertEqual("paper", record["source_type"])
                self.assertIn("paper", record)
                paper = record["paper"]
                self.assertIn("provider_ids", paper)
                for key in ("arxiv", "openalex", "doi"):
                    self.assertIn(key, paper["provider_ids"])
                for key in (
                    "title", "authors", "publication_year", "doi", "arxiv_id",
                    "open_access", "oa_status", "license", "landing_page_url",
                    "pdf_url", "resolution_status",
                ):
                    self.assertIn(key, paper)
                self.assertIn(paper["resolution_status"], {"resolved", "metadata_only", "uncertain"})
                self.assertIsInstance(paper["authors"], list)
                self.assertEqual(record["license"], paper["license"])
                self.assertIn("provider_budget", record)
                self.assertEqual(record["network_io_executed"], record["provider_budget"]["network_io_executed"])

    def test_standards_example_carries_registry_metadata(self):
        records = [record for record in self.records if record["schema_example"] == "standards_registry_entry"]
        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual("standards_registry_entry", record["source_type"])
        self.assertEqual("standards_registry_reference", record["evidence_path"])
        self.assertEqual("official_standards_registry", record["source_policy"])
        self.assertEqual("current_standard_reference", record["freshness_policy"])
        self.assertEqual("standard_designation_matches_registry", record["identity_policy"])
        standards = record.get("standards")
        self.assertIsInstance(standards, dict)
        for key in (
            "registry_provider",
            "standards_body",
            "designation",
            "title",
            "edition",
            "publication_date",
            "status",
            "ics_codes",
            "owner_committee",
            "registry_url",
            "dataset_license",
            "attribution_required",
        ):
            with self.subTest(key=key):
                self.assertIn(key, standards)
        self.assertEqual("ISO 19131:2022", standards["designation"])
        self.assertEqual("published", standards["status"])

    def test_delivery_failure_codes_can_be_candidate_risk_flags(self):
        documented = SOURCE_DISCOVERY_DOC.read_text(encoding="utf-8")
        for code in DELIVERY_FAILURE_CODES:
            with self.subTest(code=code):
                self.assertIn(f"`{code}`", documented)

        failure_examples = [
            record
            for record in self.records
            if set(record["reasoning"]["risk_flags"]) & set(DELIVERY_FAILURE_CODES)
        ]
        self.assertTrue(failure_examples, "expected at least one candidate example with delivery failure risk flags")
        for record in failure_examples:
            with self.subTest(candidate=record["candidate_id"]):
                self.assertEqual("review", record["recommended_action"])
                self.assertNotEqual("fetch", record["recommended_action"])


if __name__ == "__main__":
    unittest.main()
