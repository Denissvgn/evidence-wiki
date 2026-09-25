"""Fresh canonical state and bounded, explicitly qualified caller observations."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from ._pack_io import identity, json_document, read_file, yaml_document
from .pack_discovery import owner
from .planning_contracts import refuse
from .setup_store import target_key


def reason(error):
    import re

    if type(error) is ValueError and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", str(error)):
        return str(error)
    value = getattr(error, "details", {}) or {}
    value = (
        value.get("reason", value.get("field", "owner_observation_unavailable"))
        if isinstance(value, dict)
        else "owner_observation_unavailable"
    )
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_/-]{0,150}", value)
        else "owner_observation_unavailable"
    )


def optional(call):
    try:
        return {"state": "observed", "result": call()}
    except (Exception, SystemExit) as error:
        return {"state": "unavailable", "reason": reason(error), "error_code": getattr(error, "error_code", None)}


def setup_observation(root, files):
    raw = files.get("docs/research-requirements.json")
    if raw is None:
        return {"state": "not_recorded", "trust": "local_unauthenticated"}
    try:
        target = json_document(raw)["request"]["payload"]["target"]
        writable = Path(target["writable_root"]).resolve()
        if writable / target["relative_path"] != root:
            raise ValueError("setup_target_mismatch")
        folder = writable / ".evidence-wiki/setup/targets" / target_key(target) / "transactions"
        entries = list(folder.iterdir()) if folder.exists() else []
        if len(entries) != 1 or entries[0].is_symlink():
            return {"state": "absent_or_ambiguous", "trust": "local_unauthenticated"}
        value = json_document(read_file(entries[0], "receipt.json"))
        return {
            "state": "recorded",
            "status": value["status"],
            "plan_id": value["plan_id"],
            "observed_at": value["observed_at"],
            "current_readiness": "not_inferred",
            "trust": "local_unauthenticated",
        }
    except (Exception, SystemExit):
        return {"state": "unavailable", "trust": "local_unauthenticated", "current_readiness": "not_inferred"}


class ResearchObservation:
    """Bracket all owner reads with a canonical revision; never populate status caches."""

    def __init__(self, target, *, run_id=None):
        selected = os.environ.get("EVIDENCE_WIKI_PYTHON")
        if selected and Path(selected).absolute() != Path(sys.executable).absolute():
            refuse("caller_selected_interpreter_mismatch", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
        self.root = Path(target).expanduser().resolve(strict=True)
        self.root_identity = identity(self.root)
        self.revisions = owner("_evidence_revision")
        self.capture = self.revisions.capture_workspace(self.root)
        self.config = yaml_document(self.capture.files["research.yml"])
        if not isinstance(self.config, dict):
            refuse("caller_configuration_invalid")
        self.integrations_qualified = owner("_caller_context").builtin_integrations(self.config)
        self.policy = owner("_strict_evidence").resolve_policy(self.root, self.config)
        self.controls = optional(lambda: owner("_caller_context").capture(self.root))
        self.status = (
            owner("workspace_status").run_status_report(self.root, no_cache=True, run_id=run_id)
            if self.integrations_qualified
            else {
                "availability": "unavailable",
                "reason": "caller_provider_requires_explicit_qualification",
                "smoke": {"ok": False, "observation": "not_run"},
                "readiness": {"verdict": "attention_required"},
            }
        )
        q = owner("question_status")
        self.questions = q.collect_questions(q.questions_directory(self.root, self.config))
        if len(self.questions) > 300:
            refuse("research_question_bound", "ONBOARDING_LIMIT")
        requests = owner("source_requests")
        self.requests = requests.load_requests(requests.requests_path(self.root, self.config))
        if len(self.requests) > 200:
            refuse("research_request_bound", "ONBOARDING_LIMIT")
        self.mapping = optional(lambda: owner("_caller_context").original_questions(self.root, self.config))
        controller = self.status.get("run_controller", {})
        chosen = run_id or controller.get("run_id")
        self.run = owner("run_controller").load_run_state(self.root, chosen) if chosen else None
        self.managed = owner("_delegation_gate").live_pending_orders(self.root)
        self.strict = (
            optional(lambda: owner("strict_evidence").run_operation(self.root, "check"))
            if self.policy and self.integrations_qualified
            else {"state": "unavailable", "reason": "caller_provider_requires_explicit_qualification"}
            if self.policy
            else {"state": "not_configured"}
        )
        calculation_started = perf_counter()
        self.computation = (
            owner("_computation_service").status(self.root, self.config)
            if self.integrations_qualified
            else {
                "status": "blocked",
                "result_id": None,
                "measurement": None,
                "reasons": ["caller_provider_requires_explicit_qualification"],
            }
            if self.config.get("computation")
            else None
        )
        if self.computation is not None:
            self.computation["observation_seconds"] = perf_counter() - calculation_started
        self.setup = setup_observation(self.root, self.capture.files)
        self.requirements = (
            json_document(self.capture.files["docs/research-requirements.json"])
            if "docs/research-requirements.json" in self.capture.files
            else None
        )
        choice = (self.requirements or {}).get("decisions", {}).get("framework")
        self.framework = {"selection": choice, "runtime_access": "not_probed", "transport_success_is_acceptance": False}
        if choice:
            from .frameworks import compatibility

            self.framework["qualified_metadata"] = optional(
                lambda: compatibility(framework=choice["id"], version=choice["version"], mode=choice["mode"])
            )
        self.now = datetime.now(timezone.utc)
        self.basis = {
            "workspace_revision": self.capture.revision_id,
            "controls": self.controls.get("result", {}).get("context_id"),
            "policy_id": owner("_strict_contract").artifact_id(self.policy) if self.policy else None,
            "implementation": owner("_selected_publication").producer_identity(),
            "root_identity": self.root_identity,
            "scope": "bounded_current_workspace",
            "authenticated_execution": False,
        }

    def finish(self):
        if (
            identity(self.root) != self.root_identity
            or self.revisions.capture_workspace(self.root).revision_id != self.capture.revision_id
            or owner("_strict_evidence").resolve_policy(self.root, self.config) != self.policy
        ):
            refuse("research_inputs_changed", "ONBOARDING_PLAN_STALE")
        return self.basis

    def run_guard(self, agent_id):
        if not self.status.get("smoke", {}).get("ok"):
            refuse("caller_workspace_requires_repair", "ONBOARDING_CHECK_FAILED")
        if self.managed:
            refuse("managed_session_requires_existing_protocol", "ONBOARDING_OWNERSHIP_CONFLICT")
        if self.run is None or self.run.get("caller_context") is None:
            refuse("caller_bound_run_required", "ONBOARDING_OWNERSHIP_CONFLICT")
        if self.run["state"]["current"] in owner("run_controller").TERMINAL_STATES:
            refuse("caller_run_terminal", "ONBOARDING_OWNERSHIP_CONFLICT")
        owner("run_controller").require_caller_context(self.root, self.run, agent_id)
        if self.status.get("run_controller", {}).get("stale"):
            refuse("caller_run_stale_requires_inspection", "ONBOARDING_OWNERSHIP_CONFLICT")
        if self.status.get("readiness", {}).get("budget_state", {}).get("should_stop"):
            refuse("caller_run_budget_exhausted", "ONBOARDING_LIMIT")
        self.finish()

    def strict_summary(self):
        if self.strict["state"] != "observed":
            return {
                **self.strict,
                "requested_assurance": self.policy.get("assurance") if self.policy else None,
                "host_enforcement": "not_established",
                "accepted_claims": None,
                "acceptance_observed": False,
            }
        value = self.strict["result"]
        return {
            "state": "observed",
            "basis_id": value["basis_id"],
            "policy_id": value["policy_id"],
            "assurance": value["assurance"],
            "host_enforcement": "not_established",
            "accepted_claims": sum(row["accepted"] for row in value["claims"]),
            "claims_count": len(value["claims"]),
            "acceptance_observed": True,
            "claims": [
                {
                    "id": row["claim"]["id"],
                    "question_slug": row["claim"]["question_slug"],
                    "accepted": row["accepted"],
                    "reasons": row["reasons"],
                    "review": row["review"],
                }
                for row in value["claims"]
            ],
            "limits": value["limits"],
            "evaluated_at": value["evaluated_at"],
            "acceptance_scope": "current strict check; final release still required",
        }


def request_row(value):
    return {
        key: value.get(key)
        for key in ("request_id", "kind", "query_or_identifier", "question_slugs", "status", "source_id", "scope")
    }
