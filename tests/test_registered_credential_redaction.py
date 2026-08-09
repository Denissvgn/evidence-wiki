"""A declared credential must be redacted from the moment its declaration is known.

`_acquisition_transport` registers a declared credential for redaction when it resolves
a `{{credential:NAME}}` placeholder -- that is, at transport time. But `validate_request`
and `plan_fetch`/`plan_search` run *before* transport, and their refusals quote the
request document the caller supplied. A workspace whose request file carries the key
inline would therefore render it in the clear inside `PROVIDER_REQUEST_INVALID` or
`PROVIDER_PLAN_INVALID`, which is exactly the envelope an operator pastes into a bug
report.

Both entry points must arm redaction when the registration resolves, not when the socket
opens. This module runs each assertion in a subprocess with a pristine interpreter,
because the registry is process-global: an earlier test in the same process that reached
transport would register the name and make a broken implementation look correct. Unit 8
found precisely that false pass.
"""

import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
SECRET = "s3cr3t-value-that-must-never-print"  # noqa: S105 - a probe, not a credential

PROBE = textwrap.dedent(
    """
    import importlib.util, json, os, sys
    from pathlib import Path

    REPO_ROOT = Path({repo!r})
    SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
    sys.path.insert(0, str(REPO_ROOT))
    from tests._provider_plugin_fixture import installed_provider_plugins

    # The value lives where a real deployment puts it: the environment, under the
    # name the provider declared. Nothing hands it to the redactor directly.
    os.environ["KEEPA_FIXTURE_API_KEY"] = {secret!r}

    def load(name, filename):
        path = SCRIPTS / filename
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    PLUGINS = load("probe_plugins", "_provider_plugins.py")
    SCRIPT = load("probe_script", {script!r})

    # Ask the script for ITS transport module rather than loading a second copy. The
    # secret registry is module state, and these scripts reach the same source file by
    # different loaders, so an independently loaded copy would have an empty registry
    # and this probe would pass against a broken implementation.
    TRANSPORT = getattr(SCRIPT, "TRANSPORT", None) or SCRIPT._acquisition_transport

    with installed_provider_plugins():
        PLUGINS.clear_cache()
        SCRIPT.{resolver}({phase_expr})
        # The secret is never passed in; it is discovered through the declared NAME.
        rendered = TRANSPORT.redact_diagnostic("request rejected: key=" + {secret!r})

    print(json.dumps({{"rendered": rendered}}))
    """
)


def run_probe(script_filename: str, resolver: str, phase_expr: str) -> str:
    source = PROBE.format(
        repo=str(REPO_ROOT),
        script=script_filename,
        resolver=resolver,
        phase_expr=phase_expr,
        secret=SECRET,
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter, generated source
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )
    if completed.returncode != 0:
        raise AssertionError(f"probe failed: {completed.stderr}")
    return json.loads(completed.stdout.strip().splitlines()[-1])["rendered"]


class RegisteredCredentialRedactionTests(unittest.TestCase):
    """Resolving a registration arms redaction for every name it declares."""

    def test_acquisition_arms_redaction_when_the_registration_resolves(self):
        rendered = run_probe(
            "fetch_sources.py",
            "require_acquisition_registration",
            '"keepa-fixture"',
        )
        self.assertNotIn(SECRET, rendered, "a declared credential leaked before transport")

    def test_discovery_arms_redaction_when_the_registration_resolves(self):
        rendered = run_probe(
            "discover_sources.py",
            "require_registered_discovery_provider",
            '"keepa-search-fixture"',
        )
        self.assertNotIn(SECRET, rendered, "a declared credential leaked before transport")

    def test_an_undeclared_value_is_left_alone(self):
        """The diagnostic is redacted, not erased -- an operator still gets a usable message."""
        rendered = run_probe(
            "fetch_sources.py",
            "require_acquisition_registration",
            '"keepa-fixture"',
        )
        self.assertNotEqual("", rendered, "redaction must replace, not erase, the diagnostic")


if __name__ == "__main__":
    unittest.main()
