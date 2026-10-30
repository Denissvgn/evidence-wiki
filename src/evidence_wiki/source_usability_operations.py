"""Read-only source extraction, stored consistency and rights remain separate."""

from __future__ import annotations

import hashlib

from ._pack_io import bounded, canonical
from .errors import ConfigError, UsageError
from .host_context import checked_operation, current_context
from .pack_discovery import owner
from .source_inputs import SourceView
from .source_readiness import inspect_sources


class _ScopedView(SourceView):
    """Retain host denials even when a source observer reports an unavailable row."""

    def __init__(self, root):
        self.scope_error = None
        super().__init__(root)

    def read(self, relative, maximum=1_048_576):
        context = current_context()
        if context is not None:
            try:
                context.require_path(self.root / relative)
            except ConfigError as error:
                self.scope_error = error
                raise
        return super().read(relative, maximum)

    def recheck_scope(self):
        if self.scope_error is not None:
            raise self.scope_error
        context = current_context()
        if context is not None:
            for path in self.files:
                context.require_path(self.root / path)


def schema():
    """Publish the closed observation wrapper while preserving native owner facts."""
    from .source_contracts import array, obj
    facts = {"type":"object", "maxProperties":128, "additionalProperties":True}
    digest = {"type":"string", "pattern":r"^sha256:[a-f0-9]{64}$"}
    return {"$schema":"https://json-schema.org/draft/2020-12/schema", **obj(
        schema_version={"const":"evidence-source-usability-view/v1"}, sources=array(facts,32,1),
        verification=facts, basis_id=digest,
        current_rights=array(obj(source_id={"type":"string","minLength":1,"maxLength":256}, material_basis=digest,
            source_revision={"anyOf":[digest,{"type":"null"}]}, state={"enum":["qualified","legacy_compatibility","ineligible"]}, observation=facts),32,1),
        stored_consistency={"enum":["native_contract_checked","partial","not_established"]}, semantic_adequacy={"const":"not_evaluated"},
        bounds=obj(sources={"const":32}, selected_bytes={"const":33_554_432}, per_file_bytes={"const":16_777_216}),
        authorized={"const":False}, evidence_accepted={"const":False}, claim_support_eligible={"const":False})}


def current(root, *, source_ids):
    """Observe current originals and native normalized contracts without accepting claims."""
    if (type(source_ids) is not list or not 1 <= len(source_ids) <= 32
            or any(type(item) is not str or not 1 <= len(item) <= 256 or any(ord(char) < 32 for char in item) for item in source_ids)
            or len(set(source_ids)) != len(source_ids)):
        raise UsageError("SOURCE_USABILITY_REFUSED", "Source usability selection refused.", details={"field": "source_ids"})
    with checked_operation("source_usability_operations", "current", (root,), {}) as revalidate:
        context = current_context()
        if context is not None:
            context.require_grant("normalize_verify.run_verify")
        view = _ScopedView(root)
        revisions = owner("_evidence_revision")
        before = revisions.capture_workspace(root).revision_id
        inspected = inspect_sources(view, source_ids=source_ids)
        view.recheck_scope()
        invalid_utf8 = False
        records = view.manifest()
        rights = []
        gate = owner("_usage_gate")
        for row in inspected:
            source_scope = owner("source_requests").source_provenance_scope(view.root, view.config, row["source_id"]) if row["source_id"] in records else {}
            row["source_scope"] = source_scope
            row["material_basis"] = "sha256:" + hashlib.sha256(canonical({"source_id":row["source_id"],
                "originals":row.get("raw_sha256", {}), "normalized_sha256":row.get("normalized_sha256"), "scope":source_scope})).hexdigest()
            row["stored_consistency"] = "native_contract_checked" if row["extraction"] == "content_extracted" else "partial" if row["extraction"] == "partial" else "not_established"
            record = records.get(row["source_id"], {})
            normalized_path = view.generated_path("normalized_dir", "sources/normalized") + "/" + owner("normalize_sources").safe_source_id(row["source_id"]) + ".md"
            normalized = view.read(normalized_path, 16777216)
            metadata = {}
            if normalized is not None:
                try:
                    metadata, _body, _error = owner("_normalized_contract").split_record(normalized.decode("utf-8"))
                except UnicodeError:
                    invalid_utf8 = True
                    row["reasons"] = sorted(set([*row["reasons"], "normalized_utf8_invalid"]))
                    row.update(stored_consistency="not_established", extraction="invalid", complete=False, usability="not_ready")
            decision = gate.source_decision(view.root, view.config, row["source_id"], [record, metadata or {}], normalized=normalized)
            # Permissive legacy behavior is reported separately; it does not
            # create authenticated usage authorization for a captured source.
            qualified = decision.get("eligible") is True and decision.get("complete") is True and "source_revision" in decision
            state = "qualified" if qualified else "legacy_compatibility" if decision.get("compatibility") == "legacy-research" else "ineligible"
            rights.append({"source_id": row["source_id"], "material_basis":row["material_basis"],
                "source_revision":decision.get("source_revision"), "state":state, "observation":decision})
        view.recheck_scope()
        verification = ({"overall_result":"not_checked", "reason":"selected_normalized_utf8_invalid", "records":[], "network_io_executed":False}
            if invalid_utf8 else owner("normalize_verify").run_verify(root, source_ids=source_ids))
        states = [row["stored_consistency"] for row in inspected]
        consistency = ("native_contract_checked" if all(state == "native_contract_checked" for state in states)
            and verification.get("overall_result") == "verified" else "partial" if any(state != "not_established" for state in states) else "not_established")
        raw_hash = revisions.capture_workspace(root).revision_id
        if raw_hash != before:
            raise UsageError("SOURCE_USABILITY_REFUSED", "Source inputs changed during observation.", details={"field": "basis_id"})
        result = {"schema_version": "evidence-source-usability-view/v1", "sources": inspected, "verification": verification,
            "basis_id": raw_hash, "current_rights": rights,
            "stored_consistency": consistency, "semantic_adequacy": "not_evaluated",
            "bounds": {"sources":32, "selected_bytes":33_554_432, "per_file_bytes":16_777_216}, "authorized": False,
            "evidence_accepted": False, "claim_support_eligible": False}
        bounded(result)
        if len(canonical(result)) > 1_048_576:
            raise UsageError("SOURCE_USABILITY_REFUSED", "Source observation exceeds the output bound.", details={"field":"output_bytes"})
        from .onboarding_contract import _matches
        _matches(result, schema())
        view.recheck_scope()
        view.finish()
        revalidate()
        return result
