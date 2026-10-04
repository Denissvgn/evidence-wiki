"""Read-only mechanical diagnostics over an exact unaccepted draft proposal."""

from __future__ import annotations

import stat
import sys
from contextlib import nullcontext
from pathlib import Path
from types import MappingProxyType

from _evidence_revision import canonical_bytes, content_id
from _workspace_module_loader import load_workspace_module

SCRIPT_DIR = Path(__file__).resolve().parent
_SIBLINGS = {}
SCHEMA = "evidence-claim-diagnostics/v1"


def sibling(name):
    return load_workspace_module(SCRIPT_DIR, name, cache=_SIBLINGS)


def _mechanical(value):
    if value is None:
        return None
    # Native messages and resolved text are deliberately not outward diagnostics.
    return {key: value.get(key) for key in ("result", "form", "match_type", "occurrence_count",
        "global_occurrence_count", "policy")}


def schema_document():
    def obj(**fields):
        return {"type":"object", "properties":fields, "required":list(fields), "additionalProperties":False}

    def array(item, maximum):
        return {"type":"array", "items":item, "maxItems":maximum}

    def nullable(value):
        return {"anyOf":[value, {"type":"null"}]}

    string = {"type":"string", "minLength":1, "maxLength":4096}
    identifier = {"type":"string", "minLength":1, "maxLength":128}
    digest = {"type":"string", "pattern":r"^sha256:[a-f0-9]{64}$"}
    counter = nullable({"type":"integer", "minimum":0, "maximum":16_777_216})
    mechanical = obj(result=string, form={"enum":["anchor", "quote"]}, match_type=nullable(string),
        occurrence_count=counter, global_occurrence_count=counter, policy=nullable(string))
    evidence = obj(evidence_index={"type":"integer", "minimum":0, "maximum":15},
        source_id={"type":"string", "minLength":1, "maxLength":256}, record_sha256=digest,
        location_hint=nullable({"type":"string", "minLength":1, "maxLength":512}),
        anchor_pointer=nullable({"type":"string", "minLength":1, "maxLength":512}),
        form={"enum":["anchor", "quote"]}, result={"enum":["verified", "failed"]},
        reason=nullable({"type":"string", "minLength":1, "maxLength":128}), mechanical=nullable(mechanical),
        current_usage=nullable(obj(eligible={"type":"boolean"}, source_revision=nullable(digest), complete={"type":"boolean"})))
    claim = obj(claim_id=identifier, question_slug=identifier, original_id=identifier,
        qualification={"enum":list(sibling("_strict_contract").QUALIFICATIONS)}, scope=string, time=string, units=string,
        premises=array(identifier, 32), evidence=array(evidence, 16), mechanical_result={"enum":["verified", "not_established"]},
        semantic_approval={"const":"not_evaluated"}, accepted={"const":False})
    return {"$schema":"https://json-schema.org/draft/2020-12/schema", **obj(schema_version={"const":SCHEMA},
        proposal_sha256=digest, basis_id=digest, observed_at=string, claims=array(claim, 100),
        original_ids=array(identifier, 100), structural_result={"const":"valid"}, semantic_approval={"const":"not_evaluated"},
        accepted={"const":False}, published={"const":False}, bounds=obj(claims={"const":100}, output_bytes={"const":1_048_576}))}


def _capture(root, context):
    """Use the native bounded no-follow reader after each exact host path check."""
    revision = sibling("_evidence_revision")
    before = revision.observe_tree(root)
    for relative in before:
        context.require_path(root / relative)
    if sys.platform == "win32":
        files = revision.capture_windows_files(root, before)
    else:
        files = {}
        for relative, info in sorted(before.items()):
            if stat.S_ISREG(info[2]):
                context.require_path(root / relative)
                files[relative] = revision.read_observed_file(root, relative, info)
    sibling("_draft_admission").require(revision.observe_tree(root) == before, "diagnostic_inputs_changed")
    directories = tuple(sorted(relative for relative, info in before.items() if stat.S_ISDIR(info[2])))
    return revision.WorkspaceRevision(MappingProxyType(files), directories)


def current(root, proposal):
    """Observe current native checks; this route never publishes or accepts claims."""
    draft = sibling("_draft_admission")
    return draft.typed(_current)(root, proposal)


def _current(root, proposal):
    draft = sibling("_draft_admission")
    bridge = sys.modules.get("evidence_wiki.host_context")
    context = bridge.current_context() if bridge is not None else None
    draft.require(context is not None, "diagnostic_host_context_required")
    context.require_grant("strict_evidence.run_operation.check")
    root = context.require_path(Path(root).expanduser().absolute())
    proposal = sibling("_draft_admission_contract").proposal_document(proposal)
    capture = _capture(root, context)
    # The immutable host scope covers every captured dependency, not merely the
    # displayed source selection. No denied contents are included in this DTO.
    for relative in capture.files:
        context.require_path(root / relative)
    config = draft.configuration(capture)
    policy = sibling("_strict_evidence").resolve_policy(root, config)
    draft.require(policy is not None, "strict_policy_missing")
    draft.require(config.get("computation") is None and not any(claim.get("calculations")
        for claim in proposal["inventory"]["claims"]), "computation_admission_unsupported", unsupported=True)
    for relative, expected in policy["instructions"].items():
        draft.require(relative in capture.files and sibling("_requirements_bridge").instruction_matches(
            root, relative, capture.files[relative], expected), "instructions_changed")
    scopes = draft.originals(capture, root, config, policy, proposal)
    usage = sibling("_evidence_usage")
    selected = usage.current_view(root, config) if usage.configured(config) else nullcontext(None)
    with selected as live:
        observed = draft.clock(draft.current_time())
        rows, binding = draft.iter_source_diagnostics(root, config, policy, capture, proposal,
            observed, scopes, _live_view=live)
        by_claim = {}
        for row in rows:
            public = {key: row[key] for key in ("evidence_index", "source_id", "record_sha256",
                "location_hint", "anchor_pointer", "form", "result", "reason")}
            public["mechanical"] = _mechanical(row["mechanical"])
            public["current_usage"] = ({"eligible": row["usage"].get("eligible") is True,
                "source_revision": row["usage"].get("source_revision"),
                "complete": row["usage"].get("complete") is True}
                if row.get("usage") is not None else None)
            by_claim.setdefault(row["claim_id"], []).append(public)
        claims = []
        originals = {row["slug"]: row["original_id"] for row in proposal["inventory"]["questions"]}
        for claim in proposal["inventory"]["claims"]:
            evidence = by_claim.get(claim["id"], [])
            claims.append({"claim_id":claim["id"], "question_slug":claim["question_slug"],
                "original_id":originals[claim["question_slug"]], "qualification":claim["qualification"],
                "scope":claim["scope"], "time":claim["time"], "units":claim["units"],
                "premises":list(claim["premises"]),
                "evidence":evidence, "mechanical_result":"verified" if evidence and all(
                    row["result"] == "verified" for row in evidence) else "not_established",
                "semantic_approval":"not_evaluated", "accepted":False})
        if live is not None:
            draft.require(draft.private_view(live) == binding, "source_authority_changed")
            live.revalidate(root, config)
        closing = draft.clock(draft.current_time())
        draft.require(closing >= observed, "source_clock_regressed")
        for claim in proposal["inventory"]["claims"]:
            for index, evidence in enumerate(claim["evidence"]):
                age = (closing - sibling("_evidence_authority").timestamp(evidence["observed_at"])).total_seconds()
                # A known invalid age remains a local diagnostic; an age that
                # became invalid during observation invalidates the observation.
                previous = by_claim[claim["id"]][index]
                if previous["result"] == "verified":
                    draft.require(0 <= age <= policy["max_source_age_seconds"], "source_age_invalid")
        for relative in capture.files:
            context.require_path(root / relative)
        draft.require(bridge.current_context() is context, "diagnostic_host_selection_changed")
        draft.require(_capture(root, context).revision_id == capture.revision_id, "diagnostic_inputs_changed")
        proposal_sha256 = draft.file_hash(canonical_bytes(proposal))
        permitted = [{"claim_id":claim["claim_id"], "evidence": [row for row in claim["evidence"]
            if row["current_usage"] is not None and row["current_usage"]["eligible"]]}
            for claim in claims]
        # The native full-workspace revision remains private for closing CAS.
        # Only caller-provided proposal material and permitted facts are public.
        basis = content_id("evidence-claim-diagnostics-public/v1", {
            "proposal_sha256":proposal_sha256, "permitted_mechanical_usage":permitted})
        result = {"schema_version":SCHEMA, "proposal_sha256":proposal_sha256,
            "basis_id":basis, "observed_at":observed.isoformat(), "claims":claims,
            "original_ids":[row["original_id"] for row in proposal["originals"]],
            "structural_result":"valid", "semantic_approval":"not_evaluated", "accepted":False,
            "published":False, "bounds":{"claims":100, "output_bytes":1_048_576}}
        draft.require(len(canonical_bytes(result)) <= 1_048_576, "diagnostic_output_bound")
        from evidence_wiki.onboarding_contract import _matches
        _matches(result, schema_document())
        return result
