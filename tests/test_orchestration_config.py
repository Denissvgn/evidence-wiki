"""The optional `research.yml` `orchestration:` section.

The section decides whether this workspace acquires evidence through its own providers or
hands acquisition to an external acquirer. Getting it wrong is silent in both directions —
a workspace that believes it delegates but reads `providers` never issues an acquisition
order at all, and one that reads `delegated` without a declared acquirer would address
work orders to nobody — so every rejection is covered here rather than left to a default.
"""

import importlib.util
import re
import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
TEMPLATE_RESEARCH_YML = REPO_ROOT / "workspace-template" / "research.yml"
RESEARCH_YML_DOC = REPO_ROOT / "workspace-template" / "docs" / "research-yml.md"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CONFIG = load_script_module("research_orchestration_config", "_orchestration_config.py")

PROVIDERS_DEFAULTS = {
    "acquisition_mode": "providers",
    "acquirer_agent_id": None,
    "max_attempts_per_request": 2,
}


def delegated(**overrides):
    section = {"acquisition": "delegated", "acquirer_agent_id": "autoseller-orchestrator"}
    section.update(overrides)
    return {"orchestration": section}


class OrchestrationConfigTests(unittest.TestCase):
    def assertRejected(self, document, *, contains: str):
        with self.assertRaises(CONFIG.OrchestrationConfigError) as caught:
            CONFIG.orchestration_config(document)
        self.assertEqual("CONFIG_INVALID", caught.exception.error_code)
        self.assertIn(contains, caught.exception.message)
        self.assertTrue(caught.exception.remediation)
        return caught.exception

    # -- absence and defaults ----------------------------------------------------

    def test_absent_section_uses_documented_defaults(self):
        self.assertEqual(PROVIDERS_DEFAULTS, CONFIG.orchestration_config({"project": {"name": "fixture"}}))

    def test_empty_and_null_sections_match_absence(self):
        self.assertEqual(PROVIDERS_DEFAULTS, CONFIG.orchestration_config({}))
        self.assertEqual(PROVIDERS_DEFAULTS, CONFIG.orchestration_config({"orchestration": None}))
        self.assertEqual(PROVIDERS_DEFAULTS, CONFIG.orchestration_config({"orchestration": {}}))

    def test_explicit_providers_matches_the_default(self):
        self.assertEqual(
            PROVIDERS_DEFAULTS,
            CONFIG.orchestration_config({"orchestration": {"acquisition": "providers"}}),
        )

    def test_a_non_dict_config_is_treated_as_absent(self):
        # Callers pass whatever `research.yml` parsed to; a scalar document has no section.
        self.assertEqual(PROVIDERS_DEFAULTS, CONFIG.orchestration_config(None))

    # -- delegated declarations --------------------------------------------------

    def test_delegated_declaration_round_trips(self):
        self.assertEqual(
            {
                "acquisition_mode": "delegated",
                "acquirer_agent_id": "autoseller-orchestrator",
                "max_attempts_per_request": 2,
            },
            CONFIG.orchestration_config(delegated()),
        )

    def test_explicit_attempts_budget_is_kept(self):
        settings = CONFIG.orchestration_config(delegated(max_attempts_per_request=5))
        self.assertEqual(5, settings["max_attempts_per_request"])

    def test_boundary_attempt_budgets_are_accepted(self):
        for value in (1, CONFIG.MAX_MAX_ATTEMPTS_PER_REQUEST):
            with self.subTest(value=value):
                settings = CONFIG.orchestration_config(delegated(max_attempts_per_request=value))
                self.assertEqual(value, settings["max_attempts_per_request"])

    def test_surrounding_whitespace_is_stripped(self):
        settings = CONFIG.orchestration_config(
            {"orchestration": {"acquisition": " delegated ", "acquirer_agent_id": "  acquirer-1  "}}
        )
        self.assertEqual("delegated", settings["acquisition_mode"])
        self.assertEqual("acquirer-1", settings["acquirer_agent_id"])

    def test_is_delegated_reads_validated_settings(self):
        self.assertTrue(CONFIG.is_delegated(CONFIG.orchestration_config(delegated())))
        self.assertFalse(CONFIG.is_delegated(CONFIG.orchestration_config({})))

    # -- section shape -----------------------------------------------------------

    def test_non_mapping_section_is_rejected(self):
        self.assertRejected({"orchestration": "delegated"}, contains="must be a mapping")
        self.assertRejected({"orchestration": []}, contains="must be a mapping")

    def test_unknown_section_key_is_rejected(self):
        self.assertRejected(
            {"orchestration": {"acquisition_mode": "delegated"}},
            contains="unknown keys: acquisition_mode",
        )

    def test_experimental_prefixed_key_is_allowed(self):
        settings = CONFIG.orchestration_config(delegated(**{"x-approval-queue": "queue-42"}))
        self.assertEqual("delegated", settings["acquisition_mode"])

    # -- acquisition mode --------------------------------------------------------

    def test_unknown_acquisition_mode_is_rejected(self):
        exception = self.assertRejected(
            {"orchestration": {"acquisition": "external"}},
            contains="orchestration.acquisition rejected value 'external'",
        )
        # The message names the alternatives, so an operator does not have to open docs.
        self.assertIn("providers, delegated", exception.message)

    def test_non_string_acquisition_mode_is_rejected(self):
        for value in (True, 1, ["delegated"]):
            with self.subTest(value=value):
                self.assertRejected(
                    {"orchestration": {"acquisition": value}},
                    contains="orchestration.acquisition rejected value",
                )

    # -- acquirer agent id -------------------------------------------------------

    def test_delegation_without_an_acquirer_is_rejected(self):
        self.assertRejected(
            {"orchestration": {"acquisition": "delegated"}},
            contains="orchestration.acquirer_agent_id is required",
        )

    def test_empty_or_whitespace_acquirer_is_rejected(self):
        for value in ("", "   ", "\t"):
            with self.subTest(value=value):
                self.assertRejected(
                    delegated(acquirer_agent_id=value),
                    contains="orchestration.acquirer_agent_id rejected value",
                )

    def test_non_string_acquirer_is_rejected(self):
        for value in (True, 7, ["acquirer"], {"id": "acquirer"}):
            with self.subTest(value=value):
                self.assertRejected(
                    delegated(acquirer_agent_id=value),
                    contains="orchestration.acquirer_agent_id rejected value",
                )

    def test_control_characters_in_the_acquirer_are_rejected(self):
        # A work order carries this id into durable JSON that hosts parse and log.
        for value in ("acquirer\nid", "acquirer\x00id", "acquirer\x7fid"):
            with self.subTest(value=repr(value)):
                self.assertRejected(
                    delegated(acquirer_agent_id=value),
                    contains="orchestration.acquirer_agent_id rejected value",
                )

    def test_overlong_acquirer_is_rejected_at_the_boundary(self):
        longest = "a" * CONFIG.MAX_AGENT_ID_LENGTH
        self.assertEqual(
            longest,
            CONFIG.orchestration_config(delegated(acquirer_agent_id=longest))["acquirer_agent_id"],
        )
        self.assertRejected(
            delegated(acquirer_agent_id=longest + "a"),
            contains="orchestration.acquirer_agent_id rejected value",
        )

    # -- attempts budget ---------------------------------------------------------

    def test_non_integer_attempt_budget_is_rejected(self):
        for value in ("2", 2.5, True, [2]):
            with self.subTest(value=value):
                self.assertRejected(
                    delegated(max_attempts_per_request=value),
                    contains="max_attempts_per_request must be a positive integer",
                )

    def test_null_attempt_budget_is_rejected_rather_than_meaning_unlimited(self):
        # `review.max_pending_review_hours` uses null to disable a check. Reusing that
        # spelling here would spell "retry forever", which is the one thing the exhaustion
        # bound exists to prevent.
        exception = self.assertRejected(
            delegated(max_attempts_per_request=None),
            contains="max_attempts_per_request must be a positive integer",
        )
        self.assertIn("no unlimited value", exception.message)

    def test_out_of_range_attempt_budget_is_rejected(self):
        for value in (0, -1, CONFIG.MAX_MAX_ATTEMPTS_PER_REQUEST + 1):
            with self.subTest(value=value):
                self.assertRejected(
                    delegated(max_attempts_per_request=value),
                    contains="max_attempts_per_request must be between 1 and",
                )

    # -- delegated-only keys under providers -------------------------------------

    def test_acquirer_under_providers_is_rejected(self):
        exception = self.assertRejected(
            {"orchestration": {"acquisition": "providers", "acquirer_agent_id": "acquirer-1"}},
            contains="declares acquirer_agent_id under acquisition: providers",
        )
        self.assertIn("Set acquisition: delegated", exception.message)

    def test_delegated_only_keys_under_the_default_mode_are_rejected(self):
        # No `acquisition:` key at all means providers. A section carrying only
        # delegated-only keys is the likeliest way to believe delegation is on.
        self.assertRejected(
            {"orchestration": {"acquirer_agent_id": "acquirer-1"}},
            contains="declares acquirer_agent_id under acquisition: providers",
        )
        self.assertRejected(
            {"orchestration": {"max_attempts_per_request": 3}},
            contains="declares max_attempts_per_request under acquisition: providers",
        )

    def test_every_delegated_only_key_is_refused_under_providers(self):
        # One rule, not a per-key special case: a future delegated-only key is covered by
        # being listed in DELEGATED_ONLY_KEYS. This fails if one is added but not refused.
        for key in CONFIG.DELEGATED_ONLY_KEYS:
            with self.subTest(key=key):
                self.assertRejected(
                    {"orchestration": {"acquisition": "providers", key: 1}},
                    contains=f"declares {key} under acquisition: providers",
                )

    def test_multiple_delegated_only_keys_are_named_together(self):
        exception = self.assertRejected(
            {"orchestration": {"acquirer_agent_id": "acquirer-1", "max_attempts_per_request": 3}},
            contains="acquirer_agent_id, max_attempts_per_request",
        )
        self.assertIn("them", exception.message)


class AgentIdRuleTests(unittest.TestCase):
    """The declared acquirer and a session owner must be judged by one rule.

    `orchestration_controller.require_agent_id` validates `--agent-id`; this reader cannot
    call it (it raises the controller's error type), so the rule lives here as a predicate.
    These cases fail if either definition changes alone.
    """

    CONTROLLER = load_script_module("orchestration_config_controller", "orchestration_controller.py")

    CASES = (
        "agent",
        "autoseller-orchestrator",
        "Agent With Spaces",
        "ag€nt-ünïcode",
        "a" * CONFIG.MAX_AGENT_ID_LENGTH,
        "  padded  ",
        "",
        "   ",
        "a" * (CONFIG.MAX_AGENT_ID_LENGTH + 1),
        "agent\nid",
        "agent\x00id",
        "agent\x7fid",
        None,
        7,
        True,
        ["agent"],
    )

    def controller_accepts(self, value):
        try:
            return self.CONTROLLER.require_agent_id(value)
        except self.CONTROLLER.OrchestrationControllerError:
            return None

    def test_predicate_agrees_with_the_controller(self):
        for value in self.CASES:
            with self.subTest(value=repr(value)):
                accepted = self.controller_accepts(value)
                self.assertEqual(accepted is not None, CONFIG.valid_agent_id(value))

    def test_accepted_values_normalize_identically(self):
        for value in self.CASES:
            accepted = self.controller_accepts(value)
            if accepted is None:
                continue
            with self.subTest(value=repr(value)):
                settings = CONFIG.orchestration_config(
                    {"orchestration": {"acquisition": "delegated", "acquirer_agent_id": value}}
                )
                self.assertEqual(accepted, settings["acquirer_agent_id"])

    def test_the_case_table_covers_both_verdicts(self):
        verdicts = {CONFIG.valid_agent_id(value) for value in self.CASES}
        self.assertEqual({True, False}, verdicts, "the drift table must exercise both outcomes")


class PublishedContractDriftTests(unittest.TestCase):
    """The wire contract and the config reader must agree on the mode vocabulary.

    `src/evidence_wiki/orchestration_schemas.py` declares the modes literally, because the
    package publishes the contract without loading workspace code. That is the right
    boundary and the reason this check exists: a mode accepted by `research.yml` but
    missing from the published enum would produce sessions that fail a host's own
    validation.
    """

    def test_published_enum_matches_the_config_vocabulary(self):
        sys.path.insert(0, str(REPO_ROOT / "src"))
        try:
            from evidence_wiki import orchestration_schemas
        finally:
            sys.path.remove(str(REPO_ROOT / "src"))

        self.assertEqual(tuple(orchestration_schemas.ACQUISITION_MODES), CONFIG.ACQUISITION_MODES)
        self.assertEqual(
            list(CONFIG.ACQUISITION_MODES),
            orchestration_schemas.ORCHESTRATION_SESSION_SCHEMA["properties"]["acquisition_mode"]["enum"],
        )

    def test_the_managed_host_refusal_keys_on_the_same_mode_name(self):
        # The host declares the name literally: it is imported by orchestration_schemas
        # (so it cannot import back) and never loads workspace code into its process.
        # A rename here would silently stop the managed runners refusing.
        sys.path.insert(0, str(REPO_ROOT / "src"))
        try:
            from evidence_wiki import orchestration
        finally:
            sys.path.remove(str(REPO_ROOT / "src"))

        self.assertEqual(CONFIG.ACQUISITION_MODE_DELEGATED, orchestration.DELEGATED_ACQUISITION_MODE)

    def test_published_session_schema_keeps_the_fields_optional(self):
        sys.path.insert(0, str(REPO_ROOT / "src"))
        try:
            from evidence_wiki import orchestration_schemas
        finally:
            sys.path.remove(str(REPO_ROOT / "src"))

        schema = orchestration_schemas.ORCHESTRATION_SESSION_SCHEMA
        for field in ("acquisition_mode", "acquirer_agent_id", "max_attempts_per_request"):
            with self.subTest(field=field):
                self.assertIn(field, schema["properties"])
                # Sessions created before delegation existed carry none of them.
                self.assertNotIn(field, schema["required"])


class DocumentedExampleTests(unittest.TestCase):
    """The examples an operator copies must validate.

    The template ships the section commented out, so nothing exercises it at runtime;
    without this, a broken example could sit there indefinitely and fail only for the
    first person who uncommented it.
    """

    def test_template_ships_the_section_commented_out(self):
        document = yaml.safe_load(TEMPLATE_RESEARCH_YML.read_text(encoding="utf-8"))
        self.assertNotIn(
            "orchestration",
            document,
            "the template must not declare an acquisition mode; delegation is opt-in",
        )
        self.assertEqual(PROVIDERS_DEFAULTS, CONFIG.orchestration_config(document))

    def test_commented_template_example_validates_when_uncommented(self):
        lines: list[str] = []
        capturing = False
        for line in TEMPLATE_RESEARCH_YML.read_text(encoding="utf-8").splitlines():
            if line.startswith("# orchestration:"):
                capturing = True
            if capturing:
                if not line.startswith("#"):
                    break
                lines.append(re.sub(r"^# ?", "", line))
        self.assertTrue(lines, "template lost its commented orchestration example")

        settings = CONFIG.orchestration_config(yaml.safe_load("\n".join(lines)))
        self.assertEqual("delegated", settings["acquisition_mode"])
        self.assertEqual("autoseller-orchestrator", settings["acquirer_agent_id"])
        self.assertEqual(2, settings["max_attempts_per_request"])

    def test_documented_example_validates(self):
        match = re.search(
            r"```yaml\n(orchestration:\n.*?)```",
            RESEARCH_YML_DOC.read_text(encoding="utf-8"),
            re.DOTALL,
        )
        self.assertIsNotNone(match, "docs lost the orchestration example")

        settings = CONFIG.orchestration_config(yaml.safe_load(match.group(1)))
        self.assertEqual("delegated", settings["acquisition_mode"])

    def test_docs_describe_every_key_and_the_delegation_stance(self):
        doc = RESEARCH_YML_DOC.read_text(encoding="utf-8")
        self.assertIn("### `orchestration`", doc)
        for expected in ("`acquisition`", "`acquirer_agent_id`", "`max_attempts_per_request`"):
            self.assertIn(expected, doc)

        # Collapse wrapping: where a sentence breaks is not something a test should pin.
        collapsed = re.sub(r"\s+", " ", doc)
        self.assertIn("Delegation is not a provider grant", collapsed)
        self.assertIn("no unlimited value", collapsed)
        self.assertIn("counted per session", collapsed)


if __name__ == "__main__":
    unittest.main()
