"""Declared operation boundaries, independent of runtime introspection."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

OPERATION_MATRIX_VERSION = "1"

# API name, shell entry point, filesystem effects, locking, subprocess boundary.
_ROWS = (
    ("workspace.open", None, "none; validates an existing workspace", "none", "none"),
    ("workspace.close", None, "none; closes the handle", "none", "none"),
    ("workspace.versions", "evidence-wiki contract (installation metadata only)", "none", "none", "none"),
    ("workspace.status", "evidence-wiki status", "derived status cache unless no_cache", "no lock; unique temporary file and atomic cache replacement", "none"),
    ("workspace.export_answers", "evidence-wiki export", "none; CLI --output writes a caller-selected file", "none; live reads", "none"),
    ("workspace.publish_selected", "evidence-wiki publication --question SLUG", "private temporary capture; no live workspace writes; CLI --output must be outside workspace", "optimistic byte capture and revalidation; at most three evaluation attempts", "none"),
    ("workspace.doctor", "evidence-wiki doctor", "none", "none", "external tooling version probes with individual timeouts"),
    ("coverage.evaluate", "scripts/coverage_manifest.py evaluate", "rewrites the selected coverage manifest", "none; host must serialize coverage writers", "none"),
    ("grounding.verify", "evidence-wiki grounding verify", "question grounding verification only with write=True", "per-question lock when writing", "none"),
    ("normalize.verify", "evidence-wiki normalize verify", "none", "none", "none"),
    ("normalize.profiles", "evidence-wiki normalize profiles", "none", "none", "none"),
    ("normalize.validate_packet", "evidence-wiki normalize packet", "none", "bounded optimistic delivery capture; at most three attempts", "none"),
    ("normalize.validate_execution", "evidence-wiki normalize execution", "none", "bounded original closure capture; external host authority checked at one current clock", "none"),
    ("usage.status", "evidence-wiki usage status", "none", "shared host state lock", "none"),
    ("usage.transact", "evidence-wiki usage transact", "authenticated events and sanitized revision bytes in private host state", "exclusive host state lock; expected checkpoint and idempotent request ID", "none"),
    ("usage.check", "evidence-wiki usage check", "none", "shared host state lock and current external authority", "none"),
    ("usage.lineage", "evidence-wiki usage lineage", "none", "shared host state lock", "none"),
    ("usage.materialize", "evidence-wiki usage materialize", "exact approved normalized bytes; unique temporary file and atomic replacement", "exclusive host state lock; expected content hash for replacement", "none"),
    ("snapshots.prepare", "evidence-wiki snapshot prepare", "none", "shared host state lock and current external authority", "none"),
    ("snapshots.export", "evidence-wiki snapshot export", "immutable canonical snapshot; unique temporary file and atomic publication", "exclusive host state lock; prior host registration; current authority rechecked", "none"),
    ("snapshots.check", "evidence-wiki snapshot check", "none", "shared host state lock and current external authority", "none"),
    ("verify_snapshot", "evidence-wiki snapshot verify", "none", "no workspace; explicit independent trust bytes", "none"),
    ("questions.claim", "scripts/question_claim.py claim", "question claim and activity log", "per-question lock; separate log append lock", "none"),
    ("questions.release", "scripts/question_claim.py release", "question claim and activity log", "per-question lock; separate log append lock", "none"),
    ("questions.answer", "scripts/question_resolve.py answer", "question resolution and activity log", "per-question lock; separate log append lock", "none"),
    ("questions.block", "scripts/question_resolve.py block", "question resolution and activity log", "per-question lock; separate log append lock", "none"),
    ("questions.defer", "scripts/question_resolve.py defer", "question resolution and activity log", "per-question lock; separate log append lock", "none"),
    ("questions.reject", "scripts/question_resolve.py reject", "question resolution and activity log", "per-question lock; separate log append lock", "none"),
    ("questions.reopen", "scripts/question_resolve.py reopen", "question and request state, activity log, or delegated reopen claim", "owning per-question/request/claim locks; separate log append lock", "none"),
    ("questions.approve", "scripts/question_resolve.py approve", "question review and activity log", "per-question lock; separate log append lock", "none"),
    ("questions.review", "scripts/question_resolve.py review", "question policy review and activity log", "per-question lock; separate log append lock", "none"),
    ("questions.set_grounding", "scripts/question_resolve.py set-grounding", "question grounding and activity log", "per-question lock; separate log append lock", "none"),
    ("questions.add_batch", "evidence-wiki questions add", "question pages, index, handoff, activity log; dry_run previews", "no batch transaction lock; host must serialize intake writers", "none"),
    ("orchestrate.start", "evidence-wiki orchestrate start", "creates session state and retained artifacts", "controller-owned session driver lock", "one version-matched controller process per call"),
    ("orchestrate.session.next", "evidence-wiki orchestrate next", "session state, order, claims and recovery artifacts", "controller-owned session driver lock", "one version-matched controller process per call"),
    ("orchestrate.session.submit", "evidence-wiki orchestrate submit", "session state, results and committed workspace effects", "controller-owned session driver lock and effect-specific locks", "one version-matched controller process per call"),
    ("orchestrate.session.status", "evidence-wiki orchestrate status", "none", "read-only session inspection", "one version-matched controller process per call"),
    ("fleet_status", "evidence-wiki fleet-status", "per-target derived caches unless no_cache", "no lock; atomic per-target cache replacement", "none"),
    ("contract", "evidence-wiki contract", "none in a workspace; package resources may be extracted privately", "process-local resource lifetime", "none"),
)

_CLI_ONLY = (
    ("workspace.create", "evidence-wiki init / deploy", "workspace lifecycle and installation"),
    ("workspace.upgrade", "evidence-wiki upgrade", "workspace lifecycle and pending-order guards"),
    ("pack.lifecycle", "evidence-wiki pack validate / adopt / refresh", "pack lifecycle and transaction recovery"),
    ("normalize.sources", "scripts/normalize_sources.py", "full normalization and optional external tool execution"),
    ("sources.inventory", "scripts/source_inventory.py", "source registration and manifest ownership"),
    ("source_requests.mutate", "scripts/source_requests.py", "request lifecycle and delegated fulfillment"),
    ("coverage.edit", "scripts/coverage_manifest.py init / link / validate", "coverage editing and validation"),
    ("workspace.lint", "scripts/lint.py", "workspace lint report"),
    ("publication.bundle", "evidence-wiki publication bundle", "workspace-wide publication bundle"),
    ("orchestrate.managed", "evidence-wiki orchestrate run / resume", "managed runner lifecycle"),
    ("mcp.serve", "evidence-wiki serve-mcp", "long-running server lifecycle"),
)


def operation_matrix() -> dict[str, Any]:
    """Return caller-owned capability boundaries for boot-time negotiation."""
    return deepcopy({
        "matrix_version": OPERATION_MATRIX_VERSION,
        "operations": [dict(zip(("operation", "cli", "mutation", "locking", "subprocess"), row, strict=True)) for row in _ROWS],
        "cli_only": [dict(zip(("operation", "cli", "reason"), row, strict=True)) for row in _CLI_ONLY],
        "timeout_policy": "Hosts retain an outer operation timeout, including controller subprocess time.",
    })
