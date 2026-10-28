#!/usr/bin/env python3
"""Policy-owned resolution and claim-only release over existing evidence owners."""

from __future__ import annotations

import hashlib
import json
import sys
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

import yaml
from _evidence_authority import load_trust, read_host_policy, timestamp, verify_attestation
from _evidence_revision import capture_workspace, content_id, observation, read_observed_file
from _host_context import authority_path
from _publication_context import authorized_capture, strict_host, strict_internal
from _record_artifacts import json_document
from _script_errors import ScriptRefusal
from _strict_contract import (
    CLAIMS_SCHEMA_V2,
    MAX_BYTES,
    POLICY_SCHEMA_V2,
    RESULT_SCHEMA,
    RESULT_SCHEMA_V2,
    RESULT_SCHEMA_V3,
    REVIEW_SCHEMA,
    REVIEW_SCHEMA_V2,
    REVIEW_SCHEMA_V3,
    SELECTIVE_MODE,
    artifact_id,
    claims_document,
    policy_document,
    review_document,
)
from _workspace_module_loader import load_workspace_module
from _yaml_safe import safe_load_current as safe_load

SCRIPT_DIR = Path(__file__).resolve().parent
_CORE_MODULE = sys.modules[__name__]
_SIBLINGS = {}


def sibling(name):
    if name not in _SIBLINGS:
        _SIBLINGS[name] = load_workspace_module(SCRIPT_DIR, name)
    return _SIBLINGS[name]


def refusal(reason):
    code = "STRICT_HOST_BUSY" if reason == "host_state_lock_unavailable" else "STRICT_EVIDENCE_REFUSED"
    return ScriptRefusal(code, "Strict evidence requirements were not satisfied.",
                        remediation="Inspect the reason, restore the required evidence or trusted review, and retry.",
                        details={"reason": reason}, recoverable=reason == "host_state_lock_unavailable",
                        exit_code=6 if reason == "host_state_lock_unavailable" else 2)


def typed(operation):
    """Keep parser/file failures content-free across every public entry point."""
    @wraps(operation)
    def call(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (ValueError, OSError, KeyError, TypeError, AttributeError, RecursionError, yaml.YAMLError) as exc:
            reason = str(exc) if type(exc).__name__ == "EvidenceInvalid" else "strict_input_invalid_or_unreadable"
            raise refusal(reason) from None
    return call


def require(condition, reason):
    if not condition:
        raise refusal(reason)


def read_json(path):
    path = Path(path).absolute()
    info = path.lstat()
    require(info.st_size <= MAX_BYTES, "strict_document_bound_exceeded")
    raw = read_observed_file(path.parent.resolve(), path.name, observation(info))
    require(len(raw) <= MAX_BYTES, "strict_document_bound_exceeded")
    return json_document(raw)


def configuration(root):
    capture = capture_workspace(root)
    data = capture.files.get("research.yml", b"")
    require(len(data) <= MAX_BYTES, "strict_configuration_bound_exceeded")
    config = safe_load(data)
    require(isinstance(config, dict), "strict_configuration_invalid")
    sibling("_selected_publication").validate_config_paths(config)
    return config


@typed
def resolve_policy(root, config):
    """External policy wins even if a caller removes workspace requirement flags."""
    require(isinstance(config, dict), "strict_configuration_invalid")
    local = config.get("strict_evidence")
    external = None
    if authority_path(root):
        raw = read_host_policy(root)
        document = json_document(raw)
        scopes = document.get("strict_workspaces", {})
        require(isinstance(scopes, dict) and len(scopes) <= 128, "strict_host_scope_invalid")
        external = scopes.get(sibling("_evidence_usage").workspace_binding(root))
        if external is not None:
            trust_config = {"evidence_trust": {key: document[key] for key in ("policy_id", "policy_revision")}}
            load_trust(root, trust_config, datetime.now(timezone.utc))
            require(config.get("evidence_trust") == trust_config["evidence_trust"], "strict_trust_selection_changed")
    if external is None and local is None:
        return None
    policy = policy_document(external if external is not None else local)
    if external is not None:
        require(local == policy, "strict_workspace_policy_changed")
    return policy


def configured(root, config):
    if not isinstance(config, dict):
        return False
    if config.get("strict_evidence") is None and not authority_path(root):
        return False
    sibling("_selected_publication").validate_config_paths(config)
    key = (str(Path(root).resolve()), content_id("strict-config", config))
    return key not in strict_internal.get() and isinstance(config, dict) and resolve_policy(root, config) is not None


@contextmanager
def internal_publication(root, config):
    key = (str(Path(root).resolve()), content_id("strict-config", config))
    token = strict_internal.set(strict_internal.get() | {key})
    try:
        yield
    finally:
        strict_internal.reset(token)


@contextmanager
def host_delivery(root):
    """Trusted host code enters only after its concrete worker boundary succeeds."""
    token = strict_host.set(str(Path(root).resolve()))
    try:
        yield
    finally:
        strict_host.reset(token)


def selection_inputs(root, config, policy, *, view=None, computation=None):
    capture = capture_workspace(root)
    if "computation" in config:
        sibling("_computation_contract").validate_yaml(capture.files["research.yml"].decode("utf-8"))
    path = policy["claims_path"]
    require(path in capture.files and len(capture.files[path]) <= MAX_BYTES, "strict_claims_missing_or_large")
    claims = claims_document(json_document(capture.files[path]))
    require(config.get("computation") is None or claims["schema_version"] == CLAIMS_SCHEMA_V2, "strict_computation_claim_schema_required")
    if config.get("computation") is not None and computation is None:
        computation = sibling("_computation_service").required(root, config, view=view)
    for relative, expected in policy["instructions"].items():
        require(relative in capture.files and sibling("_requirements_bridge").instruction_matches(root, relative, capture.files[relative], expected),
                "strict_instructions_changed")
    # Bind all retained bytes except lifecycle-only ledgers and question pages.
    # The renderer uses reviewed question text, not editable prose from those pages.
    question_dir = sibling("question_status").questions_directory(root, config).relative_to(root).as_posix() + "/"
    evidence = {name: hashlib.sha256(data).hexdigest() for name, data in capture.files.items()
                if not name.startswith(("runs/", ".locks/", question_dir + ".locks/")) and name not in {"log.md", "index.md"}
                and not (name.startswith(question_dir) and name.endswith(".md"))}
    originals = {}
    for name, data in capture.files.items():
        if name.startswith(question_dir) and name.endswith(".md"):
            fm, body = sibling("verify_quotes").split_page(data.decode("utf-8"))
            originals[Path(name).stem] = {"question": fm.get("question") or fm.get("summary"),
                                        "metadata": fm.get("metadata"), "body": body}
    material = {"files": evidence, "claims": claims, "policy": policy, "questions": originals,
                "producer": sibling("_selected_publication").producer_identity()}
    if computation is not None:
        material["computation_id"] = computation["result_id"]
    identity = content_id("evidence-strict-basis/v1", material)
    return capture, claims, identity


def grounding_entry(claim, evidence):
    result = {"claim": claim["text"], "source_id": evidence["source_id"]}
    if evidence["anchor"] is not None:
        result["anchor"] = evidence["anchor"]
    else:
        result["quote"] = evidence["quote"]
        if evidence["location_hint"] is not None:
            result["location_hint"] = evidence["location_hint"]
    return result


def review_result(root, config, policy, basis, claim, view, now, observation):
    if view is None:
        return {"passed": False, "reason": "strict_authenticated_review_unavailable"}
    for record in reversed(list(view.state.strict_reviews.values())):
        envelope = record["envelope"]
        payload = envelope["payload"]["body"]["review"]
        if payload["basis_id"] != basis or payload["claim_id"] != claim["id"]:
            continue
        if record["authority"]["content_hash"] != view.trust["content_hash"]:
            return {"passed": False, "reason": "strict_review_authority_changed"}
        if payload["observation"] != observation:
            return {"passed": False, "reason": "strict_review_observation_changed"}
        role = "human-review" if policy["human_review"] else "evaluator"
        auth = verify_attestation(envelope, role, view.trust, now)
        generator = verify_attestation(payload["generator"], "generator", view.trust, now)
        generator_payload = {"schema_version": "evidence-strict-authorship/v1", "basis_id": basis, "claim_id": claim["id"]}
        if not auth.get("authenticated") or not generator.get("authenticated"):
            return {"passed": False, "reason": "strict_review_authentication_failed"}
        if generator.get("controller") == auth.get("controller") or payload["generator"]["payload"] != generator_payload:
            return {"passed": False, "reason": "strict_review_not_independent"}
        age = (now - timestamp(payload["reviewed_at"])).total_seconds()
        if not 0 <= age <= policy["max_review_age_seconds"] or timestamp(payload["reviewed_at"]) > timestamp(auth["issued_at"]):
            return {"passed": False, "reason": "strict_review_expired"}
        passed = all(value == "pass" for value in payload["verdicts"].values())
        return {"passed": passed, "reason": "strict_review_passed" if passed else "strict_review_not_passed",
                "review_id": artifact_id(payload), "principal": auth["principal"],
                "independence": "host_controller_assertion", "human_review": policy["human_review"]}
    return {"passed": False, "reason": "strict_review_missing_or_stale"}


@typed
def evaluate(root, config, policy, *, view=None):
    """Recompute retained evidence and checks; never infer review from source prose."""
    if policy["schema_version"] == POLICY_SCHEMA_V2:
        return evaluate_dependencies(root, config, policy, view=view)
    now = datetime.now(timezone.utc) if view is None else view.now
    computation = sibling("_computation_service").required(root, config, view=view) if config.get("computation") is not None else None
    capture, manifest, basis = selection_inputs(root, config, policy, view=view, computation=computation)
    if view is None:
        sibling("_usage_gate").require_unrestricted_legacy(root, config)
    else:
        inputs = sibling("_publication_usage").qualify_capture(capture.files, config, view, purpose="qa-export", consumer="evidence-wiki")
        if any(view.state.revisions[revision]["descriptor"].get("temporal") for revision in inputs["ancestors"]):
            sibling("_assessment_engine").current_history(view, inputs)
            temporal = sibling("_temporal_contract")
            for revision in inputs["ancestors"]:
                times = temporal.source_times(view.state.revisions[revision])
                require(times["claims"]["expires_at"] is None or times["expires"] is not None, "strict_source_expiry_unknown")
                require(times["expires"] is None or now < times["expires"], "strict_source_expired")
                if times["effective"] is not None:
                    start, end = times["effective"]
                    require(start is not None and start <= now and (end is None or now < end), "strict_source_outside_effective_interval")
    qstatus = sibling("question_status")
    known = {item["slug"] for item in qstatus.collect_questions(qstatus.questions_directory(root, config))}
    require({q["slug"] for q in manifest["questions"]} == known, "strict_question_accounting_incomplete")
    rows, accepted = [], set()
    with capture.materialize() as captured, (
        authorized_capture(captured, config, view) if view is not None else nullcontext()
    ):
        verifier = sibling("verify_quotes")
        coverage = {}
        for question in manifest["questions"]:
            actual = qstatus.load_frontmatter(qstatus.questions_directory(captured, config) / (question["slug"] + ".md"))
            require((actual.get("question") or actual.get("summary")) == question["question"], "strict_question_text_changed")
            summary = sibling("coverage_manifest").coverage_summary_for_question(captured, config, question["slug"], {"coverage_required": True})
            human, _policies = sibling("question_resolve").requires_human_review(summary)
            coverage[question["slug"]] = (summary.get("coverage_verdict") == "pass", human)
        for claim in manifest["claims"]:
            reasons = []
            covered, human = coverage[claim["question_slug"]]
            if not covered:
                reasons.append("strict_coverage_required")
            if claim["qualification"] in {"contested", "insufficient_evidence"}:
                reasons.append("strict_claim_unresolved")
            if not claim["evidence"]:
                reasons.append("strict_evidence_missing")
            for evidence in claim["evidence"]:
                path, _label = verifier.normalized_record_path(captured, config, evidence["source_id"])
                relative = path.relative_to(captured).as_posix()
                if relative not in capture.files or "sha256:" + hashlib.sha256(capture.files[relative]).hexdigest() != evidence["record_sha256"]:
                    reasons.append("strict_source_revision_changed")
                elif path.is_file():
                    metadata, _body = verifier.normalized_record_content(path)
                    provenance = metadata.get("provenance", {})
                    observed = provenance.get("retrieved_at") if isinstance(provenance, dict) else None
                    if observed is None or timestamp(observed) != timestamp(evidence["observed_at"]):
                        reasons.append("strict_source_time_unconfirmed")
                age = (now - timestamp(evidence["observed_at"])).total_seconds()
                if not 0 <= age <= policy["max_source_age_seconds"]:
                    reasons.append("strict_source_age_invalid")
                if evidence["capture"] not in {"primary", "excerpt"}:
                    reasons.append("strict_capture_insufficient")
            entries = [grounding_entry(claim, evidence) for evidence in claim["evidence"]]
            try:
                parsed = verifier.grounding_entries({"grounding": entries}, claim["question_slug"])
                checks = [verifier.verify_entry(captured, config, entry) for entry in parsed]
                if not checks or any(check["result"] != "verified" for check in checks):
                    reasons.append("strict_grounding_failed")
            except (ValueError, OSError, ScriptRefusal):
                reasons.append("strict_grounding_failed")
            if not set(claim["premises"]) <= accepted:
                reasons.append("strict_premise_not_accepted")
            for calculation in claim.get("calculations", []):
                if computation is None or calculation["result_id"] != computation["result_id"]:
                    reasons.append("strict_computation_missing_or_stale")
                    continue
                resolved = sibling("_structured_view").resolve_pointer(computation, calculation["pointer"])
                value = resolved.value if resolved.ok else None
                if (not isinstance(value, dict) or value.get("type") != "decimal"
                        or value.get(calculation["form"]) != calculation["expected"]
                        or value.get("unit") != calculation["unit"] or value.get("rounded") != calculation["rounded"]):
                    reasons.append("strict_computation_value_mismatch")
                    continue
                sources = {computation["lineage"][identifier]["source_id"] for identifier in value["lineage"]
                           if "source_id" in computation["lineage"][identifier]}
                if not sources <= {item["source_id"] for item in claim["evidence"]}:
                    reasons.append("strict_computation_source_missing")
            observation = {"basis_id": basis, "claim_id": claim["id"],
                           "producer_id": sibling("_selected_publication").producer_identity(), "reasons": sorted(set(reasons))}
            effective = {**policy, "human_review": policy["human_review"] or human}
            review = review_result(root, config, effective, basis, claim, view, now, observation)
            if not review["passed"]:
                reasons.append(review["reason"])
            if not reasons:
                accepted.add(claim["id"])
            rows.append({"claim": claim, "accepted": not reasons, "reasons": sorted(set(reasons)), "review": review,
                         "required_review_role": "human-review" if effective["human_review"] else "evaluator", "observation": observation})
    require(selection_inputs(root, config, policy, view=view)[2] == basis, "strict_inputs_changed")
    result = {"schema_version": RESULT_SCHEMA_V2 if manifest["schema_version"] == CLAIMS_SCHEMA_V2 else RESULT_SCHEMA,
            "basis_id": basis, "policy_id": artifact_id(policy),
            "evaluated_at": now.isoformat(), "producer_id": sibling("_selected_publication").producer_identity(),
            "assurance": "host_enforced" if strict_host.get() == str(root) else "artifact_checked",
            "questions": manifest["questions"], "claims": rows,
            "limits": ["Source correctness is not proved.", "Semantic judgments remain fallible.",
                       "Only the controlled artifact is covered; external caller prose is outside this result."]}
    return bounded_result(result)


def bounded_result(result):
    contract = sibling("_strict_contract")
    if result["schema_version"] in {RESULT_SCHEMA_V3, "evidence-strict-publication/v3"}:
        contract.dependency_contract().validate(result["schema_version"], result)
    else:
        contract.validate_shape(result, contract.schema_documents()[result["schema_version"]])
    require(len(sibling("_evidence_revision").canonical_bytes(result)) <= MAX_BYTES, "strict_output_bound_exceeded")
    return result


def enforce_resolution(root, config, args, frontmatter):
    policy = resolve_policy(root, config)
    if policy is None:
        return
    require(not args.allow_unclaimed and not getattr(args, "allow_uncited", False), "strict_permissive_flag_refused")
    args.require_coverage = True
    args.require_grounding = True
    require(getattr(args, "coverage_manifest", None) is None, "strict_coverage_override_refused")
    # Mechanical resolution stays with the canonical resolver. Final eligibility
    # is separately re-evaluated from the complete claim inventory and receipts.
    require(policy["assurance"] != "host_enforced" or strict_host.get() == str(root), "strict_host_required")
    usage = sibling("_evidence_usage")
    manager = usage.current_view(root, config) if usage.configured(config) else nullcontext(None)
    with manager as view:
        report = evaluate(root, config, policy, view=view)
        rows = [row for row in report["claims"] if row["claim"]["question_slug"] == args.slug.strip()]
        require(bool(rows) and all(row["accepted"] for row in rows), "strict_claim_review_required")
        require(set(args.source_id or []) == {e["source_id"] for row in rows for e in row["claim"]["evidence"]}, "strict_source_selection_mismatch")


@typed
def publication(root, slugs=None, *, view=None, expected_revision=None):
    root = Path(root).resolve()
    config = configuration(root)
    policy = resolve_policy(root, config)
    require(policy is not None, "strict_policy_missing")
    require(policy["assurance"] != "host_enforced" or strict_host.get() == str(root), "strict_host_required")
    usage = sibling("_evidence_usage")
    manager = nullcontext(view) if view is not None else usage.current_view(root, config) if usage.configured(config) else nullcontext(None)
    with manager as current:
        result = _publication_evaluation(root, config, policy, current)
        wanted = [q["slug"] for q in result["questions"]] if slugs is None else list(sibling("_selected_publication").normalize_selection(slugs))
        require(set(wanted) <= {q["slug"] for q in result["questions"]}, "strict_question_unknown")
        before = capture_workspace(root).revision_id
        require(expected_revision is None or before == expected_revision, "strict_expected_revision_changed")
        legacy_results = {}
        with internal_publication(root, config):
            for slug in wanted:
                legacy_results[slug] = sibling("_selected_publication").run_selected_publication(root, [slug], expected_revision=before,
                    _usage_view=current, _purpose="qa-export" if current else None, _consumer="evidence-wiki" if current else None,
                    _expected_config=config if current else None)
        # Rebuild every user-facing answer field exclusively from accepted records.
        records = []
        for question in result["questions"]:
            if question["slug"] not in wanted:
                continue
            rows = [r for r in result["claims"] if r["claim"]["question_slug"] == question["slug"]]
            eligible = legacy_results[question["slug"]]["verdict"] == "ship" and bool(rows) and all(r["accepted"] for r in rows)
            records.append({**question, "status": "answered" if eligible else "blocked", "accepted": eligible,
                "claims": [{**r["claim"], **({key: r[key] for key in
                    ("claim_basis_id", "observed_workspace_basis_id", "review_basis_mode")}
                    if result["schema_version"] == RESULT_SCHEMA_V3 else {}),
                    "verification": {"observation": r["observation"], "review": r["review"]}}
                           for r in rows if r["accepted"]] if eligible else [],
                "unresolved_claims": [{"id": r["claim"]["id"], "qualification": r["claim"]["qualification"], "reasons": r["reasons"]}
                                      for r in rows if not r["accepted"]],
                "gaps": sorted({reason for r in rows for reason in r["reasons"]}
                    | {"publication_" + category for category, reasons in legacy_results[question["slug"]]["readiness"]["reasons"].items() if reasons and not eligible}
                    | (set() if eligible else {"strict_release_not_eligible"}))})
        if current is not None:
            current.revalidate(root, config)
            released_ids = {claim["id"] for record in records for claim in record["claims"]}
            if released_ids:
                closing = _publication_evaluation(root, config, policy, current)
                require(released_ids <= {row["claim"]["id"] for row in closing["claims"] if row["accepted"]},
                        "strict_review_changed_during_release")
        require(capture_workspace(root).revision_id == before and selection_inputs(root, config, policy, view=current)[2] == result["basis_id"], "strict_inputs_changed")
        require(resolve_policy(root, config) == policy, "strict_policy_changed")
        originals = []
        by_slug = {row["slug"]: row for row in records}
        for original in sorted({q["original_id"] for q in result["questions"]}):
            derived = [q["slug"] for q in result["questions"] if q["original_id"] == original]
            originals.append({"original_id": original, "question_slugs": derived,
                "accepted": all(slug in by_slug and by_slug[slug]["accepted"] for slug in derived),
                "unselected_question_slugs": [slug for slug in derived if slug not in by_slug]})
        additions = ({key: result[key] for key in ("review_producer_id", "review_basis_mode", "claim_schema_version")}
                     if result["schema_version"] == RESULT_SCHEMA_V3 else {})
        return bounded_result({"schema_version": ("evidence-strict-publication/v3" if result["schema_version"] == RESULT_SCHEMA_V3
            else "evidence-strict-publication/v2" if result["schema_version"] == RESULT_SCHEMA_V2 else "evidence-strict-publication/v1"),
            **additions,
            "basis_id": result["basis_id"],
            "policy_id": result["policy_id"], "producer_id": result["producer_id"], "evaluated_at": result["evaluated_at"],
            "assurance": result["assurance"], "verdict": "ship" if all(r["accepted"] for r in records) else "blocked_on_sources",
            "questions": records, "limitations": result["limits"], "original_outcomes": originals,
            "selection_complete": len(wanted) == len(result["questions"])})


def review_snapshot(root, config, policy, view, claim_id, expected_basis):
    computation = sibling("_computation_service").required(root, config, view=view) if config.get("computation") is not None else None
    capture, manifest, basis = selection_inputs(root, config, policy, view=view, computation=computation)
    require(basis == expected_basis, "strict_review_basis_changed")
    if policy["schema_version"] == POLICY_SCHEMA_V2:
        return dependency_current(root, config, policy, view, capture, manifest, claim_id)["snapshot"]
    claims = {claim["id"]: claim for claim in manifest["claims"]}
    require(claim_id in claims, "strict_claim_unknown")
    claim = claims[claim_id]
    question = next(q for q in manifest["questions"] if q["slug"] == claim["question_slug"])
    path = sibling("question_status").questions_directory(root, config) / (question["slug"] + ".md")
    metadata, _body = sibling("verify_quotes").split_page(capture.files[path.relative_to(root).as_posix()].decode())
    inputs = sibling("_publication_usage").qualify_capture(capture.files, config, view, purpose="qa-export", consumer="evidence-wiki")
    snapshot = {"policy": policy, "claim": claim, "question": question, "metadata": metadata.get("metadata") or {},
                "premises": [claims[name] for name in claim["premises"]], "source_revisions": inputs["selected"]}
    if manifest["schema_version"] == CLAIMS_SCHEMA_V2:
        snapshot["computation"] = computation
    return snapshot


@typed
def prepare_review_material(root, claim_id, *, expected_basis_id, expected_native_snapshot_sha256):
    """Capture the complete prerequisite graph under the current native basis."""
    root = Path(root).resolve()
    config = configuration(root)
    policy = resolve_policy(root, config)
    require(policy is not None, "strict_policy_missing")
    require(policy["schema_version"] != POLICY_SCHEMA_V2, "strict_review_policy_schema_mismatch")
    usage = sibling("_evidence_usage")
    with usage.current_view(root, config) as view:
        capture, manifest, basis = selection_inputs(root, config, policy, view=view)
        require(basis == expected_basis_id, "strict_review_basis_changed")
        snapshot = review_snapshot(root, config, policy, view, claim_id, basis)
        canonical = sibling("_evidence_revision").canonical_bytes
        # Context snapshot identities use compact JSON without the native codec's trailing newline.
        snapshot_bytes = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        require(len(snapshot_bytes) <= MAX_BYTES, "strict_review_material_bound")
        require("sha256:" + hashlib.sha256(snapshot_bytes).hexdigest() == expected_native_snapshot_sha256,
                "strict_review_snapshot_changed")
        claims = {row["id"]: row for row in manifest["claims"]}
        selected, visited, active = [], set(), set()

        def collect(identifier):
            require(identifier in claims and identifier not in active, "strict_premise_missing_or_cyclic")
            if identifier in visited:
                return
            require(len(visited) < 32, "strict_review_material_bound")
            visited.add(identifier)
            active.add(identifier)
            row = claims[identifier]
            selected.append(row)
            for premise in row["premises"]:
                collect(premise)
            active.remove(identifier)

        collect(claim_id)
        questions = {row["slug"]: row for row in manifest["questions"]}
        semantic = []
        directory = sibling("question_status").questions_directory(root, config)
        for slug in sorted({row["question_slug"] for row in selected}):
            require(slug in questions, "strict_question_unknown")
            path = (directory / (slug + ".md")).relative_to(root).as_posix()
            require(path in capture.files, "strict_question_unknown")
            frontmatter, body = sibling("verify_quotes").split_page(capture.files[path].decode("utf-8"))
            semantic.append({**questions[slug], "metadata": frontmatter.get("metadata"), "body": body})
        material = {"schema_version": "research-complete-review-material/v1", "basis_id": basis,
            "native_snapshot_sha256": expected_native_snapshot_sha256, "claim_id": claim_id,
            "claims": selected, "questions": semantic}
        require(len(canonical(material)) <= MAX_BYTES, "strict_review_material_bound")
        require(configuration(root) == config and resolve_policy(root, config) == policy, "strict_policy_changed")
        require(selection_inputs(root, config, policy, view=view)[2] == basis, "strict_review_basis_changed")
        require(review_snapshot(root, config, policy, view, claim_id, basis) == snapshot, "strict_review_snapshot_changed")
        view.revalidate(root, config)
        return material


@typed
def machine_compatibility(root, *, question_slugs=None):
    """Read current policy and coverage requirements without granting a review."""
    root = Path(root).resolve()
    capture = capture_workspace(root)
    config = safe_load(capture.files.get("research.yml", b""))
    require(isinstance(config, dict), "strict_configuration_invalid")
    sibling("_selected_publication").validate_config_paths(config)
    policy = resolve_policy(root, config)
    require(policy is not None, "strict_policy_missing")
    usage = sibling("_evidence_usage")
    rows = []
    with usage.current_view(root, config) if usage.configured(config) else nullcontext(None) as view:
        with capture.materialize() as captured, (
            authorized_capture(captured, config, view) if view is not None else nullcontext()
        ):
            qstatus = sibling("question_status")
            coverage = sibling("coverage_manifest")
            policies = sibling("_evidence_policies")
            questions = qstatus.collect_questions(qstatus.questions_directory(captured, config))
            require(len(questions) <= 100, "strict_question_bound")
            known = {item["slug"] for item in questions}
            require(question_slugs is None or type(question_slugs) in {list, tuple} and len(question_slugs) <= 100
                    and all(type(slug) is str for slug in question_slugs)
                    and len(question_slugs) == len(set(question_slugs)), "strict_question_unknown")
            selected = known if question_slugs is None else set(question_slugs)
            require(bool(selected) and selected <= known, "strict_question_unknown")
            primitives = sibling("_policy_primitives")
            try:
                rules = primitives.pack_policy_rules(config)
                vocabularies = coverage.merged_policy_vocabularies(config)
                declarations_known = True
            except (primitives.PolicyRuleError, coverage.CoverageManifestError):
                rules, vocabularies, declarations_known = {}, {}, False
            manual = {"source_policy": policies.MANUAL_ONLY_SOURCE_POLICIES,
                      "freshness_policy": policies.MANUAL_ONLY_FRESHNESS_POLICIES,
                      "identity_policy": policies.MANUAL_ONLY_IDENTITY_POLICIES}
            for slug in sorted(selected):
                summary = coverage.coverage_summary_for_question(captured, config, slug, {"coverage_required": True})
                human, reasons = sibling("question_resolve").requires_human_review(summary)
                contract_known = declarations_known and summary.get("coverage_status") not in {"missing", "invalid", "not_required"}
                for facet in summary.get("coverage_facets", []):
                    for field, unconditional in manual.items():
                        name = facet[field]
                        if name in unconditional or (name in rules and rules[name].manual_review_required) or (name not in coverage.BASE_POLICY_DEFINITIONS[field]
                                and (name not in rules or rules[name].section != field)):
                            human = True
                            reasons.append(name)
                        contract_known = contract_known and name in vocabularies.get(field, {})
                rows.append({"question_slug": slug, "required_review_role":
                    "human-review" if policy["human_review"] or human else "evaluator",
                    "contract_known": contract_known, "policy_reasons": sorted(set(reasons))})
        require(configuration(root) == config and resolve_policy(root, config) == policy, "strict_policy_changed")
        require(capture_workspace(root).revision_id == capture.revision_id, "strict_basis_changed")
        if view is not None:
            view.revalidate(root, config)
    human = policy["human_review"] or any(row["required_review_role"] == "human-review" for row in rows)
    known = all(row["contract_known"] for row in rows)
    result = {"schema_version": "evidence-machine-review-policy/v1", "compatible": known and not human,
        "outcome": "policy_incompatible" if human or not known else "machine_compatible",
        "reason": "human_review_required" if human else "coverage_contract_unknown" if not known else "ordinary_evaluator_route",
        "human_review": policy["human_review"], "questions": rows,
        "policy_sha256": "sha256:" + hashlib.sha256(sibling("_evidence_revision").canonical_bytes(policy)).hexdigest(),
        "authorized": False, "evidence_accepted": False}

    sibling("_strict_contract").validate_shape(result, sibling("_strict_contract").schema_documents()[result["schema_version"]])
    return result


@typed
def review_status(root, *, claim_id=None):
    """Project current native review selection; historical records grant no approval."""
    root = Path(root).resolve()
    config = configuration(root)
    policy = resolve_policy(root, config)
    require(policy is not None, "strict_policy_missing")
    usage = sibling("_evidence_usage")
    with usage.current_view(root, config) as view:
        result = evaluate(root, config, policy, view=view)
        require(claim_id is None or type(claim_id) is str, "strict_claim_unknown")
        selected = [row for row in result["claims"] if claim_id is None or row["claim"]["id"] == claim_id]
        require(claim_id is None or bool(selected), "strict_claim_unknown")
        if result["schema_version"] == RESULT_SCHEMA_V3:
            return dependency_review_status(result, selected, view, root, config)
        rows = [{"claim_id": row["claim"]["id"], "accepted": row["accepted"],
            "required_review_role": row["required_review_role"], "reasons": row["reasons"],
            "review": row["review"]} for row in selected]
        view.revalidate(root, config)
        status = {"schema_version": "evidence-strict-review-status/v1", "basis_id": result["basis_id"],
            "claims": rows, "selection": "latest_matching_current_basis",
            "revocation_owner": "current_host_authority_and_source_usage",
            "authorized": False, "evidence_accepted": False}
        sibling("_strict_contract").validate_shape(status, sibling("_strict_contract").schema_documents()[status["schema_version"]])
        return status



def _application_observation(root, config, policy, view):
    bridge = sys.modules.get("evidence_wiki.host_context")
    getter = getattr(bridge, "review_dependency_inputs", None)
    holder = getter() if callable(getter) else None
    return None if holder is None else holder.application_observation(root, config, policy, view)


@typed
def prepare_review(root, claim_id):
    root = Path(root).resolve()
    config = configuration(root)
    policy = resolve_policy(root, config)
    require(policy is not None, "strict_policy_missing")
    usage = sibling("_evidence_usage")
    with usage.current_view(root, config) as view:
        observed = None
        if policy["schema_version"] == POLICY_SCHEMA_V2:
            borrowed = _application_observation(root, config, policy, view)
            result, observed = (_evaluate_dependency_observations(root, config, policy, view=view)
                if borrowed is None else borrowed)
        else:
            result = evaluate(root, config, policy, view=view)
        require(claim_id in {r["claim"]["id"] for r in result["claims"]}, "strict_claim_unknown")
        row = next(r for r in result["claims"] if r["claim"]["id"] == claim_id)
        snapshot = None if observed is not None else review_snapshot(root, config, policy, view, claim_id, result["basis_id"])
        additions = {}
        if result["schema_version"] == RESULT_SCHEMA_V3:
            require(row["claim_basis_id"] is not None, "selective_basis_unavailable")
            capture, producer, currents, reviews = observed
            basis = result["basis_id"]
            current = next(item for item in currents if item["material"]["claim_id"] == claim_id)
            snapshot = current["snapshot"]
            additions = {"dependency_basis": current["dependency_basis"],
                "semantic_material_id": current["dependency_basis"]["semantic_material_id"],
                "claim_schema_version": result["claim_schema_version"], "claim_basis_id": row["claim_basis_id"],
                "observed_workspace_basis_id": result["basis_id"], "review_producer_id": result["review_producer_id"]}
            _dependency_closing(root, config, policy, view, capture, basis, producer, currents, reviews)
        return {"result": result, "rubric": policy["rubric"], "snapshot": snapshot, **additions,
                "review_schema": (REVIEW_SCHEMA_V3 if result["schema_version"] == RESULT_SCHEMA_V3
                    else REVIEW_SCHEMA_V2 if result["schema_version"] == RESULT_SCHEMA_V2 else REVIEW_SCHEMA),
                "registration": {"schema_version": "evidence-usage-command/v1", "state_id": view.state.state_id,
                    "workspace_binding": view.state.binding, "request_id": None, "expected_checkpoint": view.state.checkpoint,
                    "action": "register-strict-human-review" if row["required_review_role"] == "human-review" else "register-strict-review",
                    "body": {"review": None}}}


def validate_review(root, config, envelope, view):
    policy = resolve_policy(root, config)
    require(policy is not None, "strict_policy_missing")
    payload = review_document(envelope["payload"]["body"]["review"])
    if policy["schema_version"] == POLICY_SCHEMA_V2:
        return validate_dependency_review(root, config, policy, envelope, payload, view)
    require(payload["schema_version"] != REVIEW_SCHEMA_V3, "strict_review_policy_schema_mismatch")
    _capture, claims, basis = selection_inputs(root, config, policy, view=view)
    require(payload["basis_id"] == basis and payload["claim_id"] in {c["id"] for c in claims["claims"]}, "strict_review_basis_changed")
    report = evaluate(root, config, policy, view=view)
    row = next(row for row in report["claims"] if row["claim"]["id"] == payload["claim_id"])
    auth = verify_attestation(envelope, row["required_review_role"], view.trust, view.now)
    generator = verify_attestation(payload["generator"], "generator", view.trust, view.now)
    require(auth.get("authenticated") and generator.get("authenticated"), "strict_review_authentication_failed")
    require(auth["controller"] != generator["controller"], "strict_review_not_independent")
    require(payload["generator"]["payload"] == {"schema_version": "evidence-strict-authorship/v1", "basis_id": basis,
            "claim_id": payload["claim_id"]}, "strict_authorship_mismatch")
    require(0 <= (view.now - timestamp(payload["reviewed_at"])).total_seconds() <= policy["max_review_age_seconds"], "strict_review_expired")
    require(timestamp(payload["reviewed_at"]) <= timestamp(auth["issued_at"]), "strict_review_after_attestation")
    require(payload["observation"] == row["observation"], "strict_observation_not_reproduced")
    require(payload["snapshot"] == review_snapshot(root, config, policy, view, payload["claim_id"], basis), "strict_snapshot_not_reproduced")


def render_markdown(result):
    """No caller-authored heading, link, summary or table bypasses the inventory."""
    bounded_result(result)
    def escape(value):
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("*", "\\*").replace("_", "\\_").replace("`", "\\`").replace("#", "\\#")
    lines = ["# Evidence results", "", "Assurance: " + result["assurance"], ""]
    for question in result["questions"]:
        lines += ["## " + escape(question["original_id"]), "", escape(question["question"]), ""]
        if not question["accepted"]:
            lines += ["Insufficient verified evidence: " + ", ".join(question["gaps"]), ""]
        for claim in question["claims"]:
            lines += ["- " + claim["qualification"] + ": " + escape(claim["text"]),
                      "  Scope: " + escape(claim["scope"]) + "; time: " + escape(claim["time"]) + "; units: " + escape(claim["units"])]
            for evidence in claim["evidence"]:
                lines += ["  Source: " + escape(evidence["source_id"]) + " (" + evidence["record_sha256"] + ")"]
            lines.extend("  Limitation: " + escape(note) for note in claim["limitations"])
            if claim["qualification"] == "inference":
                lines += ["  Premises: " + ", ".join(claim["premises"]), "  Derivation: " + escape(claim["derivation"])]
            for calculation in claim.get("calculations", []):
                lines += ["  Calculated value: " + escape(calculation["expected"]) + " " + escape(calculation["unit"])
                          + (" (rounded)" if calculation["rounded"] else " (exact arithmetic)") + "; result: " + calculation["result_id"]]
        lines.append("")
    lines += ["## Qualifications", "", *["- " + note for note in result["limitations"]]]
    rendered = "\n".join(lines) + "\n"
    require(len(rendered.encode("utf-8")) <= MAX_BYTES, "strict_output_bound_exceeded")
    return rendered


def _review_producer():
    bridge = sys.modules.get("evidence_wiki.host_context")
    getter = getattr(bridge, "current_review_producer", None)
    require(callable(getter), "selective_producer_unavailable")
    try:
        producer = getter()
    except Exception:
        raise refusal("selective_producer_unavailable") from None
    require(producer is not None, "selective_producer_unavailable")
    return producer


def _dependency_inputs(root, config, capture, view):
    require(view is not None, "strict_authenticated_review_unavailable")
    inputs = sibling("_publication_usage").qualify_capture(capture.files, config, view,
        purpose="qa-export", consumer="evidence-wiki")
    if any(view.state.revisions[revision]["descriptor"].get("temporal") for revision in inputs["ancestors"]):
        sibling("_assessment_engine").current_history(view, inputs)
        temporal = sibling("_temporal_contract")
        for revision in inputs["ancestors"]:
            times = temporal.source_times(view.state.revisions[revision])
            require(times["claims"]["expires_at"] is None or times["expires"] is not None, "strict_source_expiry_unknown")
            require(times["expires"] is None or view.now < times["expires"], "strict_source_expired")
            if times["effective"] is not None:
                start, end = times["effective"]
                require(start is not None and start <= view.now and (end is None or view.now < end),
                    "strict_source_outside_effective_interval")
    return inputs


def _dependency_role(root, config, policy, capture, view, slug):
    with capture.materialize() as captured, authorized_capture(captured, config, view):
        summary = sibling("coverage_manifest").coverage_summary_for_question(captured, config, slug,
            {"coverage_required": True})
        human, _policies = sibling("question_resolve").requires_human_review(summary)
    return "human-review" if policy["human_review"] or human else "evaluator"


def _dependency_snapshot(root, config, policy, capture, manifest, material, inputs, computation):
    question_owner = sibling("_question_dependencies")
    claim = material["claims"][0]
    questions = {row["slug"]: row for row in manifest["questions"]}
    pages = {}
    for row in material["questions"]:
        slug = row["manifest_question"]["slug"]
        path = row["path"]
        require(path in capture.files, "selective_original_question_missing")
        raw = capture.files[path]
        frontmatter, body = question_owner.parse_page(raw)
        pages[slug] = {"path": path, "frontmatter": question_owner.typed_node(frontmatter),
            "body": body, "raw_sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}
    target = pages[claim["question_slug"]]
    frontmatter = question_owner.typed_value(target["frontmatter"])
    return sibling("_review_dependency_contract").detached_json({"policy": policy, "claim": claim, "question": questions[claim["question_slug"]],
        "metadata": question_owner.typed_node(frontmatter.get("metadata")), "question_body": target["body"],
        "premises": sorted(material["claims"][1:], key=lambda row: row["id"].encode("utf-8")),
        "premise_questions": [{"record": questions[slug], "page": pages[slug]}
            for slug in sorted(pages, key=lambda value: value.encode("utf-8")) if slug != claim["question_slug"]],
        "source_revisions": inputs["selected"], "computation": computation, "semantic_material": material})


def dependency_current(root, config, policy, view, capture, manifest, claim_id):
    """Reconstruct current semantic facts without consulting review history."""
    require(policy["schema_version"] == POLICY_SCHEMA_V2, "selective_policy_required")
    inputs = _dependency_inputs(root, config, capture, view)
    projection = sibling("_review_dependencies")
    preimage = projection.project(root, claim_id, config=config, policy=policy,
        capture=capture, manifest=manifest, view=view)
    role = _dependency_role(root, config, policy, capture, view, preimage["claims"][0]["question_slug"])
    basis = sibling("_review_dependency_contract").basis(preimage, projection.dependency_items(preimage),
        required_review_role=role, computation_mode="configured" if config.get("computation") is not None else "none")
    material = projection.finish(preimage, basis)
    computed = preimage["global_material"]["material"]["worker_semantics"]["computation"]
    computation = None if computed is None else sibling("_question_dependencies").typed_value(computed["result"])
    facts = next(row["reasons"] for row in preimage["local_context"]["mechanical_reasons"] if row["claim_id"] == claim_id)
    observation = {"basis_id": basis["basis_id"], "claim_id": claim_id,
        "producer_id": preimage["producer_id"], "reasons": facts}
    return {"dependency_basis": basis, "material": material, "required_review_role": role,
        "observation": observation, "snapshot": _dependency_snapshot(root, config, policy, capture, manifest,
            material, inputs, computation)}


def _dependency_closing(root, config, policy, view, capture, full_basis, producer, currents=(), reviews=()):
    require(capture_workspace(root).revision_id == capture.revision_id, "strict_inputs_changed")
    require(configuration(root) == config and resolve_policy(root, config) == policy, "strict_policy_changed")
    require(selection_inputs(root, config, policy, view=view)[2] == full_basis, "strict_review_basis_changed")
    if producer is not None:
        require(_review_producer().producer_id == producer.producer_id
            and _review_producer().document() == producer.document(), "selective_producer_changed")
    if view is not None:
        view.revalidate(root, config)
        _dependency_inputs(root, config, capture, view)
        for current in currents:
            material = current["material"]
            facts = {row["claim_id"]: row["reasons"] for row in material["local_context"]["mechanical_reasons"]}
            for claim in material["claims"]:
                invalid = any(not 0 <= (view.now - timestamp(item["observed_at"])).total_seconds()
                    <= policy["max_source_age_seconds"] for item in claim["evidence"])
                require(invalid == ("strict_source_age_invalid" in facts[claim["id"]]),
                    "selective_dependency_basis_changed")
        for envelope, payload, current in reviews:
            _dependency_authenticated(envelope, payload, current, view, policy)
    require(capture_workspace(root).revision_id == capture.revision_id, "strict_inputs_changed")


def _dependency_original(root, config, policy, payload, current, *, purpose, envelope=None):
    """Verify retained originals through actual protected private custody."""
    bridge = sys.modules.get("evidence_wiki.host_context")
    getter = getattr(bridge, "review_custody", None)
    require(callable(getter), "selective_original_custody_unavailable")
    try:
        proof = getter(payload, purpose=purpose)
    except Exception:
        raise refusal("selective_original_custody_unavailable") from None
    require(proof is not None, "selective_original_custody_unavailable")
    material = getattr(proof, "original_material", None)
    if envelope is not None and callable(material):
        original_inputs, snapshot = material(envelope)
    else:
        original_inputs = proof.original_inputs()
        snapshot = proof.original_snapshot()
        if envelope is not None:
            proof.assert_review_envelope(envelope)
    require(type(original_inputs) is dict and all(type(key) is str and type(raw) is bytes
        for key, raw in original_inputs.items()), "selective_original_custody_invalid")
    require(snapshot == payload["snapshot"], "selective_original_snapshot_changed")
    require(snapshot["semantic_material"] == current["material"], "selective_original_material_changed")
    path = policy["claims_path"]
    require(path in original_inputs, "selective_original_claims_missing")
    manifest = claims_document(json_document(original_inputs[path]))
    require(manifest["schema_version"] == payload["claim_schema_version"], "selective_original_claim_schema_changed")
    closure = sibling("_review_dependencies").closure(manifest, payload["claim_id"])
    require(closure == current["material"]["claims"], "selective_original_claims_changed")
    questions = sibling("_question_dependencies")
    capture = SimpleNamespace(files=original_inputs)
    coverage = questions.typed_value(next(row["value"] for row in current["material"]["global_material"]["material"]["global_dependencies"]
        if row["kind"] == "effective_coverage" and row["coordinate"] == "coverage"))
    paths = {path}
    for row in current["material"]["questions"]:
        slug = row["manifest_question"]["slug"]
        paths.add(row["path"])
        projected = questions.project_dependencies(root, config, policy, capture, manifest, slug, payload["claim_id"],
            coverage_summary=coverage[slug]["summary"])
        require(projected == row, "selective_original_question_projection_changed")
    require(set(original_inputs) == paths, "selective_original_custody_scope_changed")
    reproduced = _dependency_snapshot(root, config, policy, capture, manifest, current["material"],
        {"selected": current["snapshot"]["source_revisions"]}, current["snapshot"]["computation"])
    require(reproduced == snapshot, "selective_original_snapshot_not_reproduced")
    if envelope is None:
        proof.assert_current()
    else:
        proof.assert_review_envelope(envelope)
    return proof


def _dependency_authorship(payload):
    return {"schema_version": "evidence-strict-authorship/v2", "basis_id": payload["basis_id"],
        "claim_id": payload["claim_id"], "claim_schema_version": payload["claim_schema_version"],
        "observed_workspace_basis_id": payload["observed_workspace_basis_id"],
        "semantic_material_id": payload["dependency_basis"]["semantic_material_id"]}


def _dependency_authenticated(envelope, payload, current, view, policy):
    auth = verify_attestation(envelope, current["required_review_role"], view.trust, view.now)
    generator = verify_attestation(payload["generator"], "generator", view.trust, view.now)
    require(auth.get("authenticated") and generator.get("authenticated"), "strict_review_authentication_failed")
    require(auth["controller"] != generator["controller"], "strict_review_not_independent")
    require(payload["generator"]["payload"] == _dependency_authorship(payload), "strict_authorship_mismatch")
    age = (view.now - timestamp(payload["reviewed_at"])).total_seconds()
    require(0 <= age <= policy["max_review_age_seconds"], "strict_review_expired")
    require(timestamp(payload["reviewed_at"]) <= timestamp(auth["issued_at"]), "strict_review_after_attestation")
    require(payload["dependency_basis"] == current["dependency_basis"], "selective_dependency_basis_changed")
    require(payload["observation"] == current["observation"], "strict_observation_not_reproduced")
    return auth


def dependency_review_result(root, config, policy, full_basis, claim, view, current):
    missing = {"passed": False, "reason": "strict_review_missing_or_stale"}
    if view is None:
        return {"passed": False, "reason": "strict_authenticated_review_unavailable"}, None
    basis = current["dependency_basis"]["basis_id"]
    # Applicability is selected before authentication, expiry or verdict checks.
    for record in reversed(list(view.state.strict_reviews.values())):
        payload = record["envelope"]["payload"]["body"]["review"]
        if (payload.get("schema_version") != REVIEW_SCHEMA_V3 or payload.get("review_basis_mode") != SELECTIVE_MODE
                or payload.get("basis_id") != basis or payload.get("claim_id") != claim["id"]
                or payload.get("claim_schema_version") != current["material"]["claim_schema_version"]):
            continue
        try:
            review_document(payload)
            require(record["authority"]["content_hash"] == view.trust["content_hash"], "strict_review_authority_changed")
            auth = _dependency_authenticated(record["envelope"], payload, current, view, policy)
            _dependency_original(root, config, policy, payload, current, purpose="reuse", envelope=record["envelope"])
        except Exception as exc:
            reason = exc.details.get("reason", "selective_original_custody_unavailable") if isinstance(exc, ScriptRefusal) else "selective_review_invalid"
            return {"passed": False, "reason": reason}, None
        passed = all(value == "pass" for value in payload["verdicts"].values())
        return {"passed": passed, "reason": "strict_review_passed" if passed else "strict_review_not_passed",
            "review_id": artifact_id(payload), "principal": auth["principal"], "independence": "host_controller_assertion",
            "human_review": current["required_review_role"] == "human-review", "basis_id": basis,
            "reuse": "fresh" if payload["observed_workspace_basis_id"] == full_basis else "reused"}, payload["observed_workspace_basis_id"]
    return missing, None


def _publication_evaluation(root, config, policy, view):
    if policy["schema_version"] != POLICY_SCHEMA_V2:
        return evaluate(root, config, policy, view=view)
    bridge = sys.modules.get("evidence_wiki.host_context")
    getter = getattr(bridge, "review_dependency_inputs", None)
    holder = getter() if callable(getter) else None
    if policy["schema_version"] == POLICY_SCHEMA_V2 and holder is not None:
        assurance = "host_enforced" if strict_host.get() == str(root) else "artifact_checked"
        result = holder.publication_observation(root, config, policy, view, assurance)
        if result is not None:
            return result
    return evaluate(root, config, policy, view=view)


@typed
def evaluate_dependencies(root, config, policy, *, view=None):
    """Keep current workspace identity and selective claim eligibility distinct."""
    return _evaluate_dependency_observations(root, config, policy, view=view)[0]


def _evaluate_dependency_observations(root, config, policy, *, view):
    """Return only observations created by this actual fresh evaluation."""
    root = Path(root).resolve()
    capture, manifest, full_basis = selection_inputs(root, config, policy, view=view)
    if view is not None:
        _dependency_inputs(root, config, capture, view)
    producer = None
    try:
        producer = _review_producer()
    except ScriptRefusal:
        pass
    rows, accepted, currents, reviews = [], set(), [], []
    bridge = sys.modules.get("evidence_wiki.host_context")
    getter = getattr(bridge, "review_dependency_inputs", None)
    holder = getter() if callable(getter) else None
    scope = nullcontext() if holder is None else holder.counter_scope(root, config, capture, manifest, view)
    with scope:
        for claim in manifest["claims"]:
            try:
                current = dependency_current(root, config, policy, view, capture, manifest, claim["id"])
            except Exception as exc:
                reason = exc.details.get("reason", "selective_basis_unavailable") if isinstance(exc, ScriptRefusal) else "selective_basis_unavailable"
                rows.append({"claim": claim, "accepted": False, "reasons": [reason],
                    "review": {"passed": False, "reason": reason}, "required_review_role": "human-review" if policy["human_review"] else "evaluator",
                    "observation": None, "claim_basis_id": None, "observed_workspace_basis_id": None,
                    "review_basis_mode": SELECTIVE_MODE})
                continue
            currents.append(current)
            reasons = list(current["observation"]["reasons"])
            if not set(claim["premises"]) <= accepted:
                reasons.append("strict_premise_not_accepted")
            review, observed = dependency_review_result(root, config, policy, full_basis, claim, view, current)
            if not review["passed"]:
                reasons.append(review["reason"])
            else:
                envelope = view.state.strict_reviews[review["review_id"]]["envelope"]
                reviews.append((envelope, envelope["payload"]["body"]["review"], current))
            if not reasons:
                accepted.add(claim["id"])
            rows.append({"claim": claim, "accepted": not reasons, "reasons": sorted(set(reasons)), "review": review,
                "required_review_role": current["required_review_role"], "observation": current["observation"],
                "claim_basis_id": current["dependency_basis"]["basis_id"], "observed_workspace_basis_id": observed,
                "review_basis_mode": SELECTIVE_MODE})
        _dependency_closing(root, config, policy, view, capture, full_basis, producer, currents, reviews)
    result = bounded_result({"schema_version": RESULT_SCHEMA_V3, "basis_id": full_basis, "policy_id": artifact_id(policy),
        "producer_id": sibling("_selected_publication").producer_identity(),
        "review_producer_id": None if producer is None else producer.producer_id,
        "review_basis_mode": SELECTIVE_MODE, "claim_schema_version": manifest["schema_version"],
        "evaluated_at": (datetime.now(timezone.utc) if view is None else view.now).isoformat(),
        "assurance": "host_enforced" if strict_host.get() == str(root) else "artifact_checked",
        "questions": manifest["questions"], "claims": rows,
        "limits": ["Source correctness is not proved.", "Semantic judgments remain fallible.",
            "Only the controlled artifact is covered; external caller prose is outside this result."]})
    observed = (capture, producer, currents, reviews)
    if holder is not None:
        holder.retain_strict_observation(root, config, policy, view, result, observed)
    return result, observed


def dependency_review_status(result, selected, view, root, config):
    bounded_result(result)
    rows = [{"claim_id": row["claim"]["id"], **{key: row[key] for key in
        ("accepted", "required_review_role", "reasons", "review", "claim_basis_id", "observed_workspace_basis_id", "review_basis_mode")}}
        for row in selected]
    status = {"schema_version": "evidence-strict-review-status/v2", **{key: result[key] for key in
        ("basis_id", "review_producer_id", "review_basis_mode", "claim_schema_version")}, "claims": rows,
        "selection": "newest_applicable_before_validity", "revocation_owner": "current_host_authority_and_source_usage",
        "authorized": False, "evidence_accepted": False}
    sibling("_review_dependency_contract").validate(status["schema_version"], status)
    return status


@typed
def prepare_dependency_review_material(root, claim_id, *, expected_workspace_basis_id,
        expected_claim_basis_id, expected_producer_id, expected_native_snapshot_sha256):
    """Return fresh facts and full closure with no history-dependent preparation."""
    root = Path(root).resolve()
    config = configuration(root)
    policy = resolve_policy(root, config)
    require(policy is not None and policy["schema_version"] == POLICY_SCHEMA_V2, "selective_policy_required")
    with sibling("_evidence_usage").current_view(root, config) as view:
        borrowed = _application_observation(root, config, policy, view)
        if borrowed and not any(c["material"]["claim_id"] == claim_id for c in borrowed[1][2]):
            borrowed = None
        if borrowed is None:
            capture, manifest, basis = selection_inputs(root, config, policy, view=view)
        else:
            result, (capture, producer, currents, reviews) = borrowed
            basis = result["basis_id"]
        require(basis == expected_workspace_basis_id, "strict_review_basis_changed")
        producer = _review_producer()
        require(producer.producer_id == expected_producer_id, "selective_producer_changed")
        if borrowed is None:
            current = dependency_current(root, config, policy, view, capture, manifest, claim_id)
        else:
            current = next((item for item in currents if item["material"]["claim_id"] == claim_id), None)
            require(current is not None, "strict_claim_unknown")
        require(current["dependency_basis"]["basis_id"] == expected_claim_basis_id, "selective_dependency_basis_changed")
        raw = json.dumps(current["snapshot"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        require(len(raw) <= MAX_BYTES, "strict_review_material_bound")
        require("sha256:" + hashlib.sha256(raw).hexdigest() == expected_native_snapshot_sha256, "strict_review_snapshot_changed")
        _dependency_closing(root, config, policy, view, capture, basis, producer, (current,))
        return current["material"]


def validate_dependency_review(root, config, policy, envelope, payload, view):
    """Authenticate fresh admission without requiring a future native result."""
    require(payload["schema_version"] == REVIEW_SCHEMA_V3, "strict_review_policy_schema_mismatch")
    capture, manifest, full_basis = selection_inputs(root, config, policy, view=view)
    require(payload["observed_workspace_basis_id"] == full_basis, "strict_review_basis_changed")
    require(payload["claim_schema_version"] == manifest["schema_version"], "selective_claim_schema_changed")
    producer = _review_producer()
    current = dependency_current(root, config, policy, view, capture, manifest, payload["claim_id"])
    _dependency_authenticated(envelope, payload, current, view, policy)
    require(payload["snapshot"] == current["snapshot"], "strict_snapshot_not_reproduced")
    proof = _dependency_original(root, config, policy, payload, current, purpose="admission", envelope=envelope)
    _dependency_closing(root, config, policy, view, capture, full_basis, producer,
        (current,), ((envelope, payload, current),))
    proof.assert_current()


@typed
def retained_review_eligibility(root, envelope, *, claim_id, expected_review_id):
    """Observe one explicit retained service through this actual strict owner."""
    return sibling("_retained_review_eligibility").observe(root, envelope,
        claim_id=claim_id, expected_review_id=expected_review_id, _core=_CORE_MODULE)
