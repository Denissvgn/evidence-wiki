"""Isolated current native observations; detached hashes establish no authority."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from evidence_wiki._pack_io import canonical, json_document, relative_path
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.errors import UsageError
from evidence_wiki.host_context import HostContext, current_context
from evidence_wiki.mutation_contracts import detached, digest
from evidence_wiki.pack_discovery import owner
from evidence_wiki.requirements_operations import REQUIREMENTS, instruction_matches, verifylineage
from evidence_wiki.retrieval_operations import _ScopedView

MAX_BYTES = 1_048_576
MAX_SOURCES = 32
SOURCE_DIMENSIONS = ("raw_content", "normalized_content", "extraction_metadata", "capture_provenance", "current_rights", "source_scope")
REQUIREMENT_DIMENSIONS = ("original_bytes", "effective_requirements", "revision_receipts")


def refuse(reason):
    raise UsageError("SOURCE_CHANGE_REFUSED", "Native change observation refused.", details={"reason": reason})


def selections(pairs):
    fields = {"before_source_id", "before_revision", "after_source_id"}
    if type(pairs) is not list or not 1 <= len(pairs) <= MAX_SOURCES:
        refuse("selection_invalid")
    identifiers = set()
    identities = set()
    for row in pairs:
        if type(row) is not dict or set(row) != fields:
            refuse("selection_invalid")
        for field in ("before_source_id", "after_source_id"):
            identifier = row[field]
            if (type(identifier) is not str or not 1 <= len(identifier) <= 256
                    or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in identifier)):
                refuse("selection_invalid")
            identifiers.add(identifier)
        if type(row["before_revision"]) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", row["before_revision"]) is None:
            refuse("selection_invalid")
        key = (row["before_source_id"], row["before_revision"], row["after_source_id"])
        if key in identities:
            refuse("selection_duplicate")
        identities.add(key)
    if len(identifiers) > MAX_SOURCES:
        refuse("distinct_source_bound")
    return detached(pairs)


def unknown(reason, *, denied=False):
    return {"availability": "denied" if denied else "unavailable", "observed_basis": None,
        "value_sha256": None, "evidence": [], "uncertainty": [reason]}


def value(part, label):
    sha = digest({"schema_version": "native-change-dimension/v1", "dimension": label, "value": part})
    return {"availability": "observed", "observed_basis": sha, "value_sha256": sha,
        "evidence": [{"ref": label, "sha256": sha}], "uncertainty": []}


def _source_scope(context, root, raw_paths, requirements, *, capture=None, record=None):
    if type(raw_paths) is not list or not raw_paths or len(raw_paths) > 16:
        return None
    allowed = requirements["request"]["payload"]["authority"]["source_scope"]
    local = []
    for item in allowed:
        if not Path(item).is_absolute():
            continue
        path = owner("_owner_paths").checked_path(root, Path(item), field="source_extent")
        if not path.is_relative_to(root):
            continue
        context.require_path(path)
        local.append((path, path.is_dir()))
    for relative in raw_paths:
        relative_path(relative)
        if not relative.startswith("raw/"):
            return None
        path = context.require_path(root / relative)
        if not any(path == extent or is_directory and path.is_relative_to(extent) for extent, is_directory in local):
            return None
    if capture is not None:
        owner("_draft_admission").original_source_paths(root, capture, record, local, raw_paths)
    return {"raw_paths": sorted(raw_paths), "scope": "frozen_original_local_extents"}


def _retained(live, context, root, source_id, revision, scope_paths, requirements, budget, *, current_revision):
    empty = {name: unknown("retained_revision_unavailable") for name in SOURCE_DIMENSIONS}
    record = live.state.revisions.get(revision)
    if record is None or record["source_id"] != source_id:
        return empty
    rights = live.check(revision, uses=["retrieval"], purpose="research", consumer="evidence-wiki")
    if rights["eligible"] is not True or rights["complete"] is not True:
        return {name: unknown("current_native_use_denied", denied=True) for name in SOURCE_DIMENSIONS}
    declared_scope = _source_scope(context, root, scope_paths, requirements)
    if declared_scope is None:
        return {name: unknown("source_extent_not_supported", denied=True) for name in SOURCE_DIMENSIONS}
    files = record["files"]
    for name, raw in files.items():
        slot = (revision, name)
        if slot not in budget["seen"]:
            budget["seen"].add(slot)
            budget["bytes"] += len(raw)
    if budget["bytes"] > MAX_BYTES:
        refuse("selected_retained_bytes_bound")
    # The source ID is known before any protected content projection. A complete
    # original path binding is still necessary before hashing retained bytes.
    normalized = record["descriptor"]["normalized_path"]
    metadata, body = {}, None
    if normalized is None:
        provenance_bytes = files.get("capture-provenance.json")
        if provenance_bytes is None:
            return {name: unknown("retained_capture_profile_unsupported") for name in SOURCE_DIMENSIONS}
        provenance = json_document(provenance_bytes)
        capture = provenance.get("host_capture")
    else:
        if normalized not in files:
            return {name: unknown("retained_normalized_missing") for name in SOURCE_DIMENSIONS}
        context.require_path(root / normalized)
        metadata, body, error = owner("_normalized_contract").split_record(files[normalized].decode("utf-8"))
        if error or metadata.get("source_id") != source_id or metadata.get("raw_paths") != scope_paths:
            return {name: unknown("retained_original_path_binding_unsupported") for name in SOURCE_DIMENSIONS}
        provenance = metadata.get("provenance", {})
        capture = provenance.get("host_capture")
    if not isinstance(capture, dict) or capture.get("schema_version") != "evidence-host-capture/v2":
        return {name: unknown("retained_capture_profile_unsupported") for name in SOURCE_DIMENSIONS}
    owner("_capture_bridge").validate(capture)
    payloads = [raw for name, raw in files.items() if name.startswith(record["descriptor"]["evidence_root"] + "/")
        and len(raw) == capture["content_bytes"] and "sha256:" + hashlib.sha256(raw).hexdigest() == capture["content_sha256"]]
    if len(payloads) != 1:
        return {name: unknown("retained_payload_binding_ambiguous") for name in SOURCE_DIMENSIONS}
    if normalized is None:
        from .source_capture_contracts import metadata as capture_metadata
        from .source_capture_operations import protected_artifacts
        if (len(scope_paths) != 1 or owner("source_inventory").stable_id(scope_paths[0]) != source_id
                or protected_artifacts(payloads[0], capture_metadata(capture), path=scope_paths[0]) != files):
            return {name: unknown("retained_capture_original_path_binding_unavailable") for name in SOURCE_DIMENSIONS}
    extraction = {name: part for name, part in metadata.items() if name != "provenance"}
    return {"raw_content": value(hashlib.sha256(payloads[0]).hexdigest(), "raw_content"),
        "normalized_content": value(body, "normalized_content") if body is not None else unknown("normalized_bytes_not_retained_in_source_revision"),
        "extraction_metadata": value(extraction, "extraction_metadata") if metadata else unknown("extraction_metadata_not_retained"),
        "capture_provenance": value(provenance, "capture_provenance"),
        "current_rights": value({"eligible": True, "complete": True, "uses": ["retrieval"], "purpose": "research",
            "consumer": "evidence-wiki", "grant": record["grant"], "scrub": record["scrub"]}, "current_rights"),
        "source_scope": value(declared_scope, "source_scope")}


def _requirements(root, raw, policy, before_revision_id):
    try:
        return _requirements_owned(root, raw, policy, before_revision_id)
    except ExecutionError:
        refuse("requirements_lineage_unavailable")


def _requirements_owned(root, raw, policy, before_revision_id):
    expected = (policy or {}).get("instructions", {}).get(REQUIREMENTS)
    if expected is None or not instruction_matches(root, raw, expected):
        refuse("protected_original_requirements_binding_required")
    document = json_document(raw)
    latest = verifylineage(root, document, check_current=True)
    if before_revision_id is None:
        prior = verifylineage(root, latest["base"], check_current=False)
    else:
        ids = [row["revision_id"] for row in latest["revisions"]]
        if before_revision_id not in ids:
            refuse("requirements_revision_unknown")
        prefix = latest["revisions"][:ids.index(before_revision_id) + 1]
        prior = verifylineage(root, {"schema_version": document["schema_version"], "base": latest["base"], "revisions": prefix}, check_current=False)
    def project(lineage):
        # Owner refs bind private whole-workspace revisions and stay private.
        declared = [{name: row[name] for name in ("revision_id", "proposal", "envelope", "decision")} for row in lineage["revisions"]]
        return {"original_bytes": value(expected, "original_bytes"),
            "effective_requirements": value(lineage["effective"], "effective_requirements"),
            "revision_receipts": value(declared, "revision_receipts")}
    return project(prior), project(latest), latest


def _current_source(live, context, root, view, records, source_id, requirements, capture):
    record = records.get(source_id)
    if record is None:
        return None, None, "selected_current_record_unavailable", False
    raw_paths = record.get("raw_paths")
    if _source_scope(context, root, raw_paths, requirements, capture=capture, record=record) is None:
        return None, None, "source_extent_not_supported", True
    captured = (record.get("provenance") or {}).get("host_capture")
    revision = ((captured or {}).get("sanitization") or {}).get("approved_revision_id")
    if record.get("kind") == "host_capture" and revision is not None:
        decision = live.check(revision, uses=["retrieval"], purpose="research", consumer="evidence-wiki")
        if decision.get("eligible") is True and decision.get("complete") is True:
            if captured.get("content_bytes", MAX_BYTES + 1) > MAX_BYTES:
                refuse("selected_current_bytes_bound")
            inspected = owner("_host_capture").inspect_record(root, record,
                read_input=lambda _root, relative, maximum: view.read(relative, min(maximum, MAX_BYTES)))
            current_raw = view.read(raw_paths[0], MAX_BYTES)
            from evidence_wiki.source_capture_contracts import metadata as capture_metadata
            from evidence_wiki.source_capture_operations import protected_artifacts
            exact = protected_artifacts(current_raw, capture_metadata(inspected["profile"]), path=raw_paths[0])
            retained = live.state.revisions.get(revision)
            if retained is None or retained["files"] != exact or retained["source_id"] != source_id:
                refuse("current_capture_revision_binding_changed")
    else:
        normalized = owner("_usage_gate").normalized_relative(view.config, source_id)
        current = view.read(normalized, MAX_BYTES)
        if current is None:
            return None, None, "selected_normalized_missing", False
        metadata, _body, error = owner("_normalized_contract").split_record(current.decode("utf-8"))
        if error:
            refuse("current_normalized_invalid")
        decision = owner("_usage_gate").source_decision(root, view.config, source_id, [record, metadata], normalized=current, view=live)
        revision = decision.get("source_revision")
    if decision.get("eligible") is not True or decision.get("complete") is not True or revision is None:
        return None, None, "current_native_use_denied", True
    return revision, raw_paths, None, False


def _observe(root, *, pairs, before_requirements_revision_id=None):
    """Collect owner-checked inputs; pure comparison grants no current acceptance."""
    pairs = selections(pairs)
    if before_requirements_revision_id is not None and (type(before_requirements_revision_id) is not str
            or not 1 <= len(before_requirements_revision_id) <= 256):
        refuse("requirements_revision_selector_invalid")
    context = current_context()
    if type(context) is not HostContext:
        refuse("host_context_required")
    context.require_grant("change_operations.current")
    root = context.require_path(Path(root)).resolve(strict=True)
    context.require_path(root / REQUIREMENTS)
    view = _ScopedView(root)
    if view.workspace != "present":
        refuse("workspace_unavailable")
    raw = view.read(REQUIREMENTS, MAX_BYTES)
    if raw is None:
        refuse("requirements_unavailable")
    revision_owner = owner("_evidence_revision")
    capture = revision_owner.capture_workspace(root)
    policy = owner("_strict_evidence").resolve_policy(root, view.config)
    usage = owner("_evidence_usage")
    if not usage.configured(view.config):
        refuse("protected_source_usage_required")
    before_requirements, after_requirements, lineage = _requirements(root, raw, policy, before_requirements_revision_id)
    records = view.manifest()
    selection = ["comparison-" + str(index + 1) for index in range(len(pairs))]
    before_sources, after_sources = {}, {}
    budget = {"bytes": 0, "seen": set()}
    with usage.current_view(root, view.config) as live:
        current_sources = {}
        for pair, reference in zip(pairs, selection, strict=True):
            for field, selected, destination in (("before_source_id", pair["before_revision"], before_sources),
                    ("after_source_id", None, after_sources)):
                identifier = pair[field]
                if identifier not in current_sources:
                    current_sources[identifier] = _current_source(live, context, root, view, records, identifier,
                        lineage["effective"], capture)
                revision, raw_paths, reason, denied = current_sources[identifier]
                if revision is None:
                    destination[reference] = {name: unknown(reason, denied=denied) for name in SOURCE_DIMENSIONS}
                    continue
                destination[reference] = _retained(live, context, root, identifier, selected or revision, raw_paths,
                    lineage["effective"], budget, current_revision=revision)
        live.revalidate(root, view.config)
        closing_before, closing_after, closing_lineage = _requirements(root, view.read(REQUIREMENTS, MAX_BYTES), policy, before_requirements_revision_id)
        if (closing_before != before_requirements or closing_after != after_requirements or closing_lineage != lineage
                or owner("_strict_evidence").resolve_policy(root, view.config) != policy
                or revision_owner.capture_workspace(root).revision_id != capture.revision_id):
            refuse("current_requirements_changed")
        view.recheck_scope()
        view.finish()
        context.require_grant("change_operations.current")
        context.require_path(root)
        now = datetime.now(timezone.utc).isoformat()
        def snapshot(sources, requirements):
            return {"schema_version": "evidence-local-change-input/v1", "selection": selection,
                "observed_basis": {"workspace_revision": None, "host_context_sha256": None, "usage_checkpoint": None,
                    "requirements_owner_receipt": None, "observed_utc": now}, "sources": sources,
                "requirements": requirements, "truncated": False}
        result = {"schema_version": "native-change-observations/v1", "before": snapshot(before_sources, before_requirements),
            "after": snapshot(after_sources, after_requirements), "owner_checks": "current_native_use_and_protected_requirements",
            "comparisons": [{"comparison_ref": reference, **pair} for reference, pair in zip(selection, pairs, strict=True)],
            "correspondence": "caller_selected_pair_not_semantic_identity",
            "semantic_impact": "not_evaluated", "remote_refresh": "not_observed", "effects_executed": 0,
            "authorized": False, "evidence_accepted": False}
        if len(canonical(result)) > MAX_BYTES:
            refuse("output_bound")
        result = _project(result)
        if (revision_owner.capture_workspace(root).revision_id != capture.revision_id
                or owner("_strict_evidence").resolve_policy(root, view.config) != policy):
            refuse("current_inputs_changed")
        context.require_grant("change_operations.current")
        context.require_path(root)
        live.revalidate(root, view.config)
        view.recheck_scope()
        view.finish()
    return result


def _dimension(before, after):
    states = {before["availability"], after["availability"]}
    state = ("identical" if before["value_sha256"] == after["value_sha256"] else "changed") if states == {"observed"} else "denied" if "denied" in states else "unavailable"
    return {"state": state, "before": before, "after": after,
        "uncertainty": sorted(set(before["uncertainty"] + after["uncertainty"]))}


def _project(observed):
    sources = []
    for selected in observed["comparisons"]:
        reference = selected["comparison_ref"]
        dimensions = {name: _dimension(observed["before"]["sources"][reference][name],
            observed["after"]["sources"][reference][name]) for name in SOURCE_DIMENSIONS}
        sources.append({**selected, "dimensions": dimensions,
            "changed_dimensions": [name for name, row in dimensions.items() if row["state"] == "changed"],
            "identical_raw_bytes_different_provenance": (dimensions["raw_content"]["state"] == "identical"
                and dimensions["capture_provenance"]["state"] == "changed")
                if all(dimensions[name]["state"] in {"identical", "changed"} for name in ("raw_content", "capture_provenance")) else None})
    result = {"schema_version": "research-local-changes/v1", "sources": sources,
        "affected_source_ids": sorted({identifier for row in sources if row["changed_dimensions"]
            for identifier in (row["before_source_id"], row["after_source_id"])}),
        "requirements": {name: _dimension(observed["before"]["requirements"][name],
            observed["after"]["requirements"][name]) for name in REQUIREMENT_DIMENSIONS},
        "observed_utc": observed["after"]["observed_basis"]["observed_utc"],
        "coverage": {"selected_pairs": len(sources), "distinct_sources": len({row[field] for row in sources
            for field in ("before_source_id", "after_source_id")}), "truncated": False,
            "scope": "explicit_selected_local_revisions", "global_absence_inferred": False},
        "correspondence": observed["correspondence"], "owner_checks": observed["owner_checks"],
        "semantic_impact": "not_evaluated", "remote_refresh": "not_observed", "effects_executed": 0,
        "authorized": False, "evidence_accepted": False}
    if len(canonical(result)) > MAX_BYTES:
        refuse("output_bound")
    return detached(result)


def current(root, *, pairs, before_requirements_revision_id=None):
    """Compare exact selected native revisions, without fetching or approving."""
    return _observe(root, pairs=pairs, before_requirements_revision_id=before_requirements_revision_id)
