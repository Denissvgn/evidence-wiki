"""Native review serialization and signing preserve large validated snapshots."""

import copy
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from evidence_wiki.agent_execution import ExecutionError, _json
from evidence_wiki.pack_discovery import owner
from tests._execution_fixture import KEYS
from tests.test_agent_qualification import signer


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _prepared():
    material = json.loads((Path(__file__).parent / "fixtures/native-review-material.json").read_text())
    native = owner("_review_dependency_contract")
    native.validate_schema(material, native.schema_documents()[material["schema_version"]])
    basis_id = "sha256:" + "1" * 64
    checks = owner("_strict_contract").CHECKS
    policy = {"schema_version": "evidence-strict-policy/v2", "review_basis_mode": "claim_dependencies_v1",
        "policy_id": "sample", "revision": "1", "assurance": "artifact_checked", "claims_path": "claims.json",
        "instructions": {"sample.md": basis_id}, "rubric": {"id": "sample", "revision": "1",
            "criteria": dict.fromkeys(checks, "Assess the retained material.")},
        "human_review": False, "max_source_age_seconds": 3600, "max_review_age_seconds": 3600}
    # Typed metadata retains independent logical depth while exceeding the ordinary node budget.
    metadata = {"t": "list", "v": [{"t": "list", "v": [
        {"t": "string", "v": "café"} for _ in range(3000)]} for _ in range(2)]}
    material["questions"][0]["semantic_frontmatter"] = metadata
    snapshot = {"policy": policy, "claim": material["claims"][0],
        "question": material["questions"][0]["manifest_question"], "metadata": {"t": "mapping", "v": []},
        "question_body": "Assess the sample claim.", "premises": [], "premise_questions": [],
        "source_revisions": {}, "computation": None, "semantic_material": material}
    observation = {"basis_id": basis_id, "claim_id": "claim", "producer_id": basis_id, "reasons": []}
    basis = {"schema_version": "evidence-review-dependency-basis/v1", "review_basis_mode": "claim_dependencies_v1",
        "basis_id": basis_id, "target_claim_id": "claim", "claim_schema_version": "evidence-strict-claims/v2",
        "required_review_role": "evaluator", "computation_mode": "none", "premise_closure": [],
        "question_slugs": ["question"], "semantic_material_id": basis_id, "dependencies": [
            {"scope": "global", "kind": "workspace_material", "coordinate": f"sample-{index}.md",
                "content_id": basis_id} for index in range(12)],
        "coverage": {"classification_complete": True, "reviewer_material_complete": True,
            "truncated": False, "missing_coordinates": [], "unclassified_coordinates": []}}
    producer = material["global_material"]["material"]["worker_semantics"]["producer"]
    producer["identity"]["manifest_id"] = native.content_id(
        "evidence-review-producer-manifest/v1", producer["manifest"])
    material["producer_id"] = native.content_id("evidence-review-producer/v1", producer["identity"])
    wrapper = material["global_material"]
    wrapper["dependency_id"] = native.content_id("research-global-dependency-material/v1",
        {key: value for key, value in wrapper.items() if key != "dependency_id"})
    del material["dependency_basis_id"]
    basis = native.basis(material, sorted(basis["dependencies"], key=lambda row: row["coordinate"]),
        required_review_role="evaluator", computation_mode="none")
    basis_id = basis["basis_id"]
    material["dependency_basis_id"] = basis_id
    observation.update(basis_id=basis_id, producer_id=material["producer_id"])
    return {"review_schema": "evidence-strict-review/v3", "claim_basis_id": basis_id,
        "rubric": policy["rubric"], "review_producer_id": material["producer_id"],
        "claim_schema_version": "evidence-strict-claims/v2", "observed_workspace_basis_id": basis_id,
        "semantic_material_id": basis["semantic_material_id"], "dependency_basis": basis, "snapshot": snapshot,
        "result": {"schema_version": "evidence-strict-result/v3", "basis_id": basis_id,
            "policy_id": basis_id, "producer_id": material["producer_id"], "evaluated_at": "2026-10-01T00:00:00Z",
            "assurance": "artifact_checked", "questions": [snapshot["question"]], "limits": [],
            "review_basis_mode": "claim_dependencies_v1", "claim_schema_version": "evidence-strict-claims/v2",
            "review_producer_id": material["producer_id"], "claims": [{"claim": snapshot["claim"], "observation": observation,
                "accepted": False, "reasons": [], "review": {"passed": False, "reason": "missing"},
                "required_review_role": "evaluator", "claim_basis_id": basis_id,
                "observed_workspace_basis_id": basis_id, "review_basis_mode": "claim_dependencies_v1"}]},
        "registration": {"schema_version": "evidence-usage-command/v1", "state_id": "sample-state",
            "workspace_binding": "sample-workspace", "expected_checkpoint": basis_id, "action": "register-strict-review", "request_id": None, "body": {"review": None}}}


def _nodes(value, *, keys=False):
    if isinstance(value, dict):
        return 1 + sum(_nodes(child, keys=keys) + int(keys) for child in value.values())
    if isinstance(value, list):
        return 1 + sum(_nodes(child, keys=keys) for child in value)
    return 1


def test_large_native_prepared_registration_signs_exact_detached_canonical_bytes():
    prepared = _prepared()
    assert _json(prepared) == _canonical(prepared)
    before = copy.deepcopy(prepared)
    assert 16384 < _nodes(prepared["snapshot"]["semantic_material"]) < _nodes(prepared, keys=True) < 65536
    delivery = dict.fromkeys(("packet_sha256", "worker_input_sha256", "semantic_material_id",
        "assembly_receipt_id", "attempt_content_sha256", "binding_sha256"), prepared["semantic_material_id"])
    delivery["attempt_id"] = "sample-attempt"
    envelope = signer().registration(prepared, dict.fromkeys(owner("_strict_contract").CHECKS, "unknown"),
        "Serialization boundary only.", "sample-request", "2026-10-01T00:00:00Z", delivery=delivery)
    assert prepared == before
    assert _nodes(envelope, keys=True) < 65536
    raw = _canonical(envelope)
    assert _json(envelope) == raw and not raw.endswith(b"\n")
    unsigned = copy.deepcopy(envelope)
    signature = unsigned["authentication"].pop("signature")
    assert signature == hmac.new(bytes.fromhex(KEYS["evaluator"]),
        b"evidence-attestation/v1\0" + _canonical(unsigned) + b"\n", hashlib.sha256).hexdigest()
    review = envelope["payload"]["body"]["review"]
    owner("_strict_contract").review_document(review)
    authorship = copy.deepcopy(review["generator"])
    generator_signature = authorship["authentication"].pop("signature")
    assert generator_signature == hmac.new(bytes.fromhex(KEYS["runner"]),
        b"evidence-attestation/v1\0" + _canonical(authorship) + b"\n", hashlib.sha256).hexdigest()
    prepared["snapshot"]["semantic_material"]["questions"][0]["semantic_frontmatter"]["v"][0]["v"][0]["v"] = "changed"
    delivery["attempt_id"] = "changed"
    assert _json(envelope) == raw
    review["snapshot"]["semantic_material"]["unexpected"] = True
    with pytest.raises(ExecutionError):
        _json(envelope)
