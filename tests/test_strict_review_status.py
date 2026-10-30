"""Current native approval selection remains distinct from historical registration."""


import hashlib

import pytest
import yaml

from evidence_wiki import Workspace
from evidence_wiki.host_context import HostContext
from tests.test_strict_evidence import CONTRACT, CORE, QUESTION, review, save_claims
from tests.test_strict_evidence import host as host


def selected_context(host):
    return HostContext([host.root], {"workspace.open", "strict_evidence.run_operation.review-status",
        "strict_evidence.run_operation.machine-compatibility", "strict_evidence.run_operation.review",
        "evidence_usage.run_transact"}, "status-reader", [host.root],
        authority_file=host.policy_path, state_directory=host.host)


def test_native_status_current_pass_is_superseded_by_later_failed_review(host):
    review(host)
    status = CORE.review_status(host.root, claim_id="product-source")
    assert status["claims"][0]["review"]["passed"]
    prior = status["claims"][0]["review"]["review_id"]
    review(host, verdict="fail")
    failed = CORE.review_status(host.root)
    assert not failed["claims"][0]["accepted"]
    assert not failed["claims"][0]["review"]["passed"]
    assert failed["claims"][0]["review"]["review_id"] != prior
    CONTRACT.validate_shape(failed, CONTRACT.schema_documents()[failed["schema_version"]])


def test_material_change_cannot_revive_prior_pass(host):
    review(host)
    host.claims["claims"][0]["limitations"].append("Additional current limitation.")
    save_claims(host)
    status = CORE.review_status(host.root)
    assert status["claims"][0]["review"]["reason"] == "strict_review_missing_or_stale"
    assert not status["claims"][0]["accepted"]


def test_machine_policy_ordinary_route_is_closed_and_current(host):
    result = CORE.machine_compatibility(host.root)
    assert result["compatible"], result
    CONTRACT.validate_shape(result, CONTRACT.schema_documents()[result["schema_version"]])


@pytest.mark.parametrize("field,name", [("source_policy", "manual_review_required"),
                                       ("freshness_policy", "manual_review")])
def test_unlinked_human_contract_is_incompatible_before_evidence(host, field, name):
    path = host.root / f"sources/coverage/{QUESTION}.yml"
    document = yaml.safe_load(path.read_bytes())
    coverage = CORE.sibling("coverage_manifest")
    facet = next(iter(coverage.all_facets(document)))
    facet[field] = name
    facet["accepted_source_ids"] = []
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    result = CORE.machine_compatibility(host.root)
    assert not result["compatible"]
    assert name in result["questions"][0]["policy_reasons"]
    CONTRACT.validate_shape(result, CONTRACT.schema_documents()[result["schema_version"]])


def test_missing_coverage_is_unknown_contract_not_machine_permission(host):
    (host.root / f"sources/coverage/{QUESTION}.yml").unlink()
    result = CORE.machine_compatibility(host.root)
    assert not result["compatible"]
    assert result["reason"] == "coverage_contract_unknown"


@pytest.mark.parametrize("selection", [[], [QUESTION, QUESTION], [3], "question"])
def test_malformed_selected_scope_refuses(host, selection):
    with pytest.raises(Exception) as caught:
        CORE.machine_compatibility(host.root, question_slugs=selection)
    assert CORE.sibling("_script_errors").is_refusal(caught.value)
    assert caught.value.details["reason"] == "strict_question_unknown"


def test_public_status_and_policy_routes_use_native_owner(host):
    context = selected_context(host)
    with context.activate(), Workspace.open(host.root) as workspace:
        status = workspace.strict.review_status("product-source")
        assert status == CORE.review_status(host.root, claim_id="product-source")
        assert workspace.strict.machine_compatibility()["compatible"]
        with pytest.raises(Exception) as caught:
            workspace.strict.revoke_source({"payload": []})
        assert caught.value.error_code == "STRICT_EVIDENCE_REFUSED"


def test_key_revocation_invalidates_current_status_without_deleting_history(host):
    review(host)
    retained = CORE.review_status(host.root)["claims"][0]["review"]["review_id"]
    host.policy["revoked_keys"] = ["evaluator-key"]
    host.save_policy()
    status = CORE.review_status(host.root)
    assert not status["claims"][0]["accepted"]
    assert status["claims"][0]["review"]["reason"] == "strict_review_authority_changed"
    with CORE.sibling("_evidence_usage").current_view(host.root, host.config) as view:
        assert any(retained == CONTRACT.artifact_id(row["envelope"]["payload"]["body"]["review"])
                   for row in view.state.strict_reviews.values())


def test_conditional_pack_policy_current_manual_outcome_blocks_machine(host):
    from tests.test_strict_evidence import USAGE
    # A real hash-bound sidecar is needed for conditional record absence; an
    # absent structured view is a mechanical failure, rather than a human gate.
    raw = host.normalized_path.read_text()
    metadata = yaml.safe_load(raw.split("---", 2)[1])
    sidecar = CORE.sibling("_structured_view").sidecar_path(host.normalized_path.parent, host.body["source_id"])
    data = b'{"present":"fact"}\n'
    metadata["structured_view"] = {"path": sidecar.relative_to(host.root).as_posix(),
        "content_hash": "sha256:" + hashlib.sha256(data).hexdigest()}
    normalized = ("---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---" + raw.split("---", 2)[2]).encode()
    supplied, files = host.temporal_source(normalized=normalized, evidence={
        "raw/web/vendor-product.html": host.raw_path.read_bytes(), sidecar.relative_to(host.root).as_posix(): data})
    host.transact(USAGE, "deposit", supplied, files)
    host.normalized_path.write_bytes(normalized)
    sidecar.write_bytes(data)
    name = "pack:controlled/required-field"
    host.config["domain_pack"] = {"name": "controlled", "policy_vocabularies": {
        "identity_policy": {name: "Required source field must be independently inspected when absent."}},
        "policy_rules": {name: {"all_of": [{"equals": {"field": "record/nonexistent",
            "value": "expected", "when_absent": "manual_review"}}]}}}
    (host.root / "research.yml").write_text(yaml.safe_dump(host.config, sort_keys=False))
    path = host.root / f"sources/coverage/{QUESTION}.yml"
    document = yaml.safe_load(path.read_bytes())
    next(iter(CORE.sibling("coverage_manifest").all_facets(document)))["identity_policy"] = name
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    result = CORE.machine_compatibility(host.root)
    assert not result["compatible"]
    assert name in result["questions"][0]["policy_reasons"]
    CONTRACT.validate_shape(result, CONTRACT.schema_documents()[result["schema_version"]])


def test_declared_pack_manual_rule_blocks_before_source_link(host):
    name = "pack:controlled/required-field"
    host.config["domain_pack"] = {"name": "controlled", "policy_vocabularies": {
        "identity_policy": {name: "This contract always requires host human review."}},
        "policy_rules": {name: {"manual_review_required": True, "all_of": [{"equals": {
            "field": "provenance/nonexistent", "value": "expected"}}]}}}
    (host.root / "research.yml").write_text(yaml.safe_dump(host.config, sort_keys=False))
    path = host.root / f"sources/coverage/{QUESTION}.yml"
    document = yaml.safe_load(path.read_bytes())
    facet = next(iter(CORE.sibling("coverage_manifest").all_facets(document)))
    facet["identity_policy"] = name
    facet["accepted_source_ids"] = []
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    result = CORE.machine_compatibility(host.root)
    assert not result["compatible"]
    assert name in result["questions"][0]["policy_reasons"]
    CONTRACT.validate_shape(result, CONTRACT.schema_documents()[result["schema_version"]])


def test_public_signed_source_revocation_uses_existing_checkpoint_owner(host):
    envelope = host.command("revoke", {"source_id": host.body["source_id"], "scope": "revision",
        "source_revision": host.body["source_revision"], "reason": "owner-withdrawal"})
    with selected_context(host).activate(), Workspace.open(host.root) as workspace:
        receipt = workspace.strict.revoke_source(envelope)
        assert receipt["checkpoint"] != host.checkpoint
        with pytest.raises(Exception) as caught:
            workspace.strict.review_status()
        assert caught.value.details["reason"] == "source_revision_revoked"


def test_public_signed_supersession_is_current_native_registration(host, monkeypatch):
    from tests.test_strict_evidence import USAGE
    review(host)
    before = CORE.review_status(host.root)["claims"][0]["review"]["review_id"]
    # Build the next authentic envelope without sending it; the public call below
    # performs the actual canonical transaction and complete owner validation.
    with monkeypatch.context() as staged:
        staged.setattr(USAGE, "transact", lambda *_args: {"checkpoint": host.checkpoint})
        envelope = review(host, verdict="unknown")
    with selected_context(host).activate(), Workspace.open(host.root) as workspace:
        receipt = workspace.strict.supersede_review(envelope)
        assert receipt["checkpoint"] != host.checkpoint
        status = workspace.strict.review_status()
        assert not status["claims"][0]["review"]["passed"]
        assert status["claims"][0]["review"]["review_id"] != before


def test_closing_configuration_change_refuses_old_compatible_observation(host, monkeypatch):
    original = CORE.configuration
    def changed(root):
        value = original(root)
        value["strict_evidence"]["human_review"] = True
        return value
    monkeypatch.setattr(CORE, "configuration", changed)
    with pytest.raises(Exception) as caught:
        CORE.machine_compatibility(host.root)
    assert caught.value.details["reason"] == "strict_policy_changed"


def test_known_machine_contract_can_preflight_before_any_claim_inventory(host):
    (host.root / host.strict_policy["claims_path"]).unlink()
    result = CORE.machine_compatibility(host.root)
    assert result["compatible"] and result["reason"] == "ordinary_evaluator_route"


def test_invalid_pack_rule_is_finite_unknown_contract(host):
    name = "pack:controlled/required-field"
    host.config["domain_pack"] = {"name": "controlled", "policy_vocabularies": {
        "identity_policy": {name: "Requires a known executable contract."}}, "policy_rules": {name: "invalid"}}
    (host.root / "research.yml").write_text(yaml.safe_dump(host.config, sort_keys=False))
    result = CORE.machine_compatibility(host.root)
    assert not result["compatible"] and result["reason"] == "coverage_contract_unknown"
    CONTRACT.validate_shape(result, CONTRACT.schema_documents()[result["schema_version"]])
