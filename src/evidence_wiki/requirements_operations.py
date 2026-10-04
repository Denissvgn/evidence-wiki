"""Native requirements effect preparation for a registered staged mutation route.

Preparation operates on the real workspace and reuses native intake/coverage
validation and rendering. Commit is intentionally supplied by the registered
MutationStore adapter; this module accepts no caller callbacks or invented receipts.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

from ._pack_io import json_document, yaml_document
from .agent_execution import ExecutionError, _ref, _utc
from .host_context import _MUTATION_GATE, _require_mutation_workspace_gate, current_context
from .mutation_contracts import canonical, detached, digest
from .mutation_store import capture_revision
from .pack_discovery import owner
from .requirements_projection import DOCUMENT, inherited_plan, revision_decision, validated_lineage

PLAN = "research-requirements-effect-plan/v1"
RESULT = "research-requirements-revision-result/v1"
REQUIREMENTS = "docs/research-requirements.json"


def _hash(data):
    return None if data is None else "sha256:" + hashlib.sha256(data).hexdigest()


def _refuse(reason):
    raise ExecutionError("requirements_owner_" + reason)


def _host(root, envelope):
    context = current_context()
    if context is None:
        _refuse("authority_required")
    root = context.require_path(Path(root), write=True)
    context.require_grant("requirements_operations.revise")
    context.require_grant("requirements.envelope." + digest(envelope).removeprefix("sha256:"))
    context.require_grant("intake_questions.run_intake")
    context.require_grant("coverage_manifest.run_init_inline")
    _require_mutation_workspace_gate(root)
    return root, context


def _idle(root, capture):
    if owner("_delegation_gate").live_pending_orders(root):
        _refuse("pending_owner_work")
    controller = owner("run_controller")
    for name, raw in capture.files.items():
        if name.startswith("runs/") and name.endswith("/run-state.json"):
            document = controller.load_run_state(root, Path(name).parent.name, _captured_raw=raw)
            if document["state"]["current"] not in controller.TERMINAL_STATES:
                _refuse("active_run")
            journal = controller._bounded_journal(controller.events_path(root, document["run_id"]))["events"]
            records = [(row.get("data") or {}).get("record", {}) for row in journal]
            if any(row.get("schema_version") == "research-execution-journal/v1" and row.get("kind") in {"execution_attempt", "execution_native"} for row in records):
                _refuse("execution_settlement_authentication_unavailable")
            if any(row.get("kind") in {"host_job", "pending_work"} for row in records):
                _refuse("pending_work_authentication_unavailable")
    for question in owner("question_status").collect_questions(owner("question_status").questions_directory(root, yaml_document(capture.files["research.yml"]))):
        if question.get("status") in {"claimed", "in_progress"} or question.get("claimed_by"):
            _refuse("claimed_question")


def _index(text, config, items, observed):
    """Exact native intake table shape, with the pinned owner event clock."""
    lines = text.splitlines()
    if "## Questions" not in lines:
        _refuse("index_table_missing")
    start = lines.index("## Questions") + 1
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    last = next((index for index in range(end - 1, start - 1, -1) if lines[index].startswith("|")), None)
    if last is None:
        _refuse("index_table_missing")
    intake, init = owner("intake_questions"), owner("init_research_workspace")
    rows = []
    for item in items:
        path = intake.wiki_root_value(config) + "/" + init.QUESTION_WIKI_DIR + "/" + item["slug"] + ".md"
        summary = init.escape_table_cell(item.get("summary") or item["question"])
        rows.append(f"| [{path}]({path}) | {summary} | {observed.date().isoformat()} | |")
    if lines[last].strip() == intake.INDEX_PLACEHOLDER_ROW:
        lines[last:last + 1] = rows
    else:
        lines[last + 1:last + 1] = rows
    return ("\n".join(lines).rstrip() + "\n").encode()


def _inherit(base, addition, config):
    coverage = owner("coverage_manifest")
    plans = {row["question_id"]: row for row in base["decisions"]["question_plans"]}
    vocab = coverage.merged_policy_vocabularies(config)
    facets = {}
    inherited_plan(base, addition)
    for reference in addition["facet_refs"]:
        plan = plans[reference["original_id"]]
        if plan.get("template") is not None:
            _refuse("template_requires_owned_snapshot")
        matches = [row for row in plan.get("facets", []) if row["facet_id"] == reference["facet_id"]]
        if len(matches) != 1:
            _refuse("inherited_facet_missing")
        facet = detached(matches[0])
        if facet.get("accepted_source_ids") or facet.get("blocking_request_ids") or facet.get("facet_verdict", "pending") != "pending":
            _refuse("caller_acceptance_forbidden")
        normalized = coverage.normalize_template_facet(facet, required=facet.get("required", True), policy_vocabularies=vocab)
        previous = facets.get(normalized["facet_id"])
        if previous is not None and previous != normalized:
            _refuse("inherited_facet_ambiguous")
        facets[normalized["facet_id"]] = normalized
    if not any(row["required"] for row in facets.values()):
        _refuse("required_facet_missing")
    return coverage.normalize_template_document({"required_facets": [row for row in facets.values() if row["required"]],
        "optional_facets": [row for row in facets.values() if not row["required"]]}, policy_vocabularies=vocab)


def _prepare_effects(root, proposal, envelope, *, request_id, expected_revision, observed_utc, input_sha256=None, workspace_binding=None):
    """Prepare declared native outputs under the real host/mutation workspace gate.

    Existing v2 lineage must first be verified by the mutation-reader bridge;
    preparation authenticates historical receipt prefixes using the same gate.
    """
    root, context = _host(root, envelope)
    request_id, observed = _ref(request_id), _utc(observed_utc)
    capture = owner("_evidence_revision").capture_workspace(root)
    if capture_revision(root, excluded=(".locks", "raw/.locks"))["revision_id"] != expected_revision:
        _refuse("revision_changed")
    _idle(root, capture)
    raw = capture.files.get(REQUIREMENTS)
    if raw is None:
        _refuse("requirements_missing")
    current = verifylineage(root, json_document(raw))
    if not current["revisions"] and raw != canonical(current["base"]) + b"\n":
        _refuse("base_bytes_not_canonical")
    decision = revision_decision(current["base"], current["effective"], proposal, envelope)
    if decision["outcome"] != "append":
        _refuse(decision["reason"])
    config = yaml_document(capture.files["research.yml"])
    policy = owner("_strict_evidence").resolve_policy(root, config)
    expected_base = (policy or {}).get("instructions", {}).get(REQUIREMENTS)
    original_bytes = canonical(current["base"]) + b"\n" if current["revisions"] else raw
    if expected_base is not None and _hash(original_bytes) != expected_base:
        _refuse("original_instruction_binding_changed")
    batch = {"schema_version": "1.0", "questions": [{"id": row["id"], "question": row["text"], "origin": "parent_agent",
        "metadata": {"research_id": row["id"], "original_ids_json": json.dumps(row["original_ids"], ensure_ascii=False),
            "original_text": row["text"]}} for row in decision["additions"]]}
    intake = owner("intake_questions")
    report = intake.run_intake_document(root, batch, dry_run=True, from_file_label=request_id, _observed_at=observed)
    if report["skipped_duplicates"] or report["counts"]["created"] != len(batch["questions"]):
        _refuse("intake_duplicate")
    binding = {"request_id": request_id, "before_revision": expected_revision, "proposal_sha256": digest(proposal),
        "envelope_sha256": digest(envelope), "workspace": str(root),
        "input_sha256": input_sha256, "workspace_binding": detached(workspace_binding)}
    stable_ref = digest({"schema_version": "research-requirements-owner-ref/v1", **binding})
    document = {"schema_version": DOCUMENT, "base": current["base"], "revisions": [*current["revisions"], {"revision_id": request_id,
        "proposal": proposal, "envelope": envelope, "decision": decision, "owner_receipt_ref": stable_ref}]}
    outputs = []
    def output(path, data):
        context.require_path(root / path, write=True)
        if len(data) > 1048576:
            _refuse("output_bound")
        outputs.append({"path": path, "prior_sha256": _hash(capture.files.get(path)), "data": data})
    coverage = owner("coverage_manifest")
    for created, addition in zip(report["created"], decision["additions"], strict=True):
        output(created["path"], created["content"].encode())
        template = _inherit(current["base"], addition, config)
        manifest = coverage.build_manifest(created["slug"], None, template, _observed_at=observed)
        coverage.validate_manifest(manifest, expected_slug=created["slug"], policy_vocabularies=coverage.merged_policy_vocabularies(config))
        output(coverage.manifest_path(root, config, created["slug"]).relative_to(root).as_posix(), yaml.safe_dump(manifest, sort_keys=False).encode())
    items = [{"slug": row["slug"], "question": row["question"], "origin": row["origin"]} for row in report["created"]]
    output("index.md", _index(capture.files["index.md"].decode(), config, items, observed))
    prior_log = capture.files.get("log.md", b"").decode()
    prefix = "# Research Wiki Activity Log\n\n" if not prior_log else "" if prior_log.endswith("\n\n") else "\n" if prior_log.endswith("\n") else "\n\n"
    entry = intake.render_log_entry(items, [], {}, request_id, observed)
    output("log.md", (prior_log + prefix + entry + "\n").encode())
    output(REQUIREMENTS, canonical(document) + b"\n")
    paths = {row["path"] for row in outputs}
    if len(paths) != len(outputs) or len(outputs) > 404:
        _refuse("output_bound")
    dependencies = [{"path": name, "sha256": _hash(data)} for name, data in sorted(capture.files.items()) if name not in paths]
    if capture_revision(root, excluded=(".locks", "raw/.locks"))["revision_id"] != expected_revision:
        _refuse("inputs_changed")
    return {"schema_version": PLAN, **binding, "owner_ref": stable_ref, "observed_utc": observed.isoformat(),
        "decision": decision, "original_binding": "protected_instruction" if expected_base is not None else "host_declared_unprotected",
        "configuration_sha256": digest(config), "document_sha256": _hash(outputs[-1]["data"]), "dependencies": dependencies,
        "outputs": outputs, "directories": list(capture.directories),
        "new_directories": sorted({parent.as_posix() for path in paths for parent in Path(path).parents
                                  if parent.as_posix() != "." and parent.as_posix() not in capture.directories}),
        "receipt_required": True, "eligibility": "invalidated_until_current_owner_checks"}


def expected_result(plan):
    """Exact result contract committed by the real registered mutation adapter."""
    return {"schema_version": RESULT, "request_id": plan["request_id"], "owner_ref": plan["owner_ref"],
        "document_sha256": plan["document_sha256"], "outputs": [{"path": row["path"], "sha256": _hash(row["data"])} for row in plan["outputs"]],
        "semantic_approval": "not_evaluated", "original_binding": plan["original_binding"], "eligibility_invalidated": True}


def _read_context(root):
    context = current_context()
    if context is None or context.mutation_control_directory is None:
        _refuse("protected_receipts_required")
    root = context.require_path(Path(root))
    return root, context


def _definitions(manifest):
    """Immutable coverage definitions, excluding owner-reviewed evolving evidence."""
    mutable = {"accepted_source_ids", "blocking_request_ids", "facet_verdict", "notes"}
    return {"coverage_profile": manifest["coverage_profile"], **{
        field: [{key: value for key, value in facet.items() if key not in mutable} for facet in manifest[field]]
        for field in ("required_facets", "optional_facets")}}


def verifylineage(root, document, *, check_current=True):
    """Authenticate each historical revision through the existing private mutation store.

    Mutable historical outputs and old workspace revisions are not compared to
    today's files. Immutable staged declarations and receipts remain exact.
    """
    from .mutation_requirements_recovery import load_plan
    from .mutation_store import MutationStore
    root, context = _read_context(root)
    lineage = validated_lineage(document)
    prefix = []
    coverage_expected = {}
    for revision in lineage["revisions"]:
        prefix.append(revision)
        store = MutationStore(root, context.mutation_control_directory)
        with store.historical_locked(revision["revision_id"], borrow_workspace=_MUTATION_GATE.get() is not None):
            saved = store._read("intent.json")
            state, receipt = store.read(), store.receipt()
            if (saved is None or state is None or receipt is None or saved["operation"] != "requirements.revise"
                    or not (receipt["status"] == "committed" or state["status"] == "reconciled" and receipt["status"] in {"partial", "commit_unknown"})
                    or saved["arguments"] != {"proposal": revision["proposal"], "envelope": revision["envelope"]}
                    or receipt["input_sha256"] != saved["input_sha256"]):
                _refuse("receipt_missing_or_mismatched")
            plan = load_plan(root, saved, state["owner_basis"], store, authenticate_host=False)
            if plan["owner_ref"] != revision["owner_receipt_ref"]:
                _refuse("receipt_binding_changed")
            expected_doc = canonical({"schema_version": DOCUMENT, "base": lineage["base"], "revisions": prefix}) + b"\n"
            if plan["outputs"][-1]["data"] != expected_doc:
                _refuse("receipt_document_changed")
            result = expected_result(plan)
            if receipt["status"] == "committed":
                if receipt["result"] != result:
                    _refuse("receipt_result_changed")
            else:
                proof = state["reconciliation"] or {}
                observation = proof.get("owner_observation") or {}
                desired = (proof.get("desired_state_proved") is True and proof.get("proof_kind") == "requirements_postconditions"
                    and observation.get("expected_result_sha256") == digest(result) and observation.get("postconditions_proved") is True)
                completed = (proof.get("completion_proved") is True and proof.get("proof_kind") == "owner_return"
                    and state["result"] == result and observation.get("retained_result_sha256") == digest(result))
                if not (desired or completed):
                    _refuse("reconciled_proof_missing")
            for output in plan["outputs"]:
                if output["path"].endswith(".yml") and "/coverage/" in output["path"]:
                    coverage_expected[output["path"]] = _definitions(yaml_document(output["data"]))
    if check_current:
        capture = owner("_evidence_revision").capture_workspace(root)
        if capture.files.get(REQUIREMENTS) != canonical(document) + b"\n":
            _refuse("current_document_changed")
        config = yaml_document(capture.files["research.yml"])
        qstatus = owner("question_status")
        questions = qstatus.collect_questions(qstatus.questions_directory(root, config))
        by_id, slugs = {}, {}
        for question in questions:
            actual = qstatus.load_frontmatter(qstatus.questions_directory(root, config) / question["path"])
            identifier = actual.get("metadata", {}).get("research_id")
            if identifier is not None and identifier in by_id:
                _refuse("current_question_identity_duplicate")
            if identifier is not None:
                by_id[identifier] = actual
                slugs[identifier] = question["slug"]
        for row in lineage["effective"]["request"]["payload"]["questions"] + lineage["effective"]["request"]["payload"]["derived_questions"]:
            actual = by_id.get(row["id"])
            refs = row.get("original_ids", [row["id"]])
            expected_metadata = {"research_id": row["id"], "original_text": row["text"],
                                 "original_ids_json": json.dumps(refs, ensure_ascii=False)}
            if (actual is None or actual.get("metadata") != expected_metadata or actual.get("question") != row["text"].strip()):
                _refuse("current_question_contract_changed")
        for plan in lineage["base"]["decisions"]["question_plans"]:
            if plan["template"] is not None:
                _refuse("base_template_snapshot_unsupported")
            actual = by_id.get(plan["question_id"])
            if actual is None:
                _refuse("current_question_contract_changed")
            slug = slugs[plan["question_id"]]
            coverage = owner("coverage_manifest")
            template = coverage.normalize_template_document({"required_facets": [row for row in plan["facets"] if row.get("required", True)],
                "optional_facets": [row for row in plan["facets"] if not row.get("required", True)]}, policy_vocabularies=coverage.merged_policy_vocabularies(config))
            path = coverage.manifest_path(root, config, slug).relative_to(root).as_posix()
            coverage_expected[path] = _definitions(coverage.build_manifest(slug, None, template))
        for path, expected in coverage_expected.items():
            actual = capture.files.get(path)
            if actual is None or _definitions(yaml_document(actual)) != expected:
                _refuse("current_inherited_criteria_changed")
    return {**lineage, "owner_authentication": "protected_mutation_receipts" if lineage["revisions"] else "not_inferred"}


def read_effective(root, *, raw=None):
    root = Path(root)
    if raw is None:
        raw = owner("_evidence_revision").capture_workspace(root).files.get(REQUIREMENTS)
    if type(raw) is not bytes or len(raw) > 1048576:
        _refuse("requirements_missing_or_bound")
    document = json_document(raw)
    if document.get("schema_version") == DOCUMENT:
        return verifylineage(root, document)["effective"]
    return validated_lineage(document)["effective"]


def instruction_matches(root, raw, expected):
    """Match protected original bytes through authenticated append-only revisions."""
    if _hash(raw) == expected:
        return True
    document = json_document(raw)
    lineage = verifylineage(root, document)
    return bool(lineage["revisions"] and _hash(canonical(lineage["base"]) + b"\n") == expected)


def setup_changes_owned(root, previous, current):
    """Recognize exact owned revision effects without rewriting setup history."""
    from .mutation_requirements_recovery import load_plan
    from .mutation_store import MutationStore
    root, context = _read_context(root)
    raw = owner("_evidence_revision").capture_workspace(root).files.get(REQUIREMENTS)
    lineage = verifylineage(root, json_document(raw))
    if not lineage["revisions"] or previous is None or current is None:
        return False
    outputs, directories = {}, set()
    for revision in lineage["revisions"]:
        store = MutationStore(root, context.mutation_control_directory)
        with store.historical_locked(revision["revision_id"], borrow_workspace=_MUTATION_GATE.get() is not None):
            intent, state = store._read("intent.json"), store.read()
            plan = load_plan(root, intent, state["owner_basis"], store, authenticate_host=False)
            outputs.update({row["path"]: _hash(row["data"])[7:] for row in plan["outputs"]})
            directories.update(plan["new_directories"])
    before = {row["path"]: row for row in previous["files"]}
    after = {row["path"]: row for row in current["files"]}
    if any(after.get(path) != row for path, row in before.items() if path not in outputs):
        return False
    if any(path not in before and path not in outputs for path in after):
        return False
    if any(after.get(path, {}).get("sha256") != expected or after.get(path, {}).get("mode") != 0o600
            for path, expected in outputs.items()):
        return False
    old_dirs = {row["path"]: row for row in previous["directories"]}
    new_dirs = {row["path"]: row for row in current["directories"]}
    return (all(new_dirs.get(path) == row for path, row in old_dirs.items())
        and set(new_dirs) - set(old_dirs) <= directories
        and all(new_dirs[path]["mode"] == 0o700 for path in set(new_dirs) - set(old_dirs)))


def revise(root, proposal, envelope, *, _store=None, _basis=None):
    """Publish declared native outputs under the live canonical workspace gate."""
    root, context = _host(root, envelope)
    from .mutation_requirements_recovery import load_plan
    from .mutation_store import MutationStore
    if (type(_store) is not MutationStore or not _store._active or _store._historical_readonly or _store.workspace_root != root
            or _store.control_root != context.mutation_control_directory.resolve(strict=True)):
        _refuse("staged_request_gate_required")
    _store._require_locked()
    intent = _store._read("intent.json")
    state = _store.read()
    if (intent is None or state is None or state["status"] != "executing" or state["owner_basis"] != _basis
            or intent["arguments"] != {"proposal": proposal, "envelope": envelope}):
        _refuse("staged_request_binding_changed")
    plan = load_plan(root, intent, _basis, _store)
    writer = owner("_usage_materialization")
    def revalidate(publishing=None):
        _host(root, envelope)
        capture = owner("_evidence_revision").capture_workspace(root)
        for dependency in plan["dependencies"]:
            if _hash(capture.files.get(dependency["path"])) != dependency["sha256"]:
                _refuse("dependencies_changed")
        known = {row["path"]: row for row in plan["outputs"]}
        temporary = set()
        if publishing is not None:
            candidates = {path for path, data in capture.files.items() if Path(path).parent == Path(publishing["path"]).parent
                and re.fullmatch(r"\.normalized-[a-f0-9]{32}", Path(path).name) is not None and data == publishing["data"]}
            if len(candidates) != 1:
                _refuse("publication_temporary_changed")
            temporary = candidates
        if set(capture.files) - {row["path"] for row in plan["dependencies"]} - set(known) - temporary:
            _refuse("undeclared_inputs_added")
        if (set(capture.directories) - set(plan["directories"]) - set(plan["new_directories"])
                or not set(plan["directories"]) <= set(capture.directories)):
            _refuse("undeclared_directories_changed")
        for path, row in known.items():
            if _hash(capture.files.get(path)) not in {row["prior_sha256"], _hash(row["data"])}:
                _refuse("owned_output_conflict")
    revalidate()
    for row in plan["outputs"]:
        context.require_path(root / row["path"], write=True)
        writer.publish_file(root, row["path"], row["data"], row["prior_sha256"], before_publish=lambda row=row: revalidate(row))
    revalidate()
    return expected_result(plan)


def prepare_revision(root, proposal, envelope):
    """Freeze an inert request for the existing serialized mutation owner."""
    from . import mutation_operations
    from .host_context import checked_operation
    with checked_operation("requirements_operations", "prepare_revision", (root, proposal, envelope), {}) as revalidate:
        current = read_effective(root)
        raw = owner("_evidence_revision").capture_workspace(root).files[REQUIREMENTS]
        base = validated_lineage(json_document(raw))["base"]
        decision = revision_decision(base, current, proposal, envelope)
        if decision["outcome"] != "append":
            _refuse(decision["reason"])
        selected = current_context()
        if selected is None:
            _refuse("authority_required")
        selected.require_grant("requirements.envelope." + digest(envelope).removeprefix("sha256:"))
        result = mutation_operations.prepare_request(root, request_id=proposal["request_id"], operation="requirements.revise",
            arguments={"proposal": proposal, "envelope": envelope})
        if owner("_evidence_revision").capture_workspace(root).files[REQUIREMENTS] != raw:
            _refuse("inputs_changed")
        revalidate()
        return result


def current(root, *, policy=None, run_id=None):
    """Project original obligations using fresh native publication and accounting."""
    from .host_context import checked_operation
    from .requirements_projection import requirements_view
    from .research_completion import _completion_observation
    from .research_observation import ResearchObservation
    with checked_operation("requirements_operations", "current", (root,), {}) as revalidate:
        observation = ResearchObservation(root, run_id=run_id)
        effective = read_effective(root, raw=observation.capture.files.get(REQUIREMENTS))
        completion = _completion_observation(observation, run_id=run_id)
        mapping = observation.mapping.get("result") or {"originals": [], "expected": {}, "gaps": ["original_question_accounting_unavailable"]}
        result = requirements_view(effective, mapping, completion["original_outcomes"])
        # A supplied run policy is retained as an inert constraint; native
        # publication and protected receipts retain all acceptance authority.
        if policy is not None:
            from .run_policy import RunPolicy
            result["run_policy"] = (policy if isinstance(policy, RunPolicy) else RunPolicy(policy)).to_dict()
        result["original_binding"] = "protected_instruction" if (observation.policy or {}).get("instructions", {}).get(REQUIREMENTS) else "host_declared_unprotected"
        result.update(target=str(observation.root), observed_at=observation.now.isoformat(), basis=observation.basis,
                      current_release=completion["status"], current_release_gaps=completion["gaps"])
        observation.finish()
        revalidate()
        return result
