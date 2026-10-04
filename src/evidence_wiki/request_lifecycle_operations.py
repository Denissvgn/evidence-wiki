"""Current evidence readiness over canonical request and question records."""

from __future__ import annotations

import json
from pathlib import Path

from ._facades._base import call_seam
from ._pack_io import canonical, json_document, relative_path, yaml_document
from .host_context import _require_mutation_workspace_gate, current_context
from .mutation_contracts import detached, digest, refuse
from .mutation_store import capture_revision
from .pack_discovery import owner
from .requirements_operations import read_effective
from .source_inputs import SourceView
from .source_readiness import inspect_sources

SCHEMA = "evidence-request-lifecycle-view/v1"


class _ScopedView(SourceView):
    scope_error = None

    def read(self, relative, maximum=1_048_576):
        if self.scope_error is not None:
            raise self.scope_error
        try:
            current_context().require_path(self.root / relative)
        except Exception as error:
            self.scope_error = error
            raise
        return super().read(relative, maximum)

    def require_scope(self):
        if self.scope_error is not None:
            raise self.scope_error


def _ids(values, *, maximum=128):
    if type(values) not in {list, tuple} or len(values) > maximum:
        refuse("REQUEST_SELECTION_BOUND", "/selection")
    native = owner("source_requests")
    result = []
    for value in values:
        if type(value) is not str or not value.strip() or len(value) > 128 or value in result:
            refuse("REQUEST_SELECTION_INVALID", "/selection")
        native._argument_text(value, "selection", 128)
        result.append(value)
    return result


def _scope(value):
    if value is None:
        return {}
    if type(value) is not dict or len(value) > 16:
        refuse("REQUEST_SCOPE_INVALID", "/scope")
    normalized = owner("_request_scope").normalize_scope(value)
    if normalized != value:
        refuse("REQUEST_SCOPE_INVALID", "/scope")
    return normalized


def _host(root, seam, *, write=False):
    context = current_context()
    if context is None or context.mutation_control_directory is None:
        refuse("AUTHORITY_REQUIRED", "/host_context")
    root = context.require_path(Path(root), write=write).resolve(strict=True)
    context.require_grant("request_lifecycle_operations." + seam)
    if write:
        _require_mutation_workspace_gate(root)
    return root, context


def _questions(root, view, effective):
    native = owner("question_status")
    directory = native.questions_directory(root, view.config)
    context = current_context()
    context.require_path(directory)
    try:
        relative = directory.relative_to(root).as_posix()
    except ValueError:
        refuse("REQUEST_QUESTION_SCOPE_UNSUPPORTED", "/questions")
    relative_path(relative)
    capture = owner("_evidence_revision").capture_workspace(root)
    declarations = effective["request"]["payload"]
    expected = {row["id"]: row for row in declarations["questions"] + declarations["derived_questions"]}
    plans = {row["question_id"]: row for row in effective["decisions"]["question_plans"]}
    result, seen = {}, set()
    for path, raw in capture.files.items():
        candidate = Path(path)
        if candidate.parent.as_posix() != relative or candidate.suffix != ".md":
            continue
        if len(result) >= 300:
            refuse("REQUEST_QUESTION_BOUND", "/questions")
        view.read(path)
        try:
            parts = raw.decode("utf-8").split("---", 2)
            front = yaml_document(parts[1].encode()) if len(parts) == 3 and not parts[0].strip() else None
        except (UnicodeError, ValueError):
            front = None
        if type(front) is not dict or front.get("type") != "question":
            continue
        metadata = front.get("metadata")
        identifier = metadata.get("research_id") if type(metadata) is dict else None
        if identifier not in expected:
            continue
        row = expected[identifier]
        refs = row.get("original_ids", [identifier])
        exact = {"research_id": identifier, "original_text": row["text"],
            "original_ids_json": json.dumps(refs, ensure_ascii=False)}
        if identifier in seen or metadata != exact or front.get("question") != row["text"].strip():
            refuse("REQUEST_QUESTION_BINDING_CHANGED", "/questions")
        seen.add(identifier)
        plan = plans.get(identifier)
        if plan is None or plan.get("template") is not None:
            refuse("REQUEST_CRITERIA_UNSUPPORTED", "/questions")
        result[candidate.stem] = {"id": identifier, "frontmatter": front, "plan": plan, "path": path}
    if seen != set(expected):
        refuse("REQUEST_QUESTION_MAPPING_INCOMPLETE", "/questions")
    return result


def _records(root, view):
    native = owner("source_requests")
    path = native.requests_path(root, view.config)
    current_context().require_path(path)
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        refuse("REQUEST_STORE_SCOPE_UNSUPPORTED", "/requests")
    raw = view.read(relative, 8_388_608)
    rows, seen = [], set()
    for line in (raw or b"").splitlines():
        if not line.strip():
            continue
        if len(rows) >= 4096:
            refuse("REQUEST_STORE_BOUND", "/requests")
        row = json_document(line)
        if (type(row) is not dict or row.get("status") not in {"open", "fulfilled"}
                or type(row.get("request_id")) is not str or row["request_id"] in seen):
            refuse("REQUEST_STORE_INVALID", "/requests")
        _ids([row["request_id"]])
        _ids(row.get("question_slugs", []))
        _scope(row.get("scope"))
        if type(row.get("query_or_identifier")) is not str or not row["query_or_identifier"].strip():
            refuse("REQUEST_STORE_INVALID", "/requests")
        seen.add(row["request_id"])
        rows.append(row)
    return path, rows


def _required_scope(effective, questions, slugs):
    declared = {row["name"]: row["value"] for row in effective["request"]["payload"].get("scope", [])}
    required = {}
    for slug in slugs:
        question = questions.get(slug)
        if question is None:
            refuse("REQUEST_FOREIGN_QUESTION", "/question_slugs")
        for criterion in question["plan"]["criteria"]:
            for key in criterion["required_scope"]:
                if key not in declared:
                    refuse("REQUEST_REQUIRED_SCOPE_UNKNOWN", "/scope")
                required[key] = declared[key]
    return required


def _source(view, source_id, scope):
    if type(source_id) is not str or not source_id.strip() or len(source_id) > 512:
        return {"source_id": source_id, "current_ready": False, "gaps": ["source_unassigned"]}
    records = view.manifest()
    record = records.get(source_id)
    if record is None:
        return {"source_id": source_id, "current_ready": False, "gaps": ["source_not_in_manifest"]}
    inspection = inspect_sources(view, source_ids=[source_id])[0]
    view.require_scope()
    gaps = list(inspection.get("reasons", []))
    if inspection.get("usability") != "usable" or inspection.get("complete") is not True:
        gaps.append("complete_usable_source_required")
    if inspection.get("delivery") != "captured":
        gaps.append("current_original_unavailable")
    source_scope = owner("source_requests").source_provenance_scope(view.root, view.config, source_id)
    conflicts, missing = owner("_request_scope").scope_match(scope, source_scope)
    if conflicts:
        gaps.append("source_scope_mismatch")
    if missing:
        gaps.append("source_scope_missing")
    normalized = view.generated_path("normalized_dir", "sources/normalized") + "/" + owner("normalize_sources").safe_source_id(source_id) + ".md"
    raw = view.read(normalized, 16_777_216)
    metadata = None
    if raw is not None:
        try:
            metadata, _body, error = owner("_normalized_contract").split_record(raw.decode("utf-8"))
        except UnicodeError:
            metadata, error = None, "normalized_encoding_invalid"
        if error or metadata is None:
            gaps.append("normalized_record_invalid")
    decision = owner("_usage_gate").source_decision(view.root, view.config, source_id,
        [record, metadata or {}], normalized=raw, uses=["retrieval"], purpose="research")
    if decision.get("eligible") is not True:
        gaps.extend(decision.get("reasons") or ["current_usage_denied_or_unknown"])
    protected = owner("_evidence_usage").configured(view.config)
    if protected and (decision.get("complete") is not True or not decision.get("source_revision")):
        gaps.append("current_usage_revision_unqualified")
    return {"source_id": source_id, "current_ready": not gaps, "gaps": sorted(set(gaps)),
        "inspection": inspection, "usage": decision,
        "permission_basis": "native_current_usage" if protected else "legacy_compatibility",
        "source_revision": decision.get("source_revision"),
        "material_basis": digest({"source_id": source_id, "originals": inspection.get("raw_sha256", {}),
            "normalized_sha256": inspection.get("normalized_sha256"), "scope": source_scope}),
        "semantic_adequacy": "not_evaluated", "evidence_accepted": False}


def _coverage_blockers(view, slug):
    relative = view.generated_path("coverage_dir", "sources/coverage") + "/" + slug + ".yml"
    raw = view.read(relative)
    if raw is None:
        return []
    document = yaml_document(raw)
    if type(document) is not dict:
        refuse("REQUEST_COVERAGE_INVALID", "/coverage")
    blockers = []
    for facet in document.get("required_facets", []) + document.get("optional_facets", []):
        if type(facet) is not dict:
            refuse("REQUEST_COVERAGE_INVALID", "/coverage")
        blockers.extend(_ids(facet.get("blocking_request_ids", [])))
    return list(dict.fromkeys(blockers))


def current(root, *, request_ids=(), question_slugs=(), max_records=200):
    """Read historical request state and independently current source readiness."""
    root, _ = _host(root, "current")
    request_ids, question_slugs = _ids(request_ids), _ids(question_slugs)
    if type(max_records) is not int or not 1 <= max_records <= 200:
        refuse("REQUEST_SELECTION_BOUND", "/max_records")
    before = capture_revision(root, excluded=(".locks", "raw/.locks"))["revision_id"]
    view = _ScopedView(root)
    effective = read_effective(root)
    questions = _questions(root, view, effective)
    if not set(question_slugs) <= set(questions):
        refuse("REQUEST_FOREIGN_QUESTION", "/question_slugs")
    _path, records = _records(root, view)
    native_requests = owner("source_requests")
    native_requests._checked_request_paths(root, view.config,
        question_slugs=list(questions), source_ids=[row["source_id"] for row in records
            if row.get("status") == "fulfilled" and type(row.get("source_id")) is str])
    lookup = {row["request_id"]: row for row in records}
    if not set(request_ids) <= set(lookup):
        refuse("REQUEST_UNKNOWN", "/request_ids")
    selected = [row for row in records if (not request_ids or row["request_id"] in request_ids)
        and (not question_slugs or set(row["question_slugs"]) & set(question_slugs))]
    truncated = len(selected) > max_records
    results = []
    for record in selected[:max_records]:
        scope = _scope(record.get("scope"))
        required = _required_scope(effective, questions, record["question_slugs"])
        conflicts = {key for key in required if key in scope and scope[key] != required[key]}
        merged = {**scope, **required}
        source = _source(view, record.get("source_id"), merged) if record["status"] == "fulfilled" else None
        gaps = ["request_scope_conflicts_with_requirements"] if conflicts else []
        if not record["question_slugs"]:
            gaps.append("request_question_scope_missing")
        if source is None:
            gaps.append("current_source_unassigned")
        else:
            gaps.extend(source["gaps"])
        results.append({"request_id": record["request_id"], "recorded_status": record["status"],
            "recorded_source_id": record.get("source_id"), "question_slugs": record["question_slugs"],
            "scope": scope, "required_scope": required, "query_or_identifier": record["query_or_identifier"],
            "current_ready": not gaps, "gaps": sorted(set(gaps)), "source": source,
            "next_operation": "native_question_check" if not gaps else "acquire_or_recheck_permitted_evidence"})
    by_request = {row["request_id"]: row for row in results}
    question_rows = []
    for slug in question_slugs or sorted(questions):
        page = questions[slug]["frontmatter"]
        blockers = list(dict.fromkeys(_ids(page.get("blocking_request_ids", [])) + _coverage_blockers(view, slug)))
        unresolved = [identifier for identifier in blockers if identifier not in by_request
            or slug not in by_request[identifier]["question_slugs"] or not by_request[identifier]["current_ready"]]
        question_rows.append({"question_slug": slug, "recorded_status": page.get("status"),
            "blocking_request_ids": blockers, "unresolved_request_ids": unresolved,
            "current_ready": bool(blockers) and not unresolved,
            "source_ids": list(dict.fromkeys(by_request[identifier]["recorded_source_id"] for identifier in blockers if identifier not in unresolved))})
    observation = view.finish()
    after = capture_revision(root, excluded=(".locks", "raw/.locks"))["revision_id"]
    if before != after:
        refuse("REQUEST_INPUTS_CHANGED", "/observation")
    result = {"schema_version": SCHEMA, "requests": results, "questions": question_rows,
        "selection": {"returned": len(results), "total": len(selected), "truncated": truncated},
        "workspace_revision": before, "observation": observation,
        "authorized": False, "evidence_accepted": False, "semantic_adequacy": "not_evaluated"}
    if len(canonical(result)) > 1_048_576:
        refuse("REQUEST_OUTPUT_BOUND", "/result")
    return detached(result)


def _call(root, stem, seam, **values):
    current_context().require_grant(stem + "." + seam)
    return call_seam(lambda name: owner(name), stem, seam, root, **values)


def _guard(root, observed):
    current = capture_revision(root, excluded=(".locks", "raw/.locks"))["revision_id"]
    if current != observed:
        refuse("REQUEST_INPUTS_CHANGED", "/before_write")


def link_current(root, *, request_id, question_slugs, scope=None):
    root, _context = _host(root, "link_current", write=True)
    question_slugs, declared = _ids(question_slugs), _scope(scope)
    view = current(root, request_ids=[request_id])
    row = view["requests"][0]
    if row["recorded_status"] != "open" or row["scope"] != declared:
        refuse("REQUEST_LINK_SCOPE_CONFLICT", "/scope")
    all_questions = {row["question_slug"] for row in view["questions"]}
    if not question_slugs or not set(question_slugs) <= all_questions:
        refuse("REQUEST_FOREIGN_QUESTION", "/question_slugs")
    # The native store owns exact linking, delegated-order freeze and logging.
    return _call(root, "source_requests", "run_link_request", request_id=request_id,
        question_slugs=question_slugs, scope=declared,
        _before_write=lambda: _guard(root, view["workspace_revision"]))


def fulfill_current(root, *, request_id, source_id, match_scope=None, require_scope=True, needs_complete=True):
    root, _context = _host(root, "fulfill_current", write=True)
    if require_scope is not True or type(needs_complete) is not bool:
        refuse("REQUEST_SCOPE_WEAKENING", "/require_scope")
    _ids([request_id])
    if type(source_id) is not str or not source_id.strip() or len(source_id) > 512:
        refuse("REQUEST_SOURCE_INVALID", "/source_id")
    observed = current(root, request_ids=[request_id])
    row = observed["requests"][0]
    declared = _scope(match_scope)
    if declared and declared != row["scope"]:
        refuse("REQUEST_SCOPE_CONFLICT", "/match_scope")
    merged = {**row["scope"], **row["required_scope"]}
    source_view = _ScopedView(root)
    source = _source(source_view, source_id, merged)
    source_view.finish()
    if not source["current_ready"]:
        refuse("REQUEST_EVIDENCE_NOT_READY", "/source")
    if row["recorded_status"] == "fulfilled" and row["recorded_source_id"] != source_id:
        refuse("REQUEST_ALREADY_FULFILLED", "/source_id")
    if not row["question_slugs"] or any(key in row["scope"] and row["scope"][key] != value
            for key, value in row["required_scope"].items()):
        refuse("REQUEST_SCOPE_CONFLICT", "/scope")
    def before_write():
        _guard(root, observed["workspace_revision"])
        rechecked = _ScopedView(root)
        actual = _source(rechecked, source_id, merged)
        rechecked.finish()
        if not actual["current_ready"] or actual["material_basis"] != source["material_basis"]:
            refuse("REQUEST_EVIDENCE_CHANGED", "/source")
    before_write()
    if source["permission_basis"] == "native_current_usage":
        result = _call(root, "source_requests", "run_fulfill_current_request", request_id=request_id,
            source_id=source_id, match_scope=row["scope"] or None, _before_write=before_write)
    else:
        result = _call(root, "source_requests", "run_fulfill_request", request_id=request_id,
            source_id=source_id, match_scope=row["scope"] or None, require_scope=True, _before_write=before_write)
    return {"schema_version": "evidence-request-fulfillment/v1", "native": result,
        "source": source, "current_ready": not result.get("contingent", False),
        "authorized": False, "evidence_accepted": False, "semantic_adequacy": "not_evaluated"}


def block_current(root, *, slug, blocking_request_ids, reason):
    root, context = _host(root, "block_current", write=True)
    _ids([slug])
    blocking_request_ids = _ids(blocking_request_ids)
    if not blocking_request_ids or type(reason) is not str or not reason.strip() or len(reason.encode()) > 16384:
        refuse("REQUEST_BLOCK_INVALID", "/blocking_request_ids")
    observed = current(root, question_slugs=[slug])
    rows = {row["request_id"]: row for row in observed["requests"]}
    if any(identifier not in rows or slug not in rows[identifier]["question_slugs"] for identifier in blocking_request_ids):
        refuse("REQUEST_BLOCK_SCOPE_CONFLICT", "/blocking_request_ids")
    return _call(root, "question_resolve", "run_block", slug=slug, agent_id=context.actor_ref,
        blocked_reason=reason, request_id=blocking_request_ids, allow_unclaimed=False,
        _before_write=lambda: _guard(root, observed["workspace_revision"]))


def reopen_current(root, *, slug, needs_complete=True):
    root, context = _host(root, "reopen_current", write=True)
    _ids([slug])
    if type(needs_complete) is not bool:
        refuse("REQUEST_ARGUMENT_INVALID", "/needs_complete")
    observed = current(root, question_slugs=[slug])
    question = observed["questions"][0]
    if question["recorded_status"] != "blocked" or not question["current_ready"]:
        refuse("REQUEST_BLOCKERS_NOT_READY", "/question")
    def before_write(usage_view=None, config=None):
        _guard(root, observed["workspace_revision"])
        checked = current(root, question_slugs=[slug])["questions"][0]
        if not checked["current_ready"] or checked["blocking_request_ids"] != question["blocking_request_ids"]:
            refuse("REQUEST_BLOCKERS_CHANGED", "/question")
        if usage_view is not None:
            usage_view.revalidate(root, config)
            records = _ScopedView(root).manifest()
            for source_id in question["source_ids"]:
                normalized = owner("_usage_gate").read_workspace_file(root,
                    owner("_usage_gate").normalized_relative(config, source_id))
                metadata, _body, error = owner("_normalized_contract").split_record(normalized.decode("utf-8"))
                decision = owner("_usage_gate").source_decision(root, config, source_id,
                    [records[source_id], metadata or {}], view=usage_view, normalized=normalized)
                if error or decision.get("eligible") is not True or decision.get("complete") is not True:
                    refuse("REQUEST_EVIDENCE_CHANGED", "/source")
            usage_view.revalidate(root, config)
    config = _ScopedView(root).config
    usage = owner("_evidence_usage")
    if usage.configured(config):
        with usage.current_view(root, config) as usage_view:
            def guarded():
                before_write(usage_view, config)
            guarded()
            return _call(root, "question_resolve", "run_reopen", slug=slug, agent_id=context.actor_ref,
                source_id=question["source_ids"], request_id=question["blocking_request_ids"],
                require_decisive_scope=True, _before_write=guarded)
    before_write()
    return _call(root, "question_resolve", "run_reopen", slug=slug, agent_id=context.actor_ref,
        source_id=question["source_ids"], request_id=question["blocking_request_ids"],
        require_decisive_scope=True, _before_write=before_write)
