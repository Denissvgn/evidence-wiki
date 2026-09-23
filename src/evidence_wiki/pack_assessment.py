"""Frozen synthetic cases and declared domain judgments outside pack distributions."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import yaml

from ._pack_io import canonical, capture_pack, read_file, yaml_document
from ._script_host import shared_assets_root
from .pack_authoring_contracts import ASSESSMENT, OBSERVATIONS, SUITE, checked, decode, digest, refuse
from .pack_authoring_store import load_draft, record
from .pack_discovery import owner
from .pack_qualification import qualify

SCENARIOS = {"adequate", "missing", "conflicting", "wrong_scope"}


def reference_data():
    raw = read_file(shared_assets_root(), "workspace-template/docs/pack-assessment-references.json")
    return json.loads(raw)


def references():
    value = reference_data()
    return {"sha256": digest(value), "schema_version": value["schema_version"],
        "strict_rubric_sha256": digest(json.loads(read_file(shared_assets_root(), "workspace-template/docs/agent-resources/strict-policy.json"))["rubric"]),
        "semantic_case_ids": [case["id"] for case in value["semantic"]["cases"]],
        "arithmetic_domains": list(value["arithmetic"]), "limits": value["limitations"],
        "reference_observation": "frozen independently specified reference data; not candidate-domain approval"}


def suite_gaps(suite, draft):
    requirements = {row["id"] for row in draft["requirements"]}
    ids = [case["id"] for case in suite["cases"]]
    if len(ids) != len(set(ids)):
        refuse("authoring_case_id_duplicate")
    for case in suite["cases"]:
        if len(case["requirement_ids"]) != len(set(case["requirement_ids"])) or not set(case["requirement_ids"]) <= requirements:
            refuse("authoring_case_requirement_unknown")
        if case["kind"] in {"policy", "computation"} and case["inputs"].get("as_of") is not None:
            owner("_evidence_authority").timestamp(case["inputs"]["as_of"])
        pending = [case["inputs"]]
        while pending:
            value = pending.pop()
            if type(value) is float:
                refuse("authoring_case_decimal_requires_lossless_encoding")
            if isinstance(value, dict):
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        if case["scenario"] == "adequate" and case["kind"] != "semantic":
            expected = case["expected"]
            if expected.get("status") not in {"observed", "passed"} or case["kind"] == "policy" and expected.get("outcome") != "pass":
                refuse("authoring_adequate_case_requires_positive_expectation")
        if case["kind"] == "semantic":
            expected = case["expected"]
            if (set(expected) != {"status", "verdicts"} or expected["status"] != "declared"
                    or not isinstance(expected["verdicts"], dict) or set(expected["verdicts"]) != set(owner("_strict_contract").CHECKS)
                    or any(value not in {"pass", "fail", "unknown"} for value in expected["verdicts"].values())):
                refuse("authoring_semantic_expected_rubric_invalid")
            reference = case["inputs"].get("reference_case_id")
            if reference is not None:
                selected = next((row for row in reference_data()["semantic"]["cases"] if row["id"] == reference), None)
                if selected is None:
                    refuse("authoring_reference_case_unknown")
                verdicts = {**dict.fromkeys(owner("_strict_contract").CHECKS, "pass"), **selected["verdicts"]}
                if expected["verdicts"] != verdicts:
                    refuse("authoring_reference_expectation_changed")
    exceptions = {(row["requirement_id"], row["scenario"]) for row in suite["exceptions"]}
    if len(exceptions) != len(suite["exceptions"]) or any(key not in requirements for key, _ in exceptions):
        refuse("authoring_case_exception_invalid")
    gaps = []
    for requirement in draft["requirements"]:
        scenarios = {case["scenario"] for case in suite["cases"] if requirement["id"] in case["requirement_ids"]}
        for scenario in sorted(SCENARIOS - scenarios):
            if (requirement["id"], scenario) not in exceptions:
                gaps.append({"reason": "required_case_missing", "requirement_id": requirement["id"], "scenario": scenario})
    return gaps


def freeze_cases(root, raw):
    root, _candidate, draft = load_draft(root)
    suite = decode(raw, SUITE)
    if suite["draft_id"] != digest(draft):
        refuse("authoring_case_draft_mismatch")
    gaps = suite_gaps(suite, draft)
    saved = record(root, "cases.json", suite)
    return {"schema_version": SUITE, "status": "frozen", "record": saved, "gaps": gaps,
        "semantic_adequacy": "not_certified", "expectations_mutable": False}


def _policy_case(case, config):
    primitives = owner("_policy_primitives")
    rules = primitives.pack_policy_rules(config)
    if case["target"] not in rules:
        return {"status": "unsupported", "reason": "policy_has_no_mechanical_rule"}
    data = case["inputs"]
    if set(data) != {"structured", "provenance", "question", "origin_host", "provider_ids", "as_of"}:
        refuse("authoring_policy_case_input_shape")
    if data["structured"] is not None and not isinstance(data["structured"], dict) or not isinstance(data["provenance"], dict):
        refuse("authoring_policy_case_input_shape")
    if data["question"] is not None and not isinstance(data["question"], dict):
        refuse("authoring_policy_case_input_shape")
    providers = data["provider_ids"]
    if not isinstance(providers, list) or len(providers) > 16 or any(not isinstance(v, str) for v in providers):
        refuse("authoring_policy_case_provider_shape")
    now = owner("_evidence_authority").timestamp(data["as_of"])
    context = primitives.RuleContext(source_id="case:" + case["id"], structured_view=data["structured"], structured_view_error=None,
        provenance=data["provenance"], question_frontmatter=data["question"], origin_host=data["origin_host"], provider_ids=tuple(providers),
        now=now, domain_matches=owner("_evidence_policies").domain_matches)
    rule = rules[case["target"]]
    result = primitives.evaluate_rule(rule, context)
    return {"status": "observed", "outcome": result.outcome,
        "human_review_required": rule.manual_review_required or result.requires_manual_review}


def _computation_case(case, config):
    if config.get("computation") is None:
        return {"status": "unsupported", "reason": "computation_absent"}
    data = case["inputs"]
    if set(data) != {"records", "as_of"} or not isinstance(data["records"], dict):
        refuse("authoring_computation_case_input_shape")
    if data["as_of"] is not None:
        owner("_evidence_authority").timestamp(data["as_of"])
    with tempfile.TemporaryDirectory(prefix="evidence-pack-arithmetic-") as temporary:
        root = Path(temporary)
        (root / "research.yml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8", newline="\n")
        raw = root / "raw/data/case.json"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(canonical(data["records"]))
        # This is an explicitly synthetic case, not a claimed JSON extraction
        # capability. Use the canonical renderers; evaluation validates the
        # resulting retained bytes and structured-view binding itself.
        normalizer = owner("normalize_sources")
        source = {"id": "data:case", "kind": "structured_data", "status": "normalized", "raw_paths": ["raw/data/case.json"]}
        output = root / "sources/normalized/data--case.md"
        output.parent.mkdir(parents=True)
        (root / "sources/manifest.jsonl").write_bytes(canonical(source) + b"\n")
        structured = normalizer.render_structured_view(data["records"])
        sidecar = output.with_suffix(".structured.json")
        sidecar.write_bytes(structured)
        rendered = normalizer.NormalizedSource(record=source, extraction_method="synthetic_case", title="Synthetic computation case", authors=[],
            abstract="Caller-supplied synthetic values; no external source authority.", outline=[], extracted_text=canonical(data["records"]).decode(),
            media=[], links=[], bibliography_files=[], included_paths=["raw/data/case.json"], warnings=[],
            adapter_name="authoring-synthetic-case", adapter_version="1")
        frontmatter = normalizer.frontmatter_for(rendered, "sources/manifest.jsonl", output, "2000-01-01",
            normalized_at="2000-01-01T00:00:00Z", structured_view={"path": "sources/normalized/" + sidecar.name,
                "content_hash": "sha256:" + hashlib.sha256(structured).hexdigest()})
        output.write_text(normalizer.render_markdown(rendered, frontmatter), encoding="utf-8", newline="\n")
        try:
            result = owner("_computation_runtime").evaluate(root, as_of=data["as_of"])
        except (Exception, SystemExit) as error:
            return {"status": "refused", "reason": owner("_computation_service").reason(error)}
    return {"status": result["status"],
        "execution_context": {"engine_id": result["engine_id"], "definition_id": result["definition_id"],
            "arithmetic": result["definition"]["arithmetic"], "clock_policy": result["definition"]["clock"],
            "observed_clock": {key: value for key, value in result["clock"].items() if key != "schedules"}},
        "graphs": {key: {name: row["value"] for name, row in graph["outputs"].items()} for key, graph in result["graphs"].items()},
        "invariants": [{key: row[key] for key in ("id", "passed", "complete", "checked", "failures")} for row in result["invariants"]],
        "schedules": [{key: row[key] for key in ("id", "state", "due_at", "next_at")} for row in result["clock"]["schedules"]]}


def reference_observations():
    results = []
    config = yaml_document(read_file(shared_assets_root(), "workspace-template/research.yml"))
    for name, reference in reference_data()["arithmetic"].items():
        case = {"id": name, "inputs": {"records": reference["records"], "as_of": reference["definition"]["clock"]["as_of"]}}
        actual = _computation_case(case, {**config, "computation": reference["definition"]})
        results.append({"id": name, "passed": actual.get("status") == "passed" and actual.get("graphs") == reference["expected_graphs"],
            "expected_graphs": reference["expected_graphs"], "actual": actual, "case_sha256": digest(reference)})
    return results


def assess(root, *, observations=None, persist=True):
    root, candidate, draft = load_draft(root)
    suite_raw = read_file(root, "records/cases.json")
    suite = decode(suite_raw, SUITE)
    if suite["draft_id"] != digest(draft):
        refuse("authoring_case_draft_mismatch")
    declared = decode(observations, OBSERVATIONS) if observations is not None else None
    if declared and declared["suite_sha256"] != digest(suite):
        refuse("authoring_observation_case_mismatch")
    observations_by_id = {row["case_id"]: row for row in declared["observations"]} if declared else {}
    semantic_ids = {case["id"] for case in suite["cases"] if case["kind"] == "semantic"}
    if declared and (len(observations_by_id) != len(declared["observations"]) or not set(observations_by_id) <= semantic_ids):
        refuse("authoring_observation_case_unknown")
    validation = qualify(candidate)
    snapshot = capture_pack(candidate)
    if snapshot.tree_sha256 != validation["identity"]["tree_sha256"]:
        refuse("authoring_candidate_changed", "ONBOARDING_PLAN_STALE")
    init = owner("init_research_workspace")
    config = init.deep_merge(yaml_document(read_file(shared_assets_root(), "workspace-template/research.yml")),
                             yaml_document(snapshot.files["research.overlay.yml"]))
    gaps = suite_gaps(suite, draft)
    gaps.extend({"reason": "unresolved_guidance", "detail": value} for value in draft["unresolved"])
    if not validation["ok"]:
        gaps.append({"reason": "canonical_validation_failed"})
    results = []
    for case in suite["cases"]:
        execution_context = None
        if case["kind"] == "semantic":
            observation = observations_by_id.get(case["id"])
            if observation is None:
                actual = {"status": "pending", "reason": "independent_semantic_observation_missing"}
            else:
                if set(observation["verdicts"]) != set(owner("_strict_contract").CHECKS):
                    refuse("authoring_semantic_rubric_incomplete")
                actual = {"status": "declared", "verdicts": observation["verdicts"]}
            basis = "caller_declared_not_authenticated"
        elif not validation["ok"]:
            actual, basis = {"status": "not_run", "reason": "canonical_validation_failed"}, "not_run"
        else:
            operation = _policy_case if case["kind"] == "policy" else _computation_case
            actual, basis = operation(case, config), "observed_on_supplied_synthetic_case"
            execution_context = actual.pop("execution_context", None)
        matches = actual == case["expected"]
        if actual["status"] in {"unsupported", "not_run"}:
            gaps.append({"reason": "case_capability_unavailable", "case_id": case["id"], "requirement_ids": case["requirement_ids"]})
        if not matches:
            gaps.append({"reason": "case_expectation_not_met", "case_id": case["id"], "requirement_ids": case["requirement_ids"]})
        results.append({"case_id": case["id"], "requirement_ids": case["requirement_ids"], "scenario": case["scenario"],
            "kind": case["kind"], "input_sha256": digest(case["inputs"]), "expected": case["expected"], "actual": actual,
            "expectation_matched": matches, "basis": basis, "execution_context": execution_context})
    changed = snapshot.tree_sha256 != draft["initial_tree_sha256"]
    changes = {**draft["classification"], "edited_after_scaffold": changed,
        "rationale": draft["rationale"], "requirements": draft["requirements"],
        "base": {key: draft["base"][key] for key in ("name", "version", "tree_sha256", "selection")} if draft["base"] else None,
        "meaning": "Formula names, thresholds, schedules and scope require independent domain judgment."}
    if changed:
        changes.update(kind="requirements_change", routine_repair=False, weakening="unassessed")
    reference_basis = references()
    reference_basis["arithmetic_observations"] = reference_observations()
    reference_basis["engine_id"] = owner("_selected_publication").producer_identity()
    if not all(row["passed"] for row in reference_basis["arithmetic_observations"]):
        gaps.append({"reason": "arithmetic_reference_case_failed"})
    value = {"schema_version": ASSESSMENT, "draft_id": digest(draft), "candidate": str(candidate),
        "identity": validation["identity"], "validation_sha256": digest(validation), "checker_sha256": validation["checker_sha256"],
        "suite": suite, "suite_sha256": digest(suite), "observations": declared, "reference_basis": reference_basis,
        "changes": changes, "cases": results, "gaps": gaps,
        "mechanical_cases_passed": validation["ok"] and all(row["expectation_matched"] and row["actual"]["status"] not in {"unsupported", "not_run"}
            for row in results if row["kind"] != "semantic") if any(row["kind"] != "semantic" for row in results) else None,
        "mechanical_case_count": sum(row["kind"] != "semantic" for row in results),
        "semantic_adequacy": "not_certified", "independent_review": "not_verified", "human_gates_removed": False,
        "limitations": [*suite["limitations"], "Synthetic cases do not establish domain correctness or source truth.",
            "Semantic observations and exceptions remain caller-declared; no LLM or independent reviewer is installed or authenticated.",
            "Arithmetic engine outcomes do not establish formula/threshold/schedule meaning; reference filing and score examples are synthetic."]}
    if capture_pack(candidate).tree_sha256 != snapshot.tree_sha256 or read_file(root, "records/cases.json") != suite_raw:
        refuse("authoring_assessment_inputs_changed", "ONBOARDING_PLAN_STALE")
    value = checked(value, ASSESSMENT)
    saved = record(root, "assessment-" + digest(value) + ".json", value) if persist else None
    return {"assessment": value, "record": saved}
