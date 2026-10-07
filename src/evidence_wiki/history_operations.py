"""Authenticated retained decisions are history, never current approval."""

import re
from dataclasses import dataclass
from pathlib import Path

from ._pack_io import json_document
from .change_operations import _requirements, _source_scope
from .errors import EvidenceWikiError, UsageError
from .host_context import current_context
from .mutation_contracts import canonical, detached, digest, validate_request_id
from .mutation_store import MutationStore
from .pack_discovery import owner
from .research_execution import ResearchExecution
from .research_session import _load
from .retrieval_operations import _ScopedView

SCHEMA = "research-decision-history/v1"
KINDS = ("claim", "source", "review", "brief")
MAX_BYTES = 1_048_576
MAX_RECORDS = 32
MAX_SOURCE_BYTES = 65_536


def refuse(reason):
    raise UsageError("RESEARCH_HISTORY_REFUSED", "Retained history cannot be read.", details={"reason": reason})


@dataclass(frozen=True)
class _HistorySubject:
    root: Path
    run_id: str
    binding: dict


def _subject(root, run_id, execution):
    """The existing observer authenticates the original binding and journal."""
    if type(execution) is not ResearchExecution:
        refuse("protected_observer_required")
    controller = _load(root, "run_controller")
    document = controller._session_state(root, run_id)
    binding = document.get("research_session")
    info = root.stat()
    expected = {"path": str(root), "device": str(info.st_dev), "inode": str(info.st_ino)}
    if (type(binding) is not dict or binding.get("run_id") != run_id
            or binding.get("workspace_binding") != expected or binding.get("target") != str(root)
            or binding.get("observer_id") != execution.identity()["observer_id"]):
        refuse("historical_binding_unavailable")
    subject = _HistorySubject(root, run_id, detached(binding))
    records = execution.records(subject)
    if not records or records[0]["kind"] != "execution_begin":
        refuse("authenticated_history_unavailable")
    return subject, records


def _dependencies(snapshot):
    if snapshot.get("computation") is not None:
        return None
    revisions = snapshot.get("source_revisions")
    if type(revisions) is not dict or not revisions or len(revisions) > 32:
        return None
    return revisions


def _packet_dependencies(row):
    packets = row.get("repair_parent_sources", row["attempt"]["input"].get("sources", []))
    if type(packets) is not list or not packets or len(packets) > 32:
        return None
    result = {}
    for packet in packets:
        binding = packet.get("source_binding", {})
        material = "material:" + binding.get("closure_id", "")
        if (packet.get("source_id") != binding.get("source_id") or not binding.get("closure_id")
                or binding["source_id"] in result and result[binding["source_id"]] != material):
            return None
        result[binding["source_id"]] = material
    return result or None


def _packets(row):
    return row.get("repair_parent_sources", row["attempt"]["input"].get("sources", []))


def _inventory_dependencies(inventory, row):
    """Merged peer claims must retain the exact source packet they cite."""
    if row is None:
        return None
    dependencies = _packet_dependencies(row)
    if dependencies is None:
        return None
    hashes = {packet["source_id"]: packet.get("record_sha256") for packet in _packets(row)}
    claims = inventory.get("claims")
    if type(claims) is not list or not 1 <= len(claims) <= 100:
        return None
    for claim in claims:
        evidence = claim.get("evidence")
        if (claim.get("premises") or claim.get("calculations") or type(evidence) is not list
                or not 1 <= len(evidence) <= 16):
            return None
        for link in evidence:
            expected = hashes.get(link.get("source_id"))
            if expected is None or link.get("record_sha256") != expected:
                return None
    return dependencies


def _items(records):
    """Select fixed native records, not caller payloads or arbitrary event blobs."""
    try:
        return _items_owned(records)
    except (KeyError, TypeError, AttributeError, IndexError):
        refuse("historical_record_schema_unsupported")


def _items_owned(records):
    attempts = {row["data"]["attempt"]["attempt_id"]: row["data"] for row in records if row["kind"] == "execution_attempt"}
    applied = {row["data"]["attempt_id"] for row in records if row["kind"] == "execution_applied"}
    all_sources = {}
    ambiguous = False
    items = []
    seen = set()
    observed_packets = []
    for record in records:
        data = record["data"]
        if record["kind"] == "execution_attempt":
            dependencies = _packet_dependencies(data)
            if dependencies is not None:
                observed_packets.extend(_packets(data))
                for identifier, revision in dependencies.items():
                    all_sources.setdefault(identifier, set()).add(revision)
                    if (identifier, revision) not in seen:
                        seen.add((identifier, revision))
                        items.append({"kind": "source", "sequence": record["sequence"],
                            "dependencies": {identifier: revision}, "payload": {"source_id": identifier,
                                "material_closure_id": revision.removeprefix("material:")},
                            "packets": [packet for packet in _packets(data) if packet["source_id"] == identifier], "applied": False})
        elif record["kind"] == "execution_application" and data.get("operation") == "generate":
            row = attempts.get(data["attempt_id"])
            inventory = data["proposal"]["inventory"]
            dependencies = _inventory_dependencies(inventory, row)
            if dependencies is None:
                ambiguous = True
            items.append({"kind": "claim", "sequence": record["sequence"], "dependencies": dependencies,
                "payload": {"original_id": data["original_id"], "inventory": data["proposal"]["inventory"]},
                "packets": _packets(row) if row is not None else [], "applied": data["attempt_id"] in applied})
        elif record["kind"] == "execution_application" and data.get("operation") == "evaluate":
            payload = data["envelope"]["payload"]["body"]["review"]
            dependencies = _dependencies(payload["snapshot"])
            if dependencies is not None:
                for identifier, revision in dependencies.items():
                    all_sources.setdefault(identifier, set()).add(revision)
            else:
                ambiguous = True
            items.append({"kind": "review", "sequence": record["sequence"], "dependencies": dependencies,
                "payload": {"original_id": data["original_id"], "claim_id": data["claim_id"],
                    "profile_ref": data["profile_ref"], "snapshot": payload["snapshot"],
                    "verdicts": payload["verdicts"], "rationale": payload["rationale"],
                    "historical_basis_id": data["strict_basis_id"]}, "applied": data["attempt_id"] in applied})
        elif record["kind"] == "execution_native" and data.get("operation") == "artifact.deliver":
            # The retained literal brief has no independent dependency index.
            # All observed scientific sources are a conservative history closure.
            dependencies = None if ambiguous or not all_sources else {
                identifier: sorted(ids) for identifier, ids in all_sources.items()}
            items.append({"kind": "brief", "sequence": record["sequence"], "dependencies": dependencies,
                "payload": {"request_id": data["reservation"]["attempt_id"], "arguments": data["arguments"]},
                "packets": list(observed_packets), "applied": False})
    return items


def _material_dependencies(item, live, cache=None):
    """Material commitments and protected usage revisions have distinct domains."""
    if item["dependencies"] is None:
        return None, {}
    dependencies = {identifier: set(revisions if type(revisions) is list else [revisions])
        for identifier, revisions in item["dependencies"].items()}
    if len(dependencies) > 32 or sum(len(ids) for ids in dependencies.values()) > 64:
        return None, {}
    cache = {} if cache is None else cache
    normalized = {}
    for packet in item.get("packets", []):
        identifier, material = packet["source_id"], packet["source_binding"]["closure_id"]
        wanted = packet.get("record_sha256")
        if wanted is None:
            return None, {}
        identity = (identifier, material, wanted)
        if identity in cache:
            candidates = cache[identity]
        else:
            candidates = _normalized_revisions(live, identifier, wanted, material)
            cache[identity] = candidates
        if len(candidates) != 1:
            return None, {}
        dependencies.setdefault(identifier, set()).discard("material:" + material)
        dependencies[identifier].add(candidates[0])
        normalized[(identifier, material)] = candidates[0]
    if any(revision.startswith("material:") for revisions in dependencies.values() for revision in revisions):
        return None, {}
    if sum(len(ids) for ids in dependencies.values()) > 64:
        return None, {}
    return {identifier: sorted(ids) for identifier, ids in dependencies.items()}, normalized


def _material_closure(live, retained, raw):
    """Reproduce the packet's material identity from exact retained originals."""
    import hashlib

    try:
        metadata, _body, error = owner("_normalized_contract").split_record(raw.decode("utf-8"))
        paths = metadata.get("raw_paths") if metadata is not None else None
        if error or type(paths) is not list or not 1 <= len(paths) <= 16:
            return None
        prefix = retained["descriptor"]["evidence_root"]
        originals = {}
        if prefix is not None:
            for path in paths:
                if type(path) is not str or prefix + "/" + path not in retained["files"]:
                    return None
                for name in (path, path + ".provenance.yml"):
                    stored = retained["files"].get(prefix + "/" + name)
                    if stored is not None:
                        originals[name] = hashlib.sha256(stored).hexdigest()
        else:
            capture = metadata.get("provenance", {}).get("host_capture", {})
            if capture.get("schema_version") != "evidence-host-capture/v2" or len(paths) != 1:
                return None
            revision = capture["sanitization"]["approved_revision_id"]
            original = live.state.revisions.get(revision)
            if original is None or revision not in retained["descriptor"]["parents"]:
                return None
            payloads = [value for name, value in original["files"].items()
                if name.startswith((original["descriptor"]["evidence_root"] or "") + "/")
                and "sha256:" + hashlib.sha256(value).hexdigest() == capture["content_sha256"]]
            if len(payloads) != 1:
                return None
            from .source_capture_operations import protected_artifacts, provenance_profile
            if protected_artifacts(payloads[0], capture, path=paths[0]) != original["files"]:
                return None
            originals = {paths[0]: hashlib.sha256(payloads[0]).hexdigest(),
                paths[0] + ".provenance.yml": hashlib.sha256(canonical(provenance_profile(capture))).hexdigest()}
        scope = owner("source_requests").normalize_scope(metadata.get("provenance", {}).get("scope"))
        return digest({"source_id": retained["source_id"], "normalized_sha256": hashlib.sha256(raw).hexdigest(),
            "originals": originals, "scope": scope})
    except (KeyError, TypeError, UnicodeError, EvidenceWikiError, ValueError):
        return None


def _normalized_revisions(live, identifier, wanted, material):
    import hashlib

    candidates = []
    for revision, retained in live.state.revisions.items():
        path = retained["descriptor"]["normalized_path"]
        raw = retained["files"].get(path) if path is not None else None
        if (retained["source_id"] != identifier or raw is None or len(raw) > MAX_SOURCE_BYTES
                or "sha256:" + hashlib.sha256(raw).hexdigest() != wanted
                or _material_closure(live, retained, raw) != material):
            continue
        candidates.append(revision)
    return candidates


def _permitted(dependencies, *, subject, live, context, view, requirements, capture):
    if dependencies is None or not dependencies or len(dependencies) > 32:
        return "unavailable", "historical_dependency_closure_unavailable"
    records = view.manifest()
    frozen = subject.binding["policy"]["scope"]
    for identifier, revisions in dependencies.items():
        revisions = revisions if type(revisions) is list else [revisions]
        if any(live.state.revisions.get(revision, {}).get("source_id") != identifier for revision in revisions):
            return "unavailable", "retained_source_missing_or_deleted"
        record = records.get(identifier)
        if record is None:
            return "unavailable", "current_source_scope_unavailable"
        paths = record.get("raw_paths")
        try:
            if _source_scope(context, subject.root, paths, requirements, capture=capture, record=record) is None:
                return "denied", "current_source_scope_denied"
            if frozen.get("source_scope_kind") == "native_extent":
                extents = [Path(item) for item in frozen["source_scope"] if Path(item).is_absolute()]
                if not extents or any(not any(subject.root.joinpath(path) == extent
                        or subject.root.joinpath(path).is_relative_to(extent) for extent in extents) for path in paths):
                    return "denied", "original_source_scope_denied"
            elif identifier not in frozen["source_scope"]:
                return "denied", "original_source_scope_denied"
        except EvidenceWikiError:
            return "denied", "current_source_scope_denied"
        for revision in revisions:
            retained = live.state.revisions[revision]
            normalized_path = retained["descriptor"]["normalized_path"]
            if normalized_path is not None:
                raw = retained["files"].get(normalized_path)
                if raw is None or len(raw) > MAX_SOURCE_BYTES:
                    return "unavailable", "retained_original_scope_unavailable"
                try:
                    metadata, _body, error = owner("_normalized_contract").split_record(raw.decode("utf-8"))
                    if error or metadata.get("source_id") != identifier or metadata.get("raw_paths") != paths:
                        return "unavailable", "retained_original_scope_unavailable"
                    if _source_scope(context, subject.root, metadata["raw_paths"], requirements) is None:
                        return "denied", "retained_original_scope_denied"
                except (UnicodeError, EvidenceWikiError):
                    return "denied", "retained_original_scope_denied"
            for purpose in ("research", "qa-export"):
                decision = live.check(revision, uses=["retrieval", "export"], purpose=purpose, consumer="evidence-wiki")
                if decision.get("eligible") is not True or decision.get("complete") is not True:
                    return "denied", "current_retained_source_use_denied"
    return "available", "current_retained_source_rights_checked"


def _source(live, payload):
    retained = live.state.revisions[payload["source_revision"]]
    path = retained["descriptor"]["normalized_path"]
    raw = retained["files"].get(path) if path is not None else None
    if raw is None or len(raw) > MAX_SOURCE_BYTES:
        return None, "retained_normalized_source_missing_or_bounded"
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return None, "retained_normalized_source_unsupported"
    return {**payload, "normalized_markdown": text, "bytes": len(raw)}, None


def _brief(subject, context, payload):
    control = context.mutation_control_directory
    if control is None:
        return None, "protected_delivery_retention_unavailable"
    store = MutationStore(subject.root, control)
    with store.historical_locked(payload["request_id"]):
        intent = store._read("intent.json")
        if (intent is None or intent["operation"] != "artifact.deliver" or intent["arguments"] != payload["arguments"]
                or intent["workspace_binding"] != subject.binding["workspace_binding"]
                or intent["host_context_sha256"] != subject.binding["host_context_sha256"]
                or intent["owner_generation"] != subject.binding["owner_generation"]):
            return None, "protected_delivery_binding_unavailable"
        receipt = store.receipt()
        if receipt is None or receipt["status"] not in {"committed", "reconciled"}:
            return None, "historical_delivery_not_completed"
        descriptor = store.staged("artifact-plan")
        if descriptor is None:
            return None, "retained_brief_missing_or_deleted"
        plan = json_document(store.read_stage(descriptor))
        if plan.get("proposal") != payload["arguments"]["proposal"]:
            refuse("retained_brief_conflict")
        raw = store.read_stage(plan["artifact"])
        proposed = payload["arguments"]["proposal"]
        import hashlib
        if (len(raw) > MAX_BYTES or len(raw) != proposed["artifact_bytes"]
                or "sha256:" + hashlib.sha256(raw).hexdigest() != proposed["artifact_sha256"]
                or receipt["result"].get("artifact_sha256") != proposed["artifact_sha256"]):
            refuse("retained_brief_conflict")
        try:
            markdown = raw.decode("utf-8")
        except UnicodeError:
            return None, "retained_brief_unsupported"
        return {"markdown": markdown, "artifact_sha256": proposed["artifact_sha256"], "bytes": len(raw),
            "delivery_completed_historically": True}, None


def _current_requirements(root, source, policy):
    """Preserve protected legacy read scope without inventing append ownership."""
    import hashlib

    from .onboarding_contract import decode_document

    name = "docs/research-requirements.json"
    raw = source.read(name, MAX_BYTES)
    expected = (policy or {}).get("instructions", {}).get(name)
    document = json_document(raw) if raw is not None else None
    if (type(document) is dict and set(document) == {"schema_version", "request", "decisions"}
            and document["schema_version"] == "evidence-research-requirements/v1"
            and document["decisions"] == {} and expected is not None
            and "sha256:" + hashlib.sha256(raw).hexdigest() == expected):
        request = decode_document("onboarding/research_request/v1", canonical(document["request"]))
        return {"request": request}, "protected_legacy_instruction_read_only"
    _before, _after, lineage = _requirements(root, raw, policy, None)
    return lineage["effective"], "protected_native_requirements_lineage"


def _history_basis(execution, selection, records):
    signed = execution.sign({"schema_version": "research-history-cursor/v1", "selection_id": selection,
        "record_authentication": [record["authentication"] for record in records]})
    return "sha256:" + signed["authentication"]


def current(root, *, run_id, execution, kind=None, after_index=0, max_records=16, expected_history_basis_id=None):
    """Read bounded retained material under current rights; start no host work."""
    run_id = validate_request_id(run_id)
    if (kind is not None and kind not in KINDS or type(after_index) is not int or after_index < 0
            or type(max_records) is not int or not 1 <= max_records <= MAX_RECORDS):
        refuse("selection_or_page_bound")
    if (expected_history_basis_id is not None and (type(expected_history_basis_id) is not str
            or re.fullmatch(r"sha256:[a-f0-9]{64}", expected_history_basis_id) is None)):
        refuse("history_basis_invalid")
    if after_index and expected_history_basis_id is None:
        refuse("history_basis_required_for_continuation")
    context = current_context()
    if context is None:
        refuse("host_context_required")
    context.require_grant("history_operations.current")
    root = context.require_path(Path(root)).resolve(strict=True)
    if Path(run_id).name != run_id or run_id in {".", ".."}:
        refuse("run_identifier_invalid")
    context.require_path(root / "runs" / run_id)
    selection = digest({"run_id": run_id, "kind": kind})
    source = _ScopedView(root)
    if source.workspace != "present":
        refuse("workspace_unavailable")
    revision_owner = owner("_evidence_revision")
    capture = revision_owner.capture_workspace(root)
    policy = owner("_strict_evidence").resolve_policy(root, source.config)
    requirements, requirements_scope = _current_requirements(root, source, policy)
    subject, records = _subject(root, run_id, execution)
    history_basis = _history_basis(execution, selection, records)
    if expected_history_basis_id is not None and expected_history_basis_id != history_basis:
        refuse("history_basis_changed")
    items = [row for row in _items(records) if kind is None or row["kind"] == kind]
    if after_index > len(items):
        refuse("cursor_outside_history")
    usage = owner("_evidence_usage")
    rows = []
    end = after_index
    with usage.current_view(root, source.config) as live:
        material_cache = {}
        for index, item in enumerate(items[after_index:after_index + max_records], start=after_index):
            dependencies, normalized = _material_dependencies(item, live, material_cache)
            availability, reason = _permitted(dependencies, subject=subject, live=live, context=context,
                view=source, requirements=requirements, capture=capture)
            payload = None
            if availability == "available":
                payload = detached(item["payload"])
                if item["kind"] == "source":
                    material = payload["material_closure_id"]
                    payload["source_revision"] = normalized[(payload["source_id"], material)]
                    payload, missing = _source(live, payload)
                elif item["kind"] == "brief":
                    payload, missing = _brief(subject, context, payload)
                else:
                    missing = None
                if missing:
                    availability, reason = "unavailable", missing
            row = {"index": index, "kind": item["kind"], "availability": availability, "reason": reason,
                "payload": payload, "historical_application_recorded": item["applied"] if payload is not None else None,
                "historical_qualification": "authenticated_native_record_not_semantic_truth" if payload is not None else "unavailable",
                "current_eligibility": "not_evaluated", "authorized": False, "evidence_accepted": False}
            if len(canonical(rows + [row])) > MAX_BYTES - 4096:
                break
            rows.append(row)
            end = index + 1
        if end == after_index and after_index < len(items):
            refuse("single_record_output_bound")
        result = {"schema_version": SCHEMA, "run_id": run_id, "selection_id": selection,
            "history_basis_id": history_basis,
            "after_index": after_index, "next_index": end if end < len(items) else None,
            "total_records": len(items), "returned_records": len(rows), "records": rows,
            "truncated": end < len(items), "complete": end == len(items),
            "history_only": True, "current_eligibility": "not_evaluated", "effects_executed": 0,
            "requirements_scope": requirements_scope,
            "retention_owner": "host_managed_native_journal_and_protected_source_and_delivery_stores",
            "deletion_owner": "preauthorized_host_retention_service; no deletion or restoration by this read",
            "authorized": False, "evidence_accepted": False}
        if len(canonical(result)) > MAX_BYTES:
            refuse("output_bound")
        if execution.records(subject) != records or revision_owner.capture_workspace(root).revision_id != capture.revision_id:
            refuse("history_or_current_scope_changed")
        source.recheck_scope()
        source.finish()
        context.require_grant("history_operations.current")
        context.require_path(root)
        live.revalidate(root, source.config)
    return result
