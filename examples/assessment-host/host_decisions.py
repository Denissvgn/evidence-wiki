"""Optional host adapter: durable decisions and reconciliation, using only stdlib."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def canonical(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(data.encode()) > 1024 * 1024:
        raise ValueError("host_decision_input_too_large")
    return data


class DecisionStore:
    """Use a private host-owned database outside the research workspace."""

    def __init__(self, path: Path):
        self.path = path
        with self.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS decisions (
                decision_id TEXT PRIMARY KEY, binding TEXT NOT NULL,
                status TEXT NOT NULL, reason TEXT, receipt TEXT
            )""")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def read(self, decision_id):
        with self.connect() as connection:
            row = connection.execute("SELECT status, reason, receipt FROM decisions WHERE decision_id = ?",
                                     (decision_id,)).fetchone()
        if row is None:
            raise ValueError("host_decision_missing")
        return {"decision_id": decision_id, "status": row[0], "reason": row[1],
                "receipt": None if row[2] is None else json.loads(row[2])}

    def finish(self, decision_id, receipt):
        # Only a reconciled or accepted receipt can finish a pending submission.
        with self.connect() as connection:
            if receipt is None:
                connection.execute("UPDATE decisions SET status = 'uncertain' WHERE decision_id = ? AND status = 'pending'",
                                   (decision_id,))
            else:
                encoded = canonical(receipt)
                connection.execute("UPDATE decisions SET status = 'accepted', receipt = ? WHERE decision_id = ? AND status IN ('pending', 'uncertain')",
                                   (encoded, decision_id))
        return self.read(decision_id)

    def decide(self, decision_id, action, envelope, *, check_evidence, approve, risk_check, current_state, submit, reconcile):
        """Callbacks are trusted host code; evidence content supplies no authority.

        submit(action, decision_id) and reconcile(decision_id) return a receipt
        only for a confirmed acceptance. None means the outcome is uncertain.
        They own provider idempotency, partial outcomes and subsequent lifecycle.
        """
        if not isinstance(decision_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", decision_id):
            raise ValueError("host_decision_id_invalid")
        action, envelope = json.loads(canonical(action)), json.loads(canonical(envelope))
        binding = hashlib.sha256(canonical({"action": action, "envelope": envelope}).encode()).hexdigest()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT binding, status FROM decisions WHERE decision_id = ?", (decision_id,)).fetchone()
            if existing is not None:
                if existing[0] != binding:
                    raise ValueError("host_decision_id_conflict")
                first_delivery = False
            else:
                first_delivery = True
                report = check_evidence(envelope)
                assessment_id = envelope["payload"]["body"]["assessment"]["assessment_id"]
                reason = None
                if report.get("eligible") is not True or report.get("assessment_id") != assessment_id:
                    reason = "evidence_ineligible"
                elif approve(action, assessment_id) is not True:
                    reason = "approval_denied"
                elif risk_check(action) is not True:
                    reason = "risk_denied"
                elif current_state(action) is not True:
                    reason = "current_state_denied"
                connection.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, NULL)",
                                   (decision_id, binding, "pending" if reason is None else "declined", reason))
            # Reservation commits before the external callback. A crash from
            # this point leaves a durable pending decision for reconciliation.
        decision = self.read(decision_id)
        if decision["status"] in {"accepted", "declined"}:
            return decision
        try:
            receipt = submit(action, decision_id) if first_delivery else reconcile(decision_id)
        except Exception:  # An exception does not prove the provider rejected it.
            receipt = None
        return self.finish(decision_id, receipt)
