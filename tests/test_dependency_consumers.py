"""Versioned native publication consumers preserve their authority boundaries."""

import copy
import json
from types import SimpleNamespace

import pytest

from evidence_wiki import cli, frameworks, orchestration
from evidence_wiki.errors import UsageError
from evidence_wiki.resolution_contracts import DEPENDENCY_RESOLUTION, RESOLUTION, checked, schema_document
from evidence_wiki.resolution_projection import _project
from tests.test_resolution_projection import H, inputs, native_question


def dependency_inputs(*, complete=False):
    value = inputs()
    publication = value["current"]["canonical_release"]["publication"]
    publication.update(schema_version="evidence-strict-publication/v3", review_basis_mode="claim_dependencies_v1",
        claim_schema_version="evidence-strict-claims/v2", review_producer_id=H)
    if complete:
        publication["verdict"] = "ship"
        for question in publication["questions"]:
            question.update(native_question(question["slug"], question["original_id"], True))
        for original in value["current"]["accounting"]["original_outcomes"]:
            original["accepted"] = True
            for question in original["questions"]:
                question.update(accepted=True, gaps=[])
                value["derived_proofs"][question["slug"]] = {
                    "eligible": True, "claim_refs": ["claim-" + question["slug"]], "reasons": []}
        for original in publication["original_outcomes"]:
            original["accepted"] = True
    for question in publication["questions"]:
        for claim in question["claims"]:
            claim.update(calculations=[], claim_basis_id=H, observed_workspace_basis_id=H,
                review_basis_mode="claim_dependencies_v1")
            claim["verification"]["review"].update(review_id=H, principal="fixture-evaluator",
                independence="host_controller_assertion", human_review=False, basis_id=H, reuse="fresh")
    return value


def test_dependency_resolution_retains_native_claim_provenance_without_private_verification():
    value = dependency_inputs(complete=True)
    result = _project(**value)
    assert result["schema_version"] == DEPENDENCY_RESOLUTION
    assert result["verdict"] == "accept"
    assert [row["outcome"] for row in result["original_outcomes"]] == ["accept", "accept"]
    claim = result["original_outcomes"][0]["eligible_questions"][0]["claims"][0]
    assert claim["claim_basis_id"] == claim["observed_workspace_basis_id"] == H
    assert claim["review_basis_mode"] == "claim_dependencies_v1"
    assert "verification" not in claim
    assert result["current_publication"] is None and result["external_action_authorized"] is False
    assert checked(result) == result
    relabeled = copy.deepcopy(result)
    relabeled["schema_version"] = RESOLUTION
    with pytest.raises(UsageError):
        checked(relabeled)


@pytest.mark.parametrize("mutation", ["provenance_missing", "provenance_invalid", "private_verification", "unknown"])
def test_dependency_resolution_closed_claim_shape(mutation):
    result = _project(**dependency_inputs())
    claim = result["original_outcomes"][0]["eligible_questions"][0]["claims"][0]
    if mutation == "provenance_missing":
        claim.pop("claim_basis_id")
    elif mutation == "provenance_invalid":
        claim["observed_workspace_basis_id"] = "unbound"
    elif mutation == "private_verification":
        claim["verification"] = {}
    else:
        claim["unknown"] = True
    with pytest.raises(UsageError):
        checked(result)


@pytest.mark.parametrize("version", ["evidence-strict-publication/v1", "evidence-strict-publication/v2"])
def test_existing_publications_keep_resolution_version_and_schema(version):
    value = inputs()
    value["current"]["canonical_release"]["publication"]["schema_version"] = version
    if version.endswith("v2"):
        for question in value["current"]["canonical_release"]["publication"]["questions"]:
            for claim in question["claims"]:
                claim["calculations"] = []
    assert _project(**value)["schema_version"] == RESOLUTION
    assert schema_document()["properties"]["schema_version"] == {"const": RESOLUTION}


@pytest.mark.parametrize("mutation", [None, "missing_provenance", "invalid_mode", "unknown_field"])
def test_framework_selective_export_requires_full_native_schema(tmp_path, monkeypatch, mutation):
    document = dependency_inputs(complete=True)["current"]["canonical_release"]["publication"]
    claim = document["questions"][0]["claims"][0]
    if mutation == "missing_provenance":
        claim.pop("claim_basis_id")
    elif mutation == "invalid_mode":
        document["review_basis_mode"] = "unknown"
    elif mutation == "unknown_field":
        document["approved"] = True
    raw = json.dumps(document)
    monkeypatch.setattr(frameworks, "resource_document", lambda *_: {"sha256": "a" * 64})
    monkeypatch.setattr(orchestration, "_execute_bounded", lambda *args, **kwargs:
        orchestration.ProcessResult(0, raw, ""))
    call = {"schema_version": frameworks.CALL_SCHEMA, "request_id": "fixture", "operation": "strict_export",
        "instruction_sha256": "a" * 64, "parameters": {}}
    if mutation is not None:
        with pytest.raises(UsageError):
            frameworks.invoke(json.dumps(call).encode(), target=tmp_path)
    else:
        result = frameworks.invoke(json.dumps(call).encode(), target=tmp_path)
        assert result["result_json"] == raw and result["evidence_acceptance"] == "eligible"
        assert result["host_enforced"] is False


@pytest.mark.parametrize("operation", ["publication", "export"])
def test_selective_cli_output_requires_host_delivery(tmp_path, monkeypatch, operation):
    output = tmp_path / "output.json"
    parsed = SimpleNamespace(project_root=tmp_path / "workspace", output=str(output), question=["one"],
        expected_revision=None, command=None, citation_verification=False, status=None, format="json")
    script = SimpleNamespace(parse_args=lambda *_: parsed, json_mode_requested=lambda *args, **kwargs: True,
        refusal=lambda *_: ValueError("strict_output_requires_host_delivery"), is_refusal=lambda exc: isinstance(exc, ValueError))
    document = {"schema_version": "evidence-strict-publication/v3"}
    handle = SimpleNamespace(publish_selected=lambda *args, **kwargs: document,
        export_answers=lambda **kwargs: document)
    monkeypatch.setattr(cli, "_packaged_script", lambda *_: script)
    monkeypatch.setattr(cli, "_forward_target", lambda args, **kwargs: args)
    monkeypatch.setattr(cli, "_handle", lambda *_: handle)
    monkeypatch.setattr(cli, "_emit_refusal", lambda *args, **kwargs: 27)
    run = cli._run_publication if operation == "publication" else cli._run_questions_export
    assert run([]) == 27
    assert not output.exists()
