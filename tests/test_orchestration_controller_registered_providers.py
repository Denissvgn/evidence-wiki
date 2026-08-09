"""The controller's provider policy must understand registered providers.

`provider_policy` is the controller's answer to "what may this workspace reach", and
it is recomputed on every replay through `verify_provider_policy_unchanged`. Once a
provider id can come from an installed distribution rather than a built-in tuple, that
recomputation acquires a second input the workspace does not own: the environment.

These tests pin three properties. An authorized id the environment cannot supply keeps
the `CONFIG_INVALID` code hosts already parse -- a missing distribution and a typo are
the same observation here, so promoting one would have re-labelled the other -- while
gaining a remediation that names installing a distribution as a fix. A distribution that
vanishes mid-session is caught by the recomputation itself rather than by the drift
diff, which matters because the drift remediation tells an operator to restore an
allow-list they never narrowed. And with nothing installed, every message is byte-for-byte
what it was before registration existed.
"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"

sys.path.insert(0, str(REPO_ROOT))
from tests._provider_plugin_fixture import (  # noqa: E402
    ACQUISITION_PROVIDER_ID,
    DISCOVERY_PROVIDER_ID,
    installed_provider_plugins,
)


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CONTROLLER = load_script_module("registered_providers_controller", SCRIPTS / "orchestration_controller.py")
PLUGINS = load_script_module("registered_providers_plugins", SCRIPTS / "_provider_plugins.py")


def config_authorizing(phase: str, providers: list[str]) -> dict:
    return {"integrations": {phase: {"enabled": True, "providers": providers}}}


class ControllerRegisteredProviderPolicyTests(unittest.TestCase):
    """`provider_policy` accepts a registered id only where its distribution exists."""

    def setUp(self):
        PLUGINS.clear_cache()

    def tearDown(self):
        PLUGINS.clear_cache()

    def test_installed_registered_provider_is_authorized_like_a_builtin(self):
        for phase, provider_id in (
            ("acquisition", ACQUISITION_PROVIDER_ID),
            ("discovery", DISCOVERY_PROVIDER_ID),
        ):
            with self.subTest(phase=phase):
                with installed_provider_plugins():
                    PLUGINS.clear_cache()
                    policy = CONTROLLER.provider_policy(config_authorizing(phase, [provider_id]))
                self.assertEqual([provider_id], policy[phase]["providers"])
                self.assertTrue(policy[phase]["enabled"])

    def test_authorized_but_uninstalled_provider_keeps_the_config_invalid_code(self):
        """The code a host parses is unchanged; the remediation gains the install route.

        A missing distribution and a typo are the same observation to the package, so
        promoting one of them to a new code would have re-labelled the other too.
        """
        with self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught:
            CONTROLLER.provider_policy(config_authorizing("acquisition", [ACQUISITION_PROVIDER_ID]))
        error = caught.exception
        self.assertEqual("CONFIG_INVALID", error.error_code)
        self.assertIn(ACQUISITION_PROVIDER_ID, error.details["unresolved_provider_ids"])
        self.assertEqual("acquisition", error.details["phase"])
        self.assertIn("entry-point group", error.remediation)

    def test_unknown_provider_still_reports_config_invalid_verbatim(self):
        """A typo'd id keeps exactly the code and message it produced before CR-5."""
        with self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught:
            CONTROLLER.provider_policy(config_authorizing("acquisition", ["not-a-real-provider"]))
        self.assertEqual("CONFIG_INVALID", caught.exception.error_code)
        self.assertIn("has unknown provider(s): not-a-real-provider", str(caught.exception))
        # With nothing registered, the message must not mention registration at all.
        self.assertNotIn("Registered providers:", str(caught.exception))

    def test_builtin_only_workspace_is_unaffected_by_registration(self):
        for phase, provider_id in (("acquisition", "arxiv"), ("discovery", "openalex")):
            with self.subTest(phase=phase):
                policy = CONTROLLER.provider_policy(config_authorizing(phase, [provider_id]))
                self.assertEqual([provider_id], policy[phase]["providers"])

    def test_registration_state_never_enters_the_compared_policy(self):
        """The policy dict is frozen into durable artifacts, so it carries authorization only."""
        with installed_provider_plugins():
            PLUGINS.clear_cache()
            policy = CONTROLLER.provider_policy(config_authorizing("acquisition", [ACQUISITION_PROVIDER_ID]))
        self.assertEqual({"enabled", "providers"}, set(policy["acquisition"]))


class ControllerRegisteredProviderDriftTests(unittest.TestCase):
    """A distribution that disappears mid-session must stop the session."""

    def setUp(self):
        PLUGINS.clear_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        (self.workspace / "research.yml").write_text(
            yaml.safe_dump(config_authorizing("acquisition", [ACQUISITION_PROVIDER_ID]), sort_keys=False),
            encoding="utf-8",
        )

    def tearDown(self):
        PLUGINS.clear_cache()
        self._tmp.cleanup()

    def session_with_frozen_policy(self) -> dict:
        with installed_provider_plugins():
            PLUGINS.clear_cache()
            policy = CONTROLLER.provider_policy(CONTROLLER.load_config(self.workspace))
        return {"provider_policy": policy}

    def test_recheck_with_the_distribution_still_installed_reports_no_drift(self):
        session = self.session_with_frozen_policy()
        with installed_provider_plugins():
            PLUGINS.clear_cache()
            CONTROLLER.verify_provider_policy_unchanged(self.workspace, session)  # must not raise

    def test_uninstalling_the_distribution_mid_session_refuses(self):
        session = self.session_with_frozen_policy()
        PLUGINS.clear_cache()
        with self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught:
            CONTROLLER.verify_provider_policy_unchanged(self.workspace, session)
        # The recompute refuses before the diff runs, so the operator is told the id no
        # longer resolves rather than being accused of narrowing an allow-list they
        # never touched -- which is what ORCHESTRATION_PROVIDER_POLICY_CHANGED would say.
        self.assertEqual("CONFIG_INVALID", caught.exception.error_code)
        self.assertNotEqual("ORCHESTRATION_PROVIDER_POLICY_CHANGED", caught.exception.error_code)
        self.assertIn(ACQUISITION_PROVIDER_ID, caught.exception.details["unresolved_provider_ids"])


class ControllerRegisteredProviderCliTests(unittest.TestCase):
    """The refusal reaches a caller as a well-formed envelope on stderr."""

    def setUp(self):
        PLUGINS.clear_cache()

    def tearDown(self):
        PLUGINS.clear_cache()

    def test_policy_refusal_renders_as_a_json_envelope(self):
        with self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught:
            CONTROLLER.provider_policy(config_authorizing("acquisition", [ACQUISITION_PROVIDER_ID]))
        error = caught.exception
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            CONTROLLER.emit_error(
                str(error),
                json_mode=True,
                error_code=error.error_code,
                recoverable=False,
                remediation=error.remediation,
                details=error.details,
            )
        self.assertEqual("", stdout.getvalue(), "a refusal must leave stdout empty")
        envelope = json.loads(stderr.getvalue())
        self.assertEqual("CONFIG_INVALID", envelope["error_code"])
        self.assertIn("entry-point group", envelope["remediation"])


if __name__ == "__main__":
    unittest.main()
