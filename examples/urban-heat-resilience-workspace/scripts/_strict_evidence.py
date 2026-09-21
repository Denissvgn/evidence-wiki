"""Policy-owned resolution and claim-only release over existing evidence owners."""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import yaml
from _evidence_authority import load_trust, read_host_policy, timestamp, verify_attestation
from _evidence_revision import capture_workspace, content_id, observation, read_observed_file
from _publication_context import authorized_capture, strict_host, strict_internal
from _record_artifacts import json_document
from _script_errors import ScriptRefusal
from _strict_contract import MAX_BYTES, RESULT_SCHEMA, artifact_id, claims_document, policy_document, review_document
from _workspace_module_loader import load_workspace_module

SCRIPT_DIR = Path(__file__).resolve().parent
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
    config = yaml.safe_load(data)
    require(isinstance(config, dict), "strict_configuration_invalid")
    sibling("_selected_publication").validate_config_paths(config)
    return config


@typed
def resolve_policy(root, config):
    """External policy wins even if a caller removes workspace requirement flags."""
    require(isinstance(config, dict), "strict_configuration_invalid")
    local = config.get("strict_evidence")
    external = None
    if os.environ.get("EVIDENCE_WIKI_AUTHORITY_FILE"):
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
    if config.get("strict_evidence") is None and not os.environ.get("EVIDENCE_WIKI_AUTHORITY_FILE"):
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


def selection_inputs(root, config, policy):
    capture = capture_workspace(root)
    path = policy["claims_path"]
    require(path in capture.files and len(capture.files[path]) <= MAX_BYTES, "strict_claims_missing_or_large")
    claims = claims_document(json_document(capture.files[path]))
    for relative, expected in policy["instructions"].items():
        require(relative in capture.files and "sha256:" + hashlib.sha256(capture.files[relative]).hexdigest() == expected,
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
    identity = content_id("evidence-strict-basis/v1", {"files": evidence, "claims": claims,
        "policy": policy, "questions": originals, "producer": sibling("_selected_publication").producer_identity()})
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
    now = datetime.now(timezone.utc) if view is None else view.now
    capture, manifest, basis = selection_inputs(root, config, policy)
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
    require(selection_inputs(root, config, policy)[2] == basis, "strict_inputs_changed")
    result = {"schema_version": RESULT_SCHEMA, "basis_id": basis, "policy_id": artifact_id(policy),
            "evaluated_at": now.isoformat(), "producer_id": sibling("_selected_publication").producer_identity(),
            "assurance": "host_enforced" if strict_host.get() == str(root) else "artifact_checked",
            "questions": manifest["questions"], "claims": rows,
            "limits": ["Source correctness is not proved.", "Semantic judgments remain fallible.",
                       "Only the controlled artifact is covered; external caller prose is outside this result."]}
    return bounded_result(result)


def bounded_result(result):
    contract = sibling("_strict_contract")
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
        result = evaluate(root, config, policy, view=current)
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
                "claims": [{**r["claim"], "verification": {"observation": r["observation"], "review": r["review"]}}
                           for r in rows if r["accepted"]] if eligible else [],
                "unresolved_claims": [{"id": r["claim"]["id"], "qualification": r["claim"]["qualification"], "reasons": r["reasons"]}
                                      for r in rows if not r["accepted"]],
                "gaps": sorted({reason for r in rows for reason in r["reasons"]} | (set() if eligible else {"strict_release_not_eligible"}))})
        if current is not None:
            current.revalidate(root, config)
            released_ids = {claim["id"] for record in records for claim in record["claims"]}
            if released_ids:
                closing = evaluate(root, config, policy, view=current)
                require(released_ids <= {row["claim"]["id"] for row in closing["claims"] if row["accepted"]},
                        "strict_review_changed_during_release")
        require(capture_workspace(root).revision_id == before and selection_inputs(root, config, policy)[2] == result["basis_id"], "strict_inputs_changed")
        require(resolve_policy(root, config) == policy, "strict_policy_changed")
        originals = []
        by_slug = {row["slug"]: row for row in records}
        for original in sorted({q["original_id"] for q in result["questions"]}):
            derived = [q["slug"] for q in result["questions"] if q["original_id"] == original]
            originals.append({"original_id": original, "question_slugs": derived,
                "accepted": all(slug in by_slug and by_slug[slug]["accepted"] for slug in derived),
                "unselected_question_slugs": [slug for slug in derived if slug not in by_slug]})
        return bounded_result({"schema_version": "evidence-strict-publication/v1", "basis_id": result["basis_id"],
            "policy_id": result["policy_id"], "producer_id": result["producer_id"], "evaluated_at": result["evaluated_at"],
            "assurance": result["assurance"], "verdict": "ship" if all(r["accepted"] for r in records) else "blocked_on_sources",
            "questions": records, "limitations": result["limits"], "original_outcomes": originals,
            "selection_complete": len(wanted) == len(result["questions"])})


def review_snapshot(root, config, policy, view, claim_id, expected_basis):
    capture, manifest, basis = selection_inputs(root, config, policy)
    require(basis == expected_basis, "strict_review_basis_changed")
    claims = {claim["id"]: claim for claim in manifest["claims"]}
    require(claim_id in claims, "strict_claim_unknown")
    claim = claims[claim_id]
    question = next(q for q in manifest["questions"] if q["slug"] == claim["question_slug"])
    path = sibling("question_status").questions_directory(root, config) / (question["slug"] + ".md")
    metadata, _body = sibling("verify_quotes").split_page(capture.files[path.relative_to(root).as_posix()].decode())
    inputs = sibling("_publication_usage").qualify_capture(capture.files, config, view, purpose="qa-export", consumer="evidence-wiki")
    return {"policy": policy, "claim": claim, "question": question, "metadata": metadata.get("metadata") or {},
            "premises": [claims[name] for name in claim["premises"]], "source_revisions": inputs["selected"]}


@typed
def prepare_review(root, claim_id):
    root = Path(root).resolve()
    config = configuration(root)
    policy = resolve_policy(root, config)
    require(policy is not None, "strict_policy_missing")
    usage = sibling("_evidence_usage")
    with usage.current_view(root, config) as view:
        result = evaluate(root, config, policy, view=view)
        require(claim_id in {r["claim"]["id"] for r in result["claims"]}, "strict_claim_unknown")
        row = next(r for r in result["claims"] if r["claim"]["id"] == claim_id)
        snapshot = review_snapshot(root, config, policy, view, claim_id, result["basis_id"])
        return {"result": result, "rubric": policy["rubric"], "snapshot": snapshot,
                "registration": {"schema_version": "evidence-usage-command/v1", "state_id": view.state.state_id,
                    "workspace_binding": view.state.binding, "request_id": None, "expected_checkpoint": view.state.checkpoint,
                    "action": "register-strict-human-review" if row["required_review_role"] == "human-review" else "register-strict-review",
                    "body": {"review": None}}}


def validate_review(root, config, envelope, view):
    policy = resolve_policy(root, config)
    require(policy is not None, "strict_policy_missing")
    payload = review_document(envelope["payload"]["body"]["review"])
    _capture, claims, basis = selection_inputs(root, config, policy)
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
        lines.append("")
    lines += ["## Qualifications", "", *["- " + note for note in result["limitations"]]]
    rendered = "\n".join(lines) + "\n"
    require(len(rendered.encode("utf-8")) <= MAX_BYTES, "strict_output_bound_exceeded")
    return rendered
