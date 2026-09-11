#!/usr/bin/env python3
"""Coherent snapshot preparation, host registration, and immutable publication."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from _evidence_authority import EvidenceInvalid, read_host_policy
from _evidence_usage import UsageState, UsageView, current_view
from _snapshot_qualifications import qualify_source
from _snapshot_verifier import (
    BOUNDS,
    CONTRACT,
    EXECUTION_CONTRACT,
    EXECUTION_MANIFEST_SCHEMA,
    EXECUTION_SCHEMA,
    MANIFEST_SCHEMA,
    SCHEMA,
    TEMPORAL_CONTRACT,
    TEMPORAL_MANIFEST_SCHEMA,
    TEMPORAL_SCHEMA,
    SnapshotInvalid,
    binding,
    canonical,
    document,
    execution,
    execution_input_context,
    identifier,
    instant,
    name,
    normalized_claims,
    public_policy,
    require,
    selection,
    temporal_source,
    validate_snapshot,
    validate_use,
    validity_deadline,
)
from _usage_materialization import publish_file


def implementation_identity() -> str:
    directory = Path(__file__).resolve().parent
    stems = ("_snapshot_export", "_snapshot_verifier", "_snapshot_qualifications", "_market_evidence", "_evidence_usage",
             "_host_evidence_store", "_evidence_authority", "_record_artifacts", "_qualified_packet",
             "_usage_materialization", "_temporal_contract", "_evidence_revision")
    paths = [directory / (stem + ".py") for stem in stems]
    paths.extend(sorted(directory.glob("_packet_vendor_*.py")))
    return identifier("evidence-snapshot-implementation/v1", {path.name: binding(path.read_bytes()) for path in paths})


def authority_bytes(root: Path, view: UsageView) -> bytes:
    raw = read_host_policy(root)
    require(binding(raw)["content_hash"] == view.trust["content_hash"], "snapshot_host_trust_changed")
    return raw


def approved(view: UsageView, revision: str, request: dict[str, Any]) -> set[str]:
    result = view.check(revision, uses=["training", "export"], purpose=request["purpose"], consumer=request["consumer"])
    require(result["eligible"] and result["complete"], result["reasons"][0] if result["reasons"] else "snapshot_usage_incomplete")
    return {revision, *result["ancestors"]}


def frozen_source(revision: str, record: dict[str, Any]) -> dict[str, Any]:
    return {"source_revision": revision, "source_id": record["source_id"], "descriptor": record["descriptor"],
            "files": {path: binding(data) for path, data in sorted(record["files"].items())},
            "grant": record["grant"], "scrub": record["scrub"], "observed_at": record["observed_at"],
            "deposit_event_id": record["event_id"], "authorization_event_id": record.get("authorization_event_id")}


def candidate(view: UsageView, request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Freeze exact bytes at an accepted host generation, with bounded exclusions."""
    state, policy = view.state, view.trust["policy"]
    require(state.last_observed is not None, "snapshot_host_state_uninitialized")
    frozen = UsageView(state, view.trust, state.last_observed)
    temporal = request.get("temporal")
    temporal_state = None
    if temporal is not None:
        require(instant(temporal["cutoff"]) <= view.now, "snapshot_temporal_cutoff_exceeds_host_clock")
        event = next((event for event in state.events if event["event_id"] == temporal["checkpoint"]), None)
        require(event is not None, "snapshot_temporal_checkpoint_unknown")
        temporal_state = UsageState(state.root, state.state_id, canonical({**state.document, "events": state.events[:event["sequence"]]}))
    examples, exclusions, all_nodes, all_files = [], [], set(), {}
    historical_inputs = False
    for revision in request["source_revisions"]:
        try:
            require(revision in state.revisions, "snapshot_source_revision_unknown")
            closure = approved(view, revision, request)
            approved(frozen, revision, request)
            require(len(closure) <= BOUNDS["lineage_nodes"], "snapshot_lineage_bound_exceeded")
            record = state.revisions[revision]
            prefix = record["descriptor"]["evidence_root"]
            require(prefix is not None, "snapshot_execution_record_missing")
            originals = {path[len(prefix) + 1:]: raw for path, raw in record["files"].items() if path.startswith(prefix + "/")}
            result = execution(originals, record["source_id"], policy, frozen.now, historical=True)
            execution(originals, record["source_id"], policy, view.now, historical=True)
            if temporal is not None:
                execution(originals, record["source_id"], policy, instant(temporal["cutoff"]), historical=True)
            require(result["example"]["label"] != "negative" or request["include_negative_examples"], "snapshot_negative_not_requested")
            for item in result["inputs"]:
                parent = item["source_revision"]
                require(parent in closure - {revision} and parent in state.revisions
                        and state.revisions[parent]["source_id"] == item["source_id"]
                        and {key: item[key] for key in ("content_hash", "size_bytes")} in (
                            binding(raw) for raw in state.revisions[parent]["files"].values()),
                        "snapshot_execution_input_lineage_missing")
            files = {}
            for node_id in sorted(closure):
                if node_id not in state.revisions:
                    continue
                source = frozen_source(node_id, state.revisions[node_id])
                validate_use(source, policy, frozen.now, request)
                if temporal_state is not None:
                    require(node_id in temporal_state.revisions, "snapshot_temporal_source_outside_checkpoint")
                    proof = temporal_state.availability.get(node_id) if temporal["mode"] == "historical-available" else None
                    temporal_source(source, state.revisions[node_id]["files"], proof, policy, temporal, temporal_state.last_observed)
                normalized = source["descriptor"]["normalized_path"]
                if normalized is not None:
                    normalized_claims(state.revisions[node_id]["files"][normalized], source["source_id"], node_id)
                qualify_source(source, state.revisions[node_id]["files"])
                files[node_id] = state.revisions[node_id]["files"]
                ancestor_prefix = source["descriptor"]["evidence_root"]
                originals = {path[len(ancestor_prefix) + 1:]: raw for path, raw in files[node_id].items()
                             if ancestor_prefix is not None and path.startswith(ancestor_prefix + "/")}
                if node_id != revision and "execution-record.json" in originals:
                    execution(originals, source["source_id"], policy, frozen.now, historical=True)
                    ancestor = execution(originals, source["source_id"], policy, view.now, historical=True)
                    if temporal is not None:
                        execution(originals, source["source_id"], policy, instant(temporal["cutoff"]), historical=True)
                    for item in ancestor["inputs"]:
                        parent = item["source_revision"]
                        require(parent in approved(view, node_id, request) - {node_id} and parent in state.revisions
                                and state.revisions[parent]["source_id"] == item["source_id"]
                                and {key: item[key] for key in ("content_hash", "size_bytes")} in (
                                    binding(raw) for raw in state.revisions[parent]["files"].values()),
                                "snapshot_execution_input_lineage_missing")
            require(len(all_nodes | closure) <= BOUNDS["lineage_nodes"]
                    and len(set(all_files) | set(files)) <= BOUNDS["source_revisions"], "snapshot_closure_bound_exceeded")
            input_context = execution_input_context({key: frozen_source(key, state.revisions[key]) for key in files}, files,
                                                     {key: state.nodes[key] for key in closure}, state.availability,
                                                     policy, view.now, state.last_observed)
            historical_inputs |= input_context["historical"]
            examples.append({"source_revision": revision, **result["example"]})
            all_nodes.update(closure)
            all_files.update(files)
        except (SnapshotInvalid, EvidenceInvalid) as exc:
            exclusions.append({"source_revision": revision, "reason": str(exc)})
    require(examples, "snapshot_no_eligible_examples")
    blobs = {binding(raw)["content_hash"]: raw for files in all_files.values() for raw in files.values()}
    require(len(blobs) <= BOUNDS["artifact_blobs"] and sum(map(len, blobs.values())) <= BOUNDS["decoded_bytes"],
            "snapshot_blob_bound_exceeded")
    sources = [frozen_source(revision, state.revisions[revision]) for revision in sorted(all_files)]
    contract = EXECUTION_CONTRACT if historical_inputs else TEMPORAL_CONTRACT if temporal is not None else CONTRACT
    schema = EXECUTION_MANIFEST_SCHEMA if historical_inputs else TEMPORAL_MANIFEST_SCHEMA if temporal is not None else MANIFEST_SCHEMA
    manifest = {"schema_version": schema, "contract": contract,
                "exporter": {"name": "evidence-wiki", "version": contract, "implementation": implementation_identity()},
                "selection": request,
                "captured_state": {"state_id": state.state_id, "workspace_binding": state.binding,
                                   "checkpoint": state.checkpoint, "observed_at": state.last_observed.isoformat()},
                "policy": {"trust_content_hash": view.trust["content_hash"], "public_trust": public_policy(policy),
                           "required_uses": ["export", "training"], "retention": "host-managed"},
                "sources": sources, "lineage": [state.nodes[node_id] for node_id in sorted(all_nodes)],
                "examples": examples, "exclusions": exclusions,
                "qualifications": [{"source_revision": source["source_revision"], **qualification}
                                   for source in sources
                                   for qualification in qualify_source(source, all_files[source["source_revision"]])],
                "coverage": {"selection_complete": True, "artifact_closure_complete": True,
                             "included_count": len(examples), "excluded_count": len(exclusions),
                             "source_count": len(sources), "lineage_count": len(all_nodes), "blob_count": len(blobs)},
                "bounds": dict(BOUNDS)}
    if historical_inputs:
        manifest["execution_inputs"] = {"availability": {revision: state.availability.get(revision) for revision in sorted(all_files)}}
    if temporal_state is not None:
        require(all(state.nodes[node]["kind"] == "source" for node in all_nodes), "snapshot_temporal_non_source_lineage_unsupported")
        manifest["temporal"] = {"checkpoint_observed_at": temporal_state.last_observed.isoformat(),
                                "availability": {revision: temporal_state.availability.get(revision)
                                                 if temporal["mode"] == "historical-available" else None
                                                 for revision in sorted(all_files)}}
    return manifest, blobs


def prepare(root: Path, config: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
    request = selection(document(canonical(supplied), 1024 * 1024))
    with current_view(root, config) as view:
        authority_bytes(root, view)
        manifest, _blobs = candidate(view, request)
        deadline = validity_deadline(manifest, {source["source_revision"]: view.state.revisions[source["source_revision"]]["files"]
                                               for source in manifest["sources"]})
        view.revalidate(root, config)
        authority_bytes(root, view)
        require(view.now < deadline, "snapshot_authority_expired")
        snapshot_id = identifier(manifest["schema_version"], manifest)
        return {"schema_version": "evidence-snapshot-preparation/v1", "snapshot_id": snapshot_id, "manifest": manifest,
                "registration": {"schema_version": "evidence-usage-command/v1", "state_id": view.state.state_id,
                                 "workspace_binding": view.state.binding, "expected_checkpoint": view.state.checkpoint,
                                 "action": "register", "body": {"node_id": snapshot_id, "kind": "snapshot",
                                     "parents": [example["source_revision"] for example in manifest["examples"]]}}}


def registered_bundle(root: Path, view: UsageView, request: dict[str, Any], request_id: str) -> tuple[bytes, dict[str, Any]]:
    event = view.state.requests.get(name(request_id))
    require(event is not None and event["command"]["payload"]["action"] == "register"
            and event["command"]["payload"]["body"]["kind"] == "snapshot", "snapshot_registration_missing")
    base = UsageState(root, view.state.state_id, canonical({**view.state.document, "events": view.state.events[:event["sequence"] - 1]}))
    historical = UsageView(base, view.trust, base.last_observed)
    manifest, blobs = candidate(historical, request)
    schema = EXECUTION_SCHEMA if "execution_inputs" in manifest else TEMPORAL_SCHEMA if "temporal" in request else SCHEMA
    bundle = {"schema_version": schema,
              "snapshot_id": identifier(manifest["schema_version"], manifest), "manifest": manifest,
              "registration": event["command"], "blobs": {key: base64.b64encode(raw).decode("ascii") for key, raw in sorted(blobs.items())}}
    data = canonical(bundle)
    validate_snapshot(data, authority_bytes(root, view), current_time=view.now, qualify_source=qualify_source)
    approved(view, bundle["snapshot_id"], request)
    return data, event


def export(root: Path, config: dict[str, Any], supplied: dict[str, Any], request_id: str) -> dict[str, Any]:
    request = selection(document(canonical(supplied), 1024 * 1024))
    with current_view(root, config, exclusive=True) as view:
        data, event = registered_bundle(root, view, request, request_id)
        bundle = document(data)
        relative = "exports/evidence-snapshots/" + bundle["snapshot_id"].removeprefix("sha256:") + ".json"

        def revalidate():
            view.revalidate(root, config)
            result = validate_snapshot(data, authority_bytes(root, view), current_time=view.now, qualify_source=qualify_source)
            view.revalidate(root, config)
            require(view.now < instant(result["valid_until"]), "snapshot_authority_expired")

        revalidate()
        created = publish_file(root, relative, data, None, before_publish=revalidate)
        revalidate()
        return {"schema_version": "evidence-snapshot-export/v1", "snapshot_id": bundle["snapshot_id"],
                "path": relative, "created": created, **binding(data), "checkpoint": view.state.checkpoint,
                "registration_event_id": event["event_id"]}


def check(root: Path, config: dict[str, Any], data: bytes) -> dict[str, Any]:
    with current_view(root, config) as view:
        result = validate_snapshot(data, authority_bytes(root, view), current_time=view.now, qualify_source=qualify_source)
        event = view.state.requests.get(result["registration"]["payload"]["request_id"])
        require(event is not None and event["command"] == result["registration"], "snapshot_registration_missing")
        request = result["manifest"]["selection"]
        decision = view.check(result["snapshot_id"], uses=["training", "export"], purpose=request["purpose"], consumer=request["consumer"])
        view.revalidate(root, config)
        require(view.now < instant(result["valid_until"]), "snapshot_authority_expired")
        return {"schema_version": "evidence-snapshot-current-use/v1", "snapshot_id": result["snapshot_id"], **decision,
                "current_use": "authorized" if decision["eligible"] else "denied"}
