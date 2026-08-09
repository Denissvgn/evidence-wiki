"""The inventory must recognise the provenance fields a registered acquisition writes.

`PROVENANCE_FIELDS` is an allow-list: anything outside it is dropped with an "unknown
provenance field ignored" warning. `registered get` writes four fields CR-5 introduced,
so before this was modelled every registered artifact drew four warnings naming fields
the package itself had authored -- and the manifest could not say which provider supplied
a source.

The split matters more than the silence. `provider_registration` is promoted because it
is the attribution a manifest reader needs, and it stays readable after the distribution
is uninstalled. `provider_capabilities` is recognised but not promoted: a declaration is
per-provider, not per-artifact, and the sidecar is its system of record. `provider_metadata`
is recognised but not promoted for a stronger reason -- it is plugin-supplied and is nested
in the sidecar precisely so it cannot forge a policy field, so copying it into the manifest
would grant it the authority the nesting withheld.
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INVENTORY = load_script_module("registered_provenance_inventory", "source_inventory.py")

REGISTRATION_BLOCK = {
    "id": "keepa-fixture",
    "phase": "acquisition",
    "distribution": "keepa-fixture",
    "version": "0.1.0",
    "entry_point": "keepa-fixture",
    "provider_api_version": 1,
}
CAPABILITY_BLOCK = {
    "allowed_domains": ["api.keepa-fixture.invalid"],
    "terms_urls": ["https://api.keepa-fixture.invalid/terms"],
    "license_inference": "partial",
    "captures_raw": True,
    "quarantine_on_incomplete": True,
    "rate_limit": {"requests": 60, "per": "minute"},
    "credentials": ["KEEPA_FIXTURE_API_KEY"],
    "request_kinds": [],
}


class RegisteredProvenanceInventoryTests(unittest.TestCase):
    """A registered acquisition's sidecar is read without complaint or loss."""

    def read_sidecar(self, document: dict) -> tuple[dict, list[str]]:
        with tempfile.TemporaryDirectory() as tmpdir:
            sidecar = Path(tmpdir) / "artifact.json.provenance.yml"
            sidecar.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            return INVENTORY.parse_provenance_sidecar(sidecar, "raw/data/artifact.json")

    def full_sidecar(self) -> dict:
        return {
            "url": "https://api.keepa-fixture.invalid/product",
            "retrieved_by": "fetch_sources.py/registered:keepa-fixture",
            "license_check_required": True,
            "provider_registration": dict(REGISTRATION_BLOCK),
            "provider_capabilities": dict(CAPABILITY_BLOCK),
            "provider_metadata": {"vendor_note": "anything the plugin likes"},
        }

    def test_no_field_the_package_writes_is_reported_as_unknown(self):
        _data, warnings = self.read_sidecar(self.full_sidecar())
        unknown = [warning for warning in warnings if "unknown provenance field" in warning]
        self.assertEqual([], unknown, "the package must not call its own fields unknown")

    def test_registration_identity_reaches_the_manifest_record(self):
        data, _warnings = self.read_sidecar(self.full_sidecar())
        self.assertEqual(REGISTRATION_BLOCK, data["provider_registration"])
        self.assertIs(True, data["license_check_required"])

    def test_declaration_and_plugin_metadata_stay_out_of_the_manifest_record(self):
        data, _warnings = self.read_sidecar(self.full_sidecar())
        for field in ("provider_capabilities", "provider_metadata"):
            with self.subTest(field=field):
                self.assertNotIn(field, data)

    def test_a_genuinely_unknown_field_is_still_reported(self):
        """Recognising CR-5's fields must not turn the allow-list into a free-for-all."""
        document = self.full_sidecar()
        document["totally_made_up_field"] = "x"
        _data, warnings = self.read_sidecar(document)
        self.assertTrue(
            any("unknown provenance field ignored: totally_made_up_field" in w for w in warnings),
            warnings,
        )

    def test_malformed_registration_is_refused_rather_than_stored(self):
        for value in ("not-a-mapping", 42, ["a"]):
            with self.subTest(value=value):
                document = self.full_sidecar()
                document["provider_registration"] = value
                data, warnings = self.read_sidecar(document)
                self.assertNotIn("provider_registration", data)
                self.assertTrue(
                    any("provider_registration must be a mapping" in w for w in warnings), warnings
                )

    def test_malformed_license_flag_is_refused_rather_than_coerced(self):
        """A truthy string must not silently become a policy assertion."""
        document = self.full_sidecar()
        document["license_check_required"] = "yes"
        data, warnings = self.read_sidecar(document)
        self.assertNotIn("license_check_required", data)
        self.assertTrue(
            any("license_check_required must be a boolean" in w for w in warnings), warnings
        )

    def test_a_sidecar_without_any_registered_field_is_unchanged(self):
        """Built-in acquisitions must read exactly as they did before CR-5."""
        data, warnings = self.read_sidecar(
            {"url": "https://arxiv.org/abs/1234.5678", "retrieved_by": "fetch_sources.py/arxiv"}
        )
        self.assertEqual([], warnings)
        self.assertNotIn("provider_registration", data)


if __name__ == "__main__":
    unittest.main()
