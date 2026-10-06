"""Inert context and field-only reports never authenticate native eligibility."""

import copy
import json

import pytest

from evidence_wiki.brief_projection import project, render, render_content
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.readability_context import create, reduce
from evidence_wiki.readability_contracts import OUTPUT, output_document
from evidence_wiki.resolution_projection import _project
from tests.test_resolution_projection import inputs


def context():
    resolution = _project(**inputs())
    for row in resolution["original_outcomes"]:
        row["original_text"] = "Question about source measurements."
    resolution["original_outcomes"][0]["eligible_questions"][0]["unresolved_claims"] = [
        {"id": "prior-private-claim", "qualification": "insufficient_evidence", "reasons": ["reviewer-secret-name-rejected"]}]
    brief = project(resolution)
    packet, binding = create(brief, context_id="blind-context", order_seed=b"s" * 32)
    return brief, packet, binding


def test_complete_blind_payload_withholds_control_metadata_and_unresolved_diagnostics():
    _brief, packet, binding = context()
    text = json.dumps(packet)
    for private in ("prior-private-claim", "secret-name", "fixture-run", "historical_fixture", "current_owned_pass", "claim-one", "question-a"):
        assert private not in text
    assert "Current publication and mandatory authenticated" in text
    assert packet["payload"]["markdown"] == render_content(packet["payload"]["brief"])
    assert "prior-private-claim" in json.dumps(binding)


def test_readable_public_and_blind_hierarchy_retains_limits_outcomes_and_scientific_fields():
    brief, packet, _binding = context()
    public = render(brief)["markdown"]
    blind = packet["payload"]["markdown"]
    for heading in ("## Original question", "### Reviewed question", "#### Findings", "#### Inferences",
            "##### Evidence", "##### Limitations", "### Derived and unresolved outcomes", "## Global limits and unresolved work"):
        assert heading in public and heading in blind
    assert "original outcomes /" not in public
    assert "Scope:" in public and "Time:" in public and "Units:" in public


def test_owned_context_reconstruction_precedes_target_mapping():
    brief, packet, binding = context()
    pointer = next(key for key in binding["field_targets"] if key.endswith("/text"))
    output = {"schema_version": OUTPUT, "context_id": packet["context_id"], "input_sha256": digest(packet),
        "state": "issues", "issues": [{"code": "meaning_unclear", "pointer": pointer,
            "field_sha256": packet["field_manifest"][pointer]}]}
    result = reduce(output, packet, binding, current_brief=brief, order_seed=b"s" * 32)
    assert result["issues"][0]["claim_id"] == "claim-one"
    changed = copy.deepcopy(brief)
    changed["original_outcomes"][0]["eligible_questions"][0]["claims"][0]["text"] = "Changed evidence statement."
    with pytest.raises(UsageError, match="Readability"):
        reduce(output, packet, binding, current_brief=changed, order_seed=b"s" * 32)


@pytest.mark.parametrize("extra", [{"replacement": "approve"}, {"approval": True}, {"principal": "reviewer"}])
def test_output_cannot_carry_edits_authority_or_reviewer_identity(extra):
    _brief, packet, _binding = context()
    output = {"schema_version": OUTPUT, "context_id": packet["context_id"], "input_sha256": digest(packet),
        "state": "issues", "issues": [], **extra}
    with pytest.raises(UsageError):
        output_document(output, packet)
