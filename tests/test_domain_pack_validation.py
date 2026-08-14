import contextlib
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


class DomainPackValidationTests(unittest.TestCase):
    def validator(self):
        return importlib.import_module("evidence_wiki.domain_pack_validator")

    def run_validator(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.validator().main(list(args))
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def test_llm_research_pack_validates_by_name(self):
        code, stdout, stderr = self.run_validator("--path", "llm-research")
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertEqual("1.0", payload["schema_version"])
        self.assertTrue(payload["ok"], payload)
        self.assertEqual("llm-research", payload["domain_pack"]["name"])
        self.assertEqual("0.1.0", payload["domain_pack"]["version"])
        self.assertEqual("0.1", payload["domain_pack"]["compatible_research_yml_contract"])
        self.assertEqual(
            {
                "academic-negative-claim-probe": "coverage-templates/academic-negative-claim-probe.yml",
                "academic-method-feasibility": "coverage-templates/academic-method-feasibility.yml",
                "vendor-product-spec": "coverage-templates/vendor-product-spec.yml",
            },
            payload["domain_pack"]["coverage_templates"],
        )
        self.assertTrue(payload["smoke_validation"]["ok"], payload["smoke_validation"]["issues"])
        self.assertEqual(0, payload["smoke_validation"]["summary"]["issue_count"])
        self.assertTrue(all(check["status"] == "pass" for check in payload["checks"]), payload["checks"])

    def test_llm_research_pack_validates_by_path(self):
        pack_path = REPO_ROOT / "domain-packs" / "llm-research"

        code, stdout, stderr = self.run_validator("--path", str(pack_path))
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(pack_path.resolve().as_posix(), payload["domain_pack"]["path"])

    def test_general_science_pack_validates_by_name(self):
        code, stdout, stderr = self.run_validator("--path", "general-science")
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual("general-science", payload["domain_pack"]["name"])
        self.assertEqual(["arxiv", "openalex"], payload["domain_pack"]["recommended_acquisition"])
        self.assertEqual(["arxiv", "openalex"], payload["domain_pack"]["recommended_discovery"])
        self.assertTrue(payload["smoke_validation"]["ok"], payload["smoke_validation"]["issues"])

    def test_general_science_pack_validates_by_path(self):
        pack_path = REPO_ROOT / "domain-packs" / "general-science"

        code, stdout, stderr = self.run_validator("--path", str(pack_path))
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(pack_path.resolve().as_posix(), payload["domain_pack"]["path"])

    def test_required_metadata_rejects_each_noncanonical_identity_field(self):
        validator = self.validator()
        valid = {
            "name": "example-pack",
            "version": "1.2.3",
            "compatible_research_yml_contract": "0.1",
        }
        invalid_values = (
            ("name", None),
            ("version", 1),
            ("compatible_research_yml_contract", ""),
            ("version", " 1.2.3 "),
        )
        for field, invalid_value in invalid_values:
            with self.subTest(field=field, value=invalid_value):
                metadata = dict(valid)
                metadata[field] = invalid_value

                info, checks = validator.metadata_check(metadata, "0.1")

                required = next(item for item in checks if item["id"] == "required_metadata")
                self.assertEqual("fail", required["status"])
                self.assertIsNone(info[field])

    def test_safe_yaml_non_json_scalar_is_a_structured_validation_failure(self):
        source_pack = REPO_ROOT / "domain-packs" / "general-science"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "general-science"
            shutil.copytree(source_pack, pack_path)
            overlay_path = pack_path / "research.overlay.yml"
            overlay_path.write_text(
                overlay_path.read_text(encoding="utf-8") + "\nreviewed_on: 2026-08-13\n",
                encoding="utf-8",
            )

            code, stdout, stderr = self.run_validator("--path", str(pack_path))

        payload = json.loads(stdout)
        checks = {item["id"]: item for item in payload["checks"]}
        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual("fail", checks["overlay_data_model"]["status"])
        self.assertIn("date values are not supported", checks["overlay_data_model"]["message"])
        self.assertEqual("fail", checks["smoke_validation"]["status"])

    def test_recursive_yaml_alias_is_a_structured_validation_failure(self):
        source_pack = REPO_ROOT / "domain-packs" / "general-science"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "general-science"
            shutil.copytree(source_pack, pack_path)
            overlay_path = pack_path / "research.overlay.yml"
            overlay_path.write_text(
                overlay_path.read_text(encoding="utf-8") + "\nrecursive: &loop [*loop]\n",
                encoding="utf-8",
            )

            code, stdout, stderr = self.run_validator("--path", str(pack_path))

        payload = json.loads(stdout)
        checks = {item["id"]: item for item in payload["checks"]}
        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual("fail", checks["overlay_data_model"]["status"])
        self.assertIn("recursive YAML aliases are not supported", checks["overlay_data_model"]["message"])

    def test_initializer_rejects_non_json_pack_values_before_writing_a_target(self):
        validator = self.validator()
        source_pack = REPO_ROOT / "domain-packs" / "general-science"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pack_path = root / "general-science"
            target = root / "workspace"
            shutil.copytree(source_pack, pack_path)
            overlay_path = pack_path / "research.overlay.yml"
            overlay_path.write_text(
                overlay_path.read_text(encoding="utf-8") + "\nreviewed_on: 2026-08-13\n",
                encoding="utf-8",
            )
            scripts = validator.load_scripts(REPO_ROOT / "workspace-template")

            with self.assertRaises(SystemExit) as caught:
                scripts.initializer.main(
                    [
                        "--starter-root",
                        str(REPO_ROOT / "workspace-template"),
                        "--target",
                        str(target),
                        "--project-name",
                        "non-json-pack",
                        "--project-description",
                        "Reject unsupported YAML values before writes.",
                        "--owner-goal",
                        "Keep lifecycle provenance deterministic.",
                        "--domain-pack",
                        str(pack_path),
                    ]
                )

            self.assertIn("JSON-compatible YAML values", str(caught.exception))
            self.assertFalse(target.exists())

    def test_legal_regulatory_pack_validates_by_name(self):
        code, stdout, stderr = self.run_validator("--path", "legal-regulatory")
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual("legal-regulatory", payload["domain_pack"]["name"])
        self.assertEqual(
            {"official-current-figure": "coverage-templates/official-current-figure.yml"},
            payload["domain_pack"]["coverage_templates"],
        )
        self.assertTrue(payload["smoke_validation"]["ok"], payload["smoke_validation"]["issues"])

    def test_legal_regulatory_pack_validates_by_path(self):
        pack_path = REPO_ROOT / "domain-packs" / "legal-regulatory"

        code, stdout, stderr = self.run_validator("--path", str(pack_path))
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(pack_path.resolve().as_posix(), payload["domain_pack"]["path"])

    def test_standards_compliance_pack_validates_by_name(self):
        code, stdout, stderr = self.run_validator("--path", "standards-compliance")
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual("standards-compliance", payload["domain_pack"]["name"])
        self.assertEqual(
            {
                "official-standard-reference": "coverage-templates/official-standard-reference.yml",
                "standards-current-version": "coverage-templates/standards-current-version.yml",
                "eu-product-requirement-profile": "coverage-templates/eu-product-requirement-profile.yml",
                "uk-geospatial-standard-register-entry": "coverage-templates/uk-geospatial-standard-register-entry.yml",
            },
            payload["domain_pack"]["coverage_templates"],
        )
        self.assertEqual([], payload["domain_pack"]["recommended_acquisition"])
        self.assertEqual([], payload["domain_pack"]["recommended_discovery"])
        self.assertTrue(payload["smoke_validation"]["ok"], payload["smoke_validation"]["issues"])

    def test_standards_compliance_pack_validates_by_path(self):
        pack_path = REPO_ROOT / "domain-packs" / "standards-compliance"

        code, stdout, stderr = self.run_validator("--path", str(pack_path))
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(pack_path.resolve().as_posix(), payload["domain_pack"]["path"])

    def validate_pack_declaring_request_kinds(self, pack_name: str, request_kinds) -> tuple[int, dict]:
        """Validate a throwaway copy of a shipped pack whose overlay declares ``request_kinds``.

        Shipped packs deliberately declare no request kinds, so every declaration case
        is exercised against a temporary copy instead of inventing placeholder kinds.
        """
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / pack_name
            shutil.copytree(source_pack, pack_path)
            overlay_path = pack_path / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["domain_pack"]["name"] = pack_name
            overlay["domain_pack"]["request_kinds"] = request_kinds
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))

            code, stdout, stderr = self.run_validator("--path", str(pack_path))

        self.assertEqual("", stderr)
        return code, json.loads(stdout)

    def assert_request_kinds_failure(self, payload: dict, *offending: str) -> None:
        checks = {check["id"]: check for check in payload["checks"]}
        request_kinds = checks["request_kinds"]
        self.assertEqual("fail", request_kinds["status"], request_kinds)
        self.assertEqual(["research.overlay.yml"], request_kinds["files"])
        self.assertEqual({}, payload["domain_pack"]["request_kinds"])
        for fragment in offending:
            self.assertIn(fragment, request_kinds["message"])

    def test_request_kinds_pass_when_pack_declares_none(self):
        code, stdout, stderr = self.run_validator("--path", "llm-research")
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        checks = {check["id"]: check for check in payload["checks"]}
        self.assertEqual("pass", checks["request_kinds"]["status"])
        self.assertEqual("No domain-pack request kinds declared.", checks["request_kinds"]["message"])
        self.assertEqual({}, payload["domain_pack"]["request_kinds"])

    def test_request_kinds_accept_valid_namespaced_declarations(self):
        code, payload = self.validate_pack_declaring_request_kinds(
            "market-data",
            [
                {
                    "id": "pack:market-data/supplier_quote",
                    "label": "Supplier quote",
                    "description": "Live SKU price + shipping + MOQ from a named supplier.",
                },
                {
                    "id": "pack:market-data/price_history",
                    "label": "Price history",
                    "description": "Historical price series for a single SKU.",
                },
            ],
        )

        self.assertEqual(0, code, payload)
        self.assertTrue(payload["ok"], payload)
        checks = {check["id"]: check for check in payload["checks"]}
        self.assertEqual("pass", checks["request_kinds"]["status"])
        self.assertEqual(
            "Domain pack declares 2 namespaced request kind(s).",
            checks["request_kinds"]["message"],
        )
        self.assertEqual(
            {
                "pack:market-data/supplier_quote": {
                    "label": "Supplier quote",
                    "description": "Live SKU price + shipping + MOQ from a named supplier.",
                },
                "pack:market-data/price_history": {
                    "label": "Price history",
                    "description": "Historical price series for a single SKU.",
                },
            },
            payload["domain_pack"]["request_kinds"],
        )

    def test_request_kinds_reject_malformed_id(self):
        code, payload = self.validate_pack_declaring_request_kinds(
            "market-data",
            [
                {
                    "id": "pack:market-data/Supplier Quote",
                    "label": "Supplier quote",
                    "description": "Uppercase and whitespace are not valid id characters.",
                }
            ],
        )

        self.assertEqual(1, code)
        self.assertFalse(payload["ok"], payload)
        self.assert_request_kinds_failure(
            payload,
            "domain_pack.request_kinds[0].id",
            "pack:market-data/Supplier Quote",
            "pack:<pack-name>/<kind-id>",
        )

    def test_request_kinds_reject_unprefixed_id_and_name_the_prefixed_form(self):
        code, payload = self.validate_pack_declaring_request_kinds(
            "market-data",
            [
                {
                    "id": "market-data/supplier_quote",
                    "label": "Supplier quote",
                    "description": "The reserved pack: prefix is missing.",
                }
            ],
        )

        self.assertEqual(1, code)
        self.assertFalse(payload["ok"], payload)
        self.assert_request_kinds_failure(
            payload,
            "domain_pack.request_kinds[0].id",
            "market-data/supplier_quote",
            "did you mean pack:market-data/supplier_quote?",
        )

    def test_request_kinds_reject_namespace_that_is_not_the_pack_name(self):
        code, payload = self.validate_pack_declaring_request_kinds(
            "market-data",
            [
                {
                    "id": "pack:other-pack/supplier_quote",
                    "label": "Supplier quote",
                    "description": "A pack may not declare kinds in another pack's namespace.",
                }
            ],
        )

        self.assertEqual(1, code)
        self.assertFalse(payload["ok"], payload)
        self.assert_request_kinds_failure(
            payload,
            "domain_pack.request_kinds[0].id",
            "pack:other-pack/supplier_quote",
            "'other-pack'",
            "'market-data'",
        )

    def test_request_kinds_reject_duplicate_ids(self):
        code, payload = self.validate_pack_declaring_request_kinds(
            "market-data",
            [
                {
                    "id": "pack:market-data/supplier_quote",
                    "label": "Supplier quote",
                    "description": "First declaration.",
                },
                {
                    "id": "pack:market-data/supplier_quote",
                    "label": "Supplier quote (again)",
                    "description": "Second declaration of the same id.",
                },
            ],
        )

        self.assertEqual(1, code)
        self.assertFalse(payload["ok"], payload)
        self.assert_request_kinds_failure(
            payload,
            "domain_pack.request_kinds[1].id",
            "pack:market-data/supplier_quote",
            "declared more than once",
        )

    def test_request_kinds_reject_missing_label_and_description(self):
        code, payload = self.validate_pack_declaring_request_kinds(
            "market-data",
            [{"id": "pack:market-data/supplier_quote"}],
        )

        self.assertEqual(1, code)
        self.assertFalse(payload["ok"], payload)
        self.assert_request_kinds_failure(
            payload,
            "domain_pack.request_kinds[0].label must be a non-empty string",
            "domain_pack.request_kinds[0].description must be a non-empty string",
        )

    def test_request_kinds_reject_builtin_shadowing(self):
        code, payload = self.validate_pack_declaring_request_kinds(
            "market-data",
            [
                {
                    "id": "dataset",
                    "label": "Dataset",
                    "description": "Built-in kinds stay reserved and cannot be redeclared.",
                }
            ],
        )

        self.assertEqual(1, code)
        self.assertFalse(payload["ok"], payload)
        self.assert_request_kinds_failure(
            payload,
            "domain_pack.request_kinds[0].id dataset",
            "reserved built-in kind",
        )

    def test_request_kinds_reject_non_list_declaration(self):
        code, payload = self.validate_pack_declaring_request_kinds(
            "market-data",
            {
                "pack:market-data/supplier_quote": {
                    "label": "Supplier quote",
                    "description": "A mapping is not the declared list form.",
                }
            },
        )

        self.assertEqual(1, code)
        self.assertFalse(payload["ok"], payload)
        self.assert_request_kinds_failure(
            payload,
            "domain_pack.request_kinds must be a list of kind declarations",
        )

    QUOTE_POLICY = "pack:market-data/quote-48h"
    QUOTE_VOCABULARY = {"freshness_policy": {QUOTE_POLICY: "A supplier quote must be at most 48 hours old."}}
    QUOTE_RULE = {"all_of": [{"max_age": {"field": "provenance/retrieved_at", "hours": 48}}]}

    def validate_pack_declaring_policy_rules(
        self,
        pack_name: str,
        policy_vocabularies,
        policy_rules,
        required_facet: dict | None = None,
        *,
        human_gated: bool = False,
    ) -> tuple[int, dict]:
        """Validate a throwaway copy of a shipped pack whose overlay declares ``policy_rules``.

        Shipped packs deliberately declare no policy rules, so every declaration case is
        exercised against a temporary copy instead of inventing placeholder rules.
        ``required_facet`` additionally writes and registers a coverage template carrying
        that facet, which is what puts a declared policy in front of the autonomy gate.
        """
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / pack_name
            shutil.copytree(source_pack, pack_path)
            overlay_path = pack_path / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["domain_pack"]["name"] = pack_name
            overlay["domain_pack"]["human_gated"] = human_gated
            overlay["domain_pack"]["policy_vocabularies"] = policy_vocabularies
            if policy_rules is not None:
                overlay["domain_pack"]["policy_rules"] = policy_rules
            if required_facet is not None:
                template_path = pack_path / "coverage-templates" / "rule-backed.yml"
                template_path.parent.mkdir(parents=True, exist_ok=True)
                template_path.write_text(
                    yaml.safe_dump(
                        {
                            "coverage_profile": "rule-backed",
                            "required_facets": [required_facet],
                            "optional_facets": [],
                        },
                        sort_keys=False,
                    )
                )
                overlay["domain_pack"]["coverage_templates"] = {"rule-backed": "coverage-templates/rule-backed.yml"}
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))

            code, stdout, stderr = self.run_validator("--path", str(pack_path))

        self.assertEqual("", stderr)
        return code, json.loads(stdout)

    def rule_backed_required_facet(self, freshness_policy: str) -> dict:
        return {
            "facet_id": "quote-freshness",
            "description": "A supplier quote must be at most 48 hours old.",
            "evidence_path": "vendor_product_spec",
            "source_policy": "official_vendor",
            "freshness_policy": freshness_policy,
            "identity_policy": "none",
            "min_sources": 1,
        }

    def assert_policy_rules_failure(self, payload: dict, *offending: str) -> None:
        checks = {check["id"]: check for check in payload["checks"]}
        policy_rules = checks["policy_rules"]
        self.assertEqual("fail", policy_rules["status"], policy_rules)
        self.assertEqual(["research.overlay.yml"], policy_rules["files"])
        self.assertEqual({}, payload["domain_pack"]["policy_rules"])
        for fragment in offending:
            self.assertIn(fragment, policy_rules["message"])

    def test_policy_rules_pass_when_pack_declares_none(self):
        code, stdout, stderr = self.run_validator("--path", "llm-research")
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        checks = {check["id"]: check for check in payload["checks"]}
        self.assertEqual("pass", checks["policy_rules"]["status"])
        self.assertEqual("No domain-pack policy rules declared.", checks["policy_rules"]["message"])
        self.assertEqual({}, payload["domain_pack"]["policy_rules"])

    def test_policy_rules_accept_valid_deterministic_declaration(self):
        code, payload = self.validate_pack_declaring_policy_rules(
            "market-data",
            {
                "freshness_policy": {self.QUOTE_POLICY: "A supplier quote must be at most 48 hours old."},
                "identity_policy": {"pack:market-data/sku-matches": "The quoted SKU must match the candidate."},
            },
            {
                self.QUOTE_POLICY: self.QUOTE_RULE,
                "pack:market-data/sku-matches": {
                    "manual_review_required": True,
                    "any_of": [
                        {"equals": {"field": "record/supplier_quote/sku", "question_field": "metadata/candidate_sku"}},
                        {"one_of_provenance": {"providers": ["partner-catalog"]}},
                    ],
                },
            },
        )

        self.assertEqual(0, code, payload)
        self.assertTrue(payload["ok"], payload)
        checks = {check["id"]: check for check in payload["checks"]}
        self.assertEqual("pass", checks["policy_rules"]["status"])
        self.assertEqual(
            "Domain pack declares 2 policy rule(s), 1 of which can route to manual review.",
            checks["policy_rules"]["message"],
        )
        self.assertEqual(
            {
                self.QUOTE_POLICY: {
                    "primitives": ["all_of", "max_age"],
                    "manual_review_required": False,
                    "manual_review_on_absence": False,
                    "section": "freshness_policy",
                },
                "pack:market-data/sku-matches": {
                    "primitives": ["any_of", "equals", "one_of_provenance"],
                    "manual_review_required": True,
                    "manual_review_on_absence": False,
                    "section": "identity_policy",
                },
            },
            payload["domain_pack"]["policy_rules"],
        )

    def test_policy_rules_reject_unknown_primitive(self):
        code, payload = self.validate_pack_declaring_policy_rules(
            "market-data",
            self.QUOTE_VOCABULARY,
            {self.QUOTE_POLICY: {"all_of": [{"no_such_primitive": {}}]}},
        )

        self.assertEqual(1, code)
        self.assertFalse(payload["ok"], payload)
        self.assert_policy_rules_failure(
            payload,
            f"domain_pack.policy_rules[{self.QUOTE_POLICY}]",
            "'no_such_primitive'",
            "max_age",
        )

    def test_policy_rules_reject_policy_absent_from_vocabularies(self):
        code, payload = self.validate_pack_declaring_policy_rules(
            "market-data",
            {"freshness_policy": {"pack:market-data/some-other-policy": "A different declared policy."}},
            {self.QUOTE_POLICY: self.QUOTE_RULE},
        )

        self.assertEqual(1, code)
        self.assertFalse(payload["ok"], payload)
        self.assert_policy_rules_failure(
            payload,
            f"domain_pack.policy_rules[{self.QUOTE_POLICY}]",
            "not declared under domain_pack.policy_vocabularies",
        )

    def test_policy_rules_reject_foreign_namespace(self):
        code, payload = self.validate_pack_declaring_policy_rules(
            "market-data",
            self.QUOTE_VOCABULARY,
            {"pack:other-pack/quote-48h": self.QUOTE_RULE},
        )

        self.assertEqual(1, code)
        self.assertFalse(payload["ok"], payload)
        self.assert_policy_rules_failure(
            payload,
            "domain_pack.policy_rules[pack:other-pack/quote-48h]",
            "'other-pack'",
            "'market-data'",
        )

    def test_rule_backed_policy_clears_a_required_facet_for_autonomous_validation(self):
        """A pack policy carrying a deterministic rule is no longer manual-only.

        The paired ``..._without_a_rule`` test removes only the rule, so together they
        show the autonomy gate turns on the rule rather than on anything else here.
        """
        code, payload = self.validate_pack_declaring_policy_rules(
            "market-data",
            self.QUOTE_VOCABULARY,
            {self.QUOTE_POLICY: self.QUOTE_RULE},
            required_facet=self.rule_backed_required_facet(self.QUOTE_POLICY),
        )

        self.assertEqual(0, code, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertFalse(payload["domain_pack"]["human_gated"])
        checks = {check["id"]: check for check in payload["checks"]}
        self.assertEqual("pass", checks["policy_rules"]["status"])
        self.assertEqual("pass", checks["autonomous_required_facets"]["status"], checks["autonomous_required_facets"])

    def test_required_facet_policy_without_a_rule_stays_manual_only(self):
        code, payload = self.validate_pack_declaring_policy_rules(
            "market-data",
            self.QUOTE_VOCABULARY,
            None,
            required_facet=self.rule_backed_required_facet(self.QUOTE_POLICY),
        )

        self.assertEqual(1, code)
        self.assertFalse(payload["ok"], payload)
        self.assertEqual({}, payload["domain_pack"]["policy_rules"])
        checks = {check["id"]: check for check in payload["checks"]}
        autonomy = checks["autonomous_required_facets"]
        self.assertEqual("fail", autonomy["status"], autonomy)
        self.assertIn(self.QUOTE_POLICY, autonomy["message"])
        self.assertIn("use a deterministic policy", autonomy["message"])

    def test_a_rule_on_an_id_declared_under_two_sections_is_refused(self):
        """`pack validate` refuses the ambiguity rather than letting it ship.

        A facet names one policy per section, so a rule on an id declared under several
        cannot say which field it decides. Refusing here is what makes the per-field
        manual-only subtraction below unambiguous for every pack that does ship.
        """
        shared = self.QUOTE_POLICY
        facet = self.rule_backed_required_facet("no_staleness_check")
        facet["identity_policy"] = shared
        code, payload = self.validate_pack_declaring_policy_rules(
            "market-data",
            {
                "freshness_policy": {shared: "A supplier quote must be at most 48 hours old."},
                "identity_policy": {shared: "The quoted SKU must match the candidate."},
            },
            {shared: self.QUOTE_RULE},
            required_facet=facet,
        )

        self.assertEqual(1, code)
        self.assert_policy_rules_failure(
            payload,
            "more than one rule-carrying vocabulary section",
            "freshness_policy, identity_policy",
        )

    def test_a_rule_exempts_only_the_section_it_decides(self):
        """The autonomy gate reads the rule's own section, not just its policy id.

        Declared under `freshness_policy` alone and used as a required facet's
        `identity_policy`, which no rule decides — so the facet still needs a human.
        """
        facet = self.rule_backed_required_facet("no_staleness_check")
        facet["identity_policy"] = "pack:market-data/sku-matches"
        code, payload = self.validate_pack_declaring_policy_rules(
            "market-data",
            {
                **self.QUOTE_VOCABULARY,
                "identity_policy": {"pack:market-data/sku-matches": "The quoted SKU must match."},
            },
            {self.QUOTE_POLICY: self.QUOTE_RULE},
            required_facet=facet,
        )

        self.assertEqual(1, code)
        checks = {check["id"]: check for check in payload["checks"]}
        autonomy = checks["autonomous_required_facets"]
        self.assertEqual("fail", autonomy["status"], autonomy)
        self.assertIn("pack:market-data/sku-matches", autonomy["message"])
        self.assertEqual("freshness_policy", payload["domain_pack"]["policy_rules"][self.QUOTE_POLICY]["section"])

    def test_rule_requiring_manual_review_keeps_a_required_facet_manual_only(self):
        code, payload = self.validate_pack_declaring_policy_rules(
            "market-data",
            self.QUOTE_VOCABULARY,
            {self.QUOTE_POLICY: {"manual_review_required": True, **self.QUOTE_RULE}},
            required_facet=self.rule_backed_required_facet(self.QUOTE_POLICY),
        )

        self.assertEqual(1, code)
        self.assertFalse(payload["ok"], payload)
        checks = {check["id"]: check for check in payload["checks"]}
        self.assertEqual("pass", checks["policy_rules"]["status"])
        self.assertTrue(payload["domain_pack"]["policy_rules"][self.QUOTE_POLICY]["manual_review_required"])
        autonomy = checks["autonomous_required_facets"]
        self.assertEqual("fail", autonomy["status"], autonomy)
        self.assertIn(self.QUOTE_POLICY, autonomy["message"])

    def test_conditional_review_rule_requires_a_human_capable_pack_for_required_facet(self):
        rule = {
            "all_of": [
                {
                    "equals": {
                        "field": "record/supplier_quote/sku",
                        "value": "B0ABC12345",
                        "when_absent": "manual_review",
                    }
                }
            ]
        }
        code, payload = self.validate_pack_declaring_policy_rules(
            "market-data",
            self.QUOTE_VOCABULARY,
            {self.QUOTE_POLICY: rule},
            required_facet=self.rule_backed_required_facet(self.QUOTE_POLICY),
        )

        self.assertEqual(1, code)
        summary = payload["domain_pack"]["policy_rules"][self.QUOTE_POLICY]
        self.assertFalse(summary["manual_review_required"])
        self.assertTrue(summary["manual_review_on_absence"])
        autonomy = {check["id"]: check for check in payload["checks"]}["autonomous_required_facets"]
        self.assertEqual("fail", autonomy["status"], autonomy)
        self.assertIn("can require manual review", autonomy["message"])
        self.assertIn("human_gated: true", autonomy["message"])
        self.assertNotIn("manual-only", autonomy["message"])
        self.assertNotIn("use a deterministic policy", autonomy["message"])

    def test_conditional_review_rule_is_allowed_when_required_facet_is_human_gated(self):
        rule = {
            "all_of": [
                {
                    "equals": {
                        "field": "record/supplier_quote/sku",
                        "value": "B0ABC12345",
                        "when_absent": "manual_review",
                    }
                }
            ]
        }
        code, payload = self.validate_pack_declaring_policy_rules(
            "market-data",
            self.QUOTE_VOCABULARY,
            {self.QUOTE_POLICY: rule},
            required_facet=self.rule_backed_required_facet(self.QUOTE_POLICY),
            human_gated=True,
        )

        self.assertEqual(0, code, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(payload["domain_pack"]["human_gated"])
        summary = payload["domain_pack"]["policy_rules"][self.QUOTE_POLICY]
        self.assertTrue(summary["manual_review_on_absence"])
        autonomy = {check["id"]: check for check in payload["checks"]}["autonomous_required_facets"]
        self.assertEqual("pass", autonomy["status"], autonomy)
        self.assertIn("can require manual review", autonomy["message"])

    def test_recommended_acquisition_rejects_unknown_provider(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "bad-provider-pack"
            shutil.copytree(source_pack, pack_path)
            overlay_path = pack_path / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["domain_pack"]["name"] = "bad-provider-pack"
            overlay["domain_pack"]["recommended_acquisition"] = ["arxiv", "unknown-provider"]
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        self.assertFalse(payload["ok"], payload)
        failures = [check for check in payload["checks"] if check["status"] == "fail"]
        self.assertTrue(
            any(check["id"] == "recommended_acquisition" and "unknown-provider" in check["message"] for check in failures),
            failures,
        )

    def test_coverage_templates_reject_unknown_policy_identifier(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "bad-coverage-template-pack"
            shutil.copytree(source_pack, pack_path)
            template_path = pack_path / "coverage-templates" / "bad-template.yml"
            template_path.parent.mkdir(parents=True, exist_ok=True)
            template_path.write_text(
                yaml.safe_dump(
                    {
                        "coverage_profile": "bad-template",
                        "required_facets": [
                            {
                                "facet_id": "bad-policy",
                                "description": "This facet uses an invalid policy.",
                                "evidence_path": "academic_method_existence",
                                "source_policy": "whatever_the_web_said",
                                "freshness_policy": "publication_identity",
                                "identity_policy": "citation_id_resolves",
                                "min_sources": 1,
                            }
                        ],
                        "optional_facets": [],
                    },
                    sort_keys=False,
                )
            )
            overlay_path = pack_path / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["domain_pack"]["name"] = "bad-coverage-template-pack"
            overlay["domain_pack"]["coverage_templates"] = {"bad-template": "coverage-templates/bad-template.yml"}
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        failures = [check for check in payload["checks"] if check["status"] == "fail"]
        self.assertTrue(
            any(check["id"] == "coverage_templates" and "whatever_the_web_said" in check["message"] for check in failures),
            failures,
        )

    def test_coverage_templates_accept_e39_policy_vocabulary(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "e39-vocabulary-pack"
            shutil.copytree(source_pack, pack_path)
            template_path = pack_path / "coverage-templates" / "e39-vocabulary.yml"
            template_path.parent.mkdir(parents=True, exist_ok=True)
            template_path.write_text(
                yaml.safe_dump(
                    {
                        "coverage_profile": "e39-vocabulary",
                        "required_facets": [
                            {
                                "facet_id": "indexed-record",
                                "description": "Accept a scholarly record from OpenAlex or arXiv.",
                                "evidence_path": "academic_method_existence",
                                "source_policy": "openalex_or_arxiv",
                                "freshness_policy": "no_staleness_check",
                                "identity_policy": "none",
                                "min_sources": 1,
                            }
                        ],
                        "optional_facets": [
                            {
                                "facet_id": "manual-review",
                                "description": "Allow a domain-pack-specific manual review rule.",
                                "evidence_path": "vendor_product_spec",
                                "source_policy": "domain_pack_allowed",
                                "freshness_policy": "manual_review",
                                "identity_policy": "origin_url_matches_candidate",
                                "min_sources": 0,
                            }
                        ],
                    },
                    sort_keys=False,
                )
            )
            overlay_path = pack_path / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["domain_pack"]["name"] = "e39-vocabulary-pack"
            overlay["domain_pack"]["coverage_templates"] = {"e39-vocabulary": "coverage-templates/e39-vocabulary.yml"}
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(
            {"e39-vocabulary": "coverage-templates/e39-vocabulary.yml"},
            payload["domain_pack"]["coverage_templates"],
        )

    def test_policy_vocabularies_reject_standards_base_policy_redefinition(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "standards-policy-collision-pack"
            shutil.copytree(source_pack, pack_path)
            overlay_path = pack_path / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["domain_pack"]["name"] = "standards-policy-collision-pack"
            overlay["domain_pack"]["policy_vocabularies"] = {
                "source_policy": {
                    "official_standards_registry": "Attempt to redefine a base standards policy.",
                },
            }
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        self.assertFalse(payload["ok"], payload)
        failures = [check for check in payload["checks"] if check["status"] == "fail"]
        self.assertTrue(
            any(
                check["id"] == "policy_vocabularies"
                and "official_standards_registry" in check["message"]
                and "namespaced id" in check["message"]
                for check in failures
            ),
            failures,
        )

    def test_required_manual_only_policy_requires_human_gated_pack(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "manual-required-pack"
            shutil.copytree(source_pack, pack_path)
            template_path = pack_path / "coverage-templates" / "manual-required.yml"
            template_path.write_text(
                yaml.safe_dump(
                    {
                        "coverage_profile": "manual-required",
                        "required_facets": [
                            {
                                "facet_id": "needs-review",
                                "description": "This facet cannot ship autonomously.",
                                "evidence_path": "vendor_product_spec",
                                "source_policy": "manual_review_required",
                                "freshness_policy": "no_staleness_check",
                                "identity_policy": "none",
                                "min_sources": 1,
                            }
                        ],
                        "optional_facets": [],
                    },
                    sort_keys=False,
                )
            )
            overlay_path = pack_path / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["domain_pack"]["name"] = "manual-required-pack"
            overlay["domain_pack"]["coverage_templates"] = {"manual-required": "coverage-templates/manual-required.yml"}
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        self.assertFalse(payload["ok"], payload)
        self.assertFalse(payload["domain_pack"]["human_gated"])
        failures = [check for check in payload["checks"] if check["status"] == "fail"]
        self.assertTrue(
            any(
                check["id"] == "autonomous_required_facets"
                and "manual_review_required" in check["message"]
                and "human_gated: true" in check["message"]
                for check in failures
            ),
            failures,
        )

    def test_required_manual_only_policy_is_allowed_when_pack_is_human_gated(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "human-gated-pack"
            shutil.copytree(source_pack, pack_path)
            template_path = pack_path / "coverage-templates" / "manual-required.yml"
            template_path.write_text(
                yaml.safe_dump(
                    {
                        "coverage_profile": "manual-required",
                        "required_facets": [
                            {
                                "facet_id": "needs-review",
                                "description": "This facet intentionally requires human review.",
                                "evidence_path": "vendor_product_spec",
                                "source_policy": "manual_review_required",
                                "freshness_policy": "manual_review",
                                "identity_policy": "none",
                                "min_sources": 1,
                            }
                        ],
                        "optional_facets": [],
                    },
                    sort_keys=False,
                )
            )
            overlay_path = pack_path / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["domain_pack"]["name"] = "human-gated-pack"
            overlay["domain_pack"]["human_gated"] = True
            overlay["domain_pack"]["coverage_templates"] = {"manual-required": "coverage-templates/manual-required.yml"}
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(payload["domain_pack"]["human_gated"])
        checks = {check["id"]: check for check in payload["checks"]}
        self.assertEqual("pass", checks["autonomous_required_facets"]["status"])

    def test_coverage_templates_reject_unsafe_pack_path(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "unsafe-coverage-template-pack"
            shutil.copytree(source_pack, pack_path)
            overlay_path = pack_path / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["domain_pack"]["name"] = "unsafe-coverage-template-pack"
            overlay["domain_pack"]["coverage_templates"] = {"unsafe-template": "../outside.yml"}
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        failures = [check for check in payload["checks"] if check["status"] == "fail"]
        self.assertTrue(
            any(check["id"] == "coverage_templates" and "must not contain '..'" in check["message"] for check in failures),
            failures,
        )

    def test_pack_tree_rejects_executable_content_before_smoke_execution(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "executable-content-pack"
            shutil.copytree(source_pack, pack_path)
            (pack_path / "install.py").write_text("raise RuntimeError('must never execute')\n", encoding="utf-8")

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        checks = {check["id"]: check for check in payload["checks"]}
        self.assertEqual("fail", checks["pack_tree_safety"]["status"])
        self.assertIn("install.py", checks["pack_tree_safety"]["files"])
        self.assertEqual("fail", checks["smoke_validation"]["status"])
        self.assertIn("pack tree safety", checks["smoke_validation"]["message"])

    def test_pack_tree_rejects_symlinked_content(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pack_path = root / "symlink-content-pack"
            shutil.copytree(source_pack, pack_path)
            outside = root / "outside.md"
            outside.write_text("# Outside\n", encoding="utf-8")
            link = pack_path / "linked.md"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks are unavailable on this platform: {exc}")

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        checks = {check["id"]: check for check in payload["checks"]}
        self.assertEqual("fail", checks["pack_tree_safety"]["status"])
        self.assertIn("linked.md", checks["pack_tree_safety"]["files"])

    @unittest.skipUnless(os.name == "posix", "literal backslashes are path characters on POSIX")
    def test_pack_tree_rejects_backslashes_in_root_and_nested_names(self):
        source_pack = REPO_ROOT / "domain-packs" / "general-science"
        for location in ("root", "nested"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                pack_path = root / ("unsafe\\pack" if location == "root" else "portable-pack")
                shutil.copytree(source_pack, pack_path)
                overlay_path = pack_path / "research.overlay.yml"
                overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
                overlay["domain_pack"]["name"] = pack_path.name
                overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8")
                if location == "nested":
                    unsafe_directory = pack_path / "unsafe\\component"
                    unsafe_directory.mkdir()
                    (unsafe_directory / "note.md").write_text("unsafe\n", encoding="utf-8")

                code, stdout, stderr = self.run_validator("--path", str(pack_path))
                payload = json.loads(stdout)

                self.assertEqual(1, code)
                self.assertEqual("", stderr)
                checks = {item["id"]: item for item in payload["checks"]}
                self.assertEqual("fail", checks["pack_tree_safety"]["status"])
                self.assertIn("non-portable", checks["pack_tree_safety"]["message"])
                self.assertIn("\\", checks["pack_tree_safety"]["message"])

    def test_pack_tree_rejects_portable_path_collision(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "portable-pack"
            shutil.copytree(source_pack, pack_path)
            (pack_path / "A.md").write_text("upper\n", encoding="utf-8")
            (pack_path / "a.md").write_text("lower\n", encoding="utf-8")
            if len({path.name for path in pack_path.iterdir() if path.name.casefold() == "a.md"}) < 2:
                self.skipTest("filesystem does not preserve case-distinct names")

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        checks = {check["id"]: check for check in payload["checks"]}
        self.assertEqual("fail", checks["pack_tree_safety"]["status"])
        self.assertIn("a.md", checks["pack_tree_safety"]["files"])

    def test_corrupted_pack_reports_missing_referenced_file(self):
        pack_path = REPO_ROOT / "tests" / "fixtures" / "domain-packs" / "corrupt-missing-scaffold"

        code, stdout, stderr = self.run_validator("--path", str(pack_path))
        payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        self.assertFalse(payload["ok"], payload)
        failures = [check for check in payload["checks"] if check["status"] == "fail"]
        self.assertTrue(failures, payload["checks"])
        self.assertTrue(
            any("scaffolds/missing.md" in check.get("files", []) for check in failures),
            failures,
        )
        self.assertTrue(
            any("missing" in check["message"].lower() for check in failures),
            failures,
        )

    def test_missing_pack_json_error_uses_shared_error_envelope(self):
        code, stdout, stderr = self.run_validator("--path", "does-not-exist", "--format", "json")
        envelope = json.loads(stderr)

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("1.0", envelope["schema_version"])
        self.assertIn("error_code", envelope)
        self.assertIn("Domain pack not found", envelope["message"])
        self.assertIn("remediation", envelope)


if __name__ == "__main__":
    unittest.main()
