"""Native acceptance scenarios over complete delivered review applications."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evidence_wiki import Workspace
from evidence_wiki._review_dependency_inputs import bind_session
from evidence_wiki.pack_discovery import owner
from evidence_wiki.research_journal_contracts import digest_record
from tools._review_dependency_case_contracts import (
    _CLAIM_CHANGES,
    _FAMILY,
    _INGRESS_REJECTION,
    CASE_IDS,
    CLAIM_IDS,
    INITIAL_CASES,
    SERVICE_CASES,
    _accepted,
    _basis,
    _dependants,
    _reason,
    _require,
    case_manifest,
    case_plan,
)
from tools._review_dependency_case_contracts import validate_case_plan as validate_case_plan
from tools._review_dependency_case_inputs import NativeCaseInputs
from tools._review_dependency_case_services import NativeCaseServices
from tools._review_dependency_workload import (
    OriginalResponses,
    OwnedChildren,
    canonical,
    digest,
    provision,
    retain,
    workload_deadline,
)
from tools.decide_release import _HostDeadline


def _reference(record):
    return digest_record({key: value for key, value in record.items() if key != "event_ref"})


def _files(directory):
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and ".locks" not in path.parts
    }


@dataclass
class NativeCases(NativeCaseServices, NativeCaseInputs):
    root: Path
    runtime: object
    originals: OriginalResponses
    children: OwnedChildren
    mode: str
    manifest: dict
    applications: dict
    fixture: dict
    sources: dict
    keys: dict = field(repr=False)
    baseline: dict | None = None
    pending_source: dict | None = field(default=None, repr=False)
    extended_caps: dict | None = field(default=None, repr=False)

    def raw(self, label, data):
        pin = retain(self.originals.directory / (str(len(self.originals.pins)) + "-" + label + ".bin"), data, raw=True)
        self.originals.pins.append(dict(pin, label=label))
        return pin

    def setup_residual(self):
        inputs = self.fixture.get("global_inputs", {}).get("unclassified", {})
        _require(type(inputs) is dict and set(inputs) <= {"notes/context.txt"}, "native_fixture_residual_scope_invalid")
        for coordinate, text in inputs.items():
            _require(type(text) is str and len(text.encode()) <= 65536, "native_fixture_residual_bound")
            path = self.root / coordinate
            path.parent.mkdir(mode=0o700, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(text.encode())
            self.raw("fixture-residual", path.read_bytes())

    def setup_usage_role(self):
        from evidence_wiki import HostRuntime

        extra_grants = {
            "publication_readiness.run_selected_publication",
            "export_answers.run_export",
            "research_session.deliver",
            "artifact_delivery_operations.deliver",
            "requirements_operations.prepare_revision",
            "requirements_operations.current",
        }
        context = replace(self.runtime.context, grants=self.runtime.context.grants | extra_grants)
        self.runtime = HostRuntime.from_services(self.runtime.policy, context, self.runtime.execution)
        authority_path = Path(self.runtime.context.authority_file)
        authority = json.loads(authority_path.read_bytes())
        if "revocation" not in authority["principals"]["usage"]["roles"]:
            authority["principals"]["usage"]["roles"].append("revocation")
            authority_path.write_bytes(canonical(authority))
            self.originals.append(
                (
                    "fixture-revocation-role",
                    {"authority_sha256": hashlib.sha256(authority_path.read_bytes()).hexdigest()},
                )
            )
        self.originals.append(("fixture-host-operation-grants", {"grants": sorted(self.runtime.context.grants)}))

    def refused(self, case_id, operation, *, reasons=(), error_code=None, error_codes=(), field=None):
        before = self.guard()
        try:
            operation()
        except (Exception, SystemExit) as error:
            observed = {
                "kind": type(error).__name__,
                "error_code": getattr(error, "error_code", None),
                "reason": _reason(error),
                "field": getattr(error, "details", {}).get("field"),
            }
            _require(
                (observed["reason"] in reasons or observed["error_code"] in error_codes)
                if error_code is None
                else observed["error_code"] == error_code,
                "unexpected_native_refusal",
            )
            if field is not None:
                _require(observed["field"] == field, "unexpected_native_refusal_field")
        else:
            raise AssertionError("native_candidate_was_not_refused")
        _require(self.guard() == before, "failed_candidate_changed_native_custody")
        self.originals.append((case_id + "-refusal", observed))
        return observed

    @contextmanager
    def session(self, run_id):
        with self.runtime.context.activate(), Workspace.open(self.root) as workspace:
            with (
                workspace.research.session(
                    self.runtime.policy, run_id=run_id, execution=self.runtime.execution
                ) as session,
                bind_session(session),
            ):
                yield workspace, session

    def guard(self):
        """Capture hashes only; never export newly provisioned fixture secrets."""
        authority = self.runtime.context.authority_file
        return {
            "workspace_files": _files(self.root),
            "authority_sha256": hashlib.sha256(Path(authority).read_bytes()).hexdigest(),
            "private_files": _files(Path(self.runtime.context.state_directory)),
            "host_grants": sorted(self.runtime.context.grants),
            "worker_files": _files(self.root.parent / "workers"),
            "worker_commands": {
                config: {
                    "argv": list(argv),
                    "files": {
                        str(argument): hashlib.sha256(Path(argument).read_bytes()).hexdigest()
                        for argument in argv[1:]
                        if Path(argument).is_file()
                    },
                }
                for config, argv in self.runtime.execution.executor._commands.items()
            },
            "execution_identity": self.runtime.execution.identity(),
            "execution_profile": self.runtime.execution.profile.document(),
        }

    def observe(self, label, run_id=None):
        run_id = run_id or next(iter(self.applications.values()))["run_id"]
        before = self.guard()
        with self.session(run_id) as (workspace, session):
            records = self.runtime.execution.records(session)
            strict, publication = workspace.strict.check(), workspace.strict.export()
            _require(self.runtime.execution.records(session) == records, "read_added_execution_event")
        _require(self.guard() == before, "read_changed_native_custody")
        self.originals.extend(
            [(label + "-strict", strict), (label + "-publication", publication), (label + "-read-custody", before)]
        )
        return {"strict": strict, "publication": publication}

    def drive(self, claim_ids, *, cycle, verdict="pass", configs=("evidence", "critic"), page_chars=16384):
        """Use actual native assembly, worker receipt, intake and application."""
        from evidence_wiki import execution_session as native
        from evidence_wiki.review_packet_execution import context_parameters, reserve_evaluator

        claim_ids = set(claim_ids)
        _require(claim_ids <= CLAIM_IDS, "unknown_native_claim_selection")
        _require(
            verdict in {"pass", "fail", "unknown"} and set(configs) <= {"evidence", "critic"},
            "native_service_selection_invalid",
        )
        prefix = {"pass": "context-2", "fail": "context-0", "unknown": "context-1"}[verdict]
        for claim in self.manifest["claims"]:
            if claim["id"] not in claim_ids:
                continue
            run_id = "review-" + digest({"cycle": cycle, "claim": claim["id"]})[:24]
            with self.session(run_id) as (_workspace, session):
                with native.gate(session):
                    native._begin(session)
                for original_id in self.runtime.policy.to_dict()["scope"]["question_ids"]:
                    native._prepare(session, original_id)
                for config_ref in configs:
                    from evidence_wiki.review_application_lease import review_application_window
                    lease = self.runtime.execution.profile.review_application_lease
                    window = review_application_window(session) if lease is not None else nullcontext()
                    with window, workload_deadline(lease.seconds) if lease is not None else nullcontext():
                        identifier = "review-" + config_ref
                        role = self.runtime.execution.profile.role_scopes.scope(config_ref).task_role
                        assembly = None
                        for ordinal in range(1024):
                            candidate = identifier + "-packet-" + str(ordinal)
                            if context_parameters(session, candidate)["context_id"].startswith(prefix):
                                assembly = candidate
                                break
                        _require(assembly is not None, "native_context_coordinate_unavailable")
                        session.assemble_review_packet(
                            request_id=assembly, claim_id=claim["id"], task_role=role, page_chars=page_chars
                        )
                        attempt = reserve_evaluator(
                            session,
                            request_id=identifier,
                            assembly_request_id=assembly,
                            original_id=claim["question_slug"],
                            claim_id=claim["id"],
                            config_ref=config_ref,
                        )
                        self.originals.extend(
                            [
                                (run_id + "-" + config_ref + "-attempt", attempt.document()),
                                (run_id + "-" + config_ref + "-worker", attempt.worker_document()),
                            ]
                        )
                        observed, reason = native._external(session, "invoke", attempt)
                        _require(reason is None, "native_worker_dispatch_unavailable")
                        started, sequence = time.monotonic(), 0
                        while True:
                            self.originals.append((run_id + "-" + config_ref + "-observation-" + str(sequence), observed))
                            self.runtime.execution.executor.authenticate(observed, attempt)
                            session.intake_observation(observed, request_id=identifier + "-intake-" + str(sequence))
                            if observed["state"] != "running":
                                break
                            _require(time.monotonic() - started < 600, "native_worker_deadline")
                            time.sleep(0.005)
                            observed, reason = native._external(session, "poll", attempt)
                            _require(reason is None, "native_worker_poll_unavailable")
                            sequence += 1
                        _require(
                            observed["state"] == "succeeded" and observed["final_accounting"], "native_worker_incomplete"
                        )
                        _require(
                            observed["output"]["verdicts"] == dict.fromkeys(owner("_strict_contract").CHECKS, verdict),
                            "native_worker_coordinate_decision_changed",
                        )
                        self.children.confirm_completed()
                        session.advance(request_id=identifier + "-apply", expected_basis_id=attempt.job["basis_id"])
                        records = self.runtime.execution.records(session)
                        applied = [
                            row
                            for row in records
                            if row["kind"] == "execution_applied"
                            and row["data"].get("attempt_id") == attempt.attempt_id
                            and row["data"].get("operation") == "evaluate"
                        ]
                        _require(
                            len(applied) == 1 and applied[0]["data"].get("passed") is (verdict == "pass"),
                            "native_review_not_applied",
                        )
                        self.applications[(cycle, claim["id"], config_ref)] = {
                            "run_id": run_id,
                            "record_sha256": _reference(applied[0]),
                            "attempt_id": attempt.attempt_id,
                            "attempt": attempt,
                            "envelope": next(
                                row["data"]["envelope"]
                                for row in records
                                if row["kind"] == "execution_application"
                                and row["data"].get("attempt_id") == attempt.attempt_id
                            ),
                            "review_context": next(
                                row["data"]["review_context"]
                                for row in records
                                if row["kind"] == "execution_attempt"
                                and row["data"].get("attempt", {}).get("attempt_id") == attempt.attempt_id
                            ),
                            "coordinate": {
                                "host_id": "direct",
                                "case_id": cycle,
                                "cycle": cycle,
                                "claim_id": claim["id"],
                                "config_ref": config_ref,
                            },
                        }
                        self.originals.append_records(run_id + "-" + config_ref + "-records", records)

    def establish(self):
        from evidence_wiki import execution_session as native

        self.drive(CLAIM_IDS, cycle="baseline")
        run_id = next(iter(self.applications.values()))["run_id"]
        with self.session(run_id) as (_workspace, session):
            for original in self.runtime.policy.to_dict()["scope"]["question_ids"]:
                native._resolve(session, original)
            self.originals.append_records("baseline-resolution", self.runtime.execution.records(session))
        self.baseline = self.observe("baseline")
        _require(_accepted(self.baseline["strict"]) == CLAIM_IDS, "native_baseline_not_accepted")
        _require(self.baseline["publication"]["verdict"] == "ship", "native_baseline_not_publishable")
        _require(len(self.applications) == 12, "native_baseline_service_cohort_incomplete")
        return self.baseline

    def retained(self, claim_id, config_ref, label):
        from evidence_wiki import execution_session as native

        application = self.applications[("baseline", claim_id, config_ref)]
        before = self.guard()
        with self.session(application["run_id"]) as (_workspace, session):
            facts = native.retained_review_evidence(
                session, {key: application[key] for key in ("run_id", "record_sha256")}, application["coordinate"],
                **({"journal_collection": True} if self.originals.journal_collection else {})
            )
        _require(self.guard() == before, "retained_read_changed_native_custody")
        self.originals.append((label + "-" + claim_id + "-" + config_ref + "-retained", facts))
        return facts

    def candidate(self, case_id):
        """Sign fresh fixture-owned candidate bytes; native admission stays live."""
        application = self.applications[("baseline", "observed-capacity", "critic")]
        with self.session(application["run_id"]) as (workspace, _session):
            prepared = workspace.strict.prepare_review("observed-capacity")
            delivery = application["envelope"]["payload"]["body"]["review"].get("delivery")
            signer = self.runtime.execution.review_signer
            return signer.registration(
                prepared,
                dict.fromkeys(owner("_strict_contract").CHECKS, "pass"),
                "Synthetic native candidate over actual prepared material.",
                "candidate-" + digest(case_id)[:24],
                datetime.now(timezone.utc).isoformat(),
                config_ref="critic",
                delivery=delivery,
            )

    def _file_case(self, case_id, *, refresh=False, change=None):
        _require(self.baseline is not None, "native_baseline_required")
        identifier, coordinate, value = _CLAIM_CHANGES[case_id] if change is None else change
        path = self.root / "docs/research-claims.json"
        original = path.read_bytes()
        opening = self.guard()
        manifest = json.loads(original)
        changed = next(row for row in manifest["claims"] if row["id"] == identifier)
        parent = changed
        for key in coordinate[:-1]:
            parent = parent[key]
        parent[coordinate[-1]] = copy.deepcopy(value)
        expected_changed = _dependants(self.manifest, identifier) if self.mode == "selective_reuse" else set(CLAIM_IDS)
        assertions, evidence = [], []
        try:
            path.write_bytes(canonical(manifest))
            mutation_custody = self.guard()
            _require(
                {key: val for key, val in mutation_custody.items() if key != "workspace_files"}
                == {key: val for key, val in opening.items() if key != "workspace_files"},
                "mutation_changed_protected_owner",
            )
            expected_files = dict(opening["workspace_files"])
            expected_files[path.relative_to(self.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            _require(mutation_custody["workspace_files"] == expected_files, "mutation_changed_unselected_file")
            current = self.observe(case_id)
            baseline, strict = self.baseline["strict"], current["strict"]
            _require(strict["basis_id"] != baseline["basis_id"], "workspace_basis_did_not_change")
            _require(_accepted(strict) == CLAIM_IDS - expected_changed, "native_acceptance_set_changed")
            if case_id == "claim-mechanical-material":
                selected = next(row for row in strict["claims"] if row["claim"]["id"] == identifier)
                _require(
                    "strict_source_time_unconfirmed" in selected["reasons"], "actual_mechanical_diagnostic_missing"
                )
            if self.mode == "selective_reuse":
                old_bases, new_bases = _basis(baseline), _basis(strict)
                _require(
                    {claim for claim in CLAIM_IDS if old_bases[claim] != new_bases[claim]} == expected_changed,
                    "native_claim_basis_set_changed",
                )
                for claim_id in sorted(CLAIM_IDS - expected_changed):
                    for config_ref in ("evidence", "critic"):
                        facts = self.retained(claim_id, config_ref, case_id)
                        _require(facts["state"] == "eligible", "retained_service_not_eligible")
                        _require(
                            facts["original"]["observed_workspace_basis_id"] != strict["basis_id"],
                            "historical_workspace_provenance_rewritten",
                        )
                        _require(
                            facts["current"]["basis_id"] == facts["original"]["basis_id"],
                            "retained_claim_basis_changed",
                        )
                        evidence.append(
                            {
                                "claim_id": claim_id,
                                "config_ref": config_ref,
                                "review_id": facts["original"]["review_id"],
                                "state": facts["state"],
                            }
                        )
                assertions.extend(
                    [
                        "exact_changed_claim_basis_set",
                        "actual_original_custody_and_live_service_eligibility",
                        "signed_historical_workspace_provenance_preserved",
                    ]
                )
            assertions.extend(
                ["full_workspace_basis_changed", "exact_before_refresh_core_acceptance", "read_only_closing_custody"]
            )
            if refresh:
                self.drive(expected_changed, cycle=case_id)
                refreshed = self.observe(case_id + "-refreshed")
                _require(_accepted(refreshed["strict"]) == CLAIM_IDS, "native_refresh_not_accepted")
                _require(refreshed["publication"]["verdict"] == "ship", "native_refresh_not_publishable")
                assertions.extend(["actual_authenticated_refresh_cohort", "final_native_acceptance_and_publication"])
            return {
                "status": "passed",
                "assertions": assertions,
                "evidence": evidence,
                "material_invalidated": sorted(expected_changed),
                "accepted_before_refresh": sorted(_accepted(strict)),
                "refreshed": refresh,
            }
        finally:
            path.write_bytes(original)
            if not refresh:
                _require(self.guard() == opening, "reversible_input_restore_changed_custody")
            else:
                restored = self.guard()
                _require(
                    restored["authority_sha256"] == opening["authority_sha256"]
                    and restored["execution_identity"] == opening["execution_identity"]
                    and restored["execution_profile"] == opening["execution_profile"],
                    "refresh_changed_protected_owner",
                )
            restored = self.observe(case_id + "-restored")
            _require(_accepted(restored["strict"]) == CLAIM_IDS, "restored_baseline_not_accepted")

    def run_case(self, case_id, *, refresh=False):
        """Dispatch each closed case coordinate to its actual native owner."""
        if case_id not in _FAMILY:
            raise ValueError("native_case_unknown")
        if case_id == "unchanged-baseline":
            observed = self.observe(case_id)
            _require(_accepted(observed["strict"]) == CLAIM_IDS, "native_baseline_not_accepted")
            return {
                "status": "passed",
                "assertions": [
                    "twelve_actual_review_applications",
                    "all_native_claims_accepted",
                    "final_native_publication",
                    "read_only_closing_custody",
                ],
                "evidence": [],
            }
        if case_id in _CLAIM_CHANGES:
            return self._file_case(case_id, refresh=refresh)
        if case_id == "claim-mechanical-material":
            claim = next(row for row in self.manifest["claims"] if row["id"] == "independent-note")
            observed = datetime.fromisoformat(claim["evidence"][0]["observed_at"].replace("Z", "+00:00")) - timedelta(
                seconds=1
            )
            return self._file_case(
                case_id, change=("independent-note", ("evidence", 0, "observed_at"), observed.isoformat())
            )
        if case_id == "claim-counterevidence-material":
            return self._counter_case(case_id)
        if case_id in {
            "question-capacity-body",
            "question-capacity-metadata",
            "question-demand-body",
            "question-demand-metadata",
        }:
            return self._question_case(case_id)
        if case_id in {"unclassified-file", "unclassified-file-addition", "unclassified-file-deletion"}:
            return self._residual_case(case_id)
        if case_id in _INGRESS_REJECTION and case_id not in {
            "mode-caller-flag-cannot-switch",
            "legacy-envelope-not-convertible",
            "selective-envelope-current-runtime-refusal",
        }:
            return self._ingress_case(case_id)
        if _FAMILY[case_id] == "invalid_graph":
            return self._graph_case(case_id)
        if _FAMILY[case_id] == "bounds":
            return self._bound_case(case_id)
        if _FAMILY[case_id] == "history":
            return self._history_case(case_id)
        if _FAMILY[case_id] == "races":
            return self._race_case(case_id)
        if _FAMILY[case_id] == "usage_time":
            return self._time_case(case_id)
        if _FAMILY[case_id] == "source":
            return self._source_case(case_id)
        if _FAMILY[case_id] == "live_output_gates":
            return self._gate_case(case_id)
        if case_id in {"computation-output", "computation-lineage", "computation-definition", "computation-clock"}:
            return self._computation_case(case_id)
        if case_id in {"full-requirements", "question-addition"}:
            return self._requirements_case(case_id)
        if case_id in {"claim-addition", "claim-deletion"}:
            return self._claim_membership_case(case_id)
        if case_id in {"source-addition", "source-deletion"}:
            return self._source_membership_case(case_id)
        if case_id == "validated-transport-only-change":
            return self._transport_case(case_id)
        if case_id in {"question-record-text", "unrelated-question-coverage"}:
            return self._layout_case(case_id)
        if case_id in {
            "policy",
            "rubric",
            "instructions",
            "authority-content",
            "authority-key-revocation",
            "controller-separation-failure",
            "current-role-failure",
        }:
            return self._protected_case(case_id)
        if case_id in SERVICE_CASES:
            return self._service_case(case_id)
        raise AssertionError("native_case_dispatch_incomplete")


def _fixture_inputs(fixture, case_id=None, *, temporal_margin_seconds=None, frozen_at=None):
    fixture = copy.deepcopy(fixture)
    if case_id == "source-addition":
        fixture["source_material"]["added"] = {"raw": "# Additional retained note\nA separate retained observation.\n"}
    if case_id == "source-record-bound":
        for index in range(62):
            fixture["source_material"]["bound-" + str(index).zfill(3)] = {
                "raw": "# Additional retained observation\nDistinct observation " + str(index) + ".\n"
            }
    if case_id == "review-max-age-expired":
        fixture["global_inputs"]["policy"]["max_review_age_seconds"] = 600
    if case_id in {"unused-source-expired", "source-effective-interval-ended", "source-effective-interval-not-started"}:
        now = datetime.now(timezone.utc) if frozen_at is None else frozen_at
        before, after = (now - timedelta(days=1)).isoformat(), (now + timedelta(seconds=1800 if temporal_margin_seconds is None else temporal_margin_seconds)).isoformat()
        claim = lambda value: {"value": value, "basis": "source-declared" if value else "unknown"}
        for name, material in fixture["source_material"].items():
            effective = None
            if name == "shared" and case_id.startswith("source-effective"):
                effective = {
                    "start": claim(after if case_id.endswith("not-started") else before),
                    "end": None if case_id.endswith("not-started") else claim(after),
                }
            material["temporal"] = {
                "schema_version": "evidence-temporal-source/v1",
                "asserting_principal": "usage",
                "measurement": None,
                "published_at": claim(before),
                "available_at": claim(before),
                "claimed_retrieved_at": claim(None),
                "effective": effective,
                "expires_at": claim(after) if name == "unused" and case_id == "unused-source-expired" else None,
                "supersedes": None,
                "record_path": None,
                "provenance_path": None,
            }
    return fixture


def _initial_mode(case_id, requested):
    if case_id in {"protected-policy-enable-selective", "legacy-envelope-not-convertible"}:
        return "legacy_full"
    if (
        case_id in SERVICE_CASES
        or case_id in _INGRESS_REJECTION
        or case_id in {"dependency-entry-bound", "dependency-basis-byte-bound", "validated-transport-only-change"}
    ):
        return "selective_reuse"
    return requested


def _finish_case_evidence(directory, report, children, source_before, refresh, bindings, ticket, *, completion_deadline=None):
    from tools._review_dependency_case_evidence import _completed_run, _seal_native_run, source_identity, write_index
    source_after = source_identity()
    if not isinstance(report["cleanup"], dict) or report["cleanup"].get("status") != "confirmed":
        report.update(status="failed", failure={"kind": "ValueError", "reason": "native_cleanup_unconfirmed", "error_code": None})
    pin = write_index(directory / "case-evidence", report, source_before=source_before,
        source_after=source_after, refresh=refresh, bindings=bindings)
    report["case_results_index_pin"] = pin
    capability = None
    if report["status"] in {"completed", "completed_with_limits"}:
        if completion_deadline is not None and time.monotonic() >= completion_deadline:
            raise _HostDeadline()
        _seal_native_run(children, ticket, pin)
        capability = _completed_run(pin, children, source_before, source_after)
        capability.assert_original()
        # Retain the live capability only on its actual owning tracker, not a DTO.
        if completion_deadline is None:
            children._native_case_completion = capability
    (directory / "report.json").write_bytes(
        (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode())
    if completion_deadline is not None:
        if time.monotonic() >= completion_deadline:
            raise _HostDeadline()
        if capability is not None:
            children._native_case_completion = capability


def _needs_isolated_fixture(case_id):
    return (
        _FAMILY[case_id] in {"history", "races", "usage_time", "source", "live_output_gates", "bounds"}
        or case_id in SERVICE_CASES
        or case_id in _INGRESS_REJECTION
        or case_id == "validated-transport-only-change"
        or case_id
        in {
            "policy",
            "rubric",
            "instructions",
            "authority-content",
            "authority-key-revocation",
            "controller-separation-failure",
            "current-role-failure",
            "computation-output",
            "computation-lineage",
            "computation-definition",
            "computation-clock",
            "claim-addition",
            "claim-deletion",
            "source-addition",
            "source-deletion",
        }
    )


def case_watchdog(cases):
    """Reserve existing fixture ceilings; this grants no additional native authority."""
    negative = {"source-effective-interval-not-started", "source-record-bound"}
    isolated = sum(case in negative or (len(cases) > 1 and _needs_isolated_fixture(case)) for case in cases)
    fixtures = 1 + isolated
    return {"fixture_reservations": fixtures, "fixture_run_seconds": 5220,
        "fixture_overhead_seconds": 600, "cleanup_seconds": 60,
        "outer_overhead_seconds": 600, "watchdog_seconds": fixtures * (5220 + 600 + 60) + 600,
        "authority_renewal": "forbidden", "run_budget_extension": "forbidden"}


def _case_deadlines(budget):
    end = time.monotonic() + budget["watchdog_seconds"]
    cleanup_end = end - budget["outer_overhead_seconds"]
    run_end = cleanup_end - budget["cleanup_seconds"]
    return run_end, cleanup_end, end


def _case_phase(end, allowance=None):
    seconds = end - time.monotonic()
    return workload_deadline(seconds if allowance is None else min(seconds, allowance))


def _retain_case_failure(directory, report, children):
    """Unpublish incomplete summaries; retain bounded failure bytes and originals."""
    # This allowance owns only failure metadata, never native work, authority,
    # index qualification or a resumed completion attempt.
    with workload_deadline(report["watchdog"]["cleanup_seconds"]):
        if hasattr(children, "_native_case_completion"):
            del children._native_case_completion
        (directory / "case-evidence/index.json").unlink(missing_ok=True)
        report.pop("case_results_index_pin", None)
        report["originals"] = children.originals.pins
        (directory / "report.json").unlink(missing_ok=True)
        try:
            raw = (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
        except (ValueError, TypeError, OverflowError, RecursionError):
            raw = b""
        if not raw or len(raw) > 16_777_216:
            import math
            failure = report.get("failure") if type(report.get("failure")) is dict else {}
            cleanup = report.get("cleanup") if type(report.get("cleanup")) is dict else {}
            timing = report.get("cleanup_timing") if type(report.get("cleanup_timing")) is dict else {}
            summary = {"schema_version": "native-review-case-failure/v1", "status": "failed", "qualification": False,
                "authority": "byte_evidence_only", "retention_status": "incomplete_inventory",
                "failure": {key: value[:128] if type(value) is str else None
                    for key in ("kind", "reason", "error_code") for value in (failure.get(key),)},
                "cleanup": {"status": "confirmed" if cleanup.get("status") == "confirmed" else "unavailable"},
                "cleanup_timing": {key: value for key in ("reserved_seconds", "elapsed_seconds", "deadline_exceeded")
                    for value in (timing.get(key),) if type(value) is bool
                    or type(value) in {int, float} and math.isfinite(value)},
                "original_responses": {"directory": str(getattr(children.originals, "directory", directory / "observations"))[:4096],
                    "pin_count": len(children.originals.pins), "inventory_complete": False}}
            raw = (json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
            _require(len(raw) <= 65_536, "native_case_failure_report_bound")
        temporary = directory / "failed-report.json"
        temporary.write_bytes(raw)
        temporary.replace(directory / "report.json")


def _case_failure(report, error, originals):
    if not getattr(originals, "journal_collection", False):
        report.update(status="failed", failure={"kind": type(error).__name__, "reason": _reason(error),
            "error_code": getattr(error, "error_code", None)})
        return
    from tools._review_dependency_workload import failure_document
    failure = failure_document(error)
    if report.get("failure") is None:
        report["failure"] = failure
    else:
        originals.append(("secondary-failure", failure))
    report["status"] = "failed"


def _finish_extended_cases(directory, report, children, source_before, refresh, bindings, ticket, deadlines, *, shards=False):
    """Use reserved terminal phases without reopening the original run window."""
    budget = report["watchdog"]
    try:
        try:
            from tools._integration_owner_pass import _defer_signals
            cleanup_started = time.monotonic()
            cleanup_end = min(deadlines[1], cleanup_started + budget["cleanup_seconds"])
            cleanup_invoked = False

            def close_owned():
                nonlocal cleanup_invoked
                with _defer_signals():
                    cleanup_invoked = True
                    try:
                        report["cleanup"] = children.close()
                        if shards:
                            report["cleanup"]["shards"] = [shard["cleanup"] for shard in report["shards"]]
                    finally:
                        elapsed = time.monotonic() - cleanup_started
                        overrun = time.monotonic() >= cleanup_end
                        report["cleanup_timing"] = {"reserved_seconds": budget["cleanup_seconds"],
                            "elapsed_seconds": elapsed, "deadline_exceeded": overrun}
                        if overrun:
                            _case_failure(report, _HostDeadline(), children.originals)
                            report["originals"] = children.originals.pins
                            # Preserve the finite safety cleanup's actual overrun before
                            # delivering the pending timeout; native work cannot resume.
                            (directory / "report.json").write_bytes(
                                (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode())

            try:
                with _case_phase(cleanup_end):
                    close_owned()
            except _HostDeadline as timeout:
                if not cleanup_invoked:
                    try:
                        close_owned()
                    finally:
                        raise timeout
                raise
        except (Exception, SystemExit, _HostDeadline) as error:
            _case_failure(report, error, children.originals)
            raise
        finally:
            with _case_phase(deadlines[2], budget["outer_overhead_seconds"]):
                report["originals"] = children.originals.pins
                (directory / "report.json").write_bytes(
                    (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode())
                _finish_case_evidence(directory, report, children, source_before, refresh, bindings, ticket,
                    completion_deadline=deadlines[2])
    except (Exception, SystemExit, _HostDeadline) as error:
        _case_failure(report, error, children.originals)
        _retain_case_failure(directory, report, children)
        raise


def _run_shards(directory, fixture, mode, cases, refresh, report, originals, source_before, bindings, extended=None):
    """Bound cumulative native launches with sequential, separately owned processes."""
    deadlines = _case_deadlines(report["watchdog"]) if extended is not None else None
    supplied = directory / "fixture.json"
    retain(supplied, fixture)
    binding_path = directory / "evidence-bindings.json"
    if bindings:
        retain(binding_path, bindings)
    shard_size = 1 if extended is not None else 8
    extended_path = directory / "extended-selection.json"
    if extended is not None:
        retain(extended_path, extended)
    children = OwnedChildren(originals, digest({"mode": mode, "cases": list(cases), "shard_size": shard_size}))
    from tools._review_dependency_case_evidence import _begin_native_run, _record_native_shard
    ticket = _begin_native_run(children, source_before, cases, digest(fixture))
    rows = {row["case_id"]: row for row in report["cases"]}
    report["shards"] = []
    budgets = [case_watchdog(cases[offset:offset + 8]) for offset in range(0, len(cases), 8)]
    report["watchdog"] = dict(case_watchdog(cases),
        fixture_reservations=sum(budget["fixture_reservations"] for budget in budgets),
        watchdog_seconds=sum(budget["watchdog_seconds"] for budget in budgets) + 600,
        shard_count=len(budgets))
    if extended is not None:
        from tools._review_dependency_case_budget import watchdog
        report["watchdog"] = watchdog(extended, cases)
    try:
        with _case_phase(deadlines[0]) if extended is not None else nullcontext():
            children.open()
            # At most eight independent fixtures plus the shared baseline, including
            # fresh service qualifications, fit beneath each child's 4096-launch cap.
            for offset in range(0, len(cases), shard_size):
                selected = cases[offset:offset + shard_size]
                output = directory / ("shard-" + str(offset // shard_size).zfill(2))
                command = [sys.executable, "-m", "tools._review_dependency_cases", "--fixture", str(supplied),
                    "--output", str(output), "--mode", mode]
                if extended is not None:
                    command.extend(("--extended-selection", str(extended_path)))
                if bindings:
                    command.extend(("--evidence-bindings", str(binding_path)))
                for case_id in selected:
                    command.extend(("--case", case_id))
                if refresh:
                    command.append("--refresh")
                with (directory / (output.name + "-stdout.log")).open("xb") as stdout, (
                    directory / (output.name + "-stderr.log")
                ).open("xb") as stderr:
                    child = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],  # noqa: S603 - fixed local interpreter/module and validated case coordinates
                        stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
                    child.wait(timeout=min(watchdog(extended, selected)["watchdog_seconds"],
                        deadlines[0] - time.monotonic()) if extended is not None else case_watchdog(selected)["watchdog_seconds"])
                    children.confirm_completed()
                from tools._review_dependency_case_evidence import existing_pin, read, read_case, read_index
                index_path = output / "case-evidence/index.json"
                _require(index_path.is_file() and index_path.stat().st_size <= 1_048_576, "native_shard_index_missing")
                shard_pin = existing_pin(index_path)
                shard_index = read_index(shard_pin, roots=[output],
                    candidate_sha256=source_before["candidate_sha256"])
                _require(shard_index.get("extended_selection") == extended, "native_shard_extended_binding_invalid")
                _require(shard_index["source"] == source_before and shard_index["refresh"] == refresh
                    and shard_index["mode"] == mode and shard_index["selected_case_ids"] == list(selected)
                    and shard_index["fixture_sha256"] == digest(fixture), "native_shard_index_binding_invalid")
                cleanup = read(shard_index["cleanup_pin"], roots=[output], maximum=16_777_216)
                shard = {"status": shard_index["status"], "cleanup": cleanup}
                report["shards"].append({"selected_case_ids": list(selected), "report": shard_pin,
                    "status": shard["status"], "cleanup": shard["cleanup"]})
                for row in shard_index["cases"]:
                    if row["case_id"] in selected:
                        rows[row["case_id"]].update(read_case(row, roots=[output]))
                _require(child.returncode == 0 and shard["status"] in {"completed", "completed_with_limits"}
                    and shard["cleanup"]["status"] == "confirmed", "native_shard_failed")
                _record_native_shard(children, ticket, child, shard_pin, selected)
            statuses = {rows[case]["status"] for case in cases}
            _require(statuses <= {"passed", "passed_limited_scope", "earlier_owner_refusal"},
                "native_shard_case_incomplete")
            report["status"] = "completed" if statuses == {"passed"} else "completed_with_limits"
    except (Exception, SystemExit, _HostDeadline) as error:
        _case_failure(report, error, children.originals)
        raise
    finally:
        if extended is not None:
            primary = sys.exc_info()[1]
            try:
                _finish_extended_cases(directory, report, children, source_before, refresh, bindings, ticket, deadlines, shards=True)
            except BaseException:
                if primary is None or not getattr(originals, "journal_collection", False):
                    raise
        else:
            try:
                report["cleanup"] = children.close()
                report["cleanup"]["shards"] = [shard["cleanup"] for shard in report["shards"]]
            finally:
                report["originals"] = originals.pins
                (directory / "report.json").write_bytes(
                    (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode())
                _finish_case_evidence(directory, report, children, source_before, refresh, bindings, ticket)
    return report


def run_cases(directory, fixture, *, mode="selective_reuse", cases=INITIAL_CASES, refresh=False, evidence_bindings=None,
        extended_recipe_pin=None, execution_profile=None, temporal_margin_seconds=None, extended_selection=None, journal_collection=False):
    """Run in an isolated process: child cleanup deliberately closes its launcher."""
    _require(mode in {"selective_reuse", "legacy_full"}, "native_mode_unknown")
    cases = tuple(cases)
    _require(len(cases) == len(set(cases)) and bool(cases) and set(cases) <= set(CASE_IDS), "native_case_selection_invalid")
    from tools._review_dependency_case_evidence import freeze_bindings, source_identity
    evidence_bindings = freeze_bindings(evidence_bindings)
    source_before = source_identity()
    extended = extended_selection
    if extended_recipe_pin is not None:
        _require(extended is None and execution_profile is not None, "native_case_extended_selection_invalid")
        from tools._review_dependency_case_budget import select
        extended = select(extended_recipe_pin, execution_profile, fixture, mode, cases, refresh,
            temporal_margin_seconds=temporal_margin_seconds, journal_collection=journal_collection)
    else:
        _require(execution_profile is None and temporal_margin_seconds is None, "native_case_extended_selection_invalid")
    if extended is not None:
        from tools._review_dependency_case_budget import validate_selection, watchdog
        validate_selection(extended, current=True)
        _require(extended["source"] == source_before and extended["fixture_sha256"] == digest(fixture)
            and extended["mode"] == mode and extended["refresh"] == refresh
            and set(cases) <= set(extended["selected_case_ids"]), "native_case_extended_selection_invalid")
    directory = Path(directory).absolute()
    _require(not directory.exists() and directory.resolve() == directory, "native_private_output_required")
    directory.mkdir(mode=0o700)
    from tools._review_journal_artifacts import selected as collection_selected
    if extended is None:
        _require(journal_collection is False, "native_case_extended_selection_invalid")
    else:
        _require(journal_collection is False or collection_selected(extended), "native_case_extended_selection_invalid")
    originals = OriginalResponses(directory / "observations", journal_collection=extended is not None and collection_selected(extended))
    rows = case_manifest()
    selected = {row["case_id"]: row for row in rows}
    report = {
        "schema_version": "native-review-case-run/v1",
        "mode": mode,
        "qualification": False,
        "case_count": 99,
        "cases": rows,
        "selected_case_ids": list(cases),
        "status": "started",
        "fixture_sha256": digest(fixture),
        "originals": [],
        "cleanup": None,
        "watchdog": case_watchdog(cases),
    }
    if extended is not None:
        from tools import _review_control_ceiling as ceiling_policy
        report.update(ceiling_policy.validate(extended))
        report.update(schema_version="native-review-case-run/v3" if originals.journal_collection else "native-review-case-run/v2", extended_selection=extended, watchdog=watchdog(extended, cases))
        if ceiling_policy.validate(extended):
            report["schema_version"] = "native-review-case-run/v5" if report["extended_selection"]["schema_version"] == "native-review-case-extended-selection/v4" else "native-review-case-run/v4"
        originals.append(("native-case-extended-selection", extended))
    if len(cases) > (1 if extended is not None else 8):
        return _run_shards(directory, fixture, mode, cases, refresh, report, originals, source_before, evidence_bindings, extended)
    children = OwnedChildren(originals, digest({"mode": mode, "fixture": fixture, "cases": list(cases)}))
    from tools._review_dependency_case_evidence import _begin_native_run, _record_native_outcome
    ticket = _begin_native_run(children, source_before, cases, digest(fixture))
    deadlines = _case_deadlines(report["watchdog"]) if extended is not None else None
    children.open()

    prepared_inputs = {}
    fixture_deadlines = {}

    def inputs(case_id=None):
        frozen = datetime.now(timezone.utc)
        selected = _fixture_inputs(fixture, case_id, frozen_at=frozen,
            temporal_margin_seconds=extended["temporal_margin_seconds"] if extended is not None else None)
        prepared_inputs[digest(selected)] = (case_id, frozen.isoformat())
        return selected

    def provision_fixture(base, selected_fixture, selected_mode):
        options = {}
        if extended is not None:
            from tools._review_dependency_case_budget import allocation
            fixture_case_id, prepared_at = prepared_inputs[digest(selected_fixture)]
            binding = allocation(extended, selected_fixture, selected_mode, cases,
                fixture_case_id=fixture_case_id, fixture_prepared_at=prepared_at)
            options = {"allocation_binding": binding, "execution_policy": extended["execution_policy"],
                "run_seconds": binding["fixture_budget"]["fixture_run_seconds"]}
            originals.append(("native-case-extended-allocation", binding))
        if extended is None:
            return provision(base, selected_fixture, originals, review_mode=selected_mode,
                children=children, native_case_plan=case_plan())
        fixture_deadlines[base / "workspace"] = time.monotonic() + options["run_seconds"]
        with workload_deadline(extended["phase_caps"]["setup"]):
            return provision(base, selected_fixture, originals, review_mode=selected_mode,
                children=children, native_case_plan=case_plan(), **options)

    def fixture_window(harness):
        if extended is None:
            return nullcontext()
        remaining = fixture_deadlines[harness.root] - time.monotonic()
        _require(remaining > 0, "native_case_fixture_deadline")
        return workload_deadline(remaining)

    try:
        with _case_phase(deadlines[0]) if extended is not None else workload_deadline(report["watchdog"]["watchdog_seconds"]):
            negative_cases = {"source-effective-interval-not-started", "source-record-bound"}
            main_fixture = inputs(cases[0] if len(cases) == 1 and cases[0] not in negative_cases else None)
            main_mode = _initial_mode(cases[0] if len(cases) == 1 else None, mode)
            report["main_baseline_mode"] = main_mode
            originals.append(("main-native-fixture", main_fixture))
            root, runtime, _sources, _keys, claims = provision_fixture(directory / "fixture", main_fixture, main_mode)
            harness = NativeCases(root, runtime, originals, children, main_mode, claims, {}, main_fixture, _sources, _keys)
            harness.extended_caps = extended["phase_caps"] if extended is not None else None
            harness.setup_residual()
            harness.setup_usage_role()
            harness.setup_pending_source()
            with fixture_window(harness):
                harness.establish()
            for case_id in cases:
                row = selected[case_id]
                try:
                    if case_id in negative_cases:
                        negative_fixture = inputs(case_id)
                        originals.append((case_id + "-negative-fixture", negative_fixture))
                        try:
                            provision_fixture(directory / ("negative-" + case_id), negative_fixture, mode)
                        except (Exception, SystemExit, _HostDeadline) as error:
                            reason = _reason(error)
                            expected = (
                                {"strict_source_outside_effective_interval"}
                                if case_id.endswith("not-started")
                                else {
                                    "assessment_source_bound_exceeded",
                                    "source_selector_invalid",
                                    "source_bound",
                                    "selective_source_bound",
                                }
                            )
                            error_code = getattr(error, "error_code", None)
                            _require(
                                reason in expected
                                or (
                                    case_id == "source-record-bound"
                                    and error_code == "RESEARCH_REVIEW_DEPENDENCY_SOURCE_BOUND"
                                ),
                                "unexpected_negative_fixture_provision_refusal",
                            )
                            physical = directory / ("negative-" + case_id) / "workspace/sources/manifest.jsonl"
                            physical_rows = physical.read_bytes().splitlines() if physical.is_file() else []
                            if case_id == "source-record-bound":
                                _require(len(physical_rows) == 65, "native_65_source_inventory_not_realized")
                                originals.append(
                                    (case_id + "-actual-inventory", [json.loads(line) for line in physical_rows])
                                )
                            row.update(
                                status="passed_limited_scope",
                                assertions=[
                                    "actual_native_first_deposit_fixture",
                                    "native_refusal_before_review_acceptance",
                                ],
                                evidence=[
                                    {
                                        "error_code": getattr(error, "error_code", None),
                                        "reason": reason,
                                        "actual_source_inventory_count": len(physical_rows),
                                    }
                                ],
                                earlier_owner_refusal=reason,
                                unavailable_branches=["same_host_accepted_baseline_before_future_effective_source"]
                                if case_id.endswith("not-started")
                                else ["later_source_count_gate_after_earlier_native_refusal"],
                                isolation="separate_fresh_negative_fixture_with_separate_genuine_positive_baseline",
                            )
                        else:
                            raise AssertionError("negative_fixture_provision_was_not_refused")
                        _record_native_outcome(children, ticket, case_id)
                        continue
                    selected_harness = harness
                    if len(cases) > 1 and _needs_isolated_fixture(case_id):
                        selected_fixture = inputs(case_id)
                        originals.append((case_id + "-native-fixture", selected_fixture))
                        selected_mode = _initial_mode(case_id, mode)
                        root, runtime, _sources, _keys, claims = provision_fixture(
                            directory / ("isolated-" + case_id), selected_fixture, selected_mode)
                        selected_harness = NativeCases(
                            root, runtime, originals, children, selected_mode, claims, {}, selected_fixture, _sources, _keys
                        )
                        selected_harness.extended_caps = extended["phase_caps"] if extended is not None else None
                        selected_harness.setup_residual()
                        selected_harness.setup_usage_role()
                        selected_harness.setup_pending_source()
                        with fixture_window(selected_harness):
                            selected_harness.establish()
                    row["initial_mode"] = selected_harness.mode
                    with fixture_window(selected_harness):
                        row.update(selected_harness.run_case(case_id, refresh=refresh))
                    _record_native_outcome(children, ticket, case_id)
                except (Exception, SystemExit, _HostDeadline) as error:
                    row.update(
                        status="failed",
                        reason=type(error).__name__,
                        assertions=[],
                        native_reason=_reason(error),
                        error_code=getattr(error, "error_code", None),
                    )
                    report["status"] = "failed"
                    raise
            statuses = {selected[case]["status"] for case in cases}
            report["status"] = (
                "completed"
                if statuses <= {"passed"}
                else (
                    "completed_with_limits"
                    if statuses <= {"passed", "passed_limited_scope", "earlier_owner_refusal"}
                    else "incomplete"
                )
            )
    except (Exception, SystemExit, _HostDeadline) as error:
        _case_failure(report, error, originals)
        raise
    finally:
        if extended is not None:
            primary = sys.exc_info()[1]
            try:
                _finish_extended_cases(directory, report, children, source_before, refresh, evidence_bindings, ticket, deadlines)
            except BaseException:
                if primary is None or not getattr(originals, "journal_collection", False):
                    raise
        else:
            try:
                report["cleanup"] = children.close()
            finally:
                report["originals"] = originals.pins
                (directory / "report.json").write_bytes(
                    (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
                )
                _finish_case_evidence(directory, report, children, source_before, refresh, evidence_bindings, ticket)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", choices=("selective_reuse", "legacy_full"), default="selective_reuse")
    parser.add_argument("--case", action="append", dest="cases", choices=CASE_IDS)
    parser.add_argument("--all-cases", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--evidence-bindings", type=Path)
    parser.add_argument("--extended-recipe", type=Path)
    parser.add_argument("--execution-profile", choices=("eight-hour", "sixteen-hour", "twenty-four-hour"))
    parser.add_argument("--temporal-margin-seconds", type=int)
    parser.add_argument("--extended-selection", type=Path)
    parser.add_argument("--journal-collection", action="store_true")
    args = parser.parse_args(argv)
    if args.all_cases and args.cases:
        parser.error("--all-cases and --case are mutually exclusive")
    fixture = json.loads(args.fixture.read_bytes())
    evidence_bindings = None
    if args.evidence_bindings:
        from tools._review_dependency_case_evidence import existing_pin, read
        binding_pin = existing_pin(args.evidence_bindings)
        evidence_bindings = read(binding_pin, roots=[args.evidence_bindings.absolute().parent])
    from tools._review_dependency_case_evidence import existing_pin
    extended_selection = json.loads(args.extended_selection.read_bytes()) if args.extended_selection else None
    report = run_cases(
        args.output,
        fixture,
        mode=args.mode,
        cases=CASE_IDS if args.all_cases else args.cases or INITIAL_CASES,
        refresh=args.refresh,
        evidence_bindings=evidence_bindings,
        extended_recipe_pin=existing_pin(args.extended_recipe) if args.extended_recipe else None,
        execution_profile=args.execution_profile, temporal_margin_seconds=args.temporal_margin_seconds,
        extended_selection=extended_selection, journal_collection=args.journal_collection,
    )
    print(json.dumps({"status": report["status"], "report": str(args.output / "report.json"), "qualification": False}))
    return 0 if report["status"] in {"completed", "completed_with_limits"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
