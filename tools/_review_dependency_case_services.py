"""Actual service selection, protected mode transitions and installed producers."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import nullcontext
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from evidence_wiki import (
    BoundedSubprocessExecutor,
    ExecutionAttempt,
    HostRuntime,
    QualifiedEvaluators,
    ResearchExecution,
    Workspace,
)
from evidence_wiki.agent_qualification import reference_suite
from evidence_wiki.pack_discovery import owner
from evidence_wiki.workflow_contracts import EXECUTION_OBSERVATION, JOB_V3
from tools._review_dependency_case_contracts import CLAIM_IDS, _accepted, _basis, _reason, _require
from tools._review_dependency_workload import _example, canonical, digest, workload_deadline
from tools._review_journal_artifacts import POLICY as JOURNAL_COLLECTION_POLICY
from tools._review_journal_artifacts import selected as collection_selected


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _constructor_inputs(value):
    """Export inert constructor arguments, never an uninitialized owner object."""
    if is_dataclass(value) and not isinstance(value, type):
        return {"constructor": type(value).__name__, "arguments": {
            field.name: _constructor_inputs(getattr(value, field.name)) for field in fields(value) if field.init}}
    if type(value) in {tuple, list, frozenset}:
        return [_constructor_inputs(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if type(value) is dict:
        return {key: _constructor_inputs(item) for key, item in value.items()}
    _require(type(value) in {str, int, float, bool, type(None)}, "native_constructor_input_unsupported")
    return value


def _construct_profile(value):
    from evidence_wiki import (
        DisagreementPlan,
        ExecutionProfile,
        ExecutionRole,
        ExecutionRoleRegistry,
        ExecutionRoleScope,
        NativeInputHandoff,
        ReviewDelivery,
    )
    from evidence_wiki.review_application_lease import ReviewApplicationLease

    constructors = {kind.__name__: kind for kind in (ExecutionProfile, ExecutionRole, ExecutionRoleRegistry,
        ExecutionRoleScope, NativeInputHandoff, DisagreementPlan, ReviewDelivery, ReviewApplicationLease)}

    def construct(item):
        if type(item) is dict:
            _require(set(item) == {"constructor", "arguments"} and item["constructor"] in constructors,
                "native_profile_constructor_unsupported")
            return constructors[item["constructor"]](**{key: construct(child) for key, child in item["arguments"].items()})
        if type(item) is list:
            return tuple(construct(child) for child in item)
        _require(type(item) in {str, int, float, bool, type(None)}, "native_profile_constructor_input_invalid")
        return item

    return construct(value)


def _history_bytes(root, state_directory):
    """Hold existing signed journal bytes only, without exporting credentials."""
    return {path: path.read_bytes() for directory in (root, Path(state_directory))
        for path in directory.rglob("*.jsonl") if path.is_file()}


def _preserved_history(before):
    for path, raw in before.items():
        _require(path.is_file() and path.read_bytes().startswith(raw), "native_signed_history_rewritten")
    return [{"path": str(path), "original_sha256": _hash(raw), "original_bytes": len(raw),
             "preserved_prefix": True} for path, raw in sorted(before.items())]


class NativeCaseServices:
    """Select real immutable services; admissions remain canonical owner calls."""

    def _new_executor(self, profile):
        current = self.runtime.execution.executor
        executor = BoundedSubprocessExecutor(profile, commands=current._commands, cwd=current._cwd,
            receipt_key=self.keys["receipt"], meter=_example()[0]())
        self.children.register_review_executor(executor, "local_review_worker")
        return executor

    def _select_services(self, profile, qualifications):
        previous = self.runtime.execution
        execution = ResearchExecution(self._new_executor(profile), qualifications=qualifications,
            review_signer=previous.review_signer, observer_key=self.keys["observer"])
        self.runtime = HostRuntime.from_services(self.runtime.policy, self.runtime.context, execution)
        self.originals.append(("native-service-selection", {"execution_identity": execution.identity(),
            "public_profile": profile.document(), "qualification_registry_sha256": qualifications.sha256}))

    def _qualify_profile(self, profile, label):
        """Score fresh authenticated reference jobs with the real parent issuer."""
        caps = getattr(self, "extended_caps", None)
        with workload_deadline(caps["setup"]) if caps is not None else nullcontext():
            issuer = self.runtime.execution.qualifications.authority
            cases = [{"case_id": "case-" + str(i), "family_id": "family-" + str(i // 10),
                "input_document": {"case_id": "case-" + str(i), "supported": i // 2 % 2 == 0, "wording": i % 2},
                "expected_label": "accept" if i // 2 % 2 == 0 else "reject", "critical": i // 2 % 2 != 0,
                "pair_id": "pair-" + str(i // 2)} for i in range(100)]
            config = owner("_strict_evidence").configuration(self.root)
            suite = reference_suite(cases, suite_ref="fixture-only", split_ref="fixture-held-out",
                domain_ref="synthetic-local", rubric=config["strict_evidence"]["rubric"])
            executor = self._new_executor(profile)
            qualifications = []
            for config_ref in ("evidence", "critic"):
                observations = []
                role = profile.role(config_ref)
                for index, case in enumerate(cases):
                    instant = datetime.now(timezone.utc)
                    job = {"schema_version": JOB_V3, "id": "reference-" + str(index), "operation": "evaluate", "role": "evaluator",
                        "controller_ref": role.controller_ref, "grant_ref": "fixture-model", "input_id": case["case_id"],
                        "basis_id": "fixture-basis", "policy_id": self.runtime.policy.policy_id, "policy_version": "1",
                        "prompt_version": role.prompt_version, "revision_precondition": "fixture-basis", "original_text": None,
                        "scope": self.runtime.policy.to_dict()["scope"]["question_ids"], "required_capabilities": ["model"],
                        "payload": {"claim_id": "reference", "packet_id": case["case_id"]}, "issued_utc": instant.isoformat(),
                        "deadline_utc": (instant + timedelta(seconds=10)).isoformat(), "timeout_seconds": 5, "max_attempts": 1,
                        "expected_result_schema": EXECUTION_OBSERVATION, "budgets": profile.document()["job_budgets"],
                        "qualifications": {"assurance": "artifact_checked", "status": "unknown", "evaluator_ref": None,
                            "qualification_ref": None, "context_basis": None}, "task_role": profile.role_scopes.scope(config_ref).task_role,
                        "role_scope_sha256": profile.role_scopes.scope(config_ref).sha256,
                        "context_id": "fixture-reference-" + str(index), "packet_sha256": _hash(canonical(case["input_document"])[:-1])}
                    attempt = ExecutionAttempt.create(attempt_id=config_ref + "-reference-" + str(index), nonce="nonce-" + str(index),
                        run_id="fixture-references", binding_id="fixture-reference-binding", profile=profile, config_ref=config_ref,
                        job=job, input_document=case["input_document"])
                    observed = executor.invoke(attempt)
                    sequence, started = 0, time.monotonic()
                    while True:
                        authenticated = executor.authenticate(observed, attempt)
                        self.originals.append((label + "-reference-" + config_ref + "-" + str(index) + "-" + str(sequence),
                            {"attempt": attempt.document(), "observation": authenticated}))
                        if observed["state"] != "running":
                            break
                        _require(time.monotonic() - started < 30, "native_reference_worker_deadline")
                        time.sleep(0.005)
                        observed = executor.poll(attempt)
                        sequence += 1
                    _require(observed["state"] == "succeeded" and observed["final_accounting"], "native_reference_worker_incomplete")
                    self.children.confirm_completed()
                    observations.append((attempt, authenticated))
                record = issuer.issue(self.runtime.policy, profile, config_ref, suite, cases, observations,
                    executor, expires_utc=self.keys["expires_at"])
                _require(issuer.verify(record) == record and len(record["observation_refs"]) == 100,
                    "native_reference_qualification_unverified")
                qualifications.append(record)
            self.originals.append((label + "-issued-qualifications", qualifications))
            return QualifiedEvaluators(qualifications, issuer)

    def _service_case(self, case_id):
        if case_id == "producer-identity":
            return self._producer_case(case_id)
        if case_id in {"qualification-profile", "current-qualification-failure"}:
            return self._qualification_case(case_id)
        return self._migration_case(case_id)

    def _qualification_case(self, case_id):
        _require(self.mode == "selective_reuse", "native_selective_service_fixture_required")
        before = self.guard()
        history = _history_bytes(self.root, self.runtime.context.state_directory)
        profile = self.runtime.execution.profile
        roles = tuple(replace(role, prompt_version="review/v2") if role.role == "evaluator" else role for role in profile.roles)
        selected = replace(profile, roles=roles)
        if case_id == "qualification-profile":
            qualifications = self._qualify_profile(selected, case_id)
        else:
            qualifications = QualifiedEvaluators([], self.runtime.execution.qualifications.authority)
        self._select_services(selected, qualifications)
        current = self.observe(case_id, run_id="observe-" + digest(case_id)[:24])
        _require(not _accepted(current["strict"]) and current["publication"]["verdict"] != "ship",
            "changed_service_reused_approval")
        _require(all(_basis(current["strict"])[claim] != _basis(self.baseline["strict"])[claim] for claim in CLAIM_IDS),
            "qualification_profile_not_globally_bound")
        _require(self.guard()["workspace_files"] == before["workspace_files"]
            and self.guard()["authority_sha256"] == before["authority_sha256"], "service_selection_changed_workspace_or_trust")
        with self.session("service-current-" + digest(case_id)[:24]) as (_workspace, session):
            observed = session.current()
            qualification = observed["qualification"]
            self.originals.append((case_id + "-actual-current", observed))
            expected = "qualified" if case_id == "qualification-profile" else "unknown"
            _require(qualification["state"] == expected, "native_qualification_state_unexpected")
            if case_id == "current-qualification-failure":
                from evidence_wiki import execution_session as native
                from evidence_wiki.review_packet_execution import reserve_evaluator

                with native.gate(session):
                    native._begin(session)
                self.originals.append_records(case_id + "-independent-run-setup", native.retained(session))
                refusal = self.refused(case_id, lambda: reserve_evaluator(session, request_id="missing-qualification",
                    assembly_request_id="not-dispatched", original_id="capacity", claim_id="observed-capacity", config_ref="critic"),
                    reasons={"qualified_packet_evaluator_required"})
            else:
                refusal = None
        return {"status": "passed", "assertions": ["actual_immutable_service_selection", "all_claim_bases_changed",
                "all_old_approvals_blocked", "actual_current_qualification", "signed_history_preserved"],
            "evidence": [{"qualification_state": qualification["state"], "refusal": refusal,
                "history": _preserved_history(history)}],
            "material_invalidated": sorted(CLAIM_IDS),
            "realization": "selected_prompt_revision_and_fresh_reference_qualification" if refusal is None
                else "selected_prompt_revision_with_empty_authenticated_registry",
            "isolation": "fresh_native_fixture_no_history_restore"}

    def _install_mode(self, mode, revision):
        _require(mode in {"legacy_full", "selective_reuse"}, "native_mode_unknown")
        path = self.root / "research.yml"
        config = yaml.safe_load(path.read_bytes())
        policy = config["strict_evidence"]
        policy.update(schema_version="evidence-strict-policy/v2" if mode == "selective_reuse" else "evidence-strict-policy/v1",
            revision=str(revision))
        if mode == "selective_reuse":
            policy["review_basis_mode"] = "claim_dependencies_v1"
        else:
            policy.pop("review_basis_mode", None)
        owner("_strict_contract").policy_document(policy)
        authority_path = Path(self.runtime.context.authority_file)
        authority = json.loads(authority_path.read_bytes())
        authority["strict_workspaces"][owner("_evidence_usage").workspace_binding(self.root)] = copy.deepcopy(policy)
        path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8", newline="\n")
        replacement = authority_path.with_name("mode-authority.json")
        with replacement.open("xb") as stream:
            stream.write(canonical(authority))
        replacement.chmod(0o600)
        os.replace(replacement, authority_path)
        profile = self.runtime.execution.profile
        delivery = replace(profile.review_delivery, context_version="dependencies" if mode == "selective_reuse" else "complete")
        selected = replace(profile, review_delivery=delivery)
        self._select_services(selected, self._qualify_profile(selected, "mode-" + mode))
        self.mode = mode
        self.originals.append(("protected-mode-selection", {"policy": policy,
            "authority_sha256": _hash(authority_path.read_bytes()), "configuration_sha256": _hash(path.read_bytes()),
            "execution_profile": selected.document()}))

    def _validate_original(self, envelope):
        """Use the genuine registration validator with current protected inputs."""
        config = owner("_strict_evidence").configuration(self.root)
        with owner("_evidence_usage").current_view(self.root, config) as view:
            return owner("_strict_evidence").validate_review(self.root, config, envelope, view)

    def _migration_case(self, case_id):
        history = _history_bytes(self.root, self.runtime.context.state_directory)
        source_mode = self.mode
        application = self.applications[("baseline", "observed-capacity", "critic")]
        original = copy.deepcopy(application["envelope"])
        self.raw(case_id + "-original-envelope", canonical(original))
        if case_id == "mode-caller-flag-cannot-switch":
            _require(self.mode == "selective_reuse", "native_selective_mode_required")
            candidate = self.candidate(case_id)
            candidate["payload"]["body"]["review"]["review_basis_mode"] = "whole_workspace"
            signer = self.runtime.execution.review_signer
            identity = next(row for row in signer.reviewers if row.config_ref == "critic")
            candidate = signer._sign(candidate["payload"], identity, datetime.now(timezone.utc).isoformat())
            self.raw(case_id + "-caller-envelope", canonical(candidate))
            with self.session("caller-mode-observation") as (_workspace, _session):
                refusal = self.refused(case_id, lambda: self._validate_original(candidate),
                    reasons={"review_dependency_contract_invalid"})
            current = self.observe(case_id, run_id="caller-mode-closing")
            _require(current["strict"]["review_basis_mode"] == "claim_dependencies_v1"
                and _accepted(current["strict"]) == CLAIM_IDS, "caller_changed_protected_mode")
            return {"status": "passed", "assertions": ["actual_signed_caller_override_refused", "protected_mode_unchanged",
                    "baseline_approvals_preserved", "failed_call_no_write"], "evidence": [refusal]}
        enable = case_id in {"protected-policy-enable-selective", "legacy-envelope-not-convertible"}
        _require(source_mode == ("legacy_full" if enable else "selective_reuse"), "native_migration_start_mode_invalid")
        selected_mode = "selective_reuse" if enable else "legacy_full"
        self._install_mode(selected_mode, 2 if enable else 3)
        current = self.observe(case_id, run_id="migration-" + digest(case_id)[:24])
        _require(not _accepted(current["strict"]) and current["publication"]["verdict"] != "ship", "migration_reused_old_approval")
        _require(current["strict"]["basis_id"] != self.baseline["strict"]["basis_id"], "migration_workspace_basis_unchanged")
        refusals = []
        with self.session("migration-ingress-" + digest(case_id)[:24]) as (_workspace, _session):
            refusal = self.refused(case_id, lambda: self._validate_original(original),
                reasons={"strict_review_policy_schema_mismatch"})
            refusals.append(refusal)
            if case_id == "legacy-envelope-not-convertible":
                relabeled = copy.deepcopy(original)
                relabeled["payload"]["body"]["review"]["schema_version"] = "evidence-strict-review/v3"
                self.raw(case_id + "-relabeled-without-resigning", canonical(relabeled))
                refusals.append(self.refused(case_id + "-relabeled", lambda: self._validate_original(relabeled),
                    reasons={"review_dependency_contract_invalid"}))
        preserved = _preserved_history(history)
        self.drive(CLAIM_IDS, cycle=case_id + "-fresh")
        refreshed = self.observe(case_id + "-fresh", run_id="migration-closing-" + digest(case_id)[:24])
        _require(_accepted(refreshed["strict"]) == CLAIM_IDS and refreshed["publication"]["verdict"] == "ship",
            "fresh_migration_reviews_not_accepted")
        _preserved_history(history)
        return {"status": "passed", "assertions": ["actual_coherent_protected_mode_transition",
                "all_old_approvals_blocked", "actual_original_envelope_policy_refusal", "no_signed_history_conversion",
                "fresh_authenticated_review_cohort", "final_native_acceptance_and_publication"],
            "evidence": [{"source_mode": source_mode, "selected_mode": selected_mode, "refusals": refusals,
                "history": preserved}], "material_invalidated": sorted(CLAIM_IDS),
            "separate_selective_positive": source_mode == "selective_reuse" or selected_mode == "selective_reuse",
            "isolation": "same_original_root_preserved_history_new_policy_revision"}

    def _producer_case(self, case_id):
        _require(self.mode == "selective_reuse", "native_selective_producer_fixture_required")
        project = Path(__file__).resolve().parents[1]
        installation = self.root.parent / "trusted-producer"
        _require(not installation.exists(), "native_producer_installation_exists")
        installation.mkdir(mode=0o700)
        ignored = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
        shutil.copytree(project / "src/evidence_wiki", installation / "src/evidence_wiki", ignore=ignored)
        for name in ("workspace-template", "domain-packs", "orchestrator"):
            shutil.copytree(project / name, installation / name, ignore=ignored)
        shutil.copyfile(project / "pyproject.toml", installation / "pyproject.toml")
        target = installation / "workspace-template/scripts/_strict_evidence.py"
        original_digest = _hash(target.read_bytes())
        with target.open("ab") as stream:
            stream.write(b"\n")
        self.originals.append((case_id + "-trusted-installation", {"root": str(installation),
            "changed_member": "assets/workspace-template/scripts/_strict_evidence.py",
            "original_sha256": original_digest, "installed_sha256": _hash(target.read_bytes()),
            "selection": "new_python_process_paired_package_and_assets_before_producer_capture"}))
        context = {field.name: _constructor_inputs(getattr(self.runtime.context, field.name))
            for field in fields(self.runtime.context) if field.init}
        stored = self.runtime.execution.qualifications
        signer = self.runtime.execution.review_signer

        def signing_inputs(identity):
            return {field.name: getattr(identity, field.name) for field in fields(identity) if field.name != "key"}

        payload = {**({"journal_collection_policy": JOURNAL_COLLECTION_POLICY} if self.originals.journal_collection else {}), "root": str(self.root), "profile": _constructor_inputs(self.runtime.execution.profile),
            "workspace_binding": {"path": str(self.root), "device": str(self.root.stat().st_dev),
                "inode": str(self.root.stat().st_ino)},
            "profile_document": self.runtime.execution.profile.document(), "policy": self.runtime.policy.to_dict(),
            "qualifications": {"records": json.loads(stored._records), "issuer_ref": stored.authority.issuer_ref,
                "generator_controller_ref": stored.authority.generator_controller_ref},
            "signer": {"authority_policy_id": signer.authority_policy_id, "authority_revision": signer.authority_revision,
                "expires_utc": signer.expires_utc, "not_before_utc": signer.not_before_utc,
                "generator": signing_inputs(signer.generator), "reviewers": [signing_inputs(row) for row in signer.reviewers]},
            "context": context, "keys": {name: self.keys[name].hex() for name in
                ("generator", "evidence", "critic", "receipt", "observer", "qualifier")},
            "commands": self.runtime.execution.executor._commands,
            "cwd": str(self.runtime.execution.executor._cwd), "expected_identity": self.runtime.execution.identity(),
            "baseline": self.baseline, "originals": [{key: application[key] for key in
                ("run_id", "record_sha256", "coordinate")} for key, application in self.applications.items() if key[0] == "baseline"]}
        code = "import sys; sys.path[:0] = " + repr([str(installation / "src"), str(project)]) + "; from tools._review_dependency_case_services import _producer_entry; _producer_entry()"
        before = self.guard()
        child = subprocess.Popen([str(project / ".venv/bin/python"), "-I", "-c", code], cwd=installation,  # noqa: S603 - trusted fixed interpreter and private installation
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        # The inherited pipe carries only this fixture's freshly minted secrets.
        # Its bytes are never written to an artifact, a log or worker input.
        stdout, stderr = child.communicate(canonical(payload), timeout=600)
        self.children.confirm_completed()
        _require(not stderr and len(stdout) <= 16_777_216, "native_producer_child_output_invalid")
        result = json.loads(stdout)
        self.originals.append((case_id + "-actual-installed-observations", result))
        _require(child.returncode == 0, "native_producer_child_failed")
        _require(result["status"] == "passed" and self.guard() == before, "native_producer_child_changed_custody")
        return {"status": "passed_limited_scope" if result["retained_refusals"] else "passed",
            "assertions": ["separate_trusted_byte_distinct_installed_producer", "actual_native_producer_capture",
                "same_original_workspace_root_and_history", "all_claim_bases_changed", "all_old_approvals_blocked",
                "read_only_closing_custody"], "evidence": [result], "material_invalidated": sorted(CLAIM_IDS),
            "unavailable_branches": ["retained_service_internals_after_original_generation_binding_refusal"]
                if result["retained_refusals"] else [],
            "isolation": "new_trusted_import_generation_same_original_fixture_root"}


def _producer_entry():
    """Expose only bounded public observations or sanitized refusal metadata."""
    try:
        _producer_observation()
    except Exception as error:
        sys.stdout.buffer.write(canonical({"status": "failed", "failure": {
            "kind": type(error).__name__, "error_code": getattr(error, "error_code", None), "reason": _reason(error)}}))
        raise SystemExit(1) from None


def _producer_observation():
    """Read an original fixture from a separately installed trusted generation."""
    from contextlib import contextmanager

    from evidence_wiki import HostReviewSigner, QualificationAuthority, RunPolicy, SigningIdentity, _review_producer
    from evidence_wiki import execution_session as native
    from evidence_wiki._review_dependency_inputs import bind_session
    from evidence_wiki.host_context import HostContext

    # Only the trusted parent invokes this private process entry point and pipe.
    raw = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
    _require(len(raw) <= 4 * 1024 * 1024, "native_producer_capsule_bound")
    payload = json.loads(raw)
    keys = {name: bytes.fromhex(value) for name, value in payload["keys"].items()}
    profile = _construct_profile(payload["profile"])
    _require(profile.document() == payload["profile_document"], "native_producer_profile_reconstruction_changed")
    stored = payload["qualifications"]
    authority = QualificationAuthority(stored["issuer_ref"], keys["qualifier"], stored["generator_controller_ref"])
    qualifications = QualifiedEvaluators(stored["records"], authority)
    saved = payload["signer"]

    def signing_identity(value):
        return SigningIdentity(**value, key=keys[value["config_ref"]])

    signer = HostReviewSigner(saved["authority_policy_id"], saved["authority_revision"],
        signing_identity(saved["generator"]), tuple(signing_identity(row) for row in saved["reviewers"]),
        saved["expires_utc"], saved["not_before_utc"])
    _require(type(signer.generator) is SigningIdentity, "native_producer_signer_reconstruction_invalid")
    executor = BoundedSubprocessExecutor(profile, commands=payload["commands"], cwd=payload["cwd"],
        receipt_key=keys["receipt"], meter=_example()[0]())
    execution = ResearchExecution(executor, qualifications=qualifications, review_signer=signer,
        observer_key=keys["observer"])
    _require(execution.identity() == payload["expected_identity"], "native_producer_runtime_identity_changed")
    runtime = HostRuntime.from_services(RunPolicy.from_dict(payload["policy"]), HostContext(**payload["context"]), execution)

    def workspace_binding():
        path = Path(payload["root"])
        info = path.stat()
        return {"path": str(path), "device": str(info.st_dev), "inode": str(info.st_ino)}

    _require(workspace_binding() == payload["workspace_binding"], "native_producer_original_root_changed")

    @contextmanager
    def session(run_id):
        with runtime.context.activate(), Workspace.open(payload["root"]) as workspace:
            with workspace.research.session(runtime.policy, run_id=run_id, execution=execution) as selected, bind_session(selected):
                yield workspace, selected

    with session("installed-producer-observation") as (workspace, _session):
        strict, publication = workspace.strict.check(), workspace.strict.export()
    baseline = payload["baseline"]["strict"]
    _require(strict["review_producer_id"] != baseline["review_producer_id"], "native_producer_identity_unchanged")
    _require(not _accepted(strict) and publication["verdict"] != "ship", "installed_producer_reused_old_approval")
    _require(all(_basis(strict)[claim] != _basis(baseline)[claim] for claim in CLAIM_IDS), "native_producer_not_globally_bound")
    retained, refusals = [], []
    for original in payload["originals"]:
        try:
            with session(original["run_id"]) as (_workspace, selected):
                facts = native.retained_review_evidence(selected,
                    {key: original[key] for key in ("run_id", "record_sha256")}, original["coordinate"],
                    **({"journal_collection": True} if collection_selected(payload) else {}))
            _require(facts["state"] != "eligible", "installed_producer_retained_old_approval")
            retained.append(facts)
        except Exception as error:
            code = getattr(error, "error_code", None)
            reason = _reason(error)
            _require(reason in {"binding_changed", "owner_generation_changed"}
                or code == "RESEARCH_BINDING_CHANGED", "unexpected_installed_producer_retained_refusal")
            refusals.append({"original": original, "error_code": code, "reason": reason})
    generation = _review_producer.capture()
    _review_producer.assert_current(generation)
    _require(generation.producer_id == strict["review_producer_id"], "native_producer_observation_changed")
    _require(workspace_binding() == payload["workspace_binding"], "native_producer_original_root_changed")
    document = {"status": "passed", "strict": strict, "publication": publication,
        "producer_generation": {"old": baseline["review_producer_id"], "current": strict["review_producer_id"]},
        "producer_observation": generation.document(), "script_root": str(generation.script_generation[0]),
        "workspace_binding": workspace_binding(),
        "retained": retained, "retained_refusals": refusals, "original_root": str(payload["root"]),
        "runtime_identity": execution.identity()}
    sys.stdout.buffer.write(canonical(document))
