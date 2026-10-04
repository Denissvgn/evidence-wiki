"""Native pilot capture extraction; originals, declared scope and gaps stay explicit."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from _workspace_module_loader import load_workspace_module

_DIR = Path(__file__).resolve().parent
VERSION = 1


def sibling(name):
    return load_workspace_module(_DIR, name)


def source_profile(record):
    return record.get("provenance", {}).get("host_capture", {})


def format_method(record):
    profile = source_profile(record)
    if profile.get("schema_version") != "evidence-host-capture/v2":
        return None
    return {"plain_text": "host_text", "markdown": "host_text", "csv": "table", "json": "host_text", "pdf": "pdf"}.get(profile.get("content_format"))


FORMAT_DEFERRAL = "host_capture_format_normalization_not_run"


def format_deferral_only(record):
    """Identify only the exact native inventory marker, retaining other refusals."""
    profile = source_profile(record)
    if profile.get("schema_version") != "evidence-host-capture/v2" or profile.get("content_format") not in {"json","csv","pdf"}:
        return False
    if sibling("normalize_sources").record_unusable_evidence_reasons(record) != [FORMAT_DEFERRAL]:
        return False
    metadata = record.get("metadata", {})
    return not (metadata.get("evidence_usable") is False and metadata.get("unusable_evidence_reasons") != [FORMAT_DEFERRAL]) and metadata.get("unusable_evidence_reasons") in (None,[FORMAT_DEFERRAL])


def retire_format_deferral(record):
    """After actual extraction, retire the owned marker in this producer's copy."""
    profile = source_profile(record)
    if profile.get("schema_version") != "evidence-host-capture/v2" or profile.get("content_format") not in {"json","csv","pdf"}:
        return
    for container in (record,record.get("metadata",{})):
        reasons = container.get("unusable_evidence_reasons")
        if type(reasons) is list and FORMAT_DEFERRAL in reasons:
            remaining = [item for item in reasons if item != FORMAT_DEFERRAL]
            container["unusable_evidence_reasons"] = remaining
            if not remaining:
                container.pop("evidence_usable",None)


def requires_refresh(record, frontmatter):
    """Refresh older capture records; future classification is not repair authority."""
    if source_profile(record).get("schema_version") != "evidence-host-capture/v2":
        return False
    value = frontmatter.get("host_capture_normalization_version")
    if value is None:
        return True
    if type(value) is not int or value != VERSION:
        errors = sibling("_script_errors")
        raise errors.ScriptRefusal("NORMALIZATION_PROFILE_UNSUPPORTED", "The capture normalization revision is unsupported.",
            exit_code=2, recoverable=False, details={"field":"host_capture_normalization_version"})
    return False


def _shell(text):
    """Recognize only text wholly composed of finite known error/auth notices."""
    normalizer = sibling("normalize_sources")
    notices = normalizer.HTML_AUTHENTICATION_MESSAGES | normalizer.HTML_SHELL_NOTICES | {
        "access denied", "permission denied", "unauthorized", "403 forbidden", "401 unauthorized", "invalid credentials"}
    notices = {" ".join(item.strip().casefold().split()).strip(" .!:;?") for item in notices}
    normalized = " ".join(text.casefold().split())
    if not normalized:
        return False
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(item) for item in sorted(notices,key=len,reverse=True)) + r")(?!\w)")
    end, seen = 0, False
    for match in pattern.finditer(normalized):
        if normalized[end:match.start()].strip(" .!:;?"):
            return False
        end, seen = match.end(), True
    return seen and not normalized[end:].strip(" .!:;?")


def _error_document(value):
    """Recognize closed error-only rows or a chain of exact single wrappers."""
    fields = {"error", "message", "detail", "status", "status_code", "code"}
    wrappers = {"response", "result", "data", "payload"}
    if type(value) is list:
        return bool(value) and all(_error_document(item) for item in value)
    if type(value) is not dict or not value:
        return False
    if len(value) == 1 and next(iter(value)) in wrappers:
        return _error_document(next(iter(value.values())))
    if not set(value) <= fields:
        return False
    return (any(type(value.get(key)) is str and _shell(value[key]) for key in ("error","message","detail"))
        or type(value.get("status_code")) in {int,str} and str(value["status_code"]) in {"401","403","404","500","503"})


def _measurement_gaps(value):
    """Only the offered value/unit measurement shape receives this syntactic check."""
    pending, missing = [value], False
    while pending:
        item = pending.pop()
        if type(item) is dict:
            number = item.get("value")
            numeric = type(number) in {int, float} or type(number) is str and re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", number) is not None
            if numeric and not any(type(item.get(key)) is str and item[key].strip() for key in ("unit", "units")):
                missing = True
            pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)
    return ["structured_measurement_units_missing"] if missing else []


def normalize(root, record, *, pdf_extractor=None):
    normalizer, capture = sibling("normalize_sources"), sibling("_host_capture")
    inspected = capture.inspect_record(root, record, **({"read_input": normalizer._capture_input_reader} if normalizer._bounded_bytes_available() else {}))
    profile = inspected["profile"]
    metadata = record.setdefault("metadata", {})
    metadata["host_capture"] = profile
    reasons = [item for item in inspected["unusable_reasons"] if item != "host_capture_format_normalization_not_run"]
    format = profile["content_format"]
    path = record["raw_paths"][0]
    warnings = normalizer.manifest_warnings(record)
    warnings.append("Origin, scope, rights and completeness remain declarations; extraction and stored consistency confer no semantic approval.")
    source = normalizer.NormalizedSource(record=record, extraction_method="host_text", title=profile["title"], authors=[],
        abstract="Native extraction of a retained capture.", outline=[], extracted_text="", media=[], links=[profile["origin_url"]],
        bibliography_files=[], included_paths=[], warnings=warnings)
    if format in {"plain_text", "markdown"}:
        source.extracted_text = inspected["content"]
        if _shell(source.extracted_text):
            reasons.append("host_capture_authentication_or_error_shell")
    elif format == "csv":
        source = normalizer.normalize_table_record(root, record)
        if source.structured is None:
            reasons.append("host_capture_structured_view_unavailable")
        else:
            reasons.extend(_measurement_gaps(source.structured["rows"]))
            if _error_document(source.structured["rows"]):
                reasons.append("host_capture_authentication_or_error_shell")
    elif format == "json":
        raw = capture.read(root, path, maximum=1048576, **({"read_input": normalizer._capture_input_reader} if normalizer._bounded_bytes_available() else {}))
        # The byte candidate already rejects duplicates/nonfinite/depth. Native
        # record parsing still applies its own strict shape and bounded JSON.
        value = sibling("_qualified_packet").strict_json(raw)
        source.structured = {"data": value}
        source.extraction_method = "json_text"
        source.extracted_text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        source.outline = [(2, "Structured original")]
        reasons.extend(_measurement_gaps(value))
        if _error_document(value):
            reasons.append("host_capture_authentication_or_error_shell")
    elif format == "pdf":
        extractor = pdf_extractor or normalizer.resolve_pdf_extractor("pypdf")
        raw_pdf = normalizer.raw_pdf_value(record)
        extraction = normalizer.extract_pdf(root / raw_pdf, raw_pdf, extractor)
        if not extraction.extractor_ran:
            raise RuntimeError("capture_pdf_extraction_failed")
        pages = extraction.reading_text.split("\f")
        if pages and not pages[-1]:
            pages.pop()
        source = normalizer.normalize_pdf_record(root, record, extractor, _extraction=extraction)
        source.structured = {"original_sha256": profile["content_sha256"], "qualification": "mechanical_stored_consistency",
            "backend": extraction.backend, "backend_version": extraction.backend_version,
            "pages": [{"page": index+1, "text": text} for index,text in enumerate(pages)]}
        if len(pages) != extraction.page_count:
            reasons.append("capture_pdf_page_accounting_incomplete")
        if source.needs_ocr:
            reasons.append("capture_pdf_text_incomplete_requires_ocr")
        if _shell(source.extracted_text):
            reasons.append("host_capture_authentication_or_error_shell")
    else:
        raise ValueError("capture_format_normalization_unsupported")
    retire_format_deferral(record)
    metadata_reasons = metadata.get("unusable_evidence_reasons")
    if type(metadata_reasons) is list:
        reasons.extend(item for item in metadata_reasons if type(item) is str and item.strip())
    if metadata.get("evidence_usable") is False and not metadata_reasons:
        reasons.append("evidence_usable:false")
    reasons = normalizer.unique_values([*normalizer.record_unusable_evidence_reasons(record), *reasons])
    metadata["host_capture"] = profile
    source.record = record
    source.title = profile["title"]
    source.title_source = "host_capture"
    source.title_confidence = "low"
    if not source.extracted_text.strip() or source.extracted_text == "None extracted.":
        source.capture_status = "failed"
        reasons.append("host_capture_extracted_text_empty")
    elif reasons or not inspected["complete"]:
        source.capture_status = "partial"
    else:
        source.capture_status = "content_extracted"
    if reasons:
        normalizer.set_record_unusable_evidence(record, reasons)
    source.warnings = normalizer.unique_values([*source.warnings,*warnings,*reasons])
    return source


def issues(root, record, frontmatter, body):
    """Recheck stored consistency without executing a PDF extractor."""
    capture, normalizer, contract = sibling("_host_capture"), sibling("normalize_sources"), sibling("_normalized_contract")
    try:
        inspected = capture.inspect_record(root, record)
        profile = inspected["profile"]
        capture.require(type(frontmatter.get("host_capture_normalization_version")) is int and frontmatter["host_capture_normalization_version"] == VERSION, "capture_normalization_version_invalid")
        capture.require(frontmatter.get("host_capture") == profile, "capture_normalized_metadata_mismatch")
        if profile["content_format"] == "pdf":
            result = sibling("_structured_view").load_sidecar(frontmatter, root / frontmatter["structured_view"]["path"])
            capture.require(result.ok and result.document.get("original_sha256") == profile["content_sha256"], "capture_pdf_original_binding_mismatch")
            pages = result.document.get("pages")
            capture.require(set(result.document) == {"original_sha256", "qualification", "backend", "backend_version", "pages"}, "capture_pdf_sidecar_shape_invalid")
            capture.require(type(pages) is list and 1 <= len(pages) <= normalizer.PDF_MAX_PAGES and all(type(row) is dict and set(row)=={"page","text"} and type(row["page"]) is int and row["page"]==index+1 and type(row["text"]) is str for index,row in enumerate(pages)), "capture_pdf_page_accounting_invalid")
            capture.require(sum(len(row["text"]) for row in pages) <= normalizer.PDF_MAX_OUTPUT_CHARS, "capture_pdf_text_bound")
            extractor = frontmatter.get("pdf_extractor")
            capture.require(type(extractor) is dict and extractor == {"name":result.document["backend"], "version":result.document["backend_version"]} and result.document["backend"] in {"pypdf", "poppler"} and type(result.document["backend_version"]) is str and 0 < len(result.document["backend_version"]) <= 512, "capture_pdf_backend_binding_invalid")
            text = normalizer.normalize_pdf_text("".join(row["text"]+"\f" for row in pages))
            retained = body.partition("## Extracted Text\n\n")[2].rpartition("\n\n## Figures and Tables")[0]
            capture.require(retained == (text or "None extracted."), "capture_pdf_page_body_mismatch")
            capture.require(frontmatter.get("extraction_method") == "pdf_text" and result.document.get("qualification")=="mechanical_stored_consistency", "capture_pdf_method_mismatch")
            if _shell(text):
                capture.require(frontmatter.get("evidence_usable") is False and frontmatter.get("status") in {"partial","failed"}
                    and "host_capture_authentication_or_error_shell" in (frontmatter.get("unusable_evidence_reasons") or []), "capture_pdf_error_shell_overstated")
            if len(text) < normalizer.PDF_MIN_CHARS_PER_PAGE * len(pages):
                capture.require(frontmatter.get("needs_ocr") is True and frontmatter.get("evidence_usable") is False and frontmatter.get("status") in {"partial", "failed"}, "capture_pdf_empty_or_sparse_overstated")
        else:
            source = normalize(root, copy.deepcopy(record))
            if source.record.get("evidence_usable") is False or source.record.get("metadata", {}).get("evidence_usable") is False:
                capture.require(frontmatter.get("evidence_usable") is False, "capture_unusable_claim")
            capture.require(frontmatter.get("extraction_method") == source.extraction_method and frontmatter.get("status") == source.capture_status, "capture_normalized_method_or_status_mismatch")
            _,expected,error = contract.split_record(normalizer.render_markdown(source, frontmatter))
            capture.require(error is None and body.strip()==expected.strip(), "capture_normalized_rendering_mismatch")
            if source.structured is not None:
                result = sibling("_structured_view").load_sidecar(frontmatter, root / frontmatter["structured_view"]["path"])
                capture.require(result.ok and result.document==source.structured, "capture_structured_original_mismatch")
        reasons = [item for item in inspected["unusable_reasons"] if item != "host_capture_format_normalization_not_run"]
        if reasons:
            capture.require(frontmatter.get("evidence_usable") is False, "capture_unusable_claim")
        if not inspected["complete"]:
            capture.require(frontmatter.get("status") in {"partial","failed"}, "capture_completeness_overstated")
        return []
    except (ValueError,TypeError,KeyError,AttributeError):
        return ["capture_normalization_original_or_stored_consistency_invalid"]
