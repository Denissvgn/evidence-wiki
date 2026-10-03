"""Lossless ordinary objectives and proposals cannot widen host declarations."""

from __future__ import annotations

import base64
import copy
import hashlib
import subprocess

import pytest

from evidence_wiki.onboarding_contract import decode_document, encode_document
from evidence_wiki.request_compiler import PROPOSAL, RequestCompilationError, compile_request
from evidence_wiki.run_policy import RunPolicy
from tests.test_run_policy import policy_document


def inputs(objective="Compare the supplied evidence.", *, ids=("q1",), human=False):
    document = policy_document()
    document["objective"] = objective
    document["scope"] = {"question_ids": list(ids), "source_scope": ["/evidence", "https://example.test/study"]}
    document["human_review"] = human
    policy = RunPolicy(document)
    host = {"target": {"writable_root": "/research", "relative_path": "study"},
        "authority": {"role": "external_host", "reference": "configured-host", "allowed_actions": ["local_setup", "local_research"],
            "source_scope": ["/evidence", "https://example.test/study"], "writable_roots": ["/research"], "credential_references": []},
        "scope": [{"name": "jurisdiction", "value": "Spain"}],
        "evidence_policy": {"schema_version": "evidence-strict-policy/v1", "policy_id": "host-evidence", "revision": "one",
            "assurance": "artifact_checked", "claims_path": "docs/claims.json", "instructions": {"AGENTS.md": "sha256:" + "a" * 64},
            "rubric": {"id": "host-rubric", "revision": "one", "criteria": {key: "Retain the host criterion." for key in
                ("support", "source_suitability", "scope", "time", "units", "counterevidence")}},
            "human_review": human, "max_source_age_seconds": 1000, "max_review_age_seconds": 1000}}
    return policy, host


def proposal(raw, *, ids=("q1",), boundaries=None, derived=None):
    boundaries = boundaries or [(0, len(raw))]
    rows = [{"id": qid, "start_byte": start, "end_byte": end} for qid, (start, end) in zip(ids, boundaries, strict=True)]
    return {"schema_version": PROPOSAL, "interpretations": [{"id": "candidate", "original_questions": rows,
        "derived_questions": derived or [], "source_ids": [], "scope": [], "assumptions": ["Proposed interpretation is unverified."],
        "rationale": "Preserve the supplied objective and host controls.", "confidence": "0.95"}]}


@pytest.mark.parametrize("text", ["  ¿Qué muestran los datos?\r\n", "第二行について調べてください。", "  قارن الأدلة المتاحة؟  ",
                                  "Compare α and β. Keep the original constraints.", "é e\u0301 🧪"])
def test_multilingual_original_bytes_are_exact_and_canonical_request_is_valid(text):
    policy, host = inputs(text)
    raw = text.encode()
    result = compile_request(raw, host_scope=host, policy=policy)
    assert result["status"] == "compiled" and result["terminal"]
    assert base64.b64decode(result["original"]["bytes_base64"]) == raw
    assert result["original"]["sha256"] == hashlib.sha256(raw).hexdigest()
    document = result["request"]["request"]
    assert document["payload"]["goal"] == text and document["payload"]["questions"] == [{"id": "q1", "text": text}]
    assert decode_document("onboarding/research_request/v2", encode_document("onboarding/research_request/v2", document)) == document
    assert result["request"]["decisions"]["policy"] == host["evidence_policy"]
    assert result["policy"] == policy.to_dict()
    assert not result["actions_executed"] and not result["authority_verified"] and not result["evidence_accepted"]
    assert result["semantic_adequacy"] == "not_evaluated" and result["model_calls"] == 0


def test_defaults_visible_detached_and_request_never_promotes_assurance():
    policy, host = inputs()
    supplied = {"language": "es", "outputs": ["json"], "budgets": {"seconds": 10}}
    result = compile_request(policy.to_dict()["objective"], host_scope=host, policy=policy, defaults=supplied)
    host["authority"]["allowed_actions"].append("acquisition")
    supplied["outputs"].clear()
    assert result["request"]["request"]["payload"]["outputs"] == ["json"]
    assert result["request"]["decisions"]["language"] == "es"
    defaults = {row["field"]: row for row in result["defaults"]}
    assert defaults["language"]["basis"] == "host_default"
    assert defaults["project_name"]["basis"] == "conservative_default"
    assert defaults["budgets.seconds"]["value"] == 10
    assert defaults["budgets.downloads"]["value"] == defaults["budgets.bytes"]["value"] == 0
    assert result["confidence_provenance"]["value"] is None


@pytest.mark.parametrize("missing", ["target", "authority", "scope", "evidence_policy", "policy", "authority.source_scope",
                                     "target.relative_path"])
def test_missing_mandatory_context_is_terminal_without_clarification(missing):
    policy, host = inputs()
    if missing == "policy":
        policy = None
    elif "." in missing:
        parent, field = missing.split(".")
        del host[parent][field]
    else:
        del host[missing]
    result = compile_request("Compare the supplied evidence.", host_scope=host, policy=policy)
    assert result["status"] == "insufficient_context" and result["terminal"]
    assert missing in result["missing"] and result["request"] is None
    assert result["reason"] == "mandatory_context_missing"


def test_empty_objective_and_ambiguous_partition_are_completed_refusals():
    result = compile_request(b" \r\n", host_scope={})
    assert result["status"] == "insufficient_context" and result["original"]["text"] == " \r\n"
    policy, host = inputs("Compare these things with their stated limits.", ids=("first", "second"))
    result = compile_request(policy.to_dict()["objective"], host_scope=host, policy=policy)
    assert result["status"] == "insufficient_context" and result["reason"] == "original_question_partition_unresolved"


def test_question_terminators_partition_without_dropping_constraints_or_whitespace():
    text = "  ¿Qué funciona?\r\n何が必要？  Keep the stated limits. "
    policy, host = inputs(text, ids=("first", "second", "constraints"))
    result = compile_request(text, host_scope=host, policy=policy)
    questions = result["request"]["request"]["payload"]["questions"]
    assert "".join(row["text"] for row in questions) == text
    assert [row["id"] for row in questions] == ["first", "second", "constraints"]


def test_anchored_proposal_keeps_every_original_and_labels_confidence_unverified():
    text = "  Compare the local observations.\r\n"
    raw = text.encode()
    policy, host = inputs(text)
    derived = [{"id": "detail", "text": "Which limits are declared?", "original_ids": ["q1"],
                "anchors": [{"original_id": "q1", "start_byte": 0, "end_byte": len(raw)}]}]
    document = proposal(raw, derived=derived)
    result = compile_request(raw, host_scope=host, policy=policy, proposal=document)
    payload = result["request"]["request"]["payload"]
    assert payload["questions"] == [{"id": "q1", "text": text}]
    assert payload["derived_questions"][0]["original_ids"] == ["q1"]
    assert result["confidence_provenance"] == {"basis": "unverified_proposal_assertion", "value": "0.95"}
    assert not result["interpretations"][0]["confidence_verified"]
    assert payload["scope"] == host["scope"] and payload["authority"] == host["authority"]


@pytest.mark.parametrize("attack", ["root", "grant", "policy", "source", "scope", "drop", "gap", "overlap", "byte_boundary", "parent", "identity"])
def test_proposals_cannot_escalate_or_discard_originals(attack):
    text = "é Compare the supplied evidence."
    policy, host = inputs(text)
    document = proposal(text.encode())
    candidate = document["interpretations"][0]
    if attack in {"root", "grant", "policy"}:
        candidate[{"root": "target", "grant": "authority", "policy": "strict_evidence"}[attack]] = {}
    elif attack == "source":
        candidate["source_ids"] = ["invented-source"]
    elif attack == "scope":
        candidate["scope"] = [{"name": "jurisdiction", "value": "Anywhere"}]
    elif attack == "drop":
        candidate["original_questions"] = []
    elif attack == "gap":
        candidate["original_questions"][0]["start_byte"] = 2
    elif attack == "overlap":
        candidate["original_questions"].append(dict(candidate["original_questions"][0]))
    elif attack == "byte_boundary":
        candidate["original_questions"][0]["end_byte"] = 1
    elif attack == "identity":
        candidate["original_questions"][0]["id"] = "other"
    else:
        candidate["derived_questions"] = [{"id": "detail", "text": "Question?", "original_ids": ["unknown"],
            "anchors": [{"original_id": "unknown", "start_byte": 0, "end_byte": 2}]}]
    with pytest.raises(RequestCompilationError):
        compile_request(text, host_scope=host, policy=policy, proposal=document)


@pytest.mark.parametrize("attack", ["objective", "human", "assurance", "seconds", "questions", "source_scope", "locator", "path"])
def test_host_defaults_and_selections_cannot_lower_or_exceed_frozen_policy(attack):
    policy, host = inputs(human=True)
    text, defaults = policy.to_dict()["objective"], {}
    if attack == "objective":
        text = "Changed objective."
    elif attack == "human":
        host["evidence_policy"]["human_review"] = False
    elif attack == "assurance":
        host["evidence_policy"]["assurance"] = "host_enforced"
    elif attack in {"seconds", "questions"}:
        defaults = {"budgets": {attack: policy.to_dict()["budgets"][attack] + 1}}
    elif attack == "source_scope":
        host["authority"]["source_scope"] = ["/private"]
    elif attack == "locator":
        host["sources"] = [{"id": "s1", "kind": "local_file", "locator": "/private/record", "question_ids": ["q1"]}]
    else:
        host["target"]["relative_path"] = "../escape"
    with pytest.raises(RequestCompilationError):
        compile_request(text, host_scope=host, policy=policy, defaults=defaults)


def test_declared_sources_are_preserved_but_never_invented_from_objective():
    text = "Use https://untrusted.test/claim and compare the observations."
    policy, host = inputs(text)
    result = compile_request(text, host_scope=host, policy=policy)
    assert result["request"]["request"]["payload"]["sources"] == []
    host["sources"] = [{"id": "s1", "locator": "/evidence/record.txt", "kind": "local_file", "question_ids": ["q1"]}]
    result = compile_request(text, host_scope=host, policy=policy)
    assert result["request"]["request"]["payload"]["sources"] == host["sources"]
    assert not result["evidence_accepted"]


def test_least_editorial_candidate_wins_without_choosing_by_model_confidence():
    policy, host = inputs()
    text = policy.to_dict()["objective"]
    document = proposal(text.encode())
    second = copy.deepcopy(document["interpretations"][0])
    second.update(id="simple", confidence="0.01")
    document["interpretations"][0]["derived_questions"] = [{"id": "extra", "text": "Extra?", "original_ids": ["q1"],
        "anchors": [{"original_id": "q1", "start_byte": 0, "end_byte": len(text.encode())}]}]
    document["interpretations"].append(second)
    result = compile_request(text, host_scope=host, policy=policy, proposal=document)
    assert result["interpretations"][0]["id"] == "simple"
    assert not result["request"]["request"]["payload"]["derived_questions"]


def test_compilation_has_no_process_network_or_evidence_effects(tmp_path, monkeypatch):
    policy, host = inputs()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("process invocation"))
    monkeypatch.setattr("evidence_wiki.host_capabilities.known_credentials", lambda: pytest.fail("credential inspection"))
    before = list(tmp_path.iterdir())
    result = compile_request(policy.to_dict()["objective"], host_scope=host, policy=policy)
    assert result["status"] == "compiled" and list(tmp_path.iterdir()) == before


@pytest.mark.parametrize("bad", [b"\xff", "bad\0text", 1, None])
def test_invalid_originals_have_content_free_typed_errors(bad):
    with pytest.raises(RequestCompilationError) as exc:
        compile_request(bad)
    assert exc.value.error_code == "REQUEST_COMPILATION_REFUSED"
    assert exc.value.details["field"] == "/objective"


@pytest.mark.parametrize("collection, entry, missing", [
    ("scope", {"name": "jurisdiction"}, "value"),
    ("scope", {"value": "Spain"}, "name"),
    ("sources", {"id": "s1", "kind": "local_file", "locator": "/evidence/record.txt"}, "question_ids"),
    ("sources", {"id": "s1", "kind": "local_file", "question_ids": ["q1"]}, "locator"),
    ("sources", {"locator": "/evidence/record.txt", "kind": "local_file", "question_ids": ["q1"]}, "id"),
    ("host_tools", {"id": "browser", "capabilities": [], "basis": "declared"}, "kind"),
])
def test_missing_array_entry_fields_are_terminal_context_refusals(collection, entry, missing):
    policy, host = inputs()
    host[collection] = [entry]
    result = compile_request(policy.to_dict()["objective"], host_scope=host, policy=policy)
    assert result["status"] == "insufficient_context" and result["terminal"]
    assert result["reason"] == "mandatory_context_missing"
    assert collection + ".0." + missing in result["missing"]
    assert result["request"] is None and not result["actions_executed"]


@pytest.mark.parametrize("action, effect", [
    ("local_setup", "revise"), ("local_research", "revise"),
    ("local_pack_authoring", "revise"), ("same_pack_refresh", "revise"),
    ("discovery", "acquire"), ("acquisition", "acquire"), ("host_capture", "acquire"),
    ("export", "deliver"), ("dependency_install", None),
])
@pytest.mark.parametrize("selection", ["offline_only", "capability_missing", "effect_and_capability"])
def test_every_authority_action_requires_representable_effect_and_capability(action, effect, selection):
    original_policy, host = inputs()
    document = original_policy.to_dict()
    document["policy_id"] = None
    if selection == "offline_only":
        document["allowed_effects"] = ["offline_check"]
    elif selection == "capability_missing" and effect is not None:
        document["host_capability_refs"][effect] = None
    policy = RunPolicy(document)
    host["authority"]["allowed_actions"] = [action]
    result = compile_request(document["objective"], host_scope=host, policy=policy)
    retained = effect is not None and selection == "effect_and_capability"
    assert result["request"]["request"]["payload"]["authority"]["allowed_actions"] == ([action] if retained else [])
    recorded = next(row for row in result["defaults"] if row["field"] == "authority.allowed_actions")
    assert recorded["requested"] == [action]
    assert recorded["decisions"][0]["effect"] == effect and recorded["decisions"][0]["retained"] is retained
    reason = ("action_not_representable" if effect is None else "effect_not_permitted" if selection == "offline_only"
              else "capability_reference_missing" if selection == "capability_missing" else "retained_declaration")
    assert recorded["decisions"][0]["reason"] == reason
    assert not result["authority_verified"] and result["policy"] == policy.to_dict()


def test_unknown_authority_actions_are_content_free_typed_refusals():
    policy, host = inputs()
    host["authority"]["allowed_actions"] = ["unknown-sensitive-action"]
    with pytest.raises(RequestCompilationError) as error:
        compile_request(policy.to_dict()["objective"], host_scope=host, policy=policy)
    assert "unknown-sensitive-action" not in str(error.value) + str(error.value.details)


@pytest.mark.parametrize("selection", ["offline_only", "capability_missing", "effect_and_capability"])
def test_real_setup_planner_respects_compiled_setup_permission(tmp_path, monkeypatch, selection):
    import evidence_wiki.planning as planning
    import evidence_wiki.planning_contracts as planning_contracts
    from evidence_wiki._pack_io import canonical
    from tests.test_research_planning import question_plan

    monkeypatch.setattr(planning_contracts, "known_credentials", lambda: ())
    monkeypatch.setattr(planning, "_no_secret_values", lambda value: None)
    original_policy, host = inputs()
    document = original_policy.to_dict()
    document["policy_id"] = None
    if selection == "offline_only":
        document["allowed_effects"] = ["offline_check"]
    elif selection == "capability_missing":
        document["host_capability_refs"]["revise"] = None
    policy = RunPolicy(document)
    host["target"]["writable_root"] = str(tmp_path)
    host["authority"]["writable_roots"] = [str(tmp_path)]
    host["question_plans"] = [question_plan("q1")]
    compiled = compile_request(document["objective"], host_scope=host, policy=policy)
    original = compiled["original"]
    evidence_policy = compiled["request"]["decisions"]["policy"]
    plan = planning.compile_plan(canonical(compiled["request"]))
    reasons = {row["reason"] for row in plan["blockers"]}
    assert ("setup_authority_not_declared" in reasons) is (selection != "effect_and_capability")
    assert not (tmp_path / "study").exists()
    assert compiled["original"] == original and compiled["request"]["decisions"]["policy"] == evidence_policy
