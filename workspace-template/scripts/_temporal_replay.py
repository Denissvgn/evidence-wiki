#!/usr/bin/env python3
"""Bounded read-only evaluation over revisions qualified at one decision instant."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
from _evidence_authority import EvidenceInvalid, authority_basis, timestamp
from _evidence_policies import domain_matches, host_from_url, rule_provider_ids
from _evidence_revision import canonical_bytes, content_id
from _evidence_usage import UsageState, UsageView, current_view
from _market_evidence import qualify_cutoff as qualify_market_cutoff
from _policy_primitives import PolicyRuleError, RuleContext, evaluate_rule, pack_policy_rules
from _snapshot_qualifications import qualify_source
from _snapshot_verifier import ClaimLoader, SnapshotInvalid, binding, document, execution, execution_input_context
from _structured_view import canonical_scalar, expected_matches, resolve_pointer
from _temporal_contract import (
    BOUNDS,
    RESULT_SCHEMA,
    authenticate_availability,
    qualify_time,
    request_document,
    require,
    source_times,
)
from _usage_gate import claims, documents_with_metadata
from query_index import Document, extract_headings, extract_title, prepare_document, rank_documents


def implementation_identity() -> str:
    directory = Path(__file__).resolve().parent
    stems = ("evidence_temporal", "_temporal_contract", "_temporal_replay", "_evidence_usage", "_evidence_authority",
             "_host_evidence_store", "_evidence_revision", "_record_artifacts", "_usage_gate", "_policy_primitives",
             "_evidence_policies", "_structured_view", "_normalized_contract", "_workspace_module_loader",
             "query_index", "_snapshot_verifier", "_snapshot_qualifications", "_market_evidence", "_qualified_packet")
    paths = [directory / (stem + ".py") for stem in stems]
    paths.extend(sorted(directory.glob("_packet_vendor_*.py")))
    return content_id("evidence-temporal-implementation/v1", {path.name: binding(path.read_bytes()) for path in paths})


def checkpoint_state(view: UsageView, checkpoint: str | None) -> UsageState:
    if checkpoint is None or checkpoint == view.state.checkpoint:
        return view.state
    for event in view.state.events:
        if event["event_id"] == checkpoint:
            frozen = {**view.state.document, "events": view.state.events[:event["sequence"]]}
            return UsageState(view.state.root, view.state.state_id, canonical_bytes(frozen))
    raise EvidenceInvalid("temporal_checkpoint_unknown")


def normalized_document(record: dict[str, Any], revision: str, times: dict[str, Any]) -> dict[str, Any]:
    path = record["descriptor"]["normalized_path"]
    require(path is not None, "temporal_normalized_record_missing")
    text = record["files"][path].decode("utf-8")
    lines = text.splitlines()
    require(lines and lines[0] == "---" and "---" in lines[1:], "temporal_normalized_metadata_missing")
    end = lines.index("---", 1)
    require(end <= 4096, "temporal_normalized_metadata_bound_exceeded")
    try:
        metadata = yaml.load("\n".join(lines[1:end]), Loader=ClaimLoader)  # noqa: S506 -- restricted SafeLoader subclass
    except yaml.YAMLError as exc:
        raise EvidenceInvalid("temporal_normalized_metadata_invalid") from exc
    require(isinstance(metadata, dict) and metadata.get("source_id") == record["source_id"], "temporal_normalized_source_mismatch")
    values = {}
    for key in ("record_path", "provenance_path"):
        relative = times["claims"][key]
        values[key] = None if relative is None else document(record["files"][relative], BOUNDS["artifact_bytes"])
    documents = [metadata, *(value for value in values.values() if value is not None)]
    reasons, claimed, _present = claims(documents, ["retrieval"])
    require(not reasons, reasons[0] if reasons else "temporal_original_use_denied")
    require(claimed is None or claimed == revision, "temporal_original_revision_mismatch")
    for value in documents_with_metadata(documents):
        require("source_id" not in value or value["source_id"] == record["source_id"], "temporal_structured_source_mismatch")
        require("evidence_usable" not in value or value["evidence_usable"] is True, "temporal_original_evidence_unusable")
    return {"metadata": metadata, "body": "\n".join(lines[end + 1:]),
            "structured": values["record_path"], "provenance": values["provenance_path"] or {}}


class Selection:
    """Keep selection, ancestry, policy inputs and query content in one locked generation."""

    def __init__(self, state: UsageState, view: UsageView, request: dict[str, Any], cutoff) -> None:
        self.state, self.view, self.request, self.cutoff = state, view, request, cutoff
        self.inspected: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, str] = {}
        self.pending: set[str] = set()
        self.byte_count = 0

    def inspect(self, revision: str) -> dict[str, Any]:
        if revision in self.errors:
            raise EvidenceInvalid(self.errors[revision])
        if revision in self.inspected:
            return self.inspected[revision]
        require(revision not in self.pending, "temporal_lineage_cycle")
        require(len(self.inspected) + len(self.errors) + len(self.pending) < BOUNDS["lineage"], "temporal_lineage_bound_exceeded")
        self.pending.add(revision)
        try:
            result = self._inspect(revision)
            self.inspected[revision] = result
            return result
        except (EvidenceInvalid, SnapshotInvalid) as exc:
            self.errors[revision] = str(exc)
            raise EvidenceInvalid(str(exc)) from exc
        except (UnicodeDecodeError, ValueError, TypeError, KeyError, RecursionError) as exc:
            self.errors[revision] = "temporal_source_invalid"
            raise EvidenceInvalid("temporal_source_invalid") from exc
        finally:
            self.pending.remove(revision)

    def _inspect(self, revision: str) -> dict[str, Any]:
        require(revision in self.state.revisions, "temporal_non_source_ancestor_unsupported")
        record = self.state.revisions[revision]
        times = source_times(record)
        qualify_time(record, times, self.cutoff, self.request["mode"])
        decision = self.view.check(revision, uses=["retrieval"], purpose=self.request["purpose"], consumer=self.request["consumer"])
        require(decision["eligible"] and decision["complete"], decision["reasons"][0] if decision["reasons"] else "temporal_usage_incomplete")
        proof = self.state.availability.get(revision)
        if self.request["mode"] == "historical-available":
            require(proof is not None, "temporal_independent_availability_missing")
            authenticate_availability(proof["body"], record, self.view.trust, timestamp(proof["observed_at"]))
        self.byte_count += sum(len(data) for data in record["files"].values())
        require(self.byte_count <= BOUNDS["artifact_bytes"], "temporal_artifact_bound_exceeded")
        normalized = normalized_document(record, revision, times)
        ancestors = set()
        for parent in record["descriptor"]["parents"]:
            selected = self.inspect(parent)
            ancestors.update({parent, *selected["ancestors"]})
        qualifications = qualify_source(record, record["files"])
        for qualification in qualifications:
            if qualification["validator"] == "market_evidence/v1":
                qualify_market_cutoff(qualification["qualifications"], times, self.cutoff, timestamp(record["observed_at"]))
        execution_result = None
        prefix = record["descriptor"]["evidence_root"]
        evidence = {} if prefix is None else {path[len(prefix) + 1:]: data for path, data in record["files"].items()
                                             if path.startswith(prefix + "/")}
        if "execution-record.json" in evidence or normalized["metadata"].get("evidence_profile") == "execution_evidence/v1":
            execution_result = execution(evidence, record["source_id"], self.view.trust["policy"], self.cutoff, historical=True)
            require(execution_result["example"]["outcome"] == "passed", "temporal_execution_verification_failed")
            for item in execution_result["inputs"]:
                source = self.state.revisions.get(item["source_revision"])
                require(item["source_revision"] in ancestors and source is not None and source["source_id"] == item["source_id"]
                        and {key: item[key] for key in ("content_hash", "size_bytes")} in [binding(data) for data in source["files"].values()],
                        "temporal_execution_input_outside_qualified_lineage")
            closure = {revision, *ancestors}
            sources = {key: {**self.state.revisions[key], "source_revision": key} for key in closure}
            execution_input_context(sources, {key: source["files"] for key, source in sources.items()},
                                    {key: self.state.nodes[key] for key in closure}, self.state.availability,
                                    self.view.trust["policy"], self.view.now, self.state.last_observed)
        return {**normalized, "times": times, "ancestors": sorted(ancestors), "qualifications": qualifications,
                "execution": None if execution_result is None else execution_result["example"],
                "identity": {"source_id": record["source_id"], "source_revision": revision, "host_observed_at": record["observed_at"],
                             "deposit_event_id": record["event_id"], "temporal": times["claims"],
                             "availability_event_id": proof["event_id"] if proof is not None and self.request["mode"] == "historical-available" else None}}

    def select(self) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
        candidates = {source: [revision for revision, value in self.state.revisions.items() if value["source_id"] == source]
                      for source in self.request["source_ids"]}
        require(sum(map(len, candidates.values())) <= BOUNDS["revisions"], "temporal_revision_bound_exceeded")
        selected, gaps, exclusions = {}, [], []
        for source, revisions in sorted(candidates.items()):
            accepted = set()
            for revision in revisions:
                try:
                    self.inspect(revision)
                    accepted.add(revision)
                except EvidenceInvalid as exc:
                    # A bound ends the whole operation; returning a partial prefix would disguise uninspected evidence.
                    if "bound_exceeded" in str(exc):
                        raise
                    exclusions.append({"source_id": source, "source_revision": revision, "reason": str(exc)})
            predecessors = {}
            chain_invalid = False
            for revision in accepted:
                # A future or otherwise excluded correction cannot invalidate past selection.
                # Every link used by a qualified correction must still be ordered and inspectable.
                times = self.inspected[revision]["times"]
                while times["claims"]["supersedes"] is not None:
                    predecessor = times["claims"]["supersedes"]
                    if predecessor not in revisions or revisions.index(predecessor) >= revisions.index(revision):
                        chain_invalid = True
                        break
                    try:
                        prior_times = source_times(self.state.revisions[predecessor])
                        require(prior_times["available"] is not None and times["available"] is not None
                                and prior_times["available"] <= times["available"], "temporal_correction_order_invalid")
                    except EvidenceInvalid:
                        chain_invalid = True
                        break
                    predecessors[revision] = predecessor
                    revision, times = predecessor, prior_times
            tips = accepted.copy()
            for revision in accepted:
                parent = predecessors.get(revision)
                while parent is not None:
                    tips.discard(parent)
                    parent = predecessors.get(parent)
            if not chain_invalid and len(tips) == 1:
                selected[source] = next(iter(tips))
            else:
                gaps.append({"source_id": source, "reason": "temporal_revision_chain_ambiguous" if chain_invalid or len(tips) > 1 else
                             "temporal_no_qualified_revision"})
        return selected, gaps, sorted(exclusions, key=lambda row: (row["source_id"], row["source_revision"]))


def evaluate_analysis(request: dict[str, Any], selection: Selection, selected: dict[str, str]) -> dict[str, Any]:
    analysis = request["analysis"]
    try:
        rules = pack_policy_rules({"domain_pack": analysis["domain_pack"]})
    except PolicyRuleError as exc:
        raise EvidenceInvalid("temporal_policy_declaration_invalid") from exc
    require(sum(len(facet["source_ids"]) * len(facet["policy_ids"]) for facet in analysis["facets"]) <= 1024,
            "temporal_rule_evaluation_bound_exceeded")
    documents, contexts = [], {}
    for source, revision in sorted(selected.items()):
        item = selection.inspected[revision]
        path = "revisions/" + revision.removeprefix("sha256:") + "/normalized.md"
        documents.append(prepare_document(Document(path=path, scope="normalized", kind="normalized_source",
                         title=extract_title(item["metadata"], item["body"], source), headings=extract_headings(item["body"]),
                         source_ids=[source], body=item["body"])))
        inputs = SimpleNamespace(provenance_by_source_id={source: item["provenance"]})
        contexts[source] = RuleContext(source_id=source, structured_view=item["structured"], structured_view_error=None,
                                      provenance=item["provenance"], question_frontmatter=analysis["question_frontmatter"],
                                      origin_host=host_from_url(item["provenance"].get("origin_url")),
                                      provider_ids=rule_provider_ids(inputs, source), now=selection.cutoff, domain_matches=domain_matches)
    facets = []
    for facet in analysis["facets"]:
        results = []
        for source in facet["source_ids"]:
            for policy in facet["policy_ids"]:
                if source not in contexts:
                    outcome, reasons = "fail", ["temporal_facet_source_unavailable"]
                elif policy not in rules:
                    outcome, reasons = "manual_review", ["temporal_policy_has_no_declarative_rule"]
                else:
                    result = evaluate_rule(rules[policy], contexts[source])
                    outcome, reasons = result.outcome, list(result.reasons)
                    if rules[policy].manual_review_required and outcome == "pass":
                        outcome, reasons = "manual_review", [*reasons, "temporal_policy_requires_recorded_review"]
                results.append({"source_id": source, "source_revision": selected.get(source), "policy_id": policy,
                                "outcome": outcome, "reasons": reasons})
        outcome = "fail" if any(row["outcome"] == "fail" for row in results) else (
            "manual_review" if any(row["outcome"] == "manual_review" for row in results) else "pass")
        facets.append({"id": facet["id"], "evaluated_at": selection.cutoff.isoformat(), "outcome": outcome, "results": results})
    grounding = []
    for assertion in analysis["grounding"]:
        revision = selected.get(assertion["source_id"])
        source = selection.inspected.get(revision) if revision else None
        resolved = resolve_pointer(source["structured"], assertion["pointer"]) if source and source["structured"] is not None else None
        scalar = canonical_scalar(resolved.value) if resolved is not None and resolved.ok else None
        require(scalar is None or len(scalar) <= 4096, "temporal_grounded_scalar_bound_exceeded")
        matches = scalar is not None and expected_matches(resolved.value, assertion["expected"])
        grounding.append({"id": assertion["id"], "source_id": assertion["source_id"], "source_revision": revision,
                          "pointer": assertion["pointer"], "value": scalar, "outcome": "pass" if matches else "fail",
                          "reason": "matched" if matches else "temporal_grounding_unavailable" if scalar is None else "value_mismatch"})
    return {"retrieval": {"query": analysis["query"], "engine": "lexical", "cache_used": False, "wiki_included": False,
                           "indexed_documents": len(documents), "results": rank_documents(documents, analysis["query"], BOUNDS["sources"])},
            "facets": facets, "grounding": grounding}


def evaluate(root: Path, config: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
    raw = canonical_bytes(supplied)
    require(len(raw) <= BOUNDS["request_bytes"], "temporal_request_bound_exceeded")
    request = request_document(document(raw, BOUNDS["request_bytes"]))
    with current_view(root, config) as view:
        cutoff = view.now if request["mode"] == "current" else timestamp(request["cutoff"])
        require(cutoff <= view.now, "temporal_cutoff_exceeds_host_clock")
        state = checkpoint_state(view, request["checkpoint"])
        selection = Selection(state, view, request, cutoff)
        selected, gaps, exclusions = selection.select()
        analysis = evaluate_analysis(request, selection, selected)
        included = {revision for revision in selected.values()}
        for revision in selected.values():
            included.update(selection.inspected[revision]["ancestors"])
        payload = {"schema_version": RESULT_SCHEMA, "mode": request["mode"], "cutoff": cutoff.isoformat(), "checkpoint": state.checkpoint,
                   "checkpoint_observed_at": state.last_observed.isoformat(), "request_id": content_id("evidence-temporal-request/v1", request),
                   "analysis_id": content_id("evidence-temporal-analysis/v1", request["analysis"]), "implementation_id": implementation_identity(),
                   "authority": authority_basis(view.trust), "complete": not gaps, "gaps": gaps, "exclusions": exclusions,
                   "selected": [selection.inspected[revision]["identity"] for _source, revision in sorted(selected.items())],
                   "lineage": [{**selection.inspected[revision]["identity"], "parents": state.nodes[revision]["parents"],
                                "qualifications": selection.inspected[revision]["qualifications"],
                                "execution": selection.inspected[revision]["execution"]} for revision in sorted(included)],
                   "bounds": dict(BOUNDS), **analysis}
        result_id = content_id("evidence-temporal-result/v1", payload)
        require(len(canonical_bytes(payload)) <= BOUNDS["artifact_bytes"], "temporal_result_bound_exceeded")
        view.revalidate(root, config)
        return {"result_id": result_id, "result": payload, "current_use": {"authorized": False,
                "reason": "temporal_analysis_is_not_a_current_use_approval"},
                "retrieval_permission": {"evaluated_at": view.now.isoformat(), "checkpoint": view.state.checkpoint,
                                         "purpose": request["purpose"], "consumer": request["consumer"]}}
