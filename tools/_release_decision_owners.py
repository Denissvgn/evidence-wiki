"""Read-only release adapters to original qualification owners."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from tools._acceptance_protocol import digest as owner_digest
from tools._release_decision import (
    CATEGORIES,
    METRICS,
    EvidenceUnavailable,
    OriginalCitation,
    OwnerEvidence,
    OwnerReader,
    _original,
    canonical,
)
from tools.measure_integration_costs import _authenticated
from tools.qualify_unattended import replay
from tools.run_comparative_trials import read_private


def unattended_readers(plan, *, report_path, observer_key, expected_report_sha256):
    """Select authenticated fixture owners without dispatching any research.

    The host supplies the original external report head and private key. The
    source replay checks original receipts, inputs, native proofs and deadlines.
    It does not attest live provider quality or malicious-worker isolation.
    """
    path = Path(report_path)
    if plan["scope"].get("kind") != "unattended_original_roles":
        raise ValueError("release_unattended_declared_role_scope_required")
    selected = [source for source in plan["required_sources"] if source["category"] in CATEGORIES]
    if len(selected) != len(CATEGORIES) or {source["category"] for source in selected} != set(CATEGORIES):
        raise ValueError("release_unattended_source_inventory")
    if any(source["source_head_sha256"] != expected_report_sha256 for source in selected):
        raise ValueError("release_unattended_external_head")
    cache, calls = {}, [0]

    def read(source):
        # One shared original owner is re-read for each complete read round.
        if calls[0] % len(selected) == 0:
            cache.clear()
            try:
                raw = read_private(path, 4_194_304)
                report = replay(path, observer_key=observer_key,
                                expected_report_sha256=expected_report_sha256)
                original = _original(raw)
                if canonical(original) != canonical(report) or owner_digest(original) != expected_report_sha256:
                    raise ValueError("release_unattended_raw_authentication_changed")
                if report.get("preliminary") is not False:
                    raise ValueError("release_unattended_final_required")
                if report["candidate_sha256"] != plan["candidate_sha256"]:
                    raise ValueError("release_unattended_candidate_changed")
                if report.get("observation_class") != "fixture_mechanics":
                    raise ValueError("release_unattended_profile")
                execution = _authenticated(path.parent / "execution.json", observer_key)
                if owner_digest(execution) != report["execution_sha256"] or read_private(path, 4_194_304) != raw:
                    raise ValueError("release_unattended_execution_or_report_changed")
                cache.update(raw=raw, report=report, expires_at=execution["deadline_utc"])
            except (ValueError, OSError, KeyError, TypeError) as error:
                cache["error"] = type(error).__name__
        calls[0] += 1
        if "error" in cache:
            raise EvidenceUnavailable("unattended_original_owner_unavailable")
        report, category = cache["report"], source["category"]
        status = "pass" if report["decision"] == "qualified" else "unknown"
        if report.get("failures"):
            status = "critical_failure"
        if category == "semantic" and plan["profile"] != "technical_fixture":
            status = "unknown"
        metrics = None
        citations = [OriginalCitation("/decision", report["decision"]),
                     OriginalCitation("/candidate_sha256", report["candidate_sha256"])]
        if category == "cost":
            metrics = {name: None for name in METRICS}
            elapsed = report["costs"]["automated"]["metrics"]["elapsed_seconds"]
            # The original five-role inventory is complete while wider host
            # telemetry remains unknown. This budget is only for that inventory.
            coverage = report["costs"]["coverage"]
            value = elapsed.get("known_subtotal") if (
                coverage["missing_attempt_ids"] == [] and coverage["missing_metrics"] == []
                and report["denominators"]["terminal_tasks"] == 5
                and report["denominators"]["started_stop_unknown_tasks"] == 0
            ) else None
            if value is not None:
                metrics["elapsed_seconds"] = value
                citations.append(OriginalCitation("/costs/automated/metrics/elapsed_seconds/known_subtotal", value))
        evidence_class = "simulated" if category in {"conformance", "semantic", "integration"} else "deterministic_verified"
        return OwnerEvidence(
            raw=cache["raw"], source_head_sha256=expected_report_sha256,
            candidate_sha256=report["candidate_sha256"], host_sha256=plan["host_sha256"],
            scope=plan["scope"], observed_at=datetime.now(timezone.utc).isoformat(),
            expires_at=cache["expires_at"], evidence_class=evidence_class,
            projection={"status": status, "metrics": metrics, "judge": None, "business": None},
            citations=tuple(citations),
        )

    # Freeze references at creation; report data cannot select a reader.
    return tuple(OwnerReader(source["category"], source["owner_ref"],
                             lambda source=dict(source): read(source)) for source in selected)


def original_raw_sha256(path):
    """Compute a bounded private artifact pin; this is not authentication."""
    return hashlib.sha256(read_private(Path(path), 4_194_304)).hexdigest()
