#!/usr/bin/env python3
"""Resolve historical execution inputs only from accepted immutable host state."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from _evidence_authority import EvidenceInvalid
from _evidence_usage import configured, current_view
from _snapshot_qualifications import qualify_source
from _snapshot_verifier import SnapshotInvalid, binding, execution_input_context, instant, require
from _usage_gate import claims, normalized_relative, read_workspace_file, resolve_revision


def assess_inputs(root: Path, config: dict[str, Any], report: dict[str, Any], trust: dict[str, Any] | None,
                  now: datetime, source_record: dict[str, Any] | None) -> dict[str, Any]:
    """Recheck exact input ancestry, current rights and authority before returning."""
    try:
        require(configured(config), "execution_historical_accepted_inputs_required")
        with current_view(root, config) as view:
            require(trust is not None and trust["content_hash"] == view.trust["content_hash"], "execution_historical_trust_changed")
            require(view.state.last_observed is not None and view.state.last_observed <= now <= view.now,
                    "execution_historical_clock_outside_host_state")
            if source_record is not None:
                reasons, claimed, _present = claims([source_record], ["retrieval"])
                require(not reasons, reasons[0] if reasons else "execution_usage_invalid")
                normalized = None if claimed is not None else read_workspace_file(root, normalized_relative(config, report["source_id"]))
                revision, _record = resolve_revision(view, report["source_id"], claimed, normalized)
            else:
                matches = []
                for revision, record in view.state.revisions.items():
                    prefix = record["descriptor"]["evidence_root"]
                    originals = {} if prefix is None else {
                        path[len(prefix) + 1:]: binding(raw) for path, raw in record["files"].items() if path.startswith(prefix + "/")
                    }
                    if record["source_id"] == report["source_id"] and originals == report["originals"]:
                        matches.append(revision)
                require(len(matches) == 1, "execution_historical_revision_missing_or_ambiguous")
                revision = matches[0]
            decision = view.check(revision, uses=["retrieval"], purpose="research", consumer="evidence-wiki")
            require(decision["eligible"] and decision["complete"], decision["reasons"][0] if decision["reasons"] else "execution_usage_incomplete")
            closure = {revision, *decision["ancestors"]}
            sources = {key: {**view.state.revisions[key], "source_revision": key} for key in closure if key in view.state.revisions}
            files = {key: source["files"] for key, source in sources.items()}
            prefix = sources[revision]["descriptor"]["evidence_root"]
            require(prefix is not None and report["originals"] == {
                path[len(prefix) + 1:]: binding(raw) for path, raw in files[revision].items() if path.startswith(prefix + "/")
            }, "execution_historical_original_binding_changed")
            for key, source in sources.items():
                qualify_source(source, files[key])
            view.revalidate(root, config)
            result = execution_input_context(sources, files, {key: view.state.nodes[key] for key in closure},
                                             view.state.availability, view.trust["policy"], view.now, view.state.last_observed)
            verdict = {"eligible": True, "reason": "execution_historical_inputs_qualified", "source_revision": revision,
                       "checkpoint": view.state.checkpoint, "evaluated_at": view.now.isoformat(), **result}
        require(view.now < instant(result["valid_until"]), "execution_historical_authority_expired")
        return {**verdict, "evaluated_at": view.now.isoformat()}
    except (EvidenceInvalid, SnapshotInvalid) as exc:
        reason = str(exc)
    except (KeyError, TypeError, ValueError, OSError, RecursionError):
        reason = "execution_historical_context_invalid"
    return {"eligible": False, "complete": False, "reason": reason}
