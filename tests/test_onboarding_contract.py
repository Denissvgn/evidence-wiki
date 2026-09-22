"""Exercise bounded artifact contracts without executing setup workflows."""

import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import evidence_wiki
from evidence_wiki._script_host import load_packaged_script
from evidence_wiki.errors import UsageError, error_from_envelope
from evidence_wiki.onboarding_contract import decode_document, document_sha256, encode_document
from evidence_wiki.onboarding_schemas import LIMITS, contract_index, schema_document, schema_ids
from tests.test_orchestration_contract_schemas import assert_matches_schema

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = {
    "package_version": "0.7.3", "starter_version": "0.7.0", "library_api_version": "12",
    "profile_schema_version": "0.1", "research_contract_version": "0.1",
}
TARGET = {"writable_root": "/research", "relative_path": "study"}
IDENTITY = {"device": "1", "inode": "42"}
TARGET_BASIS = {"target": TARGET, "root_identity": IDENTITY, "parent_identity": IDENTITY,
                "state": "absent", "directory_identity": None}
PACK = {
    "name": "custom-domain", "version": "1.0", "origin": "caller_local", "locator": "/assets/custom-domain",
    "overlay_sha256": "a" * 64, "tree_sha256": "b" * 64, "research_contract_version": "0.1",
}
AUTHORITY = {
    "role": "caller", "reference": "current research task", "allowed_actions": ["local_setup", "local_research"],
    "source_scope": [], "writable_roots": ["/research"], "credential_references": [],
}
QUESTION_MAP = [{"original_id": "q1", "question_slugs": ["q1"], "outcome": "seeded", "reason": None}]
RESOURCE = {
    "id": "guides/research/v1", "version": "1.0", "media_type": "text/markdown",
    "sha256": hashlib.sha256(b"Research guide.").hexdigest(), "content": "Research guide.",
}


def request():
    return {
        "schema_version": "1.0", "kind": "research_request", "request_id": "study-request",
        "payload": {
            "goal": "Compare the observed methods.", "questions": [{"id": "q1", "text": "What does the evidence show?"}],
            "derived_questions": [], "target": copy.deepcopy(TARGET), "outputs": ["json"], "scope": [],
            "domain": {"mode": "none", "pack": None, "rationale": "Generic guidance fits this question."},
            "sources": [], "host_tools": [], "authority": copy.deepcopy(AUTHORITY),
            "budgets": {"questions": 10, "source_requests": 5, "downloads": 0, "bytes": 0, "seconds": 600},
            "assumptions": [], "open_decisions": [],
        },
    }


def samples():
    profile = yaml.safe_load((ROOT / "tests/fixtures/workspace-init-profile.yml").read_text())
    payloads = {
        "bootstrap": {
            "installation": VERSIONS, "python_version": "3.10.0", "workspace": "absent", "guide": RESOURCE,
            "schema_ids": list(schema_ids()), "supported_operations": [], "limitations": [],
        },
        "inspection": {
            "installation": VERSIONS, "target": TARGET, "scope": "target",
            "capabilities": [{
                "id": "browser", "route": "host_capture", "available": "unknown", "compatible": "unknown",
                "configured": "unknown", "authorized": "unknown", "credentials": "unknown",
                "connectivity": "untested", "capture": "unknown", "evidence_fit": "unknown", "basis": "declared",
                "observed_at": None, "limitations": ["The host has not supplied a capture."],
            }],
            "bounds": {"total": 1, "returned": 1, "truncated": False}, "limitations": [],
        },
        "research_request": request()["payload"],
        "setup_plan": {
            "request_sha256": document_sha256("onboarding/research_request/v1", request()),
            "installation": VERSIONS, "starter_tree_sha256": "a" * 64, "target_basis": TARGET_BASIS,
            "interpreter": {
                "selection": "current", "executable": "/env/bin/python", "implementation": "cpython",
                "python_version": "3.10.0", "executable_sha256": "a" * 64, "environment_sha256": "b" * 64,
            },
            "pack": None, "inputs": [], "authority": AUTHORITY, "profile": profile,
            "question_map": QUESTION_MAP, "routes": [],
            "steps": [{"id": "create", "operation": "initialize", "depends_on": [], "paths": ["research.yml"]}],
            "assumptions": [], "open_decisions": [],
        },
        "setup_receipt": {
            "plan_sha256": "a" * 64, "transaction_id": "setup-123", "target": TARGET, "state": "checking",
            "checkpoint": ".evidence-wiki/setup/targets/abc/transactions/setup-123/checkpoint.json",
            "checks": [{"id": "smoke", "status": "pending", "observed_at": None,
                        "exit_code": None, "summary": "Not yet observed.", "evidence_sha256": None}],
            "question_map": QUESTION_MAP, "setup_ready": False, "research_complete": False,
            "recovery": "resume", "observed_at": "2026-09-16T00:00:00Z", "limitations": [],
        },
        "setup_checkpoint": {
            "plan_sha256": "a" * 64, "transaction_id": "setup-123", "target_basis": TARGET_BASIS,
            "target_identity": IDENTITY, "state": "initializing", "completed_steps": [],
            "owned_files": [], "owned_directories": [],
            "pending_write": {"path": "research.yml", "before_sha256": None, "after_sha256": "b" * 64},
            "observed_at": "2026-09-16T00:00:00Z",
        },
        "pack_spec": {
            "name": "custom-domain", "version": "1.0", "research_contract_version": "0.1",
            "scope": "Comparative research", "exclusions": [], "intended_users": [],
            "question_classes": ["method comparisons"], "source_classes": ["papers"],
            "extraction_targets": [], "claim_fields": [], "coverage_requirements": [], "output_forms": [],
            "recommended_providers": {"discovery": [], "acquisition": []}, "human_review_requirements": [],
            "base": PACK, "observed_gap": "Missing counterevidence guidance", "intended_change": "Add that guidance",
        },
        "revision_impact": {
            "base": PACK, "candidate": {**PACK, "tree_sha256": "c" * 64}, "workspace_sha256": "d" * 64,
            "changes": [{"kind": "policy", "identity": "custom-domain/counterevidence", "change": "added",
                         "semantics": "requirements"}],
            "affected_questions": ["q1"], "unresolved_mappings": [], "reevaluation_required": True,
            "bounds": {"total": 1, "returned": 1, "truncated": False}, "limitations": [],
        },
        "resource": RESOURCE,
    }
    return {name: {"schema_version": "1.0", "kind": name, "request_id": "study-request", "payload": value}
            for name, value in copy.deepcopy(payloads).items()}


def error_envelope(exc):
    return {"schema_version": "1.0", "error_code": exc.error_code, "message": exc.message,
            "recoverable": exc.recoverable, "remediation": exc.remediation, "details": exc.details}


def test_every_public_artifact_is_retrievable_and_independently_matches_its_schema():
    examples = samples()
    with pytest.raises(UsageError) as caught:
        decode_document("onboarding/research_request/v1", b"{}")
    examples["error"] = error_envelope(caught.value)
    assert set(schema_ids()) == {f"onboarding/{name}/v1" for name in examples} | {"onboarding/research_request/v2", "onboarding/bootstrap/v2", "onboarding/resource/v2", "onboarding/capabilities/v1", "onboarding/resources/v1"}
    for kind, value in examples.items():
        resource_id = f"onboarding/{kind}/v1"
        assert_matches_schema(value, schema_document(resource_id))
        assert decode_document(resource_id, encode_document(resource_id, value)) == value


def test_schemas_and_negotiation_are_caller_owned_and_have_no_workflow_claims():
    first = schema_document("onboarding/research_request/v1")
    first["properties"]["payload"]["required"].clear()
    assert schema_document("onboarding/research_request/v1")["properties"]["payload"]["required"]
    index = contract_index()
    index["limits"]["questions"] = 1
    assert contract_index()["limits"]["questions"] == 100
    assert contract_index()["workflow_commands"] == ["agent", "agent summary", "agent resources", "agent resource"]
    contract = evidence_wiki.contract()
    assert contract["onboarding_contract"] == contract_index()
    assert contract["schema_version"] == "1.0"
    assert contract["library_api"]["version"] == "12"
    assert "agent" not in contract["library_api"]["surface"]


@pytest.mark.parametrize("resource_id", ["../../private", "https://example.com/schema", "onboarding/research_request/v3", [], None])
def test_resource_ids_are_exact_and_never_echoed(resource_id):
    with pytest.raises(UsageError) as caught:
        schema_document(resource_id)
    assert caught.value.error_code == "ONBOARDING_RESOURCE_UNKNOWN"
    assert caught.value.exit_code == 2
    assert caught.value.details == {}


@pytest.mark.parametrize("raw", [
    b"{}{}", b"[]", b"null", b"true", b'{"schema_version":"1.0","schema_version":"1.0"}',
    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e999}',
    b'\xef\xbb\xbf{}', b'\xff', b'{"x":"\\ud800"}',
    b'{"payload":{"duplicate":1,"duplicate":2}}',
])
def test_malformed_json_refuses_without_parser_diagnostics(raw):
    with pytest.raises(UsageError) as caught:
        decode_document("onboarding/research_request/v1", raw)
    assert caught.value.error_code == "ONBOARDING_INVALID"
    assert not caught.value.recoverable
    assert caught.value.exit_code == 2
    assert "Traceback" not in caught.value.message


@pytest.mark.parametrize("version", ["2.0", "0.1", 1, True, None])
def test_incompatible_version_never_gets_coerced(version):
    value = request()
    value["schema_version"] = version
    with pytest.raises(UsageError, match="Unsupported onboarding") as caught:
        decode_document("onboarding/research_request/v1", json.dumps(value).encode())
    assert caught.value.error_code == "ONBOARDING_VERSION_UNSUPPORTED"


def test_unknown_fields_and_bad_values_are_redacted_and_keep_legacy_error_mapping():
    value = request()
    value["payload"]["authority"]["secret-EXAMPLE"] = "credential-value-EXAMPLE"
    with pytest.raises(UsageError) as caught:
        decode_document("onboarding/research_request/v1", json.dumps(value).encode())
    envelope = error_envelope(caught.value)
    encoded = encode_document("onboarding/error/v1", envelope)
    assert b"EXAMPLE" not in encoded
    assert envelope["details"]["field"] == "/payload/authority"
    reconstructed = error_from_envelope(json.loads(encoded))
    assert isinstance(reconstructed, UsageError)
    assert reconstructed.exit_code == 2
    assert not reconstructed.recoverable


@pytest.mark.parametrize("code, exit_code, recoverable", [
    ("ONBOARDING_INVALID", 2, False), ("ONBOARDING_PLAN_STALE", 3, False),
    ("ONBOARDING_TARGET_CONFLICT", 3, False), ("ONBOARDING_LOCK_BUSY", 6, True),
    ("ONBOARDING_WRITE_FAILED", 2, True),
])
def test_reserved_executor_errors_preserve_retry_and_exit_contracts(code, exit_code, recoverable):
    error = error_from_envelope({"schema_version": "1.0", "error_code": code, "message": "Refused."})
    assert isinstance(error, UsageError)
    assert error.exit_code == exit_code
    assert error.recoverable is recoverable
    envelope = error_envelope(error)
    assert_matches_schema(envelope, schema_document("onboarding/error/v1"))
    encode_document("onboarding/error/v1", envelope)
    envelope["recoverable"] = not recoverable
    with pytest.raises(UsageError):
        encode_document("onboarding/error/v1", envelope)


def test_exact_byte_boundary_and_unicode_size_use_input_bytes():
    resource_id = "onboarding/research_request/v1"
    value = request()
    value["payload"]["goal"] = "Métodos de investigación"
    raw = encode_document(resource_id, value)
    padded = raw + b" " * (LIMITS["document_bytes"] - len(raw))
    assert decode_document(resource_id, padded) == value
    with pytest.raises(UsageError) as caught:
        decode_document(resource_id, padded + b" ")
    assert caught.value.error_code == "ONBOARDING_LIMIT"


def test_question_limit_accepts_all_ids_at_boundary_and_rejects_overflow():
    value = request()
    value["payload"]["questions"] = [{"id": f"q{i}", "text": "Question"} for i in range(100)]
    assert len(decode_document("onboarding/research_request/v1", json.dumps(value).encode())["payload"]["questions"]) == 100
    value["payload"]["questions"].append({"id": "overflow", "text": "Question"})
    with pytest.raises(UsageError) as caught:
        encode_document("onboarding/research_request/v1", value)
    assert caught.value.error_code == "ONBOARDING_LIMIT"
    assert caught.value.details == {"field": "/payload/questions"}


def test_serialization_bounds_escaped_output_and_in_memory_input():
    for text in ("é" * 65_536, "\x00" * 65_536):
        value = samples()["setup_plan"]
        value["payload"]["profile"]["workspace_init"]["large_values"] = [text] * 20
        with pytest.raises(UsageError) as caught:
            encode_document("onboarding/setup_plan/v1", value)
        assert caught.value.error_code == "ONBOARDING_LIMIT"


def test_node_property_and_array_limits_apply_inside_opaque_profiles():
    for opaque in ({str(i): 0 for i in range(129)}, [0] * 4097, [[0] * 4096] * 17):
        value = samples()["setup_plan"]
        value["payload"]["profile"]["workspace_init"]["oversize"] = opaque
        with pytest.raises(UsageError) as caught:
            encode_document("onboarding/setup_plan/v1", value)
        assert caught.value.error_code == "ONBOARDING_LIMIT"


def test_nullable_field_keeps_a_specific_string_limit_refusal():
    value = samples()["pack_spec"]
    value["payload"]["observed_gap"] = "x" * 4097
    with pytest.raises(UsageError) as caught:
        encode_document("onboarding/pack_spec/v1", value)
    assert caught.value.error_code == "ONBOARDING_LIMIT"
    assert caught.value.details == {"field": "/payload/observed_gap"}


@pytest.mark.parametrize("budget", [True, 1.0, 1.5, -1, 1001])
def test_budgets_are_bounded_integers(budget):
    value = request()
    value["payload"]["budgets"]["downloads"] = budget
    with pytest.raises(UsageError):
        encode_document("onboarding/research_request/v1", value)


@pytest.mark.parametrize("identifier", ["has space", "../escape", "id\n", "", "é"])
def test_identifiers_are_portable_without_trailing_newline_acceptance(identifier):
    value = request()
    value["request_id"] = identifier
    with pytest.raises(UsageError):
        encode_document("onboarding/research_request/v1", value)


def test_digest_is_presentation_independent_but_covers_questions_authority_and_order():
    value = request()
    resource_id = "onboarding/research_request/v1"
    before = document_sha256(resource_id, value)
    assert before == document_sha256(resource_id, decode_document(resource_id, json.dumps(value, indent=4).encode()))
    assert before == hashlib.sha256(encode_document(resource_id, value)).hexdigest()
    for changed in ("questions", "authority"):
        candidate = copy.deepcopy(value)
        if changed == "questions":
            candidate["payload"][changed][0]["text"] += " Changed."
        else:
            candidate["payload"][changed]["allowed_actions"].append("acquisition")
        assert document_sha256(resource_id, candidate) != before


def test_recursive_or_overdeep_objects_refuse_with_a_limit():
    value = request()
    value["payload"]["scope"] = value
    with pytest.raises(UsageError) as caught:
        encode_document("onboarding/research_request/v1", value)
    assert caught.value.error_code == "ONBOARDING_LIMIT"


def test_deep_json_has_the_same_limit_refusal_before_any_parser_recursion():
    with pytest.raises(UsageError) as caught:
        decode_document("onboarding/research_request/v1", b"[" * 2000 + b"]" * 2000)
    assert caught.value.error_code == "ONBOARDING_LIMIT"
    value = request()
    value["payload"]["goal"] = '[' * 100 + '\\"' + ']' * 100
    assert decode_document("onboarding/research_request/v1", encode_document("onboarding/research_request/v1", value)) == value


def test_opaque_profile_retains_initializer_ownership_and_finite_numbers():
    value = samples()["setup_plan"]
    initializer = load_packaged_script(ROOT, "init_research_workspace")
    initializer.validate_profile(value["payload"]["profile"]["workspace_init"])
    profile = value["payload"]["profile"]["workspace_init"]
    profile["research_yml"] = {"custom_setting": 0.5}
    # Structural decoding must not impose a second copy of profile rules.
    assert decode_document("onboarding/setup_plan/v1", encode_document("onboarding/setup_plan/v1", value)) == value


def test_receipts_cannot_claim_research_completion():
    value = samples()["setup_receipt"]
    value["payload"]["research_complete"] = True
    with pytest.raises(UsageError):
        encode_document("onboarding/setup_receipt/v1", value)


def test_new_request_requires_strict_selection_and_old_request_stays_compatible():
    value = request()
    encode_document("onboarding/research_request/v1", value)
    value["schema_version"] = "2.0"
    with pytest.raises(UsageError):
        encode_document("onboarding/research_request/v2", value)
    value["payload"]["strict_evidence"] = {"mode": "strict", "assurance": "artifact_checked",
                                           "policy_id": "research", "policy_revision": "1"}
    assert decode_document("onboarding/research_request/v2", encode_document("onboarding/research_request/v2", value)) == value
    assert contract_index()["default_research_request_schema"] == "onboarding/research_request/v2"
    schema_document("onboarding/research_request/v2")["properties"]["payload"]["properties"]["strict_evidence"]["properties"]["policy_id"]["maxLength"] = 1
    assert schema_document("onboarding/research_request/v2")["properties"]["payload"]["properties"]["strict_evidence"]["properties"]["policy_id"]["maxLength"] == 128


def test_schema_access_and_decoding_are_independent_of_assets_and_execution():
    raw = json.dumps(request()).encode()
    with patch("builtins.open", side_effect=AssertionError("unexpected file read")), \
         patch("subprocess.run", side_effect=AssertionError("unexpected process")), \
         patch("evidence_wiki.resources.assets_root", side_effect=AssertionError("unexpected extraction")):
        assert schema_document("onboarding/research_request/v1")
        assert decode_document("onboarding/research_request/v1", raw)["kind"] == "research_request"
