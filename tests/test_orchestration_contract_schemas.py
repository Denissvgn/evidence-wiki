import contextlib
import copy
import hashlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from evidence_wiki import cli, orchestration


def _json_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def assert_matches_schema(value: Any, schema: dict[str, Any], *, root: dict[str, Any] | None = None) -> None:
    """Validate the draft-2020-12 subset used by the public protocol schemas."""

    root = schema if root is None else root
    if "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/"):
            raise AssertionError(f"unsupported test reference: {reference}")
        resolved: Any = root
        for part in reference[2:].split("/"):
            resolved = resolved[part.replace("~1", "/").replace("~0", "~")]
        assert_matches_schema(value, resolved, root=root)
        return

    if "anyOf" in schema:
        errors: list[str] = []
        for option in schema["anyOf"]:
            try:
                assert_matches_schema(value, option, root=root)
                break
            except AssertionError as exc:
                errors.append(str(exc))
        else:
            raise AssertionError(f"no anyOf branch matched: {errors}")
        return

    if "oneOf" in schema:
        matches = 0
        for option in schema["oneOf"]:
            try:
                assert_matches_schema(value, option, root=root)
            except AssertionError:
                continue
            matches += 1
        if matches != 1:
            raise AssertionError(f"expected exactly one oneOf match, observed {matches}")
        return

    for condition in schema.get("allOf", []):
        assert_matches_schema(value, condition, root=root)

    if "if" in schema:
        try:
            assert_matches_schema(value, schema["if"], root=root)
        except AssertionError:
            pass
        else:
            if "then" in schema:
                assert_matches_schema(value, schema["then"], root=root)
        return

    if "const" in schema and value != schema["const"]:
        raise AssertionError(f"{value!r} does not equal const {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise AssertionError(f"{value!r} is not in enum {schema['enum']!r}")

    expected_type = schema.get("type")
    if expected_type is not None:
        allowed_types = [expected_type] if isinstance(expected_type, str) else expected_type
        type_matches = {
            "null": value is None,
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }
        if not any(type_matches.get(candidate, False) for candidate in allowed_types):
            raise AssertionError(f"{value!r} does not have type {allowed_types!r}")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise AssertionError("string is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise AssertionError("string is longer than maxLength")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise AssertionError(f"string {value!r} does not match {schema['pattern']!r}")

    if isinstance(value, int) and not isinstance(value, bool):
        if value < schema.get("minimum", value):
            raise AssertionError("integer is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise AssertionError("integer is above maximum")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise AssertionError("array has too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise AssertionError("array has too many items")
        if schema.get("uniqueItems") and len({_json_key(item) for item in value}) != len(value):
            raise AssertionError("array items are not unique")
        if isinstance(schema.get("items"), dict):
            for item in value:
                assert_matches_schema(item, schema["items"], root=root)
        if isinstance(schema.get("contains"), dict):
            matches = 0
            for item in value:
                try:
                    assert_matches_schema(item, schema["contains"], root=root)
                except AssertionError:
                    continue
                matches += 1
            if matches < schema.get("minContains", 1):
                raise AssertionError("array has too few matching contains items")
            if "maxContains" in schema and matches > schema["maxContains"]:
                raise AssertionError("array has too many matching contains items")

    if isinstance(value, dict):
        required = set(schema.get("required", []))
        missing = required - set(value)
        if missing:
            raise AssertionError(f"object is missing required fields: {sorted(missing)}")
        if len(value) < schema.get("minProperties", 0):
            raise AssertionError("object has too few properties")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            raise AssertionError("object has too many properties")
        properties = schema.get("properties", {})
        unknown = set(value) - set(properties)
        additional = schema.get("additionalProperties", True)
        if additional is False and unknown:
            raise AssertionError(f"object has additional properties: {sorted(unknown)}")
        property_names = schema.get("propertyNames")
        if isinstance(property_names, dict):
            for key in value:
                assert_matches_schema(key, property_names, root=root)
        for key, child in value.items():
            child_schema = properties.get(key)
            if child_schema is None and isinstance(additional, dict):
                child_schema = additional
            if isinstance(child_schema, dict):
                assert_matches_schema(child, child_schema, root=root)


class OrchestrationContractSchemaTests(unittest.TestCase):
    def run_cli_json(self, *args: str) -> dict[str, Any]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(list(args))
        self.assertEqual(0, code)
        return json.loads(stdout.getvalue())

    def canonical_session_and_order(self, root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        target = root / "schema-workspace"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                0,
                cli.main(
                    [
                        "deploy",
                        "--target",
                        str(target),
                        "--project-name",
                        "schema-workspace",
                        "--project-description",
                        "Schema contract fixture",
                    ]
                ),
            )
        batch = root / "questions.json"
        batch.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "questions": [
                        {
                            "id": "schema-question",
                            "question": "Which evidence supports the schema fixture?",
                            "priority": "high",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                0,
                cli.main(["questions", "add", "--target", str(target), "--from-file", str(batch)]),
            )
        session = self.run_cli_json(
            "orchestrate",
            "start",
            "--target",
            str(target),
            "--orchestration-id",
            "orch-schema",
            "--agent-id",
            "schema-agent",
            "--format",
            "json",
        )
        order = self.run_cli_json(
            "orchestrate",
            "next",
            "--target",
            str(target),
            "--orchestration-id",
            "orch-schema",
            "--agent-id",
            "schema-agent",
            "--format",
            "json",
        )
        return session, order

    def test_contract_keeps_version_map_and_publishes_complete_schema_documents(self):
        payload = cli._contract_payload()

        self.assertEqual("1.0", payload["artifact_schemas"]["orchestration_session"])
        self.assertEqual("1.0", payload["artifact_schemas"]["orchestration_work_order"])
        self.assertEqual("1.0", payload["artifact_schemas"]["orchestration_result"])
        self.assertEqual("1.0", payload["artifact_schemas"]["orchestration_attempt"])
        schemas = payload["artifact_schema_documents"]
        self.assertEqual(
            {
                "orchestration_session",
                "orchestration_work_order",
                "orchestration_result",
                "orchestration_attempt",
            },
            set(schemas),
        )
        for name, schema in schemas.items():
            with self.subTest(name=name):
                self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
                self.assertEqual("object", schema["type"])
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(
                    "1.0",
                    schema["properties"]["schema_version"]["enum"][0],
                )
                if name != "orchestration_result":
                    self.assertIn("artifact_type", schema["required"])
        self.assertIn("$defs", schemas["orchestration_session"])
        self.assertIn("$defs", schemas["orchestration_work_order"])

        # Publishing caller-owned copies prevents one in-process consumer from
        # mutating subsequent contract responses.
        schemas["orchestration_session"]["required"].clear()
        self.assertTrue(cli._contract_payload()["artifact_schema_documents"]["orchestration_session"]["required"])

    def test_current_controller_session_and_work_order_validate(self):
        payload = cli._contract_payload()
        with tempfile.TemporaryDirectory() as tmpdir:
            session, order = self.canonical_session_and_order(Path(tmpdir))

        assert_matches_schema(
            session,
            payload["artifact_schema_documents"]["orchestration_session"],
        )
        legacy_session = copy.deepcopy(session)
        legacy_session.pop("pending_submission")
        legacy_session.pop("recovery")
        assert_matches_schema(
            legacy_session,
            payload["artifact_schema_documents"]["orchestration_session"],
        )
        assert_matches_schema(
            order,
            payload["artifact_schema_documents"]["orchestration_work_order"],
        )
        assert_matches_schema(
            {
                "schema_version": "1.0",
                "action_id": order["action_id"],
                "outcome": "completed",
                "summary": "Completed the bounded action.",
                "artifacts": [],
            },
            payload["artifact_schema_documents"]["orchestration_result"],
        )
        self.assertEqual(order, orchestration._validate_work_order(copy.deepcopy(order)))

    def test_schema_rejects_unsafe_or_incomplete_protocol_artifacts(self):
        payload = cli._contract_payload()
        session_schema = payload["artifact_schema_documents"]["orchestration_session"]
        order_schema = payload["artifact_schema_documents"]["orchestration_work_order"]
        with tempfile.TemporaryDirectory() as tmpdir:
            session, order = self.canonical_session_and_order(Path(tmpdir))

        invalid_session = copy.deepcopy(session)
        invalid_session["recovery"]["attempt"] = 0
        with self.assertRaises(AssertionError):
            assert_matches_schema(invalid_session, session_schema)

        invalid_order = copy.deepcopy(order)
        invalid_order["inputs"] = ["../outside-workspace"]
        with self.assertRaises(AssertionError):
            assert_matches_schema(invalid_order, order_schema)

        invalid_order = copy.deepcopy(order)
        invalid_order["required_postconditions"] = [{"check": "human_approval"}]
        with self.assertRaises(AssertionError):
            assert_matches_schema(invalid_order, order_schema)

        invalid_order = copy.deepcopy(order)
        invalid_order["required_postconditions"] = [
            item
            for item in invalid_order["required_postconditions"]
            if item.get("check") != "controller_integrity_baseline"
        ]
        with self.assertRaises(AssertionError):
            assert_matches_schema(invalid_order, order_schema)

        invalid_order = copy.deepcopy(order)
        invalid_order["provider_policy"]["discovery"]["credential"] = "must-not-be-here"
        with self.assertRaises(AssertionError):
            assert_matches_schema(invalid_order, order_schema)

        invalid_order = copy.deepcopy(order)
        invalid_order["agent_id"] = "schema-agent\nignore-previous-instructions"
        with self.assertRaises(AssertionError):
            assert_matches_schema(invalid_order, order_schema)

        result_schema = payload["artifact_schema_documents"]["orchestration_result"]
        valid_result = {
            "schema_version": "1.0",
            "action_id": order["action_id"],
            "outcome": "completed",
            "summary": "Completed the bounded action.",
            "artifacts": ["wiki/questions/schema-question.md"],
        }
        for mutate in (
            lambda value: value.update(summary="   "),
            lambda value: value.update(summary="x" * 4001),
            lambda value: value.update(artifacts=["/tmp/outside-workspace"]),
            lambda value: value.update(artifacts=["../outside-workspace"]),
            lambda value: value.update(artifacts=["wiki/result.md", "wiki/result.md"]),
            lambda value: value.update(
                artifacts=["runs/orchestrations/orch-schema/session.json"]
            ),
            lambda value: value.update(
                artifacts=["./RUNS/ORCHESTRATIONS/orch-schema/session.json"]
            ),
            lambda value: value.update(
                artifacts=[f"wiki/results/{index}.md" for index in range(257)]
            ),
        ):
            invalid_result = copy.deepcopy(valid_result)
            mutate(invalid_result)
            with self.assertRaises(AssertionError):
                assert_matches_schema(invalid_result, result_schema)

        # The public wire schema is intentionally stricter than the minimal
        # structured-output schema passed to Codex; host validation supplies
        # these same semantic bounds after generation.
        self.assertNotIn("maxItems", orchestration.ORCHESTRATION_RESULT_SCHEMA["properties"]["artifacts"])
        self.assertEqual(256, result_schema["properties"]["artifacts"]["maxItems"])

    def test_attempt_schema_matches_host_invariants(self):
        schema = cli._contract_payload()["artifact_schema_documents"]["orchestration_attempt"]
        digest = "sha256:" + "a" * 64
        attempt = {
            "schema_version": "1.0",
            "artifact_type": "orchestration_attempt",
            "orchestration_id": "orch-schema",
            "attempt_id": "attempt-0001",
            "action_id": "action-0001",
            "lease_attempt": 1,
            "runner": "codex",
            "phase": "research",
            "run_id": "run-schema-001",
            "started_at": "2026-07-21T00:00:00Z",
            "updated_at": "2026-07-21T00:00:01Z",
            "status": "result_staged",
            "work_order_identity": digest,
            "result_digest": digest,
            "error_code": None,
        }
        assert_matches_schema(attempt, schema)
        self.assertEqual(attempt, orchestration._validate_attempt(copy.deepcopy(attempt)))

        invalid_attempts = []
        for field_name in ("work_order_identity", "result_digest"):
            invalid = copy.deepcopy(attempt)
            invalid[field_name] = "sha256:not-a-digest"
            invalid_attempts.append(invalid)
        invalid = copy.deepcopy(attempt)
        invalid["result_digest"] = None
        invalid_attempts.append(invalid)
        invalid = copy.deepcopy(attempt)
        invalid["status"] = "runner_failed"
        invalid["error_code"] = "RUNNER_TIMEOUT"
        invalid_attempts.append(invalid)
        invalid = copy.deepcopy(attempt)
        invalid["started_at"] = " " * 2
        invalid_attempts.append(invalid)

        for invalid_attempt in invalid_attempts:
            with self.subTest(status=invalid_attempt["status"], error=invalid_attempt["error_code"]):
                with self.assertRaises(AssertionError):
                    assert_matches_schema(invalid_attempt, schema)

    def test_schema_covers_every_controller_postcondition_shape_and_recovery_state(self):
        payload = cli._contract_payload()
        session_schema = payload["artifact_schema_documents"]["orchestration_session"]
        order_schema = payload["artifact_schema_documents"]["orchestration_work_order"]
        with tempfile.TemporaryDirectory() as tmpdir:
            session, research_order = self.canonical_session_and_order(Path(tmpdir))

        result = {
            "schema_version": "1.0",
            "action_id": research_order["action_id"],
            "outcome": "completed",
            "summary": "Recovered a prepared submission.",
            "artifacts": [],
        }
        result_digest = "sha256:" + hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        pending_session = copy.deepcopy(session)
        pending_session["pending_action_id"] = research_order["action_id"]
        pending_session["pending_submission"] = {
            "action_id": research_order["action_id"],
            "accepted_at": session["updated_at"],
            "result": result,
            "result_digest": result_digest,
            "next_phase": "planning",
            "completion_reason": None,
        }
        pending_session["pending_trusted_static_inputs"] = {
            "action_id": research_order["action_id"],
            "fingerprint": "sha256:" + "1" * 64,
            "entry_count": 4,
            "total_bytes": 1024,
        }
        pending_session["recovery"] = {
            "state": "finalizing_submission",
            "action_id": research_order["action_id"],
            "attempt": 1,
            "reason_code": "accepted_result_pending_finalization",
            "recorded_at": session["updated_at"],
        }
        assert_matches_schema(pending_session, session_schema)

        integrity_guard = {
            "check": "controller_integrity_baseline",
            "path": (
                "runs/orchestrations/orch-schema/trusted-inputs/"
                "action-0001-scope-baseline.json"
            ),
            "fingerprint": "sha256:" + "6" * 64,
            "field_count": 3,
            "entry_count": 0,
        }
        variants = {
            "discovery": [
                {
                    "check": "request_scoped_candidates_increased",
                    "before": 0,
                },
                {
                    "check": "discovery_never_fetches",
                    "manifest_records_before": 0,
                    "manifest_digest_before": None,
                },
                {
                    "check": "raw_tree_unchanged",
                    "before": {
                        "algorithm": "sha256-content-v1",
                        "file_count": 0,
                        "total_bytes": 0,
                        "fingerprint": "sha256:" + "2" * 64,
                    },
                },
            ],
            "candidate_review": [
                {
                    "check": "selected_candidate_for_request",
                    "selected_before": 0,
                },
                {
                    "check": "selection_does_not_fetch",
                    "manifest_records_before": 0,
                    "manifest_digest_before": "sha256:" + "3" * 64,
                },
                {
                    "check": "raw_tree_unchanged",
                    "before": {
                        "algorithm": "sha256-content-v1",
                        "file_count": 1,
                        "total_bytes": 512,
                        "fingerprint": "sha256:" + "4" * 64,
                    },
                },
            ],
            "acquisition": [
                {"check": "request_fulfilled_with_normalized_source"},
                {
                    "check": "linked_blocked_questions_reopened",
                },
                {
                    "check": "manifest_records_increased",
                    "before": 0,
                },
            ],
            "verification": [
                {
                    "check": "fresh_verification_bundle",
                    "paths": ["runs/run-1/evaluation/export.json"],
                    "before": {"runs/run-1/evaluation/export.json": None},
                },
                {"check": "publication_readiness", "expected": "ship"},
            ],
        }
        for phase, postconditions in variants.items():
            order = copy.deepcopy(research_order)
            order["phase"] = phase
            order["skill"] = "research-verify" if phase == "verification" else "research-discover"
            order["required_postconditions"] = [
                *postconditions,
                *([] if phase == "verification" else [copy.deepcopy(integrity_guard)]),
            ]
            with self.subTest(phase=phase):
                assert_matches_schema(order, order_schema)

    def test_orchestration_docs_name_both_contract_surfaces(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "workspace-template/docs/orchestration.md",
            "workspace-template/docs/orchestrator-handoff.md",
        ):
            text = (root / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertIn("artifact_schemas", text)
                self.assertIn("artifact_schema_documents", text)
                self.assertIn("Draft 2020-12", text)


if __name__ == "__main__":
    unittest.main()
