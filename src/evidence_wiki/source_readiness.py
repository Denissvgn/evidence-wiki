"""Source-scoped ingestion observations with canonical record and lexical checks."""

from __future__ import annotations

import copy
import hashlib
import tempfile
from pathlib import Path

from ._pack_io import relative_path
from .errors import EvidenceWikiError
from .pack_discovery import owner
from .source_contracts import refuse


def _base(source_id, paths):
    return {"source_id": source_id, "raw_paths": paths, "delivery": "not_observed", "inventory": "missing",
        "extraction": "not_run", "retrieval": "not_checked", "usability": "not_ready", "complete": False,
        "host_capture": None, "reasons": [], "semantic_adequacy": "not_evaluated", "evidence_accepted": False}


def _materialize(root, files):
    for relative, raw in files.items():
        if raw is None:
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


def _source(view, record, records):
    source_id = record["id"]
    raw_paths = list(record.get("raw_paths") or [])
    if not all(isinstance(path, str) for path in raw_paths) or len(raw_paths) > 16:
        refuse("source_raw_paths_invalid")
    for key in ("raw_pdf", "latex_root"):
        if isinstance(record.get(key), str) and record[key] not in raw_paths:
            raw_paths.append(record[key])
    row = _base(source_id, raw_paths)
    row.update(inventory="recorded", kind=record.get("kind"))
    files, unavailable = {}, []
    raw_config = view.config.get("raw")
    roots = raw_config.get("source_roots", ["raw"]) if isinstance(raw_config, dict) else ["raw"]
    if not isinstance(roots, list) or len(roots) > 32:
        refuse("source_roots_invalid")
    for root in roots:
        relative_path(root)
    for path in raw_paths:
        relative_path(path)
        if not any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots):
            row["reasons"].append("raw_path_outside_configured_scope")
            return row
        if (view.root / path).is_dir():
            unavailable.append("bundle_originals_not_checked")
            continue
        raw = view.read(path, 16_777_216)
        files[path] = raw
        if raw is None:
            unavailable.append("raw_source_missing")
        sidecar = path + ".provenance.yml"
        files[sidecar] = view.read(sidecar)
    row["delivery"] = "missing" if unavailable else "captured" if raw_paths else "not_observed"
    row["reasons"].extend(unavailable)
    row["raw_sha256"] = {path: hashlib.sha256(raw).hexdigest() for path, raw in files.items() if raw is not None}
    for companion in owner("source_inventory").record_companion_paths(record):
        relative_path(companion)
        candidate = Path(companion)
        if not any(candidate.parent == Path(raw).parent and candidate.name.startswith("." + Path(raw).name + ".") for raw in raw_paths):
            row["reasons"].append("companion_outside_source_scope")
            return row
        files[companion] = view.read(companion, 16_777_216)
    normalizer = owner("normalize_sources")
    try:
        configured = owner("_normalization_config").normalization_config(view.config)["adapters"]
        row["normalizer_configuration"] = "valid"
    except Exception:
        configured = ()
        row["normalizer_configuration"] = "invalid"
    method = normalizer.normalization_method(view.root, record, configured)
    row["normalizer"] = method
    normalized_root = view.generated_path("normalized_dir", "sources/normalized")
    normalized_relative = normalized_root + "/" + normalizer.safe_source_id(source_id) + ".md"
    raw = view.read(normalized_relative, 16_777_216)
    files[normalized_relative] = raw
    if raw is None:
        row["reasons"].append("normalization_not_supported" if method is None else "normalization_not_run")
        row["extraction"] = "unsupported" if method is None else "not_run"
        if row["normalizer_configuration"] == "invalid":
            row["reasons"].append("normalizer_configuration_invalid")
        return row
    contract = owner("_normalized_contract")
    metadata, body, error = contract.split_record(raw.decode("utf-8"))
    if error or metadata is None:
        row.update(extraction="invalid", reasons=[*row["reasons"], "normalized_record_invalid"])
        return row
    structured = metadata.get("structured_view")
    if isinstance(structured, dict) and isinstance(structured.get("path"), str):
        relative = structured["path"]
        relative_path(relative)
        if not relative.startswith(normalized_root + "/"):
            row["reasons"].append("structured_view_outside_scope")
            return row
        files[relative] = view.read(relative, 16_777_216)
    qualified = record.get("kind") in {"codebase_architecture", "execution_evidence", "market_evidence"}
    # No captured script, provider, normalizer adapter or model executes here.
    with tempfile.TemporaryDirectory(prefix="evidence-wiki-source-view-") as temporary:
        root = Path(temporary)
        _materialize(root, files)
        path = root / normalized_relative
        if record.get("raw_fingerprint") and not unavailable:
            fingerprint = owner("source_inventory").compute_raw_fingerprint(root, record)
            if fingerprint != record["raw_fingerprint"]:
                row["reasons"].append("raw_revision_changed_since_inventory")
        validation_root = view.root if qualified else root
        violations = contract.validate_document(validation_root / normalized_relative, metadata, body, manifest_by_id=records,
            normalized_root=validation_root / normalized_root, project_root=validation_root, config=view.config)
        if qualified:
            row["qualification"] = {"state": "canonical_contract_checked", "source_scope": "selected source's owning profile",
                                    "consumption_must_recheck": True, "host_protection": "not_established"}
        if violations:
            row.update(extraction="invalid", reasons=[*row["reasons"], "normalized_contract_failed"],
                       violation_codes=sorted({value.code for value in violations}))
            return row
        if qualified and any(isinstance(metadata.get(key), dict) and metadata[key].get("valid") is True
                             for key in ("qualified_context", "execution_evidence", "market_evidence")):
            row["delivery"] = "captured"
            row["reasons"] = [reason for reason in row["reasons"] if reason != "bundle_originals_not_checked"]
            unavailable = [reason for reason in unavailable if reason != "bundle_originals_not_checked"]
        query = owner("query_index")
        document = query.build_document(root, path, "normalized")
        row["retrieval"] = "lexically_indexable" if document is not None and query.prepare_document(document).body_tokens else "empty"
        if record.get("kind") == "host_capture":
            inspected = owner("_host_capture").inspect_record(root, record)
            row["host_capture"] = inspected["profile"]
            row["complete"] = inspected["complete"]
            row["completeness_basis"] = "caller_declared_capture"
        else:
            row["complete"] = metadata.get("status") == "content_extracted"
            row["completeness_basis"] = "normalizer_status"
    row["extraction"] = metadata.get("status", "unknown")
    if metadata.get("needs_ocr"):
        row["reasons"].append("ocr_required")
        row["complete"] = False
    coverage = metadata.get("rendered_coverage")
    if isinstance(coverage, dict) and isinstance(coverage.get("ratio"), (int, float)) and coverage["ratio"] < 1:
        row["reasons"].append("partial_rendered_table_or_values")
        row["complete"] = False
    row["evidence_usable"] = metadata.get("evidence_usable") is not False and record.get("evidence_usable") is not False
    if not row["evidence_usable"]:
        row["reasons"].extend(metadata.get("unusable_evidence_reasons") or ["evidence_marked_unusable"])
    if row["extraction"] in {"stubbed", "failed"}:
        row["reasons"].append("source_content_not_extracted")
    if not row["reasons"] and row["retrieval"] == "lexically_indexable":
        row["usability"] = "usable" if row["complete"] else "partial"
    elif row["extraction"] == "partial" and row["evidence_usable"] and not unavailable:
        row["usability"] = "partial"
    row["reasons"] = sorted(set(row["reasons"]))
    row["normalized_sha256"] = hashlib.sha256(raw).hexdigest()
    return row


def inspect_sources(view, *, source_ids=(), source_paths=()):
    if len(source_ids) + len(source_paths) > 32:
        refuse("source_selection_bound", "ONBOARDING_LIMIT")
    if len(set(source_ids)) != len(source_ids) or len(set(source_paths)) != len(source_paths):
        refuse("source_selection_duplicate")
    records = view.manifest() if view.workspace == "present" else {}
    selected, result = list(source_ids), []
    for path in source_paths:
        relative_path(path)
        raw_config = view.config.get("raw")
        roots = raw_config.get("source_roots", ["raw"]) if isinstance(raw_config, dict) else ["raw"]
        if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots) or not any(path.startswith(root.rstrip("/") + "/") for root in roots):
            refuse("source_path_outside_configured_roots")
        matches = [row["id"] for row in records.values() if path in (row.get("raw_paths") or []) or path == row.get("raw_pdf") or path == row.get("latex_root")]
        if matches:
            selected.extend(item for item in matches if item not in selected)
        else:
            row = _base(None, [path])
            raw = view.read(path, 16_777_216)
            row.update(delivery="captured" if raw else "empty" if raw is not None else "missing", reasons=["source_not_in_manifest"])
            result.append(row)
    if len(selected) + len(result) > 32:
        refuse("source_selection_ambiguous_or_large", "ONBOARDING_LIMIT")
    for source_id in selected:
        if not isinstance(source_id, str) or not 1 <= len(source_id) <= 256:
            refuse("source_id_invalid")
        if source_id not in records:
            result.append({**_base(source_id, []), "reasons": ["source_id_not_in_manifest"]})
            continue
        try:
            result.append(_source(view, copy.deepcopy(records[source_id]), records))
        except (EvidenceWikiError, OSError, ValueError, TypeError, KeyError) as error:
            result.append({**_base(source_id, []), "inventory": "recorded", "reasons": [getattr(error, "details", {}).get("field", "source_inputs_invalid_or_unavailable")]})
    return result
