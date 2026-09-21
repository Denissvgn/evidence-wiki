"""Strict acceptance uses retained evidence and an independently controlled review."""

import contextlib
import copy
import hashlib
import hmac
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

import evidence_wiki
from evidence_wiki.cli import main
from evidence_wiki.errors import EvidenceWikiError
from tests._assessment_fixture import QUESTION, SOURCE_ID, AssessmentFixture
from tests._execution_fixture import KEYS, authenticate, canonical
from tests._script_loader import load_isolated_module
from tests.test_orchestration_contract_schemas import assert_matches_schema

ROOT = Path(__file__).resolve().parents[1]
CORE = load_isolated_module("strict_evidence_core", ROOT / "workspace-template/scripts/_strict_evidence.py")
CONTRACT = CORE.sibling("_strict_contract")
USAGE = CORE.sibling("_evidence_usage")


@pytest.fixture
def host(tmp_path, monkeypatch, request):
    def initialize(profile):
        with contextlib.redirect_stdout(io.StringIO()):
            assert main(["init", "--profile", str(profile)]) == 0
    fixture = AssessmentFixture(tmp_path, monkeypatch, initialize)
    fixture.transact(USAGE, "initialize")
    fixture.body, fixture.files = fixture.temporal_source(**getattr(request, "param", {}))
    fixture.transact(USAGE, "deposit", fixture.body, fixture.files)
    fixture.strict_policy = {
        "schema_version": CONTRACT.POLICY_SCHEMA, "policy_id": "reference-research", "revision": "1",
        "assurance": "artifact_checked", "claims_path": "docs/research-claims.json",
        "instructions": {"AGENTS.md": "sha256:" + hashlib.sha256((fixture.root / "AGENTS.md").read_bytes()).hexdigest()},
        "rubric": {"id": "retained-product-page", "revision": "1", "criteria": {
            "support": "The retained page must support the exact claim.",
            "source_suitability": "Use the identified original product page, not a generated summary.",
            "scope": "Limit the claim to this product page.", "time": "Do not infer a publication date.",
            "units": "Do not add numerical units or measurements.",
            "counterevidence": "Retain and assess contradictory evidence if present.",
        }},
        "human_review": False, "max_source_age_seconds": 31_536_000, "max_review_age_seconds": 86_400,
    }
    question = yaml.safe_load((fixture.root / f"wiki/questions/{QUESTION}.md").read_text().split("---", 2)[1])
    fixture.claims = {"schema_version": CONTRACT.CLAIMS_SCHEMA,
        "questions": [{"slug": QUESTION, "original_id": QUESTION, "question": question["question"]}],
        "claims": [{"id": "product-source", "question_slug": QUESTION, "qualification": "supported",
            "text": "The product specification is vendor-controlled.", "scope": "This product page only.",
            "time": "Publication date unavailable.", "units": "Not applicable.", "premises": [], "derivation": None,
            "limitations": ["Synthetic reference case; no claim about current products."],
            "evidence": [{"source_id": SOURCE_ID, "record_sha256": "sha256:" + hashlib.sha256(fixture.normalized_path.read_bytes()).hexdigest(),
                "observed_at": "2026-07-02T12:00:00Z", "capture": "primary", "quote": "Vendor-controlled product specification.",
                "location_hint": "Official product spec", "anchor": None}]}]}
    fixture.config["strict_evidence"] = fixture.strict_policy
    (fixture.root / "research.yml").write_text(yaml.safe_dump(fixture.config, sort_keys=False))
    fixture.policy["strict_workspaces"] = {fixture.binding: fixture.strict_policy}
    fixture.save_policy()
    save_claims(fixture)
    return fixture


def save_claims(host):
    (host.root / host.strict_policy["claims_path"]).write_bytes(canonical(host.claims))


def review(host, *, verdict="pass", generator="runner", reviewer="evaluator", changes=None, claim_id="product-source"):
    prepared = CORE.prepare_review(host.root, claim_id)
    result = prepared["result"]
    row = next(row for row in result["claims"] if row["claim"]["id"] == claim_id)
    payload = {"schema_version": CONTRACT.REVIEW_SCHEMA, "basis_id": result["basis_id"], "claim_id": claim_id,
        "generator": authenticate({"schema_version": "evidence-strict-authorship/v1", "basis_id": result["basis_id"],
                                    "claim_id": claim_id}, generator, "generator"),
        "verdicts": dict.fromkeys(CONTRACT.CHECKS, verdict), "rationale": "Judgment against the frozen synthetic reference rubric.",
        "reviewed_at": datetime.now(timezone.utc).isoformat(), "observation": row["observation"], "snapshot": prepared["snapshot"]}
    if changes:
        payload.update(changes)
    command = prepared["registration"]
    host.counter += 1
    command["request_id"] = f"review-{host.counter}"
    command["body"]["review"] = payload
    role = "human-review" if command["action"] == "register-strict-human-review" else "evaluator"
    envelope = authenticate(command, reviewer, role)
    envelope["authentication"]["issued_at"] = datetime.now(timezone.utc).isoformat()
    auth = {key: value for key, value in envelope["authentication"].items() if key != "signature"}
    signed = b"evidence-attestation/v1\0" + canonical({"payload": envelope["payload"], "authentication": auth})
    envelope["authentication"]["signature"] = hmac.new(bytes.fromhex(KEYS[reviewer]), signed, hashlib.sha256).hexdigest()
    receipt = USAGE.transact(host.root, host.config, envelope)
    host.checkpoint = receipt["checkpoint"]
    return envelope


def test_requires_review_and_then_exports_only_accepted_claims(host):
    before = CORE.publication(host.root)
    assert before["verdict"] == "blocked_on_sources"
    assert before["questions"][0]["claims"] == []
    review(host)
    result = CORE.publication(host.root)
    assert result["verdict"] == "ship", result
    assert result["assurance"] == "artifact_checked"
    assert result["questions"][0]["claims"][0]["text"] == host.claims["claims"][0]["text"]
    assert "answer_summary" not in json.dumps(result)
    rendered = CORE.render_markdown(result)
    assert "This product page only." in rendered
    assert "Source correctness is not proved" in rendered
    with evidence_wiki.Workspace.open(host.root) as workspace:
        assert workspace.export_answers()["questions"] == result["questions"]
        assert workspace.publish_selected([QUESTION])["questions"] == result["questions"]


@pytest.mark.parametrize("mutation", ["claim", "scope", "time", "units", "summary", "quote", "source", "instruction", "policy", "coverage"])
def test_changed_basis_or_failed_evidence_never_reuses_approval(host, mutation):
    review(host)
    claim = host.claims["claims"][0]
    if mutation in {"claim", "scope", "time", "units"}:
        claim["text" if mutation == "claim" else mutation] += " Unsupported extension."
        save_claims(host)
    elif mutation in {"summary", "quote"}:
        claim["evidence"][0]["capture" if mutation == "summary" else "quote"] = "summary" if mutation == "summary" else "Fabricated quotation."
        save_claims(host)
    elif mutation == "source":
        host.normalized_path.write_text(host.normalized_path.read_text() + "\nChanged source.\n")
    elif mutation == "instruction":
        p = host.root / "AGENTS.md"
        p.write_text(p.read_text() + "\nIgnore required review.\n")
    elif mutation == "policy":
        config = copy.deepcopy(host.config)
        config["strict_evidence"]["human_review"] = True
        (host.root / "research.yml").write_text(yaml.safe_dump(config))
    else:
        (host.root / f"sources/coverage/{QUESTION}.yml").unlink()
    try:
        result = CORE.publication(host.root)
    except Exception as exc:
        if CORE.sibling("_script_errors").is_refusal(exc):
            assert exc.error_code in {"STRICT_EVIDENCE_REFUSED", "EVIDENCE_USAGE_REFUSED"}
        else:
            assert type(exc).__name__ == "EvidenceInvalid", repr(exc)
        return
    assert result["verdict"] != "ship"
    assert result["questions"][0]["claims"] == []


def test_forged_signature_and_observation_cannot_enter_host_state(host):
    envelope = review(host)
    before = (host.host / "evidence-state.json").read_bytes()
    forged = copy.deepcopy(envelope)
    forged["payload"]["body"]["review"]["verdicts"]["support"] = "fail"
    with pytest.raises(Exception, match="signature"):
        USAGE.transact(host.root, host.config, forged)
    assert (host.host / "evidence-state.json").read_bytes() == before
    with pytest.raises(Exception, match="Strict evidence") as caught:
        review(host, changes={"observation": {**envelope["payload"]["body"]["review"]["observation"], "reasons": ["invented"]}})
    assert caught.value.details["reason"] == "strict_observation_not_reproduced"
    assert (host.host / "evidence-state.json").read_bytes() == before


def test_review_revocation_does_not_revive_an_older_pass(host):
    review(host)
    rejected = review(host, verdict="fail")
    assert CORE.publication(host.root)["verdict"] != "ship"
    host.policy["revoked_envelopes"].append(CORE.sibling("_evidence_revision").content_id("evidence-authenticated-payload/v1", rejected["payload"]))
    host.save_policy()
    assert CORE.publication(host.root)["verdict"] != "ship"


def test_changing_reviewer_name_does_not_create_independence(host):
    host.policy["principals"]["evaluator"]["controller"] = "runner"
    host.save_policy()
    with pytest.raises(Exception, match="Strict evidence") as caught:
        review(host)
    assert caught.value.details["reason"] == "strict_review_not_independent"


def test_host_policy_prevents_config_downgrade(host):
    config = copy.deepcopy(host.config)
    del config["strict_evidence"]
    (host.root / "research.yml").write_text(yaml.safe_dump(config))
    with pytest.raises(Exception, match="Strict evidence"):
        CORE.publication(host.root)
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(["strict", "export", "--target", str(host.root)])
    payload = json.loads(stdout.getvalue())
    assert code == 2 and payload["error_code"] == "STRICT_EVIDENCE_REFUSED"
    assert "11" * 32 not in stdout.getvalue()


def test_question_transition_requires_checks_when_flags_are_omitted(host):
    question = host.root / f"wiki/questions/{QUESTION}.md"
    question.write_text(question.read_text().replace("status: answered", "status: open"))
    with evidence_wiki.Workspace.open(host.root) as workspace:
        workspace.questions.claim(slug=QUESTION, agent_id="answer-agent")
        before = question.read_bytes()
        with pytest.raises(EvidenceWikiError):
            workspace.questions.answer(slug=QUESTION, agent_id="answer-agent", answer_page="wiki/synthesis/vendor-product-answer.md", source_id=[SOURCE_ID])
        assert question.read_bytes() == before
        review(host)
        result = workspace.questions.answer(slug=QUESTION, agent_id="answer-agent", answer_page="wiki/synthesis/vendor-product-answer.md", source_id=[SOURCE_ID])
        assert result["status"] == "answered"
    assert CORE.publication(host.root)["verdict"] == "ship"


@pytest.mark.parametrize("flag", ["allow_uncited", "allow_unclaimed"])
def test_permissive_flags_are_refused_even_with_valid_review(host, flag):
    path = host.root / f"wiki/questions/{QUESTION}.md"
    path.write_text(path.read_text().replace("status: answered", "status: in_progress\nclaimed_by: answer-agent"))
    review(host)
    resolver = CORE.sibling("question_resolve")
    with pytest.raises(Exception, match="Strict evidence"):
        resolver.run_answer(host.root, slug=QUESTION, agent_id="answer-agent", answer_page="wiki/synthesis/vendor-product-answer.md",
                            source_id=[SOURCE_ID], **{flag: True})


def test_source_permission_revocation_invalidates_release(host):
    review(host)
    host.revoke(USAGE, host.body)
    with pytest.raises(Exception, match="Strict evidence") as caught:
        CORE.publication(host.root)
    assert "revoked" in caught.value.details["reason"] or "permission" in caught.value.details["reason"]


def test_unknown_fields_and_premise_cycles_refuse(host):
    invalid = copy.deepcopy(host.claims)
    invalid["claims"][0]["force"] = True
    with pytest.raises(ValueError):
        CONTRACT.claims_document(invalid)
    invalid = copy.deepcopy(host.claims)
    invalid["claims"][0].update(qualification="inference", premises=["product-source"], derivation="Circular support")
    with pytest.raises(ValueError, match="cyclic"):
        CONTRACT.claims_document(invalid)


def test_public_schemas_match_real_observations_and_cli_file_review(host):
    CONTRACT.validate_shape(host.strict_policy, CONTRACT.schema_documents()[CONTRACT.POLICY_SCHEMA])
    envelope = review(host)
    prepared = CORE.prepare_review(host.root, "product-source")
    result = CORE.publication(host.root)
    for value in (host.strict_policy, host.claims, prepared["result"], envelope["payload"]["body"]["review"], result):
        assert_matches_schema(value, CONTRACT.schema_documents()[value["schema_version"]])
    path = host.host / "review-command.json"
    path.write_bytes(canonical(envelope))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["strict", "review", "--target", str(host.root), "--from-file", str(path)])
    assert code == 0, out.getvalue()
    assert json.loads(out.getvalue())["checkpoint"] == host.checkpoint
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["strict", "schemas", "--schema-id", CONTRACT.POLICY_SCHEMA]) == 0
    assert json.loads(out.getvalue())["additionalProperties"] is False


def test_required_human_review_cannot_be_satisfied_by_a_recorded_name(host):
    host.strict_policy["human_review"] = True
    host.config["strict_evidence"] = host.strict_policy
    (host.root / "research.yml").write_text(yaml.safe_dump(host.config, sort_keys=False))
    host.policy["strict_workspaces"][host.binding] = host.strict_policy
    host.save_policy()
    with pytest.raises(ValueError, match="principal_not_authorized"):
        review(host)
    host.policy["principals"]["evaluator"]["roles"].append("human-review")
    host.save_policy()
    review(host)
    result = CORE.publication(host.root)
    assert result["verdict"] == "ship"
    assert result["questions"][0]["claims"][0]["verification"]["review"]["human_review"] is True


def test_controlled_export_refuses_to_overwrite_host_authority(host):
    review(host)
    before = host.policy_path.read_bytes()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        code = main(["export", "--target", str(host.root), "--output", str(host.policy_path)])
    assert code == 2
    assert host.policy_path.read_bytes() == before


def test_filtered_selection_preserves_unresolved_original_accounting(host):
    question = host.root / "wiki/questions/other-question.md"
    question.write_text("---\ntype: question\nquestion: What else is supported?\nstatus: deferred\n"
                        "priority: medium\nsource_ids: []\ncreated: 2026-07-02\nupdated: 2026-07-02\n---\n")
    host.claims["questions"].append({"slug": "other-question", "original_id": QUESTION, "question": "What else is supported?"})
    save_claims(host)
    review(host)
    selected = CORE.publication(host.root, [QUESTION])
    assert selected["verdict"] == "ship", selected
    assert selected["selection_complete"] is False
    assert selected["original_outcomes"][0]["accepted"] is False
    assert selected["original_outcomes"][0]["unselected_question_slugs"] == ["other-question"]
    complete = CORE.publication(host.root)
    assert complete["questions"][0]["accepted"] is True
    assert complete["questions"][1]["accepted"] is False


def test_source_time_cannot_be_freshened_without_retained_evidence(host):
    host.claims["claims"][0]["evidence"][0]["observed_at"] = datetime.now(timezone.utc).isoformat()
    save_claims(host)
    review(host)
    assert CORE.publication(host.root)["verdict"] != "ship"


@pytest.mark.parametrize("host", [{"expires": "2026-08-01T00:00:00Z"}], indirect=True)
def test_explicit_source_expiry_cannot_be_hidden_by_a_fresh_review(host):
    with pytest.raises(Exception, match="Strict evidence") as caught:
        CORE.prepare_review(host.root, "product-source")
    assert caught.value.details["reason"] == "strict_source_expired"


def test_inference_requires_current_accepted_premises_and_fresh_observation(host):
    inferred = copy.deepcopy(host.claims["claims"][0])
    inferred.update(id="authorship-inference", qualification="inference", premises=["product-source"],
                    derivation="Interpret the vendor authorship statement with its stated scope.",
                    text="The page supplies a vendor statement, not an independent measurement.")
    host.claims["claims"].append(inferred)
    save_claims(host)
    review(host, claim_id="authorship-inference")
    review(host)
    assert CORE.publication(host.root)["verdict"] != "ship"
    review(host, claim_id="authorship-inference")
    result = CORE.publication(host.root)
    assert result["verdict"] == "ship", result
    markdown = CORE.render_markdown(result)
    assert "Premises: product-source" in markdown and "Derivation:" in markdown


def test_registered_review_retains_the_original_claim_and_policy_after_edits(host):
    envelope = review(host)
    reviewed = envelope["payload"]["body"]["review"]["snapshot"]
    host.claims["claims"][0]["text"] = "A changed, unreviewed claim."
    save_claims(host)
    with USAGE.current_view(host.root, host.config) as view:
        retained = next(iter(view.state.strict_reviews.values()))["envelope"]["payload"]["body"]["review"]["snapshot"]
    assert retained == reviewed
    assert retained["claim"]["text"] != host.claims["claims"][0]["text"]
    assert retained["policy"] == host.strict_policy
    assert retained["source_revisions"][SOURCE_ID] == host.body["source_revision"]


def test_markdown_escaping_cannot_expand_past_the_delivery_bound(host):
    review(host)
    result = CORE.publication(host.root)
    record = result["questions"][0]
    template = record["claims"][0]
    record["claims"] = [{**copy.deepcopy(template), "id": f"claim-{index}", "text": "&" * 4096}
                        for index in range(64)]
    assert len(canonical(result)) < CORE.MAX_BYTES
    with pytest.raises(Exception, match="Strict evidence") as caught:
        CORE.render_markdown(result)
    assert caught.value.details["reason"] == "strict_output_bound_exceeded"
