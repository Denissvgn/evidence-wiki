"""Impact explanations must agree with a fresh complete strict owner evaluation."""

import copy
from dataclasses import replace

import pytest
import yaml

from evidence_wiki import Workspace, impact_operations
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.pack_discovery import owner
from tests.test_strict_evidence import CORE, QUESTION, review, save_claims
from tests.test_strict_evidence import host as host


def context(host):
    return HostContext([host.root], {"workspace.open", "impact_operations.current"}, "impact-reader", [],
        authority_file=host.policy_path, state_directory=host.host)


def observe(host):
    with context(host).activate(), Workspace.open(host.root) as handle:
        return handle.research.impact()


def assert_full_owner(host, result):
    fresh = CORE.review_status(host.root)
    assert result["basis_id"] == fresh["basis_id"]
    assert [(row["claim_id"], row["accepted"], row["reasons"], row["review_reason"]) for row in result["claims"]] == [
        (row["claim_id"], row["accepted"], row["reasons"], row["review"]["reason"]) for row in fresh["claims"]]
    assert not result["selective_approval_reuse"] and not result["evidence_accepted"]
    assert result["effects_executed"] == 0


def test_current_review_remains_native_and_read_only(host):
    review(host)
    before = owner("_evidence_revision").capture_workspace(host.root)
    report = observe(host)
    assert_full_owner(host, report)
    assert not report["requires_review_claim_ids"]
    assert report["claims"][0]["known_dependencies"]["source_ids"] == [host.body["source_id"]]
    assert report["claims"][0]["retained_review_basis_changed"] is False
    assert dict(owner("_evidence_revision").capture_workspace(host.root).files) == dict(before.files)


@pytest.mark.parametrize("mutation", ["claim", "question", "question_text", "policy", "permission", "implementation", "workspace"])
def test_changed_global_dependencies_match_complete_strict_evaluation(host, monkeypatch, mutation):
    review(host)
    if mutation == "claim":
        host.claims["claims"][0]["limitations"].append("Additional limitation.")
        save_claims(host)
    elif mutation == "question":
        path = host.root / f"wiki/questions/{QUESTION}.md"
        path.write_text(path.read_text() + "\nAdditional question context.\n")
    elif mutation == "question_text":
        host.claims["questions"][0]["question"] += " Additional question?"
        save_claims(host)
        path = host.root / f"wiki/questions/{QUESTION}.md"
        front, body = path.read_text().split("---", 2)[1:]
        document = yaml.safe_load(front)
        document["question"] = host.claims["questions"][0]["question"]
        path.write_text("---\n" + yaml.safe_dump(document, sort_keys=False) + "---" + body)
    elif mutation == "policy":
        host.strict_policy["revision"] = "2"
        host.config["strict_evidence"] = host.strict_policy
        (host.root / "research.yml").write_text(yaml.safe_dump(host.config, sort_keys=False))
        host.policy["strict_workspaces"][host.binding] = host.strict_policy
        host.save_policy()
    elif mutation == "permission":
        host.policy["revoked_keys"] = ["evaluator-key"]
        host.save_policy()
    elif mutation == "implementation":
        for strict in (CORE, owner("_strict_evidence")):
            monkeypatch.setattr(strict.sibling("_selected_publication"), "producer_identity", lambda: "sha256:" + "e" * 64)
    else:
        (host.root / "docs/additional.txt").write_text("Unattributed workspace evidence.\n")
    result = observe(host)
    assert_full_owner(host, result)
    assert result["requires_review_claim_ids"] == ["product-source"]
    assert not result["claims"][0]["accepted"]
    assert result["conservatively_invalidated_claim_ids"] == ["product-source"]
    if mutation == "permission":
        assert result["claims"][0]["dependency_changes"] == ["permission"]


def test_unrelated_claim_change_invalidates_all_old_reviews(host):
    second = copy.deepcopy(host.claims["claims"][0])
    second["id"] = "other-conclusion"
    host.claims["claims"].append(second)
    save_claims(host)
    review(host)
    review(host, claim_id=second["id"])
    host.claims["claims"][1]["limitations"].append("New limitation.")
    save_claims(host)
    report = observe(host)
    assert_full_owner(host, report)
    assert report["conservatively_invalidated_claim_ids"] == ["product-source", "other-conclusion"]
    assert report["claims"][0]["dependency_changes"] == []
    assert report["claims"][1]["dependency_changes"] == ["claim"]


def test_source_denial_cannot_produce_eligible_impact(host):
    review(host)
    host.revoke(CORE.sibling("_evidence_usage"), host.body)
    with pytest.raises(EvidenceWikiError):
        observe(host)


def test_missing_reviews_are_unknown_without_claiming_change(host):
    report = observe(host)
    assert_full_owner(host, report)
    assert report["claims"][0]["retained_review_basis_changed"] is None
    assert not report["conservatively_invalidated_claim_ids"]
    assert report["requires_review_claim_ids"] == ["product-source"]


def test_transitive_dependency_changes_do_not_grant_selective_reuse(host):
    base = copy.deepcopy(host.claims["claims"][0])
    second = copy.deepcopy(base)
    second.update(id="derived", qualification="inference", premises=[base["id"]], derivation="Uses the retained premise.")
    host.claims["claims"].append(second)
    save_claims(host)
    report = observe(host)
    assert report["claims"][1]["known_dependencies"]["premise_claim_ids"] == [base["id"]]
    assert "strict_premise_not_accepted" in report["claims"][1]["reasons"]


@pytest.mark.parametrize("mutation", ["workspace", "authority", "implementation"])
def test_changes_during_projection_refuse_closed_observation(host, monkeypatch, mutation):
    original = impact_operations._project
    def changed(*args):
        result = original(*args)
        if mutation == "workspace":
            path = host.root / f"wiki/questions/{QUESTION}.md"
            path.write_text(path.read_text() + "\nChanged after evaluation.\n")
        elif mutation == "authority":
            host.policy["revoked_keys"] = ["evaluator-key"]
            host.save_policy()
        else:
            monkeypatch.setattr(owner("_strict_evidence").sibling("_selected_publication"), "producer_identity", lambda: "sha256:" + "f" * 64)
        return result
    monkeypatch.setattr(impact_operations, "_project", changed)
    with pytest.raises(EvidenceWikiError):
        observe(host)


def test_scope_grants_and_closed_handle_remain_required(host):
    selected = replace(context(host), grants=frozenset({"workspace.open"}))
    with selected.activate(), Workspace.open(host.root) as handle, pytest.raises(EvidenceWikiError):
        handle.research.impact()
    with context(host).activate(), Workspace.open(host.root) as handle:
        pass
    with pytest.raises(EvidenceWikiError):
        handle.research.impact()


def test_retained_source_change_is_precise_but_all_review_bases_change(host):
    import hashlib

    review(host)
    raw = host.normalized_path.read_bytes() + b"\nAdditional retained observation.\n"
    body, files = host.temporal_source(normalized=raw, supersedes=host.body["source_revision"])
    host.transact(CORE.sibling("_evidence_usage"), "deposit", body, files)
    host.normalized_path.write_bytes(raw)
    host.claims["claims"][0]["evidence"][0]["record_sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    save_claims(host)
    report = observe(host)
    assert_full_owner(host, report)
    assert report["claims"][0]["known_changed_source_ids"] == [host.body["source_id"]]
    assert "source" in report["claims"][0]["dependency_changes"]
    assert report["conservatively_invalidated_claim_ids"] == ["product-source"]


def test_selected_comparison_unknowns_are_preserved_in_native_join(host, tmp_path):
    import hashlib
    import json

    from evidence_wiki.mutation_contracts import canonical
    from tests.test_research_planning import request

    declared = request(tmp_path)
    declared["request"]["payload"]["authority"]["source_scope"] = [str(host.root / "raw")]
    question = host.claims["questions"][0]
    declared["request"]["payload"]["questions"] = [{"id": QUESTION, "text": question["question"]}]
    plan = declared["decisions"]["question_plans"][0]
    plan["question_id"] = QUESTION
    coverage_path = host.root / f"sources/coverage/{QUESTION}.yml"
    coverage = yaml.safe_load(coverage_path.read_bytes())
    coverage["coverage_profile"] = owner("coverage_manifest").DEFAULT_COVERAGE_PROFILE
    coverage_path.write_text(yaml.safe_dump(coverage, sort_keys=False))
    plan["facets"] = [{name: value for name, value in facet.items() if name not in {
        "accepted_source_ids", "blocking_request_ids", "facet_verdict", "notes"}} for facet in coverage["required_facets"]]
    plan["criteria"][0]["facet_id"] = plan["facets"][0]["facet_id"]
    question_path = host.root / f"wiki/questions/{QUESTION}.md"
    front, body = question_path.read_text().split("---", 2)[1:]
    document = yaml.safe_load(front)
    document["metadata"] = {"research_id": QUESTION, "original_text": question["question"],
        "original_ids_json": json.dumps([QUESTION])}
    question_path.write_text("---\n" + yaml.safe_dump(document, sort_keys=False) + "---" + body)
    raw = canonical({**declared, "schema_version": "evidence-research-requirements/v1"}) + b"\n"
    path = "docs/research-requirements.json"
    (host.root / path).write_bytes(raw)
    host.strict_policy["instructions"][path] = "sha256:" + hashlib.sha256(raw).hexdigest()
    host.config["strict_evidence"] = host.strict_policy
    (host.root / "research.yml").write_text(yaml.safe_dump(host.config, sort_keys=False))
    host.policy["strict_workspaces"][host.binding] = host.strict_policy
    host.save_policy()
    review(host)
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    selected = replace(context(host), mutation_control_directory=control,
        grants=context(host).grants | {"change_operations.current"})
    pairs = [{"before_source_id": host.body["source_id"], "before_revision": host.body["source_revision"],
        "after_source_id": host.body["source_id"]}]
    with selected.activate(), Workspace.open(host.root) as handle:
        report = handle.research.impact(pairs=pairs)
    assert_full_owner(host, report)
    assert report["changes"]["coverage"]["selected_pairs"] == 1
    assert report["claims"][0]["unavailable_selected_source_ids"] == [host.body["source_id"]]
    assert not report["claims"][0]["known_changed_source_ids"]
    assert report["claims"][0]["accepted"]


def test_output_overflow_is_refused_not_truncated(host, monkeypatch):
    monkeypatch.setattr(impact_operations, "MAX_BYTES", 1)
    with pytest.raises(EvidenceWikiError) as caught:
        observe(host)
    assert caught.value.details["reason"] == "output_bound"


def test_legacy_assessment_refresh_is_never_used_to_accept(host, monkeypatch):
    monkeypatch.setattr(owner("_assessment_refresh"), "plan",
        lambda *_args, **_kwargs: pytest.fail("Impact ran legacy assessment refresh."))
    report = observe(host)
    assert not report["claims"][0]["accepted"]
    assert report["legacy_assessments"].startswith("not_strict_approval")


def test_computation_change_invalidates_all_reviews_under_native_scientific_owner(host):
    from evidence_wiki.computation import evaluate
    from tests.test_computation_integration import constant_definition

    host.config["computation"] = constant_definition()
    config_path = host.root / "research.yml"
    config_path.write_text(yaml.safe_dump(host.config, sort_keys=False))
    computed = evaluate(host.root)
    host.claims["schema_version"] = "evidence-strict-claims/v2"
    host.claims["claims"][0]["calculations"] = []
    inferred = copy.deepcopy(host.claims["claims"][0])
    inferred.update(id="derived-calculation", qualification="inference", premises=["product-source"],
        derivation="Multiply the declared constant by two.")
    inferred["calculations"] = [{
        "result_id": computed["result_id"], "pointer": "/graphs/worksheet/outputs/total", "expected": "6",
        "form": "value", "unit": "units", "rounded": False}]
    host.claims["claims"].append(inferred)
    save_claims(host)
    review(host)
    review(host, claim_id="derived-calculation")
    assert observe(host)["claims"][1]["accepted"]
    host.config["computation"]["graphs"]["worksheet"]["constants"]["amount"]["value"] = "4"
    config_path.write_text(yaml.safe_dump(host.config, sort_keys=False))
    report = observe(host)
    assert_full_owner(host, report)
    assert report["conservatively_invalidated_claim_ids"] == ["product-source", "derived-calculation"]
    assert "strict_computation_missing_or_stale" in report["claims"][1]["reasons"]
    assert report["claims"][1]["known_dependencies"]["calculation_refs"]


@pytest.mark.parametrize("pairs,revision", [([], None), (None, "unknown"),
    ([{"before_source_id": "x", "before_revision": "bad", "after_source_id": "x"}], None)])
def test_invalid_selectors_never_access_host(pairs, revision, monkeypatch):
    monkeypatch.setattr(impact_operations, "current_context", lambda: pytest.fail("Invalid selector accessed host."))
    with pytest.raises(EvidenceWikiError):
        impact_operations.current(None, pairs=pairs, before_requirements_revision_id=revision)


@pytest.mark.parametrize("same_identity", [True, False])
def test_comparison_candidates_cannot_infer_changed_source_identity(host, same_identity):
    review(host)
    strict = owner("_strict_evidence")
    source_id = host.body["source_id"]
    after_source_id = source_id if same_identity else "different-source"
    selected_changes = {"affected_source_ids": [source_id, after_source_id], "sources": [{
        "before_source_id": source_id, "after_source_id": after_source_id,
        "changed_dimensions": ["raw_content"], "dimensions": {"raw_content": {"state": "changed"}}}]}
    with context(host).activate(), owner("_evidence_usage").current_view(host.root, host.config) as live:
        result = strict.evaluate(host.root, host.config, host.strict_policy, view=live)
        sources = strict.sibling("_publication_usage").qualify_capture(
            owner("_evidence_revision").capture_workspace(host.root).files, host.config, live,
            purpose="qa-export", consumer="evidence-wiki")["selected"]
        report = impact_operations._project({**result, "effective_policy": host.strict_policy,
            "source_revisions": sources}, live, selected_changes)
    row = report["claims"][0]
    assert row["comparison_affected_source_ids"] == [source_id]
    assert row["known_changed_source_ids"] == ([source_id] if same_identity else [])
    assert row["accepted"] and not row["conservative_invalidation"]


@pytest.mark.parametrize("revoked", [False, True])
def test_retained_comparison_rights_are_reacquired_for_the_native_join(host, revoked):
    selected = {"sources": [{"before_revision": host.body["source_revision"],
        "dimensions": {"raw_content": {"before": {"availability": "observed"}}}}]}
    if revoked:
        host.revoke(CORE.sibling("_evidence_usage"), host.body)
    with context(host).activate(), owner("_evidence_usage").current_view(host.root, host.config) as live:
        if revoked:
            with pytest.raises(EvidenceWikiError) as caught:
                impact_operations._recheck_comparisons(live, selected)
            assert caught.value.details["reason"] == "comparison_usage_changed"
        else:
            impact_operations._recheck_comparisons(live, selected)
            assert any(key[0] == host.body["source_revision"] for key in live.checked)
            live.revalidate(host.root, host.config)
