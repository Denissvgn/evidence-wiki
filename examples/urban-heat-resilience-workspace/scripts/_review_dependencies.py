#!/usr/bin/env python3
"""Native current dependency projection with separate semantic and raw custody."""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

from _evidence_revision import MAX_DEPTH, MAX_FILES, canonical_bytes, content_id
from _workspace_module_loader import load_workspace_module

# These are persisted native custody documents, not effective semantic owners.
# Recognition is by exact discriminator; ordinary strings and field-name
# resemblance confer neither authority nor a lifecycle classification.
_NATIVE_CUSTODY_SCHEMAS = frozenset({
    "evidence-authentication/v1", "evidence-trust-policy/v1", "evidence-usage-state/v1",
    "evidence-strict-authorship/v1", "evidence-strict-authorship/v2",
    "evidence-strict-review/v1", "evidence-strict-review/v2", "evidence-strict-review/v3",
    "evidence-strict-result/v1", "evidence-strict-result/v2", "evidence-strict-result/v3",
    "evidence-strict-publication/v1", "evidence-strict-publication/v2", "evidence-strict-publication/v3",
    "evidence-strict-review-status/v1", "evidence-strict-review-status/v2", "evidence-strict-action/v1",
    "evidence-workspace-revision/v1", "evidence-claim-diagnostics/v1",
    "evidence-snapshot/v1", "evidence-snapshot/v2", "evidence-snapshot/v3", "evidence-snapshot/v4",
    "evidence-snapshot-manifest/v1", "evidence-snapshot-manifest/v2", "evidence-snapshot-manifest/v3", "evidence-snapshot-manifest/v4",
    "evidence-snapshot-selection/v1", "evidence-snapshot-selection/v2",
    "evidence-snapshot-contract/v1", "evidence-snapshot-contract/v2", "evidence-snapshot-contract/v3", "evidence-snapshot-contract/v4",
    "evidence-review-delivery-binding/v1", "research-execution-journal/v1", "research-execution-observation/v1",
    "evidence-assessment/v1", "evidence-assessment-refresh/v1", "evidence-availability-receipt/v1", "evidence-temporal-result/v1",
    "execution-accounting-observation/v1", "execution-coverage-closure/v1", "execution-accounting-verification/v1", "execution-accounting-report/v2",
    "research-host-assembled-review/v1", "research-host-assembled-review/v2", "research-host-assembled-review/v3",
    "research-complete-review-delivery/v1", "research-complete-review-delivery/v2", "research-complete-review-delivery/v3",
    "research-review-sections/v1", "research-review-sections/v2", "research-review-sections/v3",
    "research-blinded-review-input/v1", "research-blinded-review-input/v2", "research-blinded-review-input/v3",
    "research-host-job/v1", "research-host-job/v2", "research-host-job/v3", "research-host-job/v4", "research-host-job/v5", "research-host-job/v6",
    "research-host-result/v1", "research-decision/v1", "research-host-qualification/v1",
    "research-protected-host-qualification/v1", "research-agent-qualification/v1",
    "research-review-service-status/v1", "research-review-service-result/v1",
    "research-mutation-intent/v1", "research-mutation-state/v1", "research-mutation-receipt/v1",
    "research-mutation-revision/v1", "research-mutation-inspection/v1", "research-mutation-reconciliation/v1",
    "research-review-context/v1", "research-review-context/v2", "research-review-context/v3", "research-review-context/v4",
    "research-review-context-binding/v1", "research-review-context-binding/v2", "research-review-context-binding/v3", "research-review-context-binding/v4",
    "research-paged-review/v1", "research-paged-review/v2", "research-paged-review/v3", "research-review-page/v1",
    "research-review-packet/v1", "research-review-packet/v2", "research-review-packet/v3",
    "research-complete-review-material/v1", "research-dependency-review-material/v1",
})

SCRIPT_DIR = Path(__file__).resolve().parent
_SIBLINGS = {}
MATERIAL = "research-dependency-review-material/v1"
GLOBAL = "research-global-dependency-material/v1"
REASONS = frozenset({
    "strict_coverage_required", "strict_claim_unresolved", "strict_evidence_missing",
    "strict_source_revision_changed", "strict_source_time_unconfirmed", "strict_source_age_invalid",
    "strict_capture_insufficient", "strict_grounding_failed", "strict_computation_missing_or_stale",
    "strict_computation_value_mismatch", "strict_computation_source_missing",
})


def sibling(name):
    return load_workspace_module(SCRIPT_DIR, name, cache=_SIBLINGS)


def require(condition, reason):
    if not condition:
        raise sibling("_strict_evidence").refusal(reason)


def _semantic_value(value):
    """Refuse recognized native custody even when nested in a selected value."""
    pending = [(value, 0, frozenset())]
    count = 0
    while pending:
        item, depth, parents = pending.pop()
        count += 1
        require(count <= 65_536 and depth <= 64, "selective_selected_value_bound")
        if type(item) not in {dict, list}:
            continue
        require(id(item) not in parents, "selective_selected_value_cycle")
        parents = parents | {id(item)}
        if type(item) is dict:
            schema = item.get("schema_version")
            require(type(schema) is not str or schema not in _NATIVE_CUSTODY_SCHEMAS,
                    "selective_selected_custody_unsupported")
            pending.extend((child, depth + 1, parents) for child in item.values())
        else:
            pending.extend((child, depth + 1, parents) for child in item)
    return value


def _selected_bytes(path, raw):
    """Inspect actual native structured encodings without interpreting prose."""
    try:
        decoded = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        return
    try:
        value = sibling("_record_artifacts").strict_json(raw)
    except ValueError:
        value = None
    if type(value) in {dict, list}:
        _semantic_value(value)
        return
    # Native append-only stores declare JSONL encoding. Do not reinterpret a
    # prose/code example line as an independently persisted native artifact.
    if path.endswith(".jsonl"):
        for line in raw.splitlines():
            if not line.strip():
                continue
            value = sibling("_record_artifacts").strict_json(line)
            _semantic_value(value)
    if path.endswith((".yml", ".yaml")):
        value = sibling("_yaml_safe").safe_load_current(decoded)
        _semantic_value(value)
    elif decoded.startswith(("---\n", "---\r\n", "---\r")):
        frontmatter, _body = sibling("_question_dependencies").parse_page(raw)
        _semantic_value(frontmatter)


def _typed(value):
    return sibling("_question_dependencies").typed_node(_semantic_value(value))


def _ordered(values):
    return sorted(values, key=lambda value: value.encode("utf-8"))


def _hash(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _workspace_path(value):
    """Qualify captured coordinates without imposing artifact naming rules."""
    require(type(value) is str and bool(value), "selective_workspace_path_invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        require(False, "selective_workspace_path_invalid")
    parts = value.split("/")
    require(len(encoded) <= 4096 and len(parts) <= MAX_DEPTH + 1, "selective_workspace_path_bound")
    require(all(part not in {"", ".", ".."} and not part.endswith((".", " "))
                and not any(character in part for character in '\\:<>"|?*')
                and not any(ord(character) < 32 or ord(character) == 127 for character in part)
                for part in parts), "selective_workspace_path_invalid")
    return value


def _commit(path, raw, *, workspace=False):
    if workspace:
        _workspace_path(path)
    else:
        sibling("_record_artifacts").artifact_path(path)
    _selected_bytes(path, raw)
    return {"path": path, "sha256": _hash(raw)}


def closure(manifest, claim_id):
    """Return subject-first DFS, refusing cycles, omissions and a 33rd record."""
    sibling("_strict_contract").claims_document(manifest)
    by_id = {claim["id"]: claim for claim in manifest["claims"]}
    require(claim_id in by_id, "selective_claim_unknown")
    result, seen, active = [], set(), set()

    def visit(identifier):
        require(identifier in by_id and identifier not in active, "selective_premise_missing_or_cyclic")
        if identifier in seen:
            return
        require(len(result) < 32, "selective_claim_closure_bound")
        active.add(identifier)
        seen.add(identifier)
        result.append(by_id[identifier])
        for premise in by_id[identifier]["premises"]:
            visit(premise)
        active.remove(identifier)

    visit(claim_id)
    return result


def _owner_inputs():
    bridge = sys.modules.get("evidence_wiki.host_context")
    require(bridge is not None, "selective_host_inputs_unavailable")
    getter = getattr(bridge, "review_dependency_inputs", None)
    producer_getter = getattr(bridge, "current_review_producer", None)
    require(callable(getter) and callable(producer_getter), "selective_host_inputs_unavailable")
    inputs, producer = getter(), producer_getter()
    require(inputs is not None and producer is not None, "selective_host_inputs_unavailable")
    return inputs, producer


def _deterministic_policy(config):
    """Inspect the validated primitive graph, never text-match declarations."""
    native = sibling("_policy_primitives")
    excluded = sibling("_question_dependencies").COORDINATION | sibling("_question_dependencies").MIRRORS

    def field(reference):
        if reference is None or reference.root is not None:
            return
        pointer = reference.pointer
        require(type(pointer) is str and pointer.startswith("/") and len(pointer) > 1,
                "selective_question_policy_read_unsupported")
        first = pointer[1:].split("/", 1)[0].replace("~1", "/").replace("~0", "~")
        require(first not in excluded, "selective_question_policy_read_unsupported")

    def visit(primitive):
        require(primitive.name in native.PRIMITIVE_NAMES, "selective_policy_primitive_unsupported")
        require(primitive.name != "max_age", "selective_volatile_policy_unsupported")
        field(primitive.field)
        for operand in (primitive.operand, primitive.minimum, primitive.maximum):
            if operand is not None:
                field(operand.ref)
        for child in primitive.children:
            visit(child)

    for rule in native.pack_policy_rules(config).values():
        visit(rule.composition)


def _requirements(root, capture):
    bridge = sibling("_requirements_bridge")
    require(bridge.PATH in capture.files, "selective_requirements_missing")
    # The existing owner validates the complete declaration and authenticated
    # lineage. No caller-provided rendition supplies effective requirements.
    from evidence_wiki.requirements_operations import read_effective
    value = read_effective(root, raw=capture.files[bridge.PATH])
    require(type(value) is dict and set(value) == {"schema_version", "request", "decisions"}
            and value["schema_version"] == bridge.BASE, "selective_requirements_unsupported")
    return value


def _coverage(root, config, capture, manifest, view):
    native = sibling("coverage_manifest")
    result, summaries = {}, {}
    with capture.materialize() as captured, sibling("_publication_context").authorized_capture(captured, config, view):
        for question in sorted(manifest["questions"], key=lambda row: row["slug"].encode("utf-8")):
            slug = question["slug"]
            path = native.manifest_path(captured, config, slug)
            relative = path.relative_to(captured).as_posix()
            require(relative in capture.files, "selective_coverage_missing")
            document = native.load_yaml_mapping(path, error_code="COVERAGE_MANIFEST_INVALID")
            vocabularies = native.merged_policy_vocabularies(config)
            native.validate_manifest(document, expected_slug=slug, policy_vocabularies=vocabularies)
            summary = native.coverage_summary_for_question(captured, config, slug, {"coverage_required": True})
            require(summary.get("coverage_status") != "invalid", "selective_coverage_unsupported")
            result[slug] = {"manifest": document, "summary": summary}
            summaries[slug] = summary
    return result, summaries


def revision_artifact_path(revision, path, workspace_paths=()):
    """Name authenticated retained revision bytes outside workspace coordinates."""
    require(type(revision) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", revision) is not None,
            "selective_source_revision_invalid")
    sibling("_record_artifacts").artifact_path(path)
    logical = "source-revisions/" + revision[7:] + "/" + path
    sibling("_record_artifacts").artifact_path(logical)
    folded = logical.casefold()
    require(not any(name.casefold() == folded or name.casefold().startswith(folded + "/")
                    or folded.startswith(name.casefold() + "/") for name in workspace_paths),
            "selective_source_coordinate_collision")
    return logical


def _source_material(root, config, capture, view):
    require(view is not None, "selective_source_authority_unavailable")
    publication = sibling("_publication_usage")
    qualified = publication.qualify_capture(capture.files, config, view, purpose="qa-export", consumer="evidence-wiki")
    manifest_path = config.get("sources", {}).get("manifest_path", "sources/manifest.jsonl")
    records = {}
    for line in capture.files[manifest_path].splitlines():
        if line.strip():
            record = sibling("_record_artifacts").json_document(line)
            require(record["id"] not in records, "selective_source_duplicate")
            records[record["id"]] = record
    require(set(records) == set(qualified["selected"]), "selective_source_accounting_incomplete")
    rows, details, byte_values = [], [], {}
    byte_values[manifest_path] = _commit(manifest_path, capture.files[manifest_path])
    folded_paths = {manifest_path.casefold(): manifest_path}

    def add(path, raw):
        value = _commit(path, raw)
        require(path.casefold() not in folded_paths or folded_paths[path.casefold()] == path, "selective_source_coordinate_collision")
        folded_paths[path.casefold()] = path
        require(path not in byte_values or byte_values[path] == value, "selective_source_byte_collision")
        byte_values[path] = value
        return value

    def semantic_revision(revision):
        original = view.state.revisions[revision]
        required = {"source_id", "files", "descriptor", "grant", "scrub", "event_id", "observed_at"}
        require(required <= original.keys() <= required | {"authorization_event_id"}, "selective_source_state_unsupported")
        # Native validators own grant/scrub exact payload shape and authentication;
        # signatures, ledger IDs and current probe clocks remain private.
        grant = sibling("_evidence_usage").validate_grant(original["grant"], original["source_id"], revision)
        scrub = sibling("_evidence_usage").validate_scrub(original["scrub"], revision, grant)
        descriptor = sibling("_evidence_usage").source_descriptor(original["files"])
        return original, descriptor, grant, scrub

    for identifier in _ordered(records):
        revision = qualified["selected"][identifier]
        original, descriptor, grant, scrub = semantic_revision(revision)
        relative = sibling("_usage_gate").normalized_relative(config, identifier)
        require(relative in capture.files, "selective_normalized_source_missing")
        metadata, _body = sibling("verify_quotes").split_page(capture.files[relative].decode("utf-8", errors="strict"))
        raw = publication.qualify_raw_files(capture.files, records[identifier], original)
        raw_paths = _ordered(path for path in raw if not path.endswith(".provenance.yml"))
        provenance_paths = _ordered(path for path in raw if path.endswith(".provenance.yml"))
        for path, data in raw.items():
            add(path, data)
        add(relative, capture.files[relative])
        structured = sibling("_structured_view").structured_view_binding(metadata)
        if structured is not None:
            require(structured[0] in capture.files and _hash(capture.files[structured[0]]) == structured[1],
                    "selective_structured_source_changed")
            add(structured[0], capture.files[structured[0]])
        ancestors, pending = set(), list(descriptor["parents"])
        while pending:
            ancestor = pending.pop()
            require(ancestor in view.state.revisions, "selective_source_ancestry_unsupported")
            if ancestor in ancestors:
                continue
            require(len(ancestors) < 128, "selective_source_ancestry_unsupported")
            ancestors.add(ancestor)
            pending.extend(view.state.revisions[ancestor]["descriptor"]["parents"])
        ancestry = []
        for ancestor in _ordered(ancestors):
            parent, parent_descriptor, parent_grant, parent_scrub = semantic_revision(ancestor)
            files = []
            for path, data in sorted(parent["files"].items(), key=lambda row: row[0].encode("utf-8")):
                logical = revision_artifact_path(ancestor, path, capture.files)
                files.append(add(logical, data))
            ancestry.append({"source_revision": ancestor, "source_id": parent["source_id"],
                             "descriptor": _typed(parent_descriptor), "grant": _typed(parent_grant),
                             "scrub": _typed(parent_scrub), "files": files})
        # Commit every retained admission artifact, including descriptor bytes and
        # auxiliary records not represented by a normalized workspace path.
        for path, data in original["files"].items():
            logical = revision_artifact_path(revision, path, capture.files)
            add(logical, data)
        rows.append({"source_id": identifier, "source_revision": revision, "normalized_path": relative,
                     "raw_paths": raw_paths, "provenance_paths": provenance_paths,
                     "ancestor_revision_ids": _ordered(ancestors)})
        details.append({"source_id": identifier, "source_revision": revision,
                        "manifest_record": _typed(records[identifier]), "normalized_metadata": _typed(metadata),
                        "revision_descriptor": _typed(descriptor), "grant": _typed(grant), "scrub": _typed(scrub), "ancestry": ancestry})
    return rows, details, byte_values


def _proposal(manifest, requirements):
    originals = {}
    for question in manifest["questions"]:
        originals.setdefault(question["original_id"], []).append(question["slug"])
    declared = {row["id"]: row for row in requirements["request"]["payload"]["questions"]}
    return {"schema_version": "evidence-draft-proposal/v1", "inventory": manifest,
            "originals": [{"original_id": identifier, "text": declared[identifier]["text"], "question_slugs": slugs}
                          for identifier, slugs in originals.items()]}


def _mechanical(root, config, policy, capture, manifest, claims, requirements, view):
    draft = sibling("_draft_admission")
    proposal = _proposal(manifest, requirements)
    scopes = draft.originals(capture, root, config, policy, proposal)
    local = {**proposal, "inventory": {**manifest, "claims": claims}}
    rows, _binding = draft.iter_source_diagnostics(root, config, policy, capture, local, view.now, scopes, _live_view=view)
    by_claim = {}
    for row in rows:
        public = {key: row[key] for key in ("evidence_index", "source_id", "record_sha256", "location_hint", "anchor_pointer", "form", "result", "reason")}
        public["mechanical"] = sibling("_claim_diagnostics")._mechanical(row["mechanical"])
        public["current_usage"] = ({"eligible": row["usage"].get("eligible") is True,
                                     "source_revision": row["usage"].get("source_revision"),
                                     "complete": row["usage"].get("complete") is True}
                                    if row.get("usage") is not None else None)
        by_claim.setdefault(row["claim_id"], []).append(public)
    originals = {question["slug"]: question["original_id"] for question in manifest["questions"]}
    result = []
    for claim in claims:
        evidence = by_claim.get(claim["id"], [])
        result.append({"claim_id": claim["id"], "question_slug": claim["question_slug"], "original_id": originals[claim["question_slug"]],
                       "qualification": claim["qualification"], "scope": claim["scope"], "time": claim["time"], "units": claim["units"],
                       "premises": list(claim["premises"]), "evidence": evidence,
                       "mechanical_result": "verified" if evidence and all(row["result"] == "verified" for row in evidence) else "not_established",
                       "semantic_approval": "not_evaluated", "accepted": False})
    return result


def _facts(root, config, policy, capture, claims, summaries, computation, view):
    """Derive current facts directly; review/history evaluation is never called."""
    verifier, strict = sibling("verify_quotes"), sibling("_strict_evidence")
    timestamp = sibling("_evidence_authority").timestamp
    output = []
    with capture.materialize() as captured, sibling("_publication_context").authorized_capture(captured, config, view):
        for claim in claims:
            reasons = set()
            if summaries[claim["question_slug"]].get("coverage_verdict") != "pass":
                reasons.add("strict_coverage_required")
            if claim["qualification"] in {"contested", "insufficient_evidence"}:
                reasons.add("strict_claim_unresolved")
            if not claim["evidence"]:
                reasons.add("strict_evidence_missing")
            for evidence in claim["evidence"]:
                path, _label = verifier.normalized_record_path(captured, config, evidence["source_id"])
                relative = path.relative_to(captured).as_posix()
                if relative not in capture.files or _hash(capture.files[relative]) != evidence["record_sha256"]:
                    reasons.add("strict_source_revision_changed")
                else:
                    metadata, _body = verifier.normalized_record_content(path)
                    provenance = metadata.get("provenance", {})
                    observed = provenance.get("retrieved_at") if type(provenance) is dict else None
                    if observed is None or timestamp(observed) != timestamp(evidence["observed_at"]):
                        reasons.add("strict_source_time_unconfirmed")
                age = (view.now - timestamp(evidence["observed_at"])).total_seconds()
                if not 0 <= age <= policy["max_source_age_seconds"]:
                    reasons.add("strict_source_age_invalid")
                if evidence["capture"] not in {"primary", "excerpt"}:
                    reasons.add("strict_capture_insufficient")
            try:
                entries = verifier.grounding_entries({"grounding": [strict.grounding_entry(claim, item) for item in claim["evidence"]]}, claim["question_slug"])
                checks = [verifier.verify_entry(captured, config, entry) for entry in entries]
                if not checks or any(check["result"] != "verified" for check in checks):
                    reasons.add("strict_grounding_failed")
            except (ValueError, OSError, sibling("_script_errors").ScriptRefusal):
                reasons.add("strict_grounding_failed")
            for calculation in claim.get("calculations", []):
                if computation is None or calculation["result_id"] != computation["result_id"]:
                    reasons.add("strict_computation_missing_or_stale")
                    continue
                resolved = sibling("_structured_view").resolve_pointer(computation, calculation["pointer"])
                value = resolved.value if resolved.ok else None
                if (type(value) is not dict or value.get("type") != "decimal"
                        or value.get(calculation["form"]) != calculation["expected"]
                        or value.get("unit") != calculation["unit"] or value.get("rounded") != calculation["rounded"]):
                    reasons.add("strict_computation_value_mismatch")
                    continue
                sources = {computation["lineage"][identifier]["source_id"] for identifier in value["lineage"]
                           if "source_id" in computation["lineage"][identifier]}
                if not sources <= {item["source_id"] for item in claim["evidence"]}:
                    reasons.add("strict_computation_source_missing")
            require(reasons <= REASONS, "selective_mechanical_reason_unsupported")
            output.append({"claim_id": claim["id"], "reasons": _ordered(reasons)})
    return output


def _sources(documents):
    require(type(documents) is list and len(documents) <= 64, "selective_source_document_bound")
    result = {}
    for source in documents:
        require(type(source) is dict and set(source) == {"source_id", "source_revision", "record_sha256", "metadata", "content"},
                "selective_source_document_unsupported")
        identifier = source["source_id"]
        require(identifier not in result, "selective_source_document_duplicate")
        require(type(source["content"]) is str and len(source["content"]) <= 65_536, "selective_source_content_bound")
        source["content"].encode("utf-8", errors="strict")
        result[identifier] = {**source, "metadata": _typed(source["metadata"])}
    return result


def _counter(value, claims, questions, sources):
    require(type(value) is dict and set(value) == {"state", "questions", "slices", "receipt_semantics", "search_completeness", "limitations"},
            "selective_counter_unsupported")
    require(value["state"] in {"current_native_instruction", "not_observed"} and value["search_completeness"] == "not_established"
            and value["receipt_semantics"] == [], "selective_counter_unsupported")
    rows = []
    for row in value["questions"]:
        require(type(row) is dict and set(row) == {"question_slug", "claim_ids", "scope", "criteria", "alternative_sources"},
                "selective_counter_unsupported")
        require(row["claim_ids"] == [claim["id"] for claim in claims if claim["question_slug"] == row["question_slug"]],
                "selective_counter_claim_changed")
        alternatives = []
        for source in row["alternative_sources"]:
            require(type(source) is dict and set(source) == {"source_id", "record_sha256", "metadata", "content"}, "selective_counter_unsupported")
            actual = sources.get(source["source_id"])
            require(actual is not None and all(actual[key] == source[key] for key in ("record_sha256", "content")), "selective_counter_source_changed")
            alternatives.append({**source, "metadata": _typed(source["metadata"])})
        rows.append({**row, "scope": _typed(row["scope"]), "criteria": _typed(row["criteria"]), "alternative_sources": alternatives})
    wanted = [row["manifest_question"]["slug"] for row in questions]
    require([row["question_slug"] for row in rows] == (wanted if value["state"] == "current_native_instruction" else []),
            "selective_counter_question_changed")
    for row in value["slices"]:
        require(type(row) is dict and set(row) == {"source_id", "record_sha256", "start", "end", "quote", "truncated", "grounding", "semantic_approval", "accepted"},
                "selective_counter_slice_unsupported")
        source = sources.get(row["source_id"])
        require(source is not None and source["record_sha256"] == row["record_sha256"]
                and type(row["start"]) is int and type(row["end"]) is int
                and 0 <= row["start"] < row["end"] <= len(source["content"])
                and source["content"][row["start"]:row["end"]] == row["quote"]
                and row["truncated"] is False, "selective_counter_slice_changed")
    return {**value, "questions": rows}


def _qualification(inputs):
    profile = inputs.public_profile()
    require(type(profile) is dict and set(profile) == {"execution_profile", "run_policy"}, "selective_qualification_unsupported")
    from evidence_wiki.agent_execution import _json
    from evidence_wiki.run_policy import RunPolicy
    run_policy = RunPolicy(profile["run_policy"])
    require(run_policy.to_dict() == profile["run_policy"], "selective_run_policy_changed")
    return {"execution_profile_sha256": _hash(_json(profile["execution_profile"])),
            "run_policy_sha256": _hash(run_policy.to_json()),
            "public_profile": {key: _typed(value) for key, value in profile.items()}}


def _computation_inputs(computation, capture):
    paths = set()
    for source in computation["sources"]:
        for key in ("record_path", "structured_path"):
            paths.add(source[key])
        paths.update(source["raw_files"])
    require(paths <= capture.files.keys(), "selective_computation_input_missing")
    return [_commit(path, capture.files[path]) for path in _ordered(paths)]


def _complete(scope, *, limitations=()):
    return {"complete": True, "truncated": False, "missing": [], "omitted": [],
            "limitations": list(limitations), "scope_qualifiers": scope}


def project(root, claim_id, *, config, policy, capture, manifest, view):
    """Build the exact eight-field semantic preimage from protected current inputs."""
    root = Path(root)
    folded_paths = {}
    require(len(capture.files) <= MAX_FILES, "selective_workspace_path_bound")
    for path in capture.files:
        _workspace_path(path)
        require(path.casefold() not in folded_paths, "selective_workspace_path_collision")
        folded_paths[path.casefold()] = path
    require(policy.get("schema_version") == "evidence-strict-policy/v2" and policy.get("review_basis_mode") == "claim_dependencies_v1",
            "selective_policy_required")
    sibling("_strict_contract").policy_document(policy)
    require(sibling("_draft_admission").configuration(capture) == config, "selective_configuration_changed")
    require(sibling("_record_artifacts").json_document(capture.files[policy["claims_path"]]) == manifest, "selective_claims_changed")
    claims = closure(manifest, claim_id)
    require(config.get("computation") is None or manifest["schema_version"] == "evidence-strict-claims/v2", "strict_computation_claim_schema_required")
    _deterministic_policy(config)
    inputs, producer = _owner_inputs()
    producer_document = producer.document()
    require(set(producer_document) == {"identity", "manifest"}, "selective_producer_unsupported")
    requirements = _requirements(root, capture)
    coverage, summaries = _coverage(root, config, capture, manifest, view)
    question_owner = sibling("_question_dependencies")
    all_questions = [question_owner.project_dependencies(root, config, policy, capture, manifest, question["slug"], claim_id, coverage_summary=summaries[question["slug"]])
                     for question in sorted(manifest["questions"], key=lambda row: row["slug"].encode("utf-8"))]
    projected_question_paths = {row["path"] for row in all_questions}
    question_dir = sibling("question_status").questions_directory(root, config).relative_to(root).as_posix() + "/"
    actual_questions = set()
    for path, raw in capture.files.items():
        if path.startswith(question_dir) and path.endswith(".md") and "/" not in path[len(question_dir):]:
            frontmatter, _body = question_owner.parse_page(raw)
            if frontmatter.get("type") == "question":
                actual_questions.add(Path(path).stem)
    require(actual_questions == {row["slug"] for row in manifest["questions"]}, "strict_question_accounting_incomplete")
    for question in all_questions:
        _semantic_value(sibling("_question_dependencies").typed_value(question["semantic_frontmatter"]))
    slugs = _ordered({claim["question_slug"] for claim in claims})
    questions = [row for row in all_questions if row["manifest_question"]["slug"] in slugs]
    inventory, records, source_bytes = _source_material(root, config, capture, view)
    all_sources = _sources(inputs.source_documents(root, config, capture, view))
    source_revisions = {row["source_id"]: row["source_revision"] for row in inventory}
    for identifier, source in all_sources.items():
        require(source_revisions.get(identifier) == source["source_revision"], "selective_source_revision_changed")
        path = sibling("_usage_gate").normalized_relative(config, identifier)
        require(path in capture.files and _hash(capture.files[path]) == source["record_sha256"], "selective_source_record_changed")
    computation = sibling("_computation_service").required(root, config, view=view) if config.get("computation") is not None else None
    computation_sources = []
    computation_bytes = []
    if computation is not None:
        computation_bytes = _computation_inputs(computation, capture)
        for row in sorted(computation["sources"], key=lambda value: value["source_id"].encode("utf-8")):
            source = all_sources.get(row["source_id"])
            require(source is not None and source["record_sha256"] == row["record_sha256"]
                    and source["source_revision"] == row["source_revision"], "selective_computation_source_changed")
            computation_sources.append(source)
    counter = _counter(inputs.counter_projection(root, config, capture, manifest, [claim["id"] for claim in claims], slugs, view), claims, questions, all_sources)
    needed = {item["source_id"] for claim in claims for item in claim["evidence"]}
    needed.update(item["source_id"] for row in counter["questions"] for item in row["alternative_sources"])
    needed.update(item["source_id"] for item in counter["slices"])
    require(needed <= all_sources.keys() and len(needed | {source["source_id"] for source in computation_sources}) <= 32, "selective_local_source_unavailable_or_large")
    local_sources = [all_sources[identifier] for identifier in _ordered(needed)]
    for claim in claims:
        for evidence in claim["evidence"]:
            require(all_sources[evidence["source_id"]]["record_sha256"] == evidence["record_sha256"], "selective_evidence_source_changed")
    mechanical = _mechanical(root, config, policy, capture, manifest, claims, requirements, view)
    facts = _facts(root, config, policy, capture, claims, summaries, computation, view)
    dependencies = []

    def add(kind, coordinate, value):
        dependencies.append({"kind": kind, "coordinate": coordinate, "value": value})

    add("source_inventory", "sources", {"sources": inventory})
    for row in records:
        add("source_record", row["source_id"], row)
    for path in _ordered(source_bytes):
        add("source_bytes", path, source_bytes[path])
    add("claims_inventory", "claims", {"schema_version": manifest["schema_version"], "ids": _ordered(claim["id"] for claim in manifest["claims"])})
    add("questions_inventory", "questions", {"ids": _ordered(question["slug"] for question in manifest["questions"])})
    add("effective_requirements", "requirements", _typed(requirements))
    add("effective_coverage", "coverage", _typed(coverage))
    add("effective_policy", "policy", _typed(policy))
    add("authority", "authority", {"content_hash": view.trust["content_hash"]})
    add("qualification", "qualification", _qualification(inputs))
    add("producer", "producer", producer_document["identity"])
    instructions = []
    instruction_paths = set()
    for path in _ordered(policy["instructions"]):
        require(path in capture.files and sibling("_requirements_bridge").instruction_matches(root, path, capture.files[path], policy["instructions"][path]), "strict_instructions_changed")
        if path == sibling("_requirements_bridge").PATH:
            require(sibling("_record_artifacts").json_document(capture.files[path])["schema_version"] == "evidence-research-requirements/v1",
                    "selective_instruction_history_unsupported")
        value = _commit(path, capture.files[path])
        instructions.append({**value, "text": capture.files[path].decode("utf-8", errors="strict")})
        instruction_paths.add(path)
        if path not in source_bytes:
            add("instruction_bytes", path, value)
    computation_paths = {row["path"] for row in computation_bytes}
    if computation is not None:
        add("computation", "computation", {"definition": _typed(computation["definition"]), "result": _typed(computation), "inputs": computation_bytes})
    for path, raw in capture.files.items():
        selected = (not path.startswith(("runs/", ".locks/", question_dir + ".locks/"))
                    and path not in {"log.md", "index.md"} and path not in projected_question_paths)
        if selected and path != policy["claims_path"] and path not in source_bytes and path not in instruction_paths and path not in computation_paths:
            add("workspace_material", path, _commit(path, raw, workspace=True))
    dependencies.sort(key=lambda row: (row["kind"].encode("utf-8"), row["coordinate"].encode("utf-8")))
    require(len({(row["kind"], row["coordinate"]) for row in dependencies}) == len(dependencies), "selective_global_dependency_duplicate")
    roles = inputs.role_semantics()
    require(type(roles) is list and all(type(row) is dict and set(row) == {"task_role", "config_refs", "instructions", "output_contract"} for row in roles),
            "selective_role_semantics_unsupported")
    roles = [{**row, "output_contract": _typed(row["output_contract"])} for row in roles]
    require(type(roles) is list and roles == sorted(roles, key=lambda row: row["task_role"]), "selective_role_semantics_unsupported")
    global_material = {"global_dependencies": dependencies,
                       "worker_semantics": {"producer": producer_document, "policy": _typed(policy), "rubric": _typed(policy["rubric"]),
                           "requirements": _typed(requirements), "coverage": _typed(coverage), "instructions": instructions,
                           "computation": None if computation is None else {"definition": _typed(computation["definition"]), "result": _typed(computation),
                               "sources": computation_sources}, "roles": roles,
                           "completeness": _complete({"global_dependency_coordinates": [{"kind": row["kind"], "coordinate": row["coordinate"]} for row in dependencies],
                                                      "role_names": [row["task_role"] for row in roles], "computation_configured": computation is not None})}}
    wrapper = {"schema_version": GLOBAL, "material": global_material}
    wrapper["dependency_id"] = content_id(GLOBAL, wrapper)
    result = {"schema_version": MATERIAL, "claim_id": claim_id, "claim_schema_version": manifest["schema_version"],
              "producer_id": producer.producer_id, "claims": claims, "questions": questions, "global_material": wrapper,
              "local_context": {"sources": local_sources, "mechanical": mechanical, "counterevidence": counter, "mechanical_reasons": facts,
                  "completeness": _complete({"claim_ids": [claim["id"] for claim in claims], "question_slugs": slugs,
                      "source_ids": [source["source_id"] for source in local_sources], "counter_question_slugs": [row["question_slug"] for row in counter["questions"]]},
                      limitations=[*counter["limitations"], *[note for claim in claims for note in claim["limitations"]]])}}
    contract = sibling("_review_dependency_contract")
    result = contract.detached_json(result)
    contract.validate_material(result, complete=False)
    require(len(canonical_bytes(result)) <= 1_048_576, "selective_material_bound")
    inputs.assert_current()
    require(producer.document() == producer_document, "selective_producer_changed")
    return result


def dependency_items(preimage):
    """Reproduce every frozen item from the complete reversible preimage."""
    contract = sibling("_review_dependency_contract")
    contract.validate_material(preimage, complete=False)
    entries = []

    def add(scope, kind, coordinate, value):
        entries.append({"scope": scope, "kind": kind, "coordinate": coordinate,
                        "content_id": contract.item_id(kind, coordinate, value)})

    for row in preimage["global_material"]["material"]["global_dependencies"]:
        add("global", row["kind"], row["coordinate"], row["value"])
    for index, claim in enumerate(preimage["claims"]):
        add("local", "target_claim" if index == 0 else "premise_claim", claim["id"], claim)
    for question in preimage["questions"]:
        slug = question["manifest_question"]["slug"]
        add("local", "question_record", slug, question["manifest_question"])
        add("local", "question_page", slug, {key: value for key, value in question.items() if key != "manifest_question"})
    counter = preimage["local_context"]["counterevidence"]
    for claim, mechanical in zip(preimage["claims"], preimage["local_context"]["mechanical"], strict=True):
        add("local", "mechanical", claim["id"], mechanical)
        question = next((row for row in counter["questions"] if row["question_slug"] == claim["question_slug"]), None)
        add("local", "counterevidence", claim["id"], {"question": question, "slices": counter["slices"],
            "receipt_semantics": counter["receipt_semantics"], "state": counter["state"],
            "search_completeness": counter["search_completeness"], "limitations": counter["limitations"]})
    return sorted(entries, key=lambda row: (row["scope"].encode("utf-8"), row["kind"].encode("utf-8"), row["coordinate"].encode("utf-8")))


def finish(preimage, dependency_basis):
    """Bind a finished carrier to the independently computed native basis."""
    contract = sibling("_review_dependency_contract")
    contract.validate_material(preimage, complete=False)
    contract.validate("evidence-review-dependency-basis/v1", dependency_basis)
    require(dependency_basis["target_claim_id"] == preimage["claim_id"]
            and dependency_basis["claim_schema_version"] == preimage["claim_schema_version"]
            and dependency_basis["premise_closure"] == _ordered(claim["id"] for claim in preimage["claims"][1:])
            and dependency_basis["question_slugs"] == [row["manifest_question"]["slug"] for row in preimage["questions"]]
            and dependency_basis["semantic_material_id"] == content_id(MATERIAL, preimage)
            and dependency_basis["dependencies"] == dependency_items(preimage), "selective_basis_material_mismatch")
    target_question = next(row for row in preimage["questions"] if row["manifest_question"]["slug"] == preimage["claims"][0]["question_slug"])
    expected_role = "human-review" if target_question["canonical_mirrors"]["human_review_required"] else "evaluator"
    expected_mode = "none" if preimage["global_material"]["material"]["worker_semantics"]["computation"] is None else "configured"
    require(dependency_basis["required_review_role"] == expected_role and dependency_basis["computation_mode"] == expected_mode,
            "selective_basis_policy_mismatch")
    require(dependency_basis["basis_id"] == content_id("evidence-review-dependency-basis/v1", {key: value for key, value in dependency_basis.items() if key != "basis_id"}),
            "selective_basis_identity_mismatch")
    result = {**preimage, "dependency_basis_id": dependency_basis["basis_id"]}
    contract.validate_material(result, complete=True)
    return result
