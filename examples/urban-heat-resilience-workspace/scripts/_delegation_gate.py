#!/usr/bin/env python3
"""Refuse workspace mutations that no pending work order accounts for.

Under ``orchestration.acquisition: delegated`` the host is the acquirer. It fulfils source
requests and reopens questions with the same workspace commands an in-workspace worker
uses — but it must do so *while executing the work order that scopes them*, not between
actions. A fulfilment recorded after ``submit`` returns is a real mutation of durable
evidence state that no work order accounts for, which is the one thing the controller's
integrity baseline exists to prevent.

This module answers one question for those commands: is this mutation sanctioned by a
pending work order right now? Two rules, one scan:

- a **source request** is sanctioned when a live session's pending order is a delegated
  acquisition order whose scope names it;
- a **question** is sanctioned when a live session's pending order scopes its slug, in any
  phase — research orders legitimately mutate their scoped questions, so keying on the
  scope rather than the phase preserves every existing in-order path.

The gate is deliberately narrow. Without the ``orchestration:`` section, or with no live
session, nothing is refused: an operator working a workspace by hand is not driving a
protocol, and the commands behave exactly as they did before delegation existed.

**The scan takes no locks, by design.** Session and work-order documents are written with
an atomic temp-file replace, so a reader observes one complete document or the previous
one, never a mix. The remaining races resolve safely: a spurious refusal the caller
retries, or an in-scope mutation the postconditions verify anyway. Under the single-driver
rule the delegate and the driver are the same host, so this is not racing a foreign writer
in any supported deployment. Do not add locking here.

Unreadable state fails closed. A session or work order this module cannot parse is not
evidence that a mutation is unsanctioned, so it refuses rather than skipping the file —
corruption must not be a way to open the gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ORCHESTRATIONS_DIR = "runs/orchestrations"
SESSION_FILENAME = "session.json"
WORK_ORDERS_DIR = "work-orders"
LIVE_SESSION_STATUSES = frozenset({"active", "paused"})
ACQUISITION_PHASE = "acquisition"
DELEGATED_ACQUISITION_MODE = "delegated"

# One session document and one work order are small, bounded artifacts. The cap exists so a
# corrupted or hostile file cannot make a CLI command read an unbounded amount.
MAX_CONTROL_DOCUMENT_BYTES = 8 * 1024 * 1024


class DelegationGateError(Exception):
    """A refused out-of-band mutation, or control state the gate could not read."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        remediation: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.remediation = remediation
        self.details = details or {}


def _read_control_document(path: Path, label: str) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_CONTROL_DOCUMENT_BYTES:
            raise DelegationGateError(
                "ORCHESTRATION_STATE_UNREADABLE",
                f"{label} is too large to read safely: {path.name}",
                remediation="Restore the orchestration control tree before mutating workspace evidence.",
            )
        document = json.loads(path.read_text(encoding="utf-8"))
    except DelegationGateError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DelegationGateError(
            "ORCHESTRATION_STATE_UNREADABLE",
            f"{label} could not be read: {exc}",
            remediation=(
                "Restore the orchestration control tree; unreadable session state cannot authorize a mutation."
            ),
        ) from exc
    if not isinstance(document, dict):
        raise DelegationGateError(
            "ORCHESTRATION_STATE_UNREADABLE",
            f"{label} is not a JSON object: {path.name}",
            remediation="Restore the orchestration control tree before mutating workspace evidence.",
        )
    return document


def live_pending_orders(project_root: Path) -> list[dict[str, Any]]:
    """Return the pending work order of every live session, with its session id.

    A live session with no pending action contributes an entry with ``work_order: None``:
    the session is still driving, so its existence matters to the caller even though it
    sanctions nothing right now.
    """
    root = project_root / ORCHESTRATIONS_DIR
    if not root.is_dir():
        return []
    pending: list[dict[str, Any]] = []
    for session_path in sorted(root.glob(f"*/{SESSION_FILENAME}")):
        session = _read_control_document(session_path, "orchestration session")
        if session.get("status") not in LIVE_SESSION_STATUSES:
            continue
        orchestration_id = session.get("orchestration_id") or session_path.parent.name
        action_id = session.get("pending_action_id")
        work_order: dict[str, Any] | None = None
        if isinstance(action_id, str) and action_id:
            order_path = session_path.parent / WORK_ORDERS_DIR / f"{action_id}.json"
            if order_path.is_file():
                work_order = _read_control_document(order_path, "orchestration work order")
        pending.append(
            {
                "orchestration_id": str(orchestration_id),
                "action_id": action_id if isinstance(action_id, str) else None,
                "work_order": work_order,
            }
        )
    return pending


def _scope_ids(work_order: dict[str, Any] | None, field: str) -> set[str]:
    if not isinstance(work_order, dict):
        return set()
    scope = work_order.get("scope")
    if not isinstance(scope, dict):
        return set()
    return {value for value in scope.get(field, []) if isinstance(value, str) and value}


def _sanctions_request(work_order: dict[str, Any] | None, request_id: str) -> bool:
    if not isinstance(work_order, dict):
        return False
    if work_order.get("phase") != ACQUISITION_PHASE:
        return False
    if work_order.get("acquisition_mode") != DELEGATED_ACQUISITION_MODE:
        return False
    return request_id in _scope_ids(work_order, "request_ids")


def _sanctions_question(work_order: dict[str, Any] | None, question_slug: str) -> bool:
    # Any phase: a research order mutates its scoped questions, and an acquisition order
    # reopens the ones its requests unblock. The scope is the authorization, not the phase.
    return question_slug in _scope_ids(work_order, "question_slugs")


def require_sanctioned_mutation(
    project_root: Path,
    delegated: bool,
    *,
    request_id: str | None = None,
    question_slug: str | None = None,
    error_code: str,
    subject: str,
    remediation: str,
) -> None:
    """Raise when a live session exists and none of its pending orders sanction this change.

    ``delegated`` is the caller's already-validated acquisition mode. A workspace that does
    not delegate is never gated: its acquisition happens through work orders the controller
    issues to its own providers, and nothing about the CLI changes.
    """
    if not delegated:
        return
    live = live_pending_orders(project_root)
    if not live:
        return
    for entry in live:
        work_order = entry["work_order"]
        if request_id is not None and _sanctions_request(work_order, request_id):
            return
        if question_slug is not None and _sanctions_question(work_order, question_slug):
            return
    raise DelegationGateError(
        error_code,
        (
            f"{subject} is not scoped by any pending work order while a delegated orchestration session "
            "is live; a mutation between actions is unaccounted for in the audit trail"
        ),
        remediation=remediation,
        details={
            "live_sessions": [
                {"orchestration_id": entry["orchestration_id"], "pending_action_id": entry["action_id"]}
                for entry in live
            ],
        },
    )
