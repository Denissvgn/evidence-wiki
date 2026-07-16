import hashlib
import hmac
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
SIGNATURE_SCRIPT_PATH = SCRIPTS / "_handoff_signature.py"


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SIGNATURE = load_script_module("research_handoff_signature", SIGNATURE_SCRIPT_PATH)


class HandoffSignatureTests(unittest.TestCase):
    def expected_signature(self, secret: str, payload: bytes) -> str:
        digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return f"hmac-sha256:{digest}"

    def test_sign_handoff_uses_stable_json_payload(self):
        handoff = {
            "chain_run_id": "run-2026-06-09-a",
            "task_id": "chain-task-0042",
            "requested_by": "planner-agent",
        }

        signature = SIGNATURE.sign_handoff(handoff, "workspace-secret")

        self.assertEqual(
            self.expected_signature(
                "workspace-secret",
                b'{"task_id":"chain-task-0042","requested_by":"planner-agent","chain_run_id":"run-2026-06-09-a"}',
            ),
            signature,
        )

    def test_missing_handoff_fields_are_signed_as_empty_strings(self):
        signature = SIGNATURE.sign_handoff({"task_id": "chain-task-0042"}, "workspace-secret")

        self.assertEqual(
            self.expected_signature(
                "workspace-secret",
                b'{"task_id":"chain-task-0042","requested_by":"","chain_run_id":""}',
            ),
            signature,
        )

    def test_secret_source_precedence_and_blank_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".research-handoff-secret").write_text("sidecar-secret\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"EVIDENCE_WIKI_HANDOFF_SECRET": "env-secret"}):
                self.assertEqual("env-secret", SIGNATURE.handoff_secret(root))

            with mock.patch.dict(os.environ, {"EVIDENCE_WIKI_HANDOFF_SECRET": "  "}):
                self.assertIsNone(SIGNATURE.handoff_secret(root))

            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual("sidecar-secret", SIGNATURE.handoff_secret(root))

    def test_verify_reports_verified_invalid_unsigned_and_unconfigured_states(self):
        handoff = {"task_id": "chain-task-0042"}
        signature = SIGNATURE.sign_handoff(handoff, "workspace-secret")

        verified = SIGNATURE.verify_handoff_signature(handoff, signature, "workspace-secret")
        self.assertEqual("verified", verified.status)
        self.assertIsNone(verified.error_code)

        invalid = SIGNATURE.verify_handoff_signature(handoff, signature, "other-secret")
        self.assertEqual("invalid", invalid.status)
        self.assertEqual("HANDOFF_SIGNATURE_INVALID", invalid.error_code)

        unsigned = SIGNATURE.verify_handoff_signature(handoff, None, "workspace-secret")
        self.assertEqual("unsigned", unsigned.status)
        self.assertEqual("HANDOFF_SIGNATURE_INVALID", unsigned.error_code)

        unconfigured = SIGNATURE.verify_handoff_signature(handoff, None, None)
        self.assertEqual("unconfigured", unconfigured.status)
        self.assertIsNone(unconfigured.error_code)


if __name__ == "__main__":
    unittest.main()
