"""Explicit host services and authenticated canonical execution journal records."""

from __future__ import annotations

import hashlib
import hmac
import threading
from datetime import datetime, timezone

from . import execution_accounting as accounting
from .agent_execution import ExecutionAttempt, ExecutionProfile
from .mutation_contracts import canonical, detached, digest, validate_request_id
from .research_session_contracts import refuse

JOURNAL = "research-execution-journal/v1"
SCOPE = "session-work-execution"
_HOST_CALL_CAPACITY = threading.BoundedSemaphore(16)


def bounded_host_call(function, arguments, timeout_seconds):
    """Bound a host wait; timed-out effects remain externally uncertain."""
    if timeout_seconds <= 0:
        return None, "host_wait_expired"
    capacity = _HOST_CALL_CAPACITY
    if not capacity.acquire(blocking=False):
        return None, "host_observation_capacity_exhausted"
    finished = threading.Event()
    result = []
    def invoke():
        try:
            result.append((True, function(*arguments)))
        except BaseException:
            result.append((False, None))
        finally:
            capacity.release()
            finished.set()
    try:
        threading.Thread(target=invoke, daemon=True, name="research-host-observation").start()
    except RuntimeError:
        capacity.release()
        return None, "host_observation_failed"
    if not finished.wait(timeout_seconds):
        return None, "host_wait_expired"
    return result[0][1], None if result[0][0] else "host_observation_failed"


def now_utc():
    return datetime.now(timezone.utc).isoformat()


class ResearchExecution:
    """Host-selected services; no provider discovery, credentials or worker keys.

    The signing key protects the retained expected inventory and accounting
    records. It must be host-owned and stable across restarts. The scope covers
    session work attempts; prior setup, calibration and surrounding human
    activity remain outside this measurement boundary.
    """

    def __init__(self, executor, *, qualifications, review_signer, observer_key: bytes):
        from .agent_qualification import HostReviewSigner, QualifiedEvaluators

        if (type(executor.profile) is not ExecutionProfile or type(observer_key) is not bytes
                or not 32 <= len(observer_key) <= 128 or type(qualifications) is not QualifiedEvaluators
                or type(review_signer) is not HostReviewSigner):
            refuse("EXECUTION_CONFIG_INVALID")
        try:
            generator_role = executor.profile.role(review_signer.generator.config_ref)
            if (generator_role.role != "host_worker" or generator_role.controller_ref != review_signer.generator.controller_ref
                    or qualifications.authority.generator_controller_ref != generator_role.controller_ref):
                refuse("EXECUTION_SIGNER_MISMATCH")
            for identity in review_signer.reviewers:
                role = executor.profile.role(identity.config_ref)
                if role.role != "evaluator" or role.controller_ref != identity.controller_ref:
                    refuse("EXECUTION_SIGNER_MISMATCH")
        except ValueError:
            refuse("EXECUTION_SIGNER_MISMATCH")
        self._executor = executor
        self._profile = executor.profile
        self._qualifications = qualifications
        self._review_signer = review_signer
        self._observer_key = observer_key
        self._binding = self.identity()

    @property
    def executor(self):
        return self._executor

    @property
    def profile(self):
        return self._profile

    @property
    def qualifications(self):
        return self._qualifications

    @property
    def review_signer(self):
        return self._review_signer

    def identity(self):
        return {"execution_profile_id": "sha256:" + self.profile.sha256,
            "qualification_id": "sha256:" + self.qualifications.sha256.removeprefix("sha256:"),
            "signer_id": "sha256:" + self.review_signer.sha256.removeprefix("sha256:"),
            "observer_id": "sha256:" + hashlib.sha256(self._observer_key).hexdigest()}

    def check(self, binding):
        if self.identity() != self._binding or self.executor.profile != self.profile:
            refuse("EXECUTION_CONFIG_CHANGED")
        if any(binding.get(key) != value for key, value in self._binding.items()):
            refuse("BINDING_CHANGED")

    def _signature(self, value):
        unsigned = {**value, "authentication": None}
        return hmac.new(self._observer_key, canonical(unsigned), hashlib.sha256).hexdigest()

    def sign(self, value):
        value = detached(value)
        value["authentication"] = self._signature(value)
        return value

    def _verify(self, value):
        signature = value.get("authentication") if type(value) is dict else None
        if type(signature) is not str or not hmac.compare_digest(self._signature(value), signature):
            refuse("EXECUTION_OBSERVATION_UNAUTHENTICATED")

    def seal_record(self, session, kind, request_id, data, *, records=None):
        records = self.records(session) if records is None else records
        previous = {key: value for key, value in records[-1].items() if key != "event_ref"} if records else None
        return self.sign({"schema_version": JOURNAL, "kind": kind,
            "binding_id": digest(session.binding), "request_id": validate_request_id(request_id),
            "sequence": len(records), "previous_record_ref": digest(previous) if previous is not None else None,
            "data": detached(data), "authentication": None})

    def records(self, session):
        from .research_session import _load

        controller = _load(session.root, "run_controller")
        if not controller.run_state_path(session.root, session.run_id).exists():
            return []
        document = controller._session_state(session.root, session.run_id)
        if document.get("research_session") != session.binding:
            refuse("BINDING_CHANGED")
        result = []
        binding = session.binding
        binding_id = digest(binding)
        for event in controller._bounded_journal(controller.events_path(session.root, session.run_id))["events"]:
            data = event.get("data") or {}
            record = data.get("record", {})
            if record.get("schema_version") != JOURNAL:
                continue
            previous = {key: value for key, value in result[-1].items() if key != "event_ref"} if result else None
            if (set(record) != {"schema_version", "kind", "binding_id", "request_id", "sequence", "previous_record_ref", "data", "authentication"}
                    or record["binding_id"] != binding_id
                    or record["request_id"] != data.get("request_id") or record["sequence"] != len(result)
                    or record["previous_record_ref"] != (digest(previous) if previous is not None else None)):
                refuse("EXECUTION_JOURNAL_INVALID")
            self._verify(record)
            result.append({**record, "event_ref": event["event_id"]})
        return result

    def attempt(self, document):
        attempt = ExecutionAttempt.create(attempt_id=document["attempt_id"], nonce=document["nonce"],
            run_id=document["run_id"], binding_id=document["binding_id"], profile=self.profile,
            config_ref=document["config_ref"], job=document["job"], input_document=document["input"])
        if attempt.document() != document:
            refuse("EXECUTION_ATTEMPT_CHANGED")
        return attempt

    def authenticate(self, observation, attempt):
        from .agent_execution import validate_observation

        value, reason = bounded_host_call(self.executor.authenticate, (observation, attempt), self.profile.cleanup_timeout_seconds)
        if reason is not None:
            refuse("EXECUTION_OBSERVATION_UNAUTHENTICATED")
        value = validate_observation(value)
        if value != observation:
            refuse("EXECUTION_OBSERVATION_UNAUTHENTICATED")
        return value

    def verifier(self):
        def verify(request):
            self._verify(request["envelope"])
            envelope = request["envelope"]
            return {"schema_version": accounting.VERIFICATION, "envelope_sha256": request["envelope_sha256"],
                "binding": request["binding"], "controller_ref": "session-observer",
                "verified_at": envelope.get("observed_at", envelope.get("closed_at")),
                "expires_at": "9999-12-31T23:59:59+00:00",
                "authenticated": True}
        return accounting.CoverageVerifier("session-observer", verify)

    def baseline(self, session):
        return self.sign({"schema_version": accounting.OBSERVATION, "observation_id": "baseline",
            "binding": {"session_binding_id": digest(session.binding)}, "scope_id": SCOPE,
            "controller_ref": "session-observer", "attempt_id": None, "observed_at": now_utc(), "sequence": 0,
            "slots": ["baseline"], "state": "started", "usage": {"seconds": "0", "model_calls": 0,
                "tool_calls": 0, "tokens": 0, "cost": "0"},
            "cost_unit": {"currency": self.profile.currency, "pricing_ref": self.profile.pricing_ref},
            "provenance": "measured", "final_usage": False, "correction": False,
            "artifact_refs": [], "human_observer": None, "authentication": None})

    def accounting_observation(self, attempt, observation, *, authenticated=False):
        if not authenticated:
            observation = self.authenticate(observation, attempt)
        values = observation["observed_usage"]
        seconds = values["seconds"]
        state = {"succeeded": "completed", "expired": "failed", "unavailable": "unknown", "running": "started"}.get(
            observation["state"], observation["state"])
        complete = observation["final_accounting"]
        if state == "unknown" and observation["state"] == "unavailable" and complete:
            state = "failed"
        slots = ["dispatch", "result", "usage"] if complete else ["dispatch"]
        return self.sign({"schema_version": accounting.OBSERVATION,
            "observation_id": attempt.attempt_id + "." + str(observation["sequence"]),
            "binding": {"session_binding_id": attempt.binding_id}, "scope_id": SCOPE,
            "controller_ref": "session-observer", "attempt_id": attempt.attempt_id,
            "observed_at": observation["observed_utc"], "sequence": observation["sequence"], "slots": slots,
            "state": state, "usage": {**values, "seconds": None if seconds is None else format(seconds, ".9f")},
            "cost_unit": {"currency": observation["currency"], "pricing_ref": observation["pricing_ref"]},
            "provenance": "measured", "final_usage": complete, "correction": False,
            "artifact_refs": [digest(observation)], "human_observer": None, "authentication": None})

    def ledger(self, session, records=None):
        records = self.records(session) if records is None else records
        plan = None
        attempts = {}
        for record in records:
            value = record["data"]
            if record["kind"] == "execution_begin":
                if plan is not None:
                    refuse("EXECUTION_JOURNAL_INVALID")
                plan = accounting.start_coverage(binding={"session_binding_id": digest(session.binding)},
                    scope_id=SCOPE, baseline=value["baseline"], verifier=self.verifier())
            elif record["kind"] == "execution_attempt":
                if plan is None:
                    refuse("EXECUTION_JOURNAL_INVALID")
                attempt = self.attempt(value["attempt"])
                if attempt.attempt_id in attempts:
                    refuse("EXECUTION_JOURNAL_INVALID")
                attempts[attempt.attempt_id] = attempt
                plan = accounting.reserve_attempt(plan, value["reservation"])
            elif record["kind"] == "execution_observation":
                if plan is None or value["attempt_id"] not in attempts:
                    refuse("EXECUTION_JOURNAL_INVALID")
                attempt = attempts[value["attempt_id"]]
                from .agent_execution import validate_observation

                observation = validate_observation(value["observation"])
                if (observation["attempt_id"] != attempt.attempt_id or observation["job_sha256"] != attempt.job_sha256
                        or observation["binding_id"] != attempt.binding_id or observation["nonce"] != attempt.nonce):
                    refuse("EXECUTION_JOURNAL_INVALID")
                meter = self.accounting_observation(attempt, observation, authenticated=True)
                plan = accounting.observe_attempt(plan, meter, self.verifier())
            elif record["kind"] == "execution_native":
                if plan is None:
                    refuse("EXECUTION_JOURNAL_INVALID")
                plan = accounting.reserve_attempt(plan, value["reservation"])
            elif record["kind"] == "execution_native_observation":
                if plan is None:
                    refuse("EXECUTION_JOURNAL_INVALID")
                plan = accounting.observe_attempt(plan, value["observation"], self.verifier())
            elif record["kind"] == "execution_close":
                if plan is None:
                    refuse("EXECUTION_JOURNAL_INVALID")
                if not any(item["kind"] == "execution_step" and item["data"]["step"]["terminal"]
                        and digest(item["data"]["step"]["decision"]) == value["closure"]["terminal_decision_ref"] for item in records):
                    refuse("EXECUTION_JOURNAL_INVALID")
                plan = accounting.close_coverage(plan, value["closure"], self.verifier())
        return plan

    def native_observation(self, session, attempt_id, *, seconds, succeeded, invocation_observed=True, tool_calls=1):
        return self.sign({"schema_version": accounting.OBSERVATION, "observation_id": attempt_id + ".final",
            "binding": {"session_binding_id": digest(session.binding)}, "scope_id": SCOPE,
            "controller_ref": "session-observer", "attempt_id": attempt_id, "observed_at": now_utc(), "sequence": 1,
            "slots": ["dispatch", "result", "usage"] if invocation_observed else ["result"],
            "state": ("completed" if succeeded else "failed") if invocation_observed else "unknown",
            "usage": {"seconds": format(seconds, ".9f") if invocation_observed else None,
                "model_calls": 0, "tool_calls": tool_calls if invocation_observed else None, "tokens": 0, "cost": "0"},
            "cost_unit": {"currency": self.profile.currency, "pricing_ref": self.profile.pricing_ref},
            "provenance": "measured", "final_usage": invocation_observed, "correction": False, "artifact_refs": [],
            "human_observer": None, "authentication": None})

    def closure(self, plan, decision_ref):
        return self.sign({"schema_version": accounting.CLOSURE, "binding": plan["binding"], "scope_id": SCOPE,
            "controller_ref": "session-observer", "plan_sha256": accounting.digest(plan), "closed_at": now_utc(),
            "terminal_decision_ref": decision_ref,
            "last_observation_ids": [row["envelope"]["observation_id"] for row in plan["observations"]],
            "authentication": None})
