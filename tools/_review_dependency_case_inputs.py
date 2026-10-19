"""Actual owner input, negative history and controlled-interleaving adapters."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import queue
import secrets
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from evidence_wiki import Workspace
from evidence_wiki.pack_discovery import owner
from tools._review_dependency_case_contracts import CLAIM_IDS, _accepted, _basis, _dependants, _reason, _require
from tools._review_dependency_workload import _deposit, _example, canonical, digest, render_question_body


class NativeCaseInputs:
    """Every adapter invokes real owners and retains its actual assertion scope."""

    def _link_current_sources(self, label):
        actions = []
        with self.runtime.context.activate(), Workspace.open(self.root) as workspace:
            for question in self.manifest["questions"]:
                request = workspace.mutations.prepare(
                    request_id=label + "-link-" + question["slug"],
                    operation="coverage.link",
                    arguments={
                        "slug": question["slug"],
                        "facet_id": "primary",
                        "clear_accepted_source_ids": True,
                        "accepted_source_ids": [source["source_id"] for source in self.sources.values()],
                    },
                )
                linked = workspace.mutations.submit(request)
                request = workspace.mutations.prepare(
                    request_id=label + "-evaluate-" + question["slug"],
                    operation="coverage.evaluate",
                    arguments={"slug": question["slug"]},
                )
                evaluated = workspace.mutations.submit(request)
                _require(
                    linked["status"] == evaluated["status"] == "committed", "source_membership_coverage_not_committed"
                )
                actions.append({"link": linked, "evaluation": evaluated})
        self.originals.append((label + "-coverage", actions))

    def _source_files(self, name):
        source = self.sources[name]
        selected = {
            source["normalized_path"],
            "raw/papers/" + name + ".md",
            "raw/papers/" + name + ".md.provenance.yml",
        }
        sidecar = (self.root / source["normalized_path"]).with_suffix(".structured.json")
        if sidecar.exists():
            selected.add(sidecar.relative_to(self.root).as_posix())
        _require(all((self.root / path).is_file() for path in selected), "native_source_owned_files_missing")
        return {path: (self.root / path).read_bytes() for path in sorted(selected)}

    def setup_pending_source(self):
        """Withhold only an already captured unused input before any review exists."""
        if "added" not in self.sources:
            return
        _require(not self.applications and self.baseline is None, "pending_source_after_native_baseline")
        source = self.sources["added"]
        files = self._source_files("added")
        for path in files:
            (self.root / path).unlink()
        self.pending_source = {"source": self.sources.pop("added"), "files": files}
        with self.runtime.context.activate(), Workspace.open(self.root) as workspace:
            inventory = workspace.sources.inventory()
        self.originals.append(("pending-source-initial-inventory", inventory))
        self._link_current_sources("pending-source")
        manifest = [json.loads(line) for line in (self.root / "sources/manifest.jsonl").read_bytes().splitlines()]
        _require(
            len(manifest) == 3 and source["source_id"] not in {row["id"] for row in manifest},
            "pending_source_not_withheld_before_baseline",
        )
        self.originals.append(
            (
                "pending-source-before-baseline",
                {
                    "source_id": source["source_id"],
                    "current_source_count": len(manifest),
                    "captured_and_authorized_before_baseline": True,
                    "files": {path: hashlib.sha256(raw).hexdigest() for path, raw in files.items()},
                },
            )
        )

    def _source_membership_case(self, case_id):
        if case_id == "source-addition":
            _require(self.pending_source is not None, "native_pending_source_fixture_missing")
            for path, raw in self.pending_source["files"].items():
                with (self.root / path).open("xb") as stream:
                    stream.write(raw)
            self.sources["added"] = self.pending_source["source"]
        else:
            removed = self._source_files("unused")
            self.sources.pop("unused")
            self._link_current_sources(case_id)
            for path in removed:
                (self.root / path).unlink()
        with self.runtime.context.activate(), Workspace.open(self.root) as workspace:
            inventory = workspace.sources.inventory()
        self.originals.append((case_id + "-inventory", inventory))
        manifest = [json.loads(line) for line in (self.root / "sources/manifest.jsonl").read_bytes().splitlines()]
        _require(
            {row["id"] for row in manifest} == {row["source_id"] for row in self.sources.values()},
            "native_membership_inventory_mismatch",
        )
        observed = self.observe(case_id)
        _require(not _accepted(observed["strict"]), "source_membership_inherited_old_approval")
        _require(
            observed["strict"]["basis_id"] != self.baseline["strict"]["basis_id"],
            "source_membership_workspace_basis_unchanged",
        )
        if self.mode == "selective_reuse":
            _require(
                all(
                    value != _basis(self.baseline["strict"])[claim]
                    for claim, value in _basis(observed["strict"]).items()
                ),
                "source_membership_did_not_invalidate_all_bases",
            )
        return {
            "status": "passed_limited_scope" if case_id == "source-addition" else "passed",
            "assertions": [
                "actual_same_workspace_source_inventory_transition",
                "actual_global_basis_invalidation",
                "no_inherited_approval",
                "capture_and_authority_history_preserved",
            ],
            "evidence": [],
            "actual_source_count": len(manifest),
            "realization_difference": "Previously captured and authorized input is first materialized after the genuine three-source review baseline."
            if case_id == "source-addition"
            else "Coverage is natively relinked before removing the unused source's current materialization.",
            "unavailable_branches": ["protected_post_baseline_new_capture_authorization"]
            if case_id == "source-addition"
            else [],
        }

    @contextmanager
    def fixture_clock(self, observed):
        usage = owner("_evidence_usage")
        actual = usage.datetime

        class ObservedClock(actual):
            @classmethod
            def now(cls, tz=None):
                return observed.replace(tzinfo=None) if tz is None else observed.astimezone(tz)

        usage.datetime = ObservedClock
        try:
            yield
        finally:
            usage.datetime = actual

    def usage_action(self, action, body, request_id):
        with self.runtime.context.activate(), Workspace.open(self.root) as workspace:
            status = workspace.usage.status()
            command = {
                "schema_version": "evidence-usage-command/v1",
                "state_id": status["state_id"],
                "workspace_binding": status["workspace_binding"],
                "expected_checkpoint": status["checkpoint"],
                "request_id": request_id,
                "action": action,
                "body": body,
            }
            role = "revocation" if action == "revoke" else "usage"
            envelope = _example()[2](command, "usage", role, self.keys)
            receipt = workspace.usage.transact(envelope)
        self.originals.append((request_id + "-actual-usage", {"envelope": envelope, "receipt": receipt}))
        return receipt

    def _time_case(self, case_id):
        application = self.applications[("baseline", "observed-capacity", "critic")]
        if case_id == "unused-source-permission-revoked":
            with self.session(application["run_id"]) as (workspace, _session):
                receipt = self.usage_action(
                    "revoke",
                    {
                        "source_id": self.sources["unused"]["source_id"],
                        "scope": "source",
                        "source_revision": None,
                        "reason": "owner-withdrawal",
                    },
                    "withdraw-unused",
                )
                refused = self.refused(case_id, workspace.strict.check, reasons={"source_revision_revoked"})
                self.refused(case_id + "-publication", workspace.strict.export, reasons={"source_revision_revoked"})
            return {
                "status": "passed_limited_scope",
                "assertions": [
                    "actual_signed_unused_source_revocation",
                    "actual_global_source_gate_refusal",
                    "failed_call_no_write",
                ],
                "evidence": [receipt, refused],
                "unavailable_branches": ["native_per_claim_rows_after_earlier_global_source_gate"],
            }
        with self.session(application["run_id"]) as (workspace, _session):
            config = owner("_strict_evidence").configuration(self.root)
            with owner("_evidence_usage").current_view(self.root, config) as view:
                last_observed = view.state.last_observed
                actual_now = view.now
            if case_id == "time-regression":
                target = last_observed - timedelta(seconds=1)
                expected = {"host_state_clock_regressed"}
            elif case_id == "review-max-age-expired":
                maximum = config["strict_evidence"]["max_review_age_seconds"]
                _require(maximum <= 600, "native_review_age_fixture_not_selected")
                target = actual_now + timedelta(seconds=maximum + 1)
                expected = None
            elif case_id == "unused-source-expired":
                expiry = self.sources["unused"]["descriptor"]["temporal"]["expires_at"]["value"]
                target = datetime.fromisoformat(expiry.replace("Z", "+00:00")) + timedelta(seconds=1)
                expected = {"strict_source_expired"}
            elif case_id == "source-effective-interval-ended":
                end = self.sources["shared"]["descriptor"]["temporal"]["effective"]["end"]["value"]
                target = datetime.fromisoformat(end.replace("Z", "+00:00")) + timedelta(seconds=1)
                expected = {"strict_source_outside_effective_interval"}
            else:
                raise AssertionError("native_temporal_dispatch_incomplete")
            self.originals.append(
                (
                    case_id + "-clock-observation",
                    {
                        "actual_opening_now": actual_now.isoformat(),
                        "last_committed_observation": last_observed.isoformat(),
                        "offline_fixture_now": target.isoformat(),
                        "signed_times_edited": False,
                    },
                )
            )
            with self.fixture_clock(target):
                if expected is not None:
                    refused = self.refused(case_id, workspace.strict.check, reasons=expected)
                    self.refused(case_id + "-publication", workspace.strict.export, reasons=expected)
                else:
                    opening = self.guard()
                    strict = workspace.strict.check()
                    _require(
                        not _accepted(strict) and _basis(strict) == _basis(self.baseline["strict"]),
                        "expired_reviews_reused_or_basis_changed",
                    )
                    _require(
                        all("strict_review_expired" in row["reasons"] for row in strict["claims"]),
                        "review_expiry_branch_not_exercised",
                    )
                    publication = workspace.strict.export()
                    _require(
                        publication["verdict"] != "ship" and self.guard() == opening,
                        "expired_review_read_wrote_or_published",
                    )
                    self.originals.extend([(case_id + "-strict", strict), (case_id + "-publication", publication)])
                    refused = None
        return {
            "status": "passed" if expected is None else "passed_limited_scope",
            "assertions": [
                "actual_native_fixture_clock_gate",
                "signed_time_facts_preserved",
                "failed_or_read_call_no_write",
            ],
            "evidence": [refused] if refused else [],
            "unavailable_branches": ["per_claim_result_after_earlier_temporal_refusal"] if expected is not None else [],
        }

    def _source_case(self, case_id):
        from evidence_wiki._review_dependency_inputs import current_inputs

        name = "unused" if case_id in {"unrelated-source-raw", "source-provenance"} else "shared"
        source = self.sources[name]
        files, descriptor = dict(source["files"]), copy.deepcopy(source["descriptor"])
        normalized = source["normalized_path"]
        raw_path = "raw/papers/" + name + ".md"
        if case_id.endswith("-raw"):
            path = self.root / raw_path
            path.write_bytes(path.read_bytes() + b"\nRetained original observation amended.\n")
            files["evidence/" + raw_path] = path.read_bytes()
        elif case_id == "shared-source-normalized":
            path = self.root / normalized
            path.write_bytes(path.read_bytes() + b"\nRetained normalized observation amended.\n")
            files[normalized] = path.read_bytes()
        elif case_id == "source-provenance":
            path = self.root / (raw_path + ".provenance.yml")
            provenance = json.loads(path.read_bytes())
            capture = provenance.get("host_capture", provenance)
            capture["origin_url"] = "https://example.org/revised-origin"
            path.write_bytes(canonical(provenance))
            files["evidence/" + raw_path + ".provenance.yml"] = path.read_bytes()
        else:
            descriptor["parents"] = [source["revision"]]
        files["source-record.json"] = canonical(descriptor)
        receipts = []
        application = self.applications[("baseline", "observed-capacity", "critic")]
        with self.session(application["run_id"]) as (workspace, _session):
            revision = _deposit(workspace, case_id, descriptor, files, self.keys, receipts)
            self.originals.append((case_id + "-native-source-deposit", receipts))
            config = owner("_strict_evidence").configuration(self.root)

            def qualify_actual_source():
                capture = owner("_evidence_revision").capture_workspace(self.root)
                with owner("_evidence_usage").current_view(self.root, config) as view:
                    owner("_publication_usage").qualify_capture(
                        capture.files, config, view, purpose="qa-export", consumer="evidence-wiki"
                    )
                    current_inputs().source_documents(self.root, config, capture, view)

            refused = self.refused(
                case_id,
                qualify_actual_source,
                reasons={
                    "usage_revision_missing_or_ambiguous",
                    "assessment_raw_revision_mismatch",
                    "host_capture_original_or_qualifications_invalid",
                },
                error_codes={"RESEARCH_REVIEW_DEPENDENCY_SOURCE_UNAVAILABLE"},
            )
        return {
            "status": "passed_limited_scope",
            "executed_scope": "actual_native_source_revision_or_capture_refusal",
            "assertions": [
                "actual_source_bytes_or_ancestry",
                "actual_signed_source_revision_deposit",
                "failed_call_no_write",
            ],
            "evidence": [{"previous_revision": source["revision"], "attempted_revision": revision}, refused],
            "unavailable_branches": ["per_claim_semantic_identity_comparison_after_invalid_or_ambiguous_source"],
            "isolation": "fresh_native_fixture_no_source_history_restore",
        }

    def _gate_case(self, case_id):
        if case_id == "live-computation-failure":
            return self._computation_case(case_id)
        if case_id == "live-coverage-failure":
            application = self.applications[("baseline", "observed-capacity", "critic")]
            with self.session(application["run_id"]) as (workspace, _session):
                prepared = workspace.mutations.prepare(
                    request_id="remove-demand-coverage",
                    operation="coverage.link",
                    arguments={"slug": "demand", "facet_id": "primary", "clear_accepted_source_ids": True},
                )
                linked = workspace.mutations.submit(prepared)
                _require(linked["status"] == "committed", "coverage_owner_change_not_committed")
                prepared = workspace.mutations.prepare(
                    request_id="evaluate-missing-coverage", operation="coverage.evaluate", arguments={"slug": "demand"}
                )
                evaluated = workspace.mutations.submit(prepared)
                _require(evaluated["status"] == "committed", "coverage_owner_evaluation_not_committed")
            self.originals.append((case_id + "-actual-coverage-actions", {"link": linked, "evaluation": evaluated}))
            observed = self.observe(case_id)
            _require(not _accepted(observed["strict"]), "failed_coverage_reused_old_approvals")
            target = next(row for row in observed["strict"]["claims"] if row["claim"]["id"] == "independent-note")
            _require("strict_coverage_required" in target["reasons"], "actual_coverage_gate_not_exercised")
            return {
                "status": "passed",
                "assertions": [
                    "actual_native_coverage_link_and_evaluation",
                    "actual_mechanical_coverage_failure",
                    "global_material_change_distinct_from_logical_live_override",
                    "read_only_closing_custody",
                ],
                "evidence": [],
            }
        if case_id == "live-grounding-failure":
            path = self.root / self.sources["shared"]["normalized_path"]
            original, opening = path.read_bytes(), self.guard()
            _require(b"Capacity: 6 units." in original, "native_grounding_fixture_quote_missing")
            application = self.applications[("baseline", "observed-capacity", "critic")]
            try:
                with self.session(application["run_id"]) as (workspace, _session):
                    path.write_bytes(original.replace(b"Capacity: 6 units.", b"Capacity statement unavailable."))
                    refused = self.refused(
                        case_id,
                        workspace.strict.check,
                        reasons={"usage_revision_missing_or_ambiguous", "computation_normalized_contract_invalid"},
                    )
                return {
                    "status": "earlier_owner_refusal",
                    "assertions": [
                        "actual_retained_quote_removed_old_evidence_unchanged",
                        "native_source_or_computation_integrity_refusal",
                        "failed_call_no_write",
                    ],
                    "evidence": [refused],
                    "earlier_owner_refusal": refused["reason"],
                    "unavailable_branches": ["later_quote_specific_grounding_check"],
                }
            finally:
                path.write_bytes(original)
                _require(self.guard() == opening, "grounding_input_restore_changed_custody")
        if case_id == "export-eligibility-failure":
            application = self.applications[("baseline", "observed-capacity", "critic")]
            with self.session(application["run_id"]) as (workspace, _session):
                config = owner("_strict_evidence").configuration(self.root)
                with owner("_evidence_usage").current_view(self.root, config) as view:
                    revision = self.sources["unused"]["revision"]
                    record = view.state.revisions[revision]
                    grant, scrub = copy.deepcopy(record["grant"]["payload"]), copy.deepcopy(record["scrub"])
                grant["permissions"]["export"] = False
                receipt = self.usage_action(
                    "authorize",
                    {
                        "source_id": self.sources["unused"]["source_id"],
                        "source_revision": revision,
                        "grant": _example()[2](grant, "usage", "usage", self.keys),
                        "scrub": scrub,
                    },
                    "deny-current-export",
                )
                refused = self.refused(case_id, workspace.export_answers, reasons={"usage_permission_denied"})
            return {
                "status": "passed_limited_scope",
                "assertions": [
                    "actual_signed_export_permission_change",
                    "actual_native_export_denial",
                    "failed_call_no_write",
                ],
                "evidence": [receipt, refused],
                "unavailable_branches": ["per_claim_result_after_global_export_permission_gate"],
            }
        if case_id == "delivery-eligibility-failure":
            from evidence_wiki import HostRuntime
            from evidence_wiki.artifact_delivery_session import deliver

            grant = "artifact_delivery_operations.deliver"
            _require(grant in self.runtime.context.grants, "delivery_capability_was_not_provisioned")
            context = replace(self.runtime.context, grants=self.runtime.context.grants - {grant})
            self.runtime = HostRuntime.from_services(self.runtime.policy, context, self.runtime.execution)
            publication = self.baseline["publication"]
            data = canonical(publication)
            identity = owner("_evidence_revision").content_id(publication["schema_version"], publication)
            proposal = {
                "schema_version": "research-artifact-delivery-proposal/v1",
                "artifact_id": identity,
                "artifact_sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                "artifact_bytes": len(data),
                "destination_ref": "private-fixture",
                "plan_sha256": "sha256:" + "0" * 64,
            }
            with self.session("delivery-permission-negative") as (_workspace, session):
                refused = self.refused(
                    case_id,
                    lambda: deliver(session, request_id="denied-delivery", proposal=proposal),
                    reasons={"host_operation_not_granted"},
                )
            return {
                "status": "passed_limited_scope",
                "assertions": [
                    "actual_provisioned_delivery_capability_withdrawal",
                    "actual_native_delivery_adapter_permission_denial",
                    "failed_call_no_write",
                ],
                "evidence": [refused],
                "unavailable_branches": ["configured_delivery_plan_and_destination_after_capability_guard"],
            }
        if case_id == "release-eligibility-failure":
            path = self.root / "wiki/questions/capacity.md"
            original, opening = path.read_bytes(), self.guard()
            parts = original.decode().split("---", 2)
            front = yaml.safe_load(parts[1])
            front["status"] = "open"
            application = self.applications[("baseline", "observed-capacity", "critic")]
            try:
                with self.session(application["run_id"]) as (workspace, _session):
                    path.write_text("---\n" + yaml.safe_dump(front, sort_keys=False) + "---" + parts[2])
                    before = self.guard()
                    strict = workspace.strict.check()
                    publication = workspace.publish_selected(["capacity", "demand"])
                    _require(
                        _accepted(strict) == CLAIM_IDS and _basis(strict) == _basis(self.baseline["strict"]),
                        "release_coordinate_changed_semantic_basis",
                    )
                    _require(publication["verdict"] != "ship", "unresolved_required_question_released")
                    _require(self.guard() == before, "denied_release_added_output_or_event")
                    self.originals.extend([(case_id + "-strict", strict), (case_id + "-publication", publication)])
                return {
                    "status": "passed",
                    "assertions": [
                        "actual_unresolved_required_question",
                        "core_claims_distinct_from_publication",
                        "native_full_release_blocked",
                        "read_only_closing_custody",
                    ],
                    "evidence": [],
                }
            finally:
                path.write_bytes(original)
                _require(self.guard() == opening, "release_input_restore_changed_custody")
        raise AssertionError("native_live_gate_dispatch_incomplete")

    def _computation_case(self, case_id):
        from evidence_wiki.computation import evaluate

        config_path = self.root / "research.yml"
        config = yaml.safe_load(config_path.read_bytes())
        literal = None
        if case_id == "computation-definition":
            original = config_path.read_bytes()
            malformed = copy.deepcopy(config)
            malformed["computation"]["arithmetic"] = "exact-rational"
            try:
                config_path.write_text(yaml.safe_dump(malformed, sort_keys=False))
                with self.runtime.context.activate():
                    literal = self.refused(
                        case_id + "-literal",
                        lambda: evaluate(self.root),
                        reasons={"computation_arithmetic_invalid", "computation_schema_invalid"},
                    )
            finally:
                config_path.write_bytes(original)
            config["computation"]["arithmetic"]["precision"] = 63
            config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        elif case_id == "computation-clock":
            actual = datetime.fromisoformat(config["computation"]["clock"]["as_of"].replace("Z", "+00:00"))
            config["computation"]["clock"]["as_of"] = (actual + timedelta(minutes=1)).isoformat()
            config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        else:
            source = self.sources["demand"]
            normalized = self.root / source["normalized_path"]
            sidecar = normalized.with_suffix(".structured.json")
            records = json.loads(sidecar.read_bytes())
            _require(records["records"][0]["amount"] == "4", "native_demand_operand_changed_before_case")
            records["records"][0]["amount"] = "3"
            sidecar.write_bytes(canonical(records))
            pieces = normalized.read_text().split("---", 2)
            metadata = yaml.safe_load(pieces[1])
            metadata["structured_view"]["content_hash"] = owner("_structured_view").content_hash(sidecar.read_bytes())
            normalized.write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---" + pieces[2])
            files = dict(source["files"])
            files[source["normalized_path"]] = normalized.read_bytes()
            files[sidecar.relative_to(self.root).as_posix()] = sidecar.read_bytes()
            receipts = []
            with self.runtime.context.activate(), Workspace.open(self.root) as workspace:
                revision = _deposit(workspace, case_id, source["descriptor"], files, self.keys, receipts)
            self.originals.append(
                (
                    case_id + "-actual-input-deposit",
                    {
                        "receipts": receipts,
                        "revision": revision,
                        "source_id": source["source_id"],
                        "old_claim_references_repaired": False,
                    },
                )
            )
        with self.runtime.context.activate():
            computed = evaluate(self.root)
        self.originals.append((case_id + "-actual-computation", computed))
        old_reference = next(row for row in self.manifest["claims"] if row["id"] == "margin")["calculations"][0][
            "result_id"
        ]
        _require(computed["result_id"] != old_reference, "native_computation_result_identity_unchanged")
        if case_id in {"computation-output", "computation-lineage", "live-computation-failure"}:
            _require(
                computed["graphs"]["capacity"]["outputs"]["margin"]["value"] == "3",
                "actual_native_demand_change_not_computed",
            )
        observed = self.observe(case_id, run_id="computation-" + digest(case_id)[:24])
        _require(not _accepted(observed["strict"]), "computation_change_reused_approval")
        margin = next(row for row in observed["strict"]["claims"] if row["claim"]["id"] == "margin")
        _require("strict_computation_missing_or_stale" in margin["reasons"], "actual_native_calculation_gate_missing")
        if self.mode == "selective_reuse":
            _require(
                all(_basis(observed["strict"])[claim] != _basis(self.baseline["strict"])[claim] for claim in CLAIM_IDS),
                "computation_not_globally_bound",
            )
        return {
            "status": "passed_limited_scope" if literal else "passed",
            "assertions": [
                "actual_native_computation_input_or_definition_or_clock",
                "actual_result_identity_changed",
                "old_calculation_reference_not_repaired",
                "actual_native_stale_calculation_failure",
                "read_only_closing_custody",
            ],
            "evidence": [literal] if literal else [],
            "unavailable_branches": ["supported_exact_rational_arithmetic_transition"] if literal else [],
        }

    def _requirements_case(self, case_id):
        from evidence_wiki import requirements_projection as projection

        base = json.loads((self.root / "docs/research-requirements.json").read_bytes())
        _require(base["schema_version"] == projection.BASE, "native_base_requirements_fixture_changed")
        original = base["request"]["payload"]["questions"][0]["id"]
        addition = {
            "id": "archive",
            "text": "What archive note was retained?",
            "original_ids": [original],
            "facet_refs": [{"original_id": original, "facet_id": "primary"}],
        }
        criteria = copy.deepcopy(base)
        if case_id == "full-requirements":
            criteria["decisions"]["question_plans"][0]["criteria"][0]["stopping"] = (
                "Every retained original requires evidence for the exact capacity scope."
            )
        proposal = {
            "schema_version": projection.PROPOSAL,
            "request_id": "native-requirements-candidate",
            "parent_sha256": projection.digest(base),
            "additions": [addition],
        }
        envelope = {
            "schema_version": projection.ENVELOPE,
            "policy_ref": "fixture-requirements-allowance",
            "base_sha256": projection.digest(base),
            "criteria_sha256": projection.criteria_identity(criteria),
            "allowed_original_ids": [original],
            "allowed_questions": [{key: addition[key] for key in ("text", "original_ids", "facet_refs")}],
            "max_additions": 1,
            "max_total_questions": 3,
        }
        self.raw(
            case_id + "-actual-candidate",
            canonical({"requirements": criteria, "proposal": proposal, "envelope": envelope}),
        )
        application = self.applications[("baseline", "observed-capacity", "critic")]
        with self.session(application["run_id"]) as (workspace, _session):
            refused = self.refused(
                case_id,
                lambda: workspace.research.requirements.prepare_revision(proposal, envelope),
                reasons={"invalid_revision"} if case_id == "full-requirements" else {"question_allowance_exhausted"},
            )
        observed = self.observe(case_id + "-baseline-preserved")
        _require(_accepted(observed["strict"]) == CLAIM_IDS, "rejected_requirements_candidate_invalidated_baseline")
        return {
            "status": "passed_limited_scope",
            "executed_scope": "actual_native_requirements_owner_rejection",
            "assertions": [
                "actual_native_revision_candidate",
                "native_frozen_criteria_or_question_budget_guard",
                "independent_valid_baseline_preserved",
                "failed_call_no_write",
            ],
            "evidence": [refused],
            "unavailable_branches": [
                "same_root_replacement_of_frozen_original_criteria"
                if case_id == "full-requirements"
                else "third_question_admitted_under_existing_two_question_allowance"
            ],
            "set_comparison": "not_asserted_for_rejected_ingress",
        }

    def _claim_membership_case(self, case_id):
        inventory = copy.deepcopy(self.manifest)
        if case_id == "claim-addition":
            claim = copy.deepcopy(next(row for row in inventory["claims"] if row["id"] == "independent-note"))
            claim["id"] = "additional-note"
            inventory["claims"].append(claim)
            current_ids = CLAIM_IDS | {"additional-note"}
        else:
            inventory["claims"] = [row for row in inventory["claims"] if row["id"] != "independent-note"]
            current_ids = CLAIM_IDS - {"independent-note"}
        proposal = {
            "schema_version": "evidence-draft-proposal/v1",
            "originals": [
                {"original_id": row["original_id"], "text": row["question"], "question_slugs": [row["slug"]]}
                for row in inventory["questions"]
            ],
            "inventory": inventory,
        }
        with self.runtime.context.activate(), Workspace.open(self.root) as workspace:
            prepared = workspace.mutations.prepare(
                request_id=case_id, operation="strict.admit", arguments={"proposal": proposal}
            )
            receipt = workspace.mutations.submit(prepared)
        self.originals.append((case_id + "-native-admission", {"prepared": prepared, "receipt": receipt}))
        _require(receipt["status"] == "committed", "native_membership_admission_not_committed")
        observed = self.observe(case_id)
        strict = observed["strict"]
        _require(
            {row["claim"]["id"] for row in strict["claims"]} == current_ids and not _accepted(strict),
            "native_membership_acceptance_changed",
        )
        if self.mode == "selective_reuse":
            old, new = _basis(self.baseline["strict"]), _basis(strict)
            _require(
                all(new[claim] is not None and new[claim] != old[claim] for claim in CLAIM_IDS & current_ids),
                "membership_not_globally_bound",
            )
        return {
            "status": "passed",
            "assertions": [
                "actual_native_claim_membership_admission",
                "exact_current_membership",
                "global_membership_invalidation",
                "deleted_claim_not_current" if case_id.endswith("deletion") else "new_claim_has_no_inherited_approval",
                "read_only_closing_custody",
            ],
            "evidence": [receipt],
            "material_invalidated": sorted(CLAIM_IDS),
        }

    def _transport_case(self, case_id):
        from evidence_wiki.review_context import ReviewContext, dependency_material

        _require(self.mode == "selective_reuse", "native_selective_transport_fixture_required")
        observed = []
        for cycle, page_chars in (("transport-first", 1024), ("transport-second", 2048)):
            self.drive({"observed-capacity"}, cycle=cycle, configs=("critic",), page_chars=page_chars)
            application = self.applications[(cycle, "observed-capacity", "critic")]
            attempt = application["attempt"]
            context = ReviewContext.restore(attempt.input_document["packet"], application["review_context"])
            material = dependency_material(context)
            delivery = attempt.input_document["complete_review_delivery"]
            observed.append(
                {
                    "context_id": context.document()["context_id"],
                    "material": material,
                    "basis_id": application["envelope"]["payload"]["body"]["review"]["basis_id"],
                    "packet_sha256": delivery["packet_sha256"],
                    "worker_input_sha256": attempt.input_sha256,
                    "required_page_count": len(delivery["packet"]["header"]["pages"]),
                    "page_chars": page_chars,
                }
            )
        first, second = observed
        _require(canonical(first["material"]) == canonical(second["material"]), "validated_transport_changed_semantics")
        _require(
            first["basis_id"] == second["basis_id"]
            and first["context_id"] != second["context_id"]
            and first["packet_sha256"] != second["packet_sha256"]
            and first["worker_input_sha256"] != second["worker_input_sha256"],
            "actual_transport_identities_not_distinct",
        )
        current = self.observe(case_id)
        _require(
            _accepted(current["strict"]) == CLAIM_IDS and _basis(current["strict"]) == _basis(self.baseline["strict"]),
            "transport_only_change_invalidated_claims",
        )
        self.originals.append((case_id + "-actual-deliveries", observed))
        return {
            "status": "passed",
            "assertions": [
                "two_actual_complete_worker_applications",
                "native_reversible_semantic_bytes_equal",
                "claim_basis_unchanged",
                "actual_context_packet_input_and_page_changes",
                "read_only_closing_custody",
            ],
            "evidence": [{key: value for key, value in row.items() if key != "material"} for row in observed],
        }

    def _layout_case(self, case_id):
        application = self.applications[("baseline", "observed-capacity", "critic")]
        paths = (
            [self.root / "sources/coverage/demand.yml"]
            if case_id == "unrelated-question-coverage"
            else [self.root / "docs/research-claims.json", self.root / "wiki/questions/demand.md"]
        )
        originals, opening = {path: path.read_bytes() for path in paths}, self.guard()
        try:
            with self.session(application["run_id"]) as (workspace, _session):
                if case_id == "unrelated-question-coverage":
                    coverage = yaml.safe_load(originals[paths[0]])
                    coverage["schema_version"] = "unsupported"
                    paths[0].write_text(yaml.safe_dump(coverage, sort_keys=False))
                    native = owner("coverage_manifest")
                    config = owner("_strict_evidence").configuration(self.root)
                    refused = self.refused(
                        case_id,
                        lambda: native.validate_manifest(
                            native.load_yaml_mapping(paths[0], error_code="COVERAGE_MANIFEST_INVALID"),
                            expected_slug="demand",
                            policy_vocabularies=native.merged_policy_vocabularies(config),
                        ),
                        error_code="COVERAGE_MANIFEST_INVALID",
                    )
                else:
                    from evidence_wiki.coverage_observation import observe

                    inventory = json.loads(originals[paths[0]])
                    question = "What retained demand and service note are available?"
                    next(row for row in inventory["questions"] if row["slug"] == "demand")["question"] = question
                    paths[0].write_bytes(canonical(inventory))
                    parts = originals[paths[1]].decode().split("---", 2)
                    front = yaml.safe_load(parts[1])
                    front["question"] = question
                    context = copy.deepcopy(self.fixture["question_context"]["demand"])
                    context["question"] = question
                    paths[1].write_text(
                        "---\n"
                        + yaml.safe_dump(front, sort_keys=False)
                        + "---\n"
                        + render_question_body(context, question=question)
                    )
                    refused = self.refused(
                        case_id,
                        lambda: observe(self.root, question_slug="demand"),
                        error_codes={"MUTATION_REQUEST_QUESTION_BINDING_CHANGED"},
                    )
                before = self.guard()
                strict = workspace.strict.check()
                _require(not _accepted(strict) and self.guard() == before, "invalid_input_wrote_or_reused_approval")
                self.originals.append((case_id + "-actual-strict", strict))
            return {
                "status": "passed_limited_scope",
                "assertions": [
                    "actual_native_question_or_coverage_input_change",
                    "actual_frozen_binding_or_schema_refusal",
                    "no_core_claim_accepted",
                    "failed_or_read_call_no_write",
                ],
                "evidence": [refused],
                "unavailable_branches": [
                    "question_local_set_comparison_after_frozen_original_binding_change"
                    if case_id == "question-record-text"
                    else "per_claim_basis_after_unsupported_coverage_schema"
                ],
            }
        finally:
            for path, raw in originals.items():
                path.write_bytes(raw)
            _require(self.guard() == opening, "layout_input_restore_changed_custody")

    def _ingress_case(self, case_id):
        envelope = self.candidate(case_id)
        review = envelope["payload"]["body"]["review"]
        contract = owner("_review_dependency_contract")
        if case_id in {
            "review-signature-failure",
            "authorship-signature-failure",
            "forged-selective-signature",
            "untrusted-user-timestamp-cannot-win",
        }:
            if case_id == "authorship-signature-failure":
                auth = review["generator"]["authentication"]
            else:
                auth = envelope["authentication"]
            signature = auth["signature"]
            auth["signature"] = ("1" if signature[0] == "0" else "0") + signature[1:]
            if case_id == "authorship-signature-failure":
                signer = self.runtime.execution.review_signer
                identity = next(row for row in signer.reviewers if row.config_ref == "critic")
                envelope = signer._sign(envelope["payload"], identity, datetime.now(timezone.utc).isoformat())
            if case_id == "untrusted-user-timestamp-cannot-win":
                review["reviewed_at"] = "2099-01-01T00:00:00Z"
            self.raw(case_id + "-candidate", canonical(envelope))
            application = self.applications[("baseline", "observed-capacity", "critic")]
            with self.session(application["run_id"]) as (_workspace, _session):
                config = owner("_strict_evidence").configuration(self.root)
                observed = self.refused(
                    case_id,
                    lambda: owner("_evidence_usage").transact(self.root, config, envelope),
                    reasons={"authentication_signature_mismatch", "strict_review_authentication_failed"},
                )
            return {
                "status": "passed",
                "assertions": ["actual_signed_candidate_tamper_refused", "failed_call_no_write"],
                "evidence": [observed],
            }
        if case_id == "missing-complete-packet":
            from evidence_wiki.review_packet_execution import context_parameters
            from evidence_wiki.review_page_operations import _current_packet

            application = self.applications[("baseline", "observed-capacity", "critic")]
            with self.session(application["run_id"]) as (_workspace, session):
                request = "negative-complete-packet"
                actual = session.assemble_review_packet(
                    request_id=request, claim_id="observed-capacity", task_role="critic", page_chars=16384
                )
                packet = copy.deepcopy(actual["packet"])
                parameters = context_parameters(session, request)
                arguments = {
                    "claim_id": "observed-capacity",
                    "context_id": parameters["context_id"],
                    "task_role": "critic",
                    "page_chars": 16384,
                    "source_ids": sorted(row["source_id"] for row in self.sources.values()),
                    "_order_seed": parameters["order_seed"],
                    "context_version": self.runtime.execution.profile.review_delivery.context_version,
                }
                with _current_packet(self.root, packet=packet, **arguments):
                    pass
                packet["pages"].pop()
                self.raw(case_id + "-missing-pages", canonical(packet))

                def consume():
                    with _current_packet(self.root, packet=packet, **arguments):
                        pass

                observed = self.refused(case_id, consume, reasons={"decisive_context_incomplete_or_stale"})
            return {
                "status": "passed",
                "assertions": [
                    "actual_current_complete_packet_positive_control",
                    "actual_missing_page_refusal",
                    "failed_call_no_write",
                ],
                "evidence": [observed],
            }
        _require(self.mode == "selective_reuse", "native_selective_wire_fixture_required")
        if case_id == "transport-binding-forged":
            review["delivery"]["binding_sha256"] = "sha256:" + "0" * 64
            signer = self.runtime.execution.review_signer
            identity = next(row for row in signer.reviewers if row.config_ref == "critic")
            envelope = signer._sign(envelope["payload"], identity, datetime.now(timezone.utc).isoformat())
            self.raw(case_id + "-candidate", canonical(envelope))
            application = self.applications[("baseline", "observed-capacity", "critic")]
            with self.session(application["run_id"]) as (_workspace, _session):
                config = owner("_strict_evidence").configuration(self.root)
                observed = self.refused(
                    case_id,
                    lambda: owner("_evidence_usage").transact(self.root, config, envelope),
                    reasons={"selective_original_custody_unavailable", "selective_original_custody_invalid"},
                )
            return {
                "status": "passed_limited_scope",
                "assertions": [
                    "actual_valid_outer_signature_over_forged_delivery_binding",
                    "actual_native_original_custody_refusal",
                    "failed_call_no_write",
                ],
                "evidence": [observed],
                "unavailable_branches": ["fresh_worker_application_for_forged_delivery_binding"],
            }
        if case_id == "duplicate-json-key":
            wire = b'{"schema_version":"evidence-strict-review/v3",' + canonical(review).lstrip()[1:]
            operation = lambda: contract.loads(wire)
            reasons = {"review_dependency_json_invalid"}
        elif case_id == "nonfinite-json-number":
            review["rationale"] = float("nan")
            wire = json.dumps(review, allow_nan=True).encode()
            operation = lambda: contract.loads(wire)
            reasons = {"review_dependency_json_invalid"}
        else:
            if case_id == "unknown-field":
                review["unexpected"] = True
            elif case_id == "unsupported-basis-schema":
                review["dependency_basis"]["schema_version"] = "evidence-review-dependency-basis/v9"
            elif case_id == "forged-basis-binding":
                review["basis_id"] = "sha256:" + "0" * 64
            elif case_id == "incomplete-manifest":
                review["dependency_basis"]["dependencies"].pop()
            elif case_id == "duplicate-coordinate":
                basis = review["dependency_basis"]
                basis["dependencies"].append(copy.deepcopy(basis["dependencies"][0]))
                basis["dependencies"].sort(key=lambda row: (row["scope"], row["kind"], row["coordinate"]))
                basis["basis_id"] = owner("_evidence_revision").content_id(
                    contract.BASIS, {key: value for key, value in basis.items() if key != "basis_id"}
                )
            else:
                raise AssertionError("native_wire_dispatch_incomplete")
            wire = canonical(review)
            operation = lambda: owner("_strict_contract").review_document(contract.loads(wire))
            reasons = {
                "review_dependency_contract_invalid",
                "review_dependency_basis_id_invalid",
                "review_dependency_coordinates_invalid",
                "review_dependency_binding_invalid",
                "review_dependency_material_basis_invalid",
                "review_dependency_material_id_invalid",
            }
        pin = self.raw(case_id + "-candidate", wire)
        observed = self.refused(case_id, operation, reasons=reasons)
        return {
            "status": "passed",
            "assertions": ["actual_raw_native_parser_refusal", "failed_call_no_write"],
            "evidence": [pin, observed],
        }

    def _graph_case(self, case_id):
        manifest = copy.deepcopy(self.manifest)
        if case_id == "question-deletion":
            manifest["questions"] = [row for row in manifest["questions"] if row["slug"] != "demand"]
            reasons = {"strict_claim_scope_invalid"}
        elif case_id == "premise-cycle":
            claim = next(row for row in manifest["claims"] if row["id"] == "observed-capacity")
            claim.update(qualification="inference", premises=["margin"], derivation="Use the retained premise.")
            reasons = {"strict_premise_missing_or_cyclic"}
        elif case_id == "missing-premise-target":
            next(row for row in manifest["claims"] if row["id"] == "effective-capacity")["premises"] = ["absent"]
            reasons = {"strict_premise_missing_or_cyclic"}
        else:
            manifest["claims"].append(copy.deepcopy(manifest["claims"][0]))
            reasons = {"strict_duplicate_claim"}
        wire = canonical(manifest)
        pin = self.raw(case_id + "-claims", wire)
        refusal = self.refused(
            case_id,
            lambda: owner("_strict_contract").claims_document(owner("_record_artifacts").json_document(wire)),
            reasons=reasons,
        )
        path = self.root / "docs/research-claims.json"
        original, opening = path.read_bytes(), self.guard()
        try:
            application = self.applications[("baseline", "observed-capacity", "critic")]
            with self.session(application["run_id"]) as (workspace, _session):
                path.write_bytes(wire)
                read_refusal = self.refused(case_id + "-read", workspace.strict.check, reasons=reasons)
                publication_refusal = self.refused(case_id + "-publication", workspace.strict.export, reasons=reasons)
        finally:
            path.write_bytes(original)
            _require(self.guard() == opening, "invalid_input_restore_changed_custody")
        return {
            "status": "passed",
            "assertions": [
                "actual_native_claims_parser_refusal",
                "actual_strict_and_publication_refusal",
                "failed_call_no_write",
            ],
            "evidence": [pin, refusal, read_refusal, publication_refusal],
        }

    def _bound_case(self, case_id):
        if case_id == "dependency-entry-bound":
            paths = []
            for index in range(4097):
                path = self.root / "notes/bound" / ("entry-" + str(index).zfill(4) + ".txt")
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    stream.write(("Independent retained input " + str(index) + ".\n").encode())
                paths.append(path.relative_to(self.root).as_posix())
            self.originals.append(
                (
                    case_id + "-physical-inputs",
                    {
                        "count": len(paths),
                        "files": {path: hashlib.sha256((self.root / path).read_bytes()).hexdigest() for path in paths},
                    },
                )
            )
            application = self.applications[("baseline", "observed-capacity", "critic")]
            with self.session(application["run_id"]) as (workspace, _session):
                refusal = self.refused(
                    case_id,
                    lambda: workspace.strict.prepare_review("observed-capacity"),
                    reasons={
                        "review_dependency_collection_bound",
                        "review_dependency_tree_bound",
                        "review_dependency_bytes_bound",
                        "review_dependency_contract_invalid",
                    },
                )
            exact = refusal["reason"] == "review_dependency_collection_bound"
            return {
                "status": "passed" if exact else "earlier_owner_refusal",
                "assertions": [
                    "actual_4097_distinct_workspace_inputs",
                    "actual_native_projection_refusal",
                    "failed_call_no_write",
                ],
                "evidence": [refusal],
                "actual_added_input_count": len(paths),
                "earlier_owner_refusal": None if exact else refusal["reason"],
                "unavailable_branches": [] if exact else ["dependency_collection_guard_after_earlier_native_bound"],
            }
        if case_id == "dependency-basis-byte-bound":
            return self._basis_bytes_case(case_id)
        if case_id == "complete-packet-byte-bound":
            return self._packet_bytes_case(case_id)
        if case_id == "stricter-host-packet-bound":
            from evidence_wiki import ReviewDelivery

            refusal = self.refused(
                case_id,
                lambda: ReviewDelivery(max_packet_bytes=4095),
                error_code="REVIEW_DELIVERY_INVALID",
                field="max_packet_bytes",
            )
            return {
                "status": "earlier_owner_refusal",
                "assertions": ["actual_configuration_refusal", "failed_call_no_write"],
                "evidence": [refusal],
                "earlier_owner_refusal": "invalid_host_packet_configuration",
                "unavailable_branches": ["packet_bytes_over_successfully_configured_4095_limit"],
            }
        if case_id == "worker-input-byte-bound":
            attempt = self.applications[("baseline", "observed-capacity", "critic")]["attempt"]
            original = attempt._input_bytes
            wire = original + b" " * max(1, 524289 - len(original))
            pin = self.raw(case_id + "-wire", wire)
            refusal = self.refused(
                case_id,
                lambda: replace(attempt, _input_bytes=wire),
                reasons={"execution_attempt_invalid", "execution_document_limit"},
            )
            return {
                "status": "earlier_owner_refusal",
                "assertions": ["actual_noncanonical_input_refusal", "failed_call_no_write"],
                "evidence": [pin, refusal],
                "actual_wire_bytes": len(wire),
                "earlier_owner_refusal": refusal["reason"],
                "unavailable_branches": ["canonical_worker_input_byte_limit"],
            }
        if case_id == "claim-inventory-bound":
            manifest = copy.deepcopy(self.manifest)
            template = next(row for row in manifest["claims"] if row["id"] == "independent-note")
            manifest["claims"].extend(
                dict(copy.deepcopy(template), id="additional-note-" + str(index)) for index in range(95)
            )
            pin = self.raw(case_id + "-claims", canonical(manifest))
            refusal = self.refused(
                case_id, lambda: owner("_strict_contract").claims_document(manifest), reasons={"invalid_list_bound"}
            )
            return {
                "status": "passed",
                "assertions": ["actual_101_claims_native_parser_refusal", "failed_call_no_write"],
                "evidence": [pin, refusal],
                "actual_claim_count": len(manifest["claims"]),
            }
        if case_id == "transitive-premise-bound":
            template = next(row for row in self.manifest["claims"] if row["id"] == "independent-note")

            def chain(premises):
                manifest = copy.deepcopy(self.manifest)
                manifest["claims"] = [row for row in manifest["claims"] if row["id"] != "independent-note"]
                for index in range(premises):
                    row = dict(copy.deepcopy(template), id="chain-node-" + str(index))
                    if index:
                        row.update(
                            qualification="inference",
                            premises=["chain-node-" + str(index - 1)],
                            derivation="Use retained premise.",
                        )
                    manifest["claims"].append(row)
                manifest["claims"].append(
                    dict(
                        copy.deepcopy(template),
                        qualification="inference",
                        premises=["chain-node-" + str(premises - 1)],
                        derivation="Use retained premise.",
                    )
                )
                return manifest

            oversized, supported, first_refused = chain(33), chain(31), chain(32)
            pin = self.raw(case_id + "-claims", canonical(oversized))
            refusal = self.refused(
                case_id,
                lambda: owner("_review_dependencies").closure(oversized, "independent-note"),
                reasons={"selective_claim_closure_bound"},
            )
            selected = owner("_review_dependencies").closure(supported, "independent-note")
            _require(len(selected) == 32, "native_supported_closure_changed")
            companion = self.refused(
                case_id + "-boundary",
                lambda: owner("_review_dependencies").closure(first_refused, "independent-note"),
                reasons={"selective_claim_closure_bound"},
            )
            return {
                "status": "passed",
                "assertions": [
                    "actual_frozen_oversized_closure_refusal",
                    "actual_32_total_supported_33_total_refused",
                    "failed_call_no_write",
                ],
                "evidence": [pin, refusal, companion],
                "actual_frozen_closure_count": 34,
            }
        raise AssertionError("native_bound_dispatch_incomplete")

    def _basis_bytes_case(self, case_id):
        from evidence_wiki._pack_io import canonical as native_canonical

        contract = owner("_review_dependency_contract")
        residual = self.root / "notes/context.txt"
        original = residual.read_bytes()
        application = self.applications[("baseline", "observed-capacity", "critic")]
        with self.session(application["run_id"]) as (workspace, _session):
            opening = workspace.strict.prepare_review("observed-capacity")
            try:
                residual.write_bytes(b"x" * 1048577)
                expanded = workspace.strict.prepare_review("observed-capacity")
            finally:
                residual.write_bytes(original)
        opening_basis, expanded_basis = opening["dependency_basis"], expanded["dependency_basis"]
        old_size, new_size = len(native_canonical(opening_basis)), len(native_canonical(expanded_basis))
        _require(new_size <= 1048576 and old_size == new_size, "residual_content_unexpectedly_expanded_basis")
        _require(opening_basis["basis_id"] != expanded_basis["basis_id"], "residual_content_digest_not_bound")
        self.originals.append(
            (
                case_id + "-original-recipe",
                {
                    "actual_residual_bytes": 1048577,
                    "native_opening_basis": opening_basis,
                    "native_expanded_basis": expanded_basis,
                    "opening_basis_bytes": old_size,
                    "expanded_basis_bytes": new_size,
                },
            )
        )
        candidate = copy.deepcopy(opening_basis)
        paths = []
        created_directories = []
        opening_custody = self.guard()
        history = {
            path: path.read_bytes()
            for directory in (self.root, Path(self.runtime.context.state_directory))
            for path in directory.rglob("*.jsonl")
            if path.is_file()
        }
        bound_reasons = {
            "review_dependency_bytes_bound",
            "review_dependency_collection_bound",
            "review_dependency_tree_bound",
            "review_dependency_contract_invalid",
            "selective_material_bound",
            "strict_document_bound_exceeded",
            "strict_output_bound_exceeded",
            "strict_review_material_bound",
        }
        stage = None
        try:
            parent = self.root / "notes"
            for component in ("a" * 150, "b" * 150, "c" * 150):
                parent = parent / component
                parent.mkdir(mode=0o700)
                created_directories.append(parent)
            opening_physical_files = sum(path.is_file() for path in self.root.rglob("*"))
            _require(opening_physical_files + 2048 < 4096, "native_physical_companion_file_budget")
            for index in range(2048):
                path = parent / ("entry-" + str(index).zfill(4) + ".txt")
                coordinate = path.relative_to(self.root).as_posix()
                _require(
                    len(coordinate.encode()) <= 512 and len(str(path).encode()) < 1024,
                    "native_physical_companion_path_budget",
                )
                with path.open("xb") as stream:
                    stream.write(("Retained bounded input " + str(index) + ".\n").encode())
                paths.append(coordinate)
                value = {"path": coordinate, "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
                candidate["dependencies"].append(
                    {
                        "scope": "global",
                        "kind": "workspace_material",
                        "coordinate": coordinate,
                        "content_id": contract.item_id("workspace_material", coordinate, value),
                    }
                )
            self.originals.append(
                (
                    case_id + "-physical-companion-inputs",
                    {
                        "actual_added_file_count": len(paths),
                        "actual_total_workspace_file_count": sum(path.is_file() for path in self.root.rglob("*")),
                        "files": {path: hashlib.sha256((self.root / path).read_bytes()).hexdigest() for path in paths},
                    },
                )
            )
            with self.session(application["run_id"]) as (workspace, _session):
                strict_owner = owner("_strict_evidence")
                config = strict_owner.configuration(self.root)
                policy = strict_owner.resolve_policy(self.root, config)

                def project_current():
                    nonlocal stage
                    with owner("_evidence_usage").current_view(self.root, config) as view:
                        stage = "strict_evidence.selection_inputs"
                        capture, manifest, _full_basis = strict_owner.selection_inputs(
                            self.root, config, policy, view=view
                        )
                        stage = "strict_evidence.dependency_current"
                        return strict_owner.dependency_current(
                            self.root, config, policy, view, capture, manifest, "observed-capacity"
                        )

                native_refusal = self.refused(
                    case_id + "-physical-native-current",
                    project_current,
                    reasons=bound_reasons,
                    error_codes={"EVIDENCE_REVISION_LIMIT"},
                )
                native_stage = stage
                before_check = self.guard()
                try:
                    actual_check = workspace.strict.check()
                except (Exception, SystemExit) as error:
                    actual_check = {
                        "kind": type(error).__name__,
                        "error_code": getattr(error, "error_code", None),
                        "reason": _reason(error),
                    }
                    _require(
                        actual_check["reason"] in bound_reasons
                        or actual_check["error_code"] == "EVIDENCE_REVISION_LIMIT",
                        "unexpected_physical_companion_check_refusal",
                    )
                else:
                    _require(not _accepted(actual_check), "physical_bound_companion_reused_native_approval")
                    _require(
                        all(row["review"].get("passed") is False for row in actual_check["claims"]),
                        "physical_bound_companion_selected_old_passing_review",
                    )
                _require(self.guard() == before_check, "physical_bound_check_changed_native_custody")
                self.originals.append((case_id + "-physical-native-check", actual_check))
            candidate["dependencies"].sort(
                key=lambda row: tuple(row[key].encode() for key in ("scope", "kind", "coordinate"))
            )
            candidate["basis_id"] = contract.content_id(
                contract.BASIS, {key: value for key, value in candidate.items() if key != "basis_id"}
            )
            wire = native_canonical(candidate)
            _require(
                len(wire) > 1048576 and len(candidate["dependencies"]) < 4096,
                "native_basis_byte_companion_not_oversized",
            )
            pin = self.raw(case_id + "-unapproved-basis-wire", wire)
            refusal = self.refused(case_id, lambda: contract.loads(wire), reasons={"review_dependency_bytes_bound"})
        finally:
            for coordinate in reversed(paths):
                (self.root / coordinate).unlink()
            for directory in reversed(created_directories):
                directory.rmdir()
            current_history = {
                path: path.read_bytes()
                for directory in (self.root, Path(self.runtime.context.state_directory))
                for path in directory.rglob("*.jsonl")
                if path.is_file()
            }
            _require(current_history == history, "physical_companion_native_history_changed")
            _require(self.guard() == opening_custody, "physical_companion_cleanup_changed_native_custody")
        self.originals.append(
            (
                case_id + "-physical-companion-closing-history",
                {
                    "files": {
                        str(path): {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
                        for path, raw in sorted(history.items())
                    },
                    "preserved_exact_bytes": True,
                },
            )
        )
        return {
            "status": "passed_limited_scope",
            "assertions": [
                "actual_original_recipe_basis_size_measurement",
                "actual_physical_long_coordinate_inputs",
                "actual_native_current_owner_bound_refusal",
                "actual_native_check_no_accepted_review",
                "only_created_workspace_inputs_removed",
                "native_historical_bytes_unchanged",
                "native_unapproved_wire_byte_guard_refusal",
                "failed_call_no_write",
            ],
            "evidence": [native_refusal, pin, refusal],
            "actual_companion_wire_bytes": len(wire),
            "actual_companion_file_count": len(paths),
            "candidate_approval": False,
            "native_assertion_scope": "physical_workspace_earlier_native_owner_refusal",
            "actual_native_owner_operation": native_stage,
            "earlier_owner_refusal": native_refusal,
            "parser_companion_scope": "unapproved_wire_parser_only_refusal",
            "native_bound_qualification": False,
            "candidate_current_semantic_identity_claimed": False,
            "realization_difference": "Original content-only expansion changes the native digest without growing its basis. A separate physical workspace companion invokes actual current dependency reconstruction and strict checking, retaining the concrete earlier native guard. The separately retained unapproved wire companion proves only parser refusal before identity validation.",
            "unavailable_branches": [
                "oversized_native_current_basis_from_content_only_expansion",
                "isolated_dependency_basis_byte_guard_after_earlier_native_owner_refusal",
            ],
        }

    def _packet_bytes_case(self, case_id):
        from evidence_wiki._pack_io import canonical as native_canonical
        from evidence_wiki.review_packet_contracts import validate
        from evidence_wiki.review_packet_execution import context_parameters
        from evidence_wiki.review_page_operations import _collect_packet, prepare

        application = self.applications[("baseline", "observed-capacity", "critic")]
        raw_path = self.root / "raw/papers/unused.md"
        raw_original = raw_path.read_bytes()
        with self.session(application["run_id"]) as (workspace, session):
            try:
                raw_path.write_bytes(raw_original + b"\n" + b"x" * 60000)
                original_refusal = self.refused(
                    case_id + "-original-recipe",
                    workspace.strict.check,
                    reasons={"assessment_raw_revision_mismatch", "host_capture_original_or_qualifications_invalid"},
                )
            finally:
                raw_path.write_bytes(raw_original)
            question_path = self.root / "wiki/questions/capacity.md"
            question_original = question_path.read_bytes()
            _, front, body = question_original.decode().split("---", 2)
            parameters = context_parameters(session, "packet-byte-companion")
            arguments = {
                "claim_id": "observed-capacity",
                "context_id": parameters["context_id"],
                "task_role": "critic",
                "page_chars": 16384,
                "source_ids": [row["source_id"] for row in self.sources.values()],
                "_order_seed": parameters["order_seed"],
                "context_version": self.runtime.execution.profile.review_delivery.context_version,
            }
            observed = []
            try:
                for body_size in (24000, 36000, 48000, 60000):
                    question_path.write_text("---" + front + "---" + body + "\n" + "x" * body_size)
                    try:
                        handle = prepare(self.root, **arguments)
                        header = handle.header()
                        pages = [handle.page(row["page_id"]) for row in header["pages"]]
                        complete = handle.complete(pages)
                        version = "v3" if arguments["context_version"] == "dependencies" else "v2"
                        packet = validate(
                            "research-review-packet/" + version,
                            {
                                "schema_version": "research-review-packet/" + version,
                                "header": header,
                                "pages": pages,
                                "complete": complete,
                            },
                        )
                    except (Exception, SystemExit) as error:
                        reason = _reason(error)
                        _require(
                            reason in {"header_bound", "review_context_bound_invalid", "review_dependency_bytes_bound"},
                            "unexpected_packet_companion_early_refusal",
                        )
                        observed.append(
                            {
                                "body_added_bytes": body_size,
                                "reason": reason,
                                "error_code": getattr(error, "error_code", None),
                            }
                        )
                        break
                    wire = native_canonical(packet)
                    observed.append(
                        {
                            "body_added_bytes": body_size,
                            "actual_packet_bytes": len(wire),
                            "complete": complete["complete"],
                        }
                    )
                    _require(complete["complete"], "native_packet_companion_incomplete")
                    if len(wire) <= 196608:
                        continue
                    pin = self.raw(case_id + "-complete-native-companion", wire)
                    refusal = self.refused(
                        case_id,
                        lambda: _collect_packet(self.root, **arguments, max_packet_bytes=196608),
                        reasons={"complete_packet_requires_supported_paging_executor"},
                    )
                    return {
                        "status": "passed_limited_scope",
                        "assertions": [
                            "actual_immutable_raw_original_recipe_refusal",
                            "actual_complete_native_packet_byte_companion",
                            "native_packet_cap_refusal",
                            "failed_call_no_write",
                        ],
                        "evidence": [original_refusal, pin, refusal],
                        "companion_observations": observed,
                        "unavailable_branches": ["complete_packet_guard_after_unpermitted_unused_raw_expansion"],
                        "realization_difference": "A separate question-body companion preserves signed source bytes and measures the complete actual native packet.",
                    }
            finally:
                question_path.write_bytes(question_original)
        _require(observed and "reason" in observed[-1], "native_packet_companion_bound_not_reached")
        self.originals.append((case_id + "-companion-earlier-refusal", observed))
        return {
            "status": "earlier_owner_refusal",
            "assertions": [
                "actual_immutable_raw_original_recipe_refusal",
                "actual_native_question_body_packet_preparation_refusal",
            ],
            "evidence": [original_refusal],
            "companion_observations": observed,
            "earlier_owner_refusal": observed[-1]["reason"],
            "unavailable_branches": ["complete_packet_cap_after_earlier_context_or_header_guard"],
        }

    def _history_case(self, case_id):
        _require(self.baseline is not None, "native_baseline_required")
        baseline = self.baseline["strict"]
        subject = "observed-capacity"
        expected_blocked = _dependants(self.manifest, subject)
        changed = case_id != "recorded-review-revocation"
        if changed:
            verdict = "unknown" if "unknown" in case_id else "fail"
            self.drive({subject}, cycle=case_id, verdict=verdict, configs=("critic",))
            application = self.applications[(case_id, subject, "critic")]
            current = self.observe(case_id + "-newest")
            _require(current["strict"]["basis_id"] == baseline["basis_id"], "history_append_changed_workspace_basis")
            _require(_basis(current["strict"]) == _basis(baseline), "history_append_changed_claim_basis")
            _require(_accepted(current["strict"]) == CLAIM_IDS - expected_blocked, "newest_negative_did_not_block")
            selected = next(row for row in current["strict"]["claims"] if row["claim"]["id"] == subject)
            review = application["envelope"]["payload"]["body"]["review"]
            _require(
                selected["review"].get("review_id") == owner("_strict_contract").artifact_id(review),
                "older_review_resurrected",
            )
        else:
            application = self.applications[("baseline", subject, "critic")]
        revoked = case_id == "recorded-review-revocation" or case_id.startswith("revoked-")
        assertions = ["actual_authenticated_native_review_history"]
        if changed:
            assertions.append("native_no_older_pass_resurrection")
        evidence = []
        unavailable = []
        if revoked:
            authority_path = Path(self.runtime.context.authority_file)
            authority = json.loads(authority_path.read_bytes())
            payload_id = owner("_evidence_revision").content_id(
                "evidence-authenticated-payload/v1", application["envelope"]["payload"]
            )
            authority["revoked_envelopes"].append(payload_id)
            replacement = authority_path.with_name("replacement-authority.json")
            with replacement.open("xb") as stream:
                stream.write(canonical(authority))
            replacement.chmod(0o600)
            os.replace(replacement, authority_path)
            self.originals.append(
                (
                    case_id + "-revocation",
                    {
                        "payload_id": payload_id,
                        "authority_sha256": hashlib.sha256(authority_path.read_bytes()).hexdigest(),
                    },
                )
            )
            observed = self.observe(case_id + "-revoked", run_id="observe-" + digest(case_id)[:24])
            _require(not _accepted(observed["strict"]), "revoked_history_released_claims")
            _require(
                _basis(observed["strict"]) != _basis(baseline) or self.mode == "legacy_full",
                "authority_content_not_bound",
            )
            assertions.extend(["actual_protected_payload_revocation", "native_failure_closed_after_authority_change"])
            unavailable.append("unchanged_basis_subset_selection_after_authority_content_change")
            evidence.append({"revoked_payload_id": payload_id, "accepted_claim_ids": []})
        else:
            assertions.extend(["same_native_claim_and_workspace_bases", "exact_newest_negative_acceptance_set"])
        return {
            "status": "passed_limited_scope" if revoked else "passed",
            "assertions": assertions,
            "evidence": evidence,
            "unavailable_branches": unavailable,
            "history_selection_scope": "before_authority_revocation"
            if revoked and changed
            else "global_authority_invalidation"
            if revoked
            else "unchanged_native_bases",
            "isolation": "fresh_native_fixture_no_history_restore",
        }

    def _protected_case(self, case_id):
        config_path = self.root / "research.yml"
        config = yaml.safe_load(config_path.read_bytes())
        policy = config["strict_evidence"]
        authority_path = Path(self.runtime.context.authority_file)
        authority = json.loads(authority_path.read_bytes())
        if case_id == "policy":
            policy["revision"] = "2"
            policy["max_review_age_seconds"] = 1800
        elif case_id == "rubric":
            policy["rubric"]["revision"] = "2"
            policy["rubric"]["criteria"]["scope"] = "Assess the exact retained scope and its limitations."
        elif case_id == "instructions":
            path = self.root / "AGENTS.md"
            path.write_bytes(path.read_bytes() + b"\nAssess the exact retained observation scope.\n")
            policy["instructions"]["AGENTS.md"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            policy["revision"] = "2"
        elif case_id == "authority-content":
            authority["principals"]["additional-reviewer"] = {
                "controller": "additional-controller",
                "roles": ["evaluator"],
                "keys": {"additional-key": secrets.token_bytes(32).hex()},
            }
        elif case_id == "authority-key-revocation":
            identity = next(row for row in self.runtime.execution.review_signer.reviewers if row.config_ref == "critic")
            authority["revoked_keys"].append(identity.key_id)
        elif case_id == "controller-separation-failure":
            principal = next(
                row.principal for row in self.runtime.execution.review_signer.reviewers if row.config_ref == "critic"
            )
            authority["principals"][principal]["controller"] = (
                self.runtime.execution.review_signer.generator.controller_ref
            )
        elif case_id == "current-role-failure":
            policy["revision"] = "2"
            policy["human_review"] = True
        else:
            raise ValueError("native_protected_case_unknown")
        binding = owner("_evidence_usage").workspace_binding(self.root)
        authority["strict_workspaces"][binding] = copy.deepcopy(policy)
        owner("_strict_contract").policy_document(policy)
        config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        replacement = authority_path.with_name("replacement-authority.json")
        with replacement.open("xb") as stream:
            stream.write(canonical(authority))
        replacement.chmod(0o600)
        os.replace(replacement, authority_path)
        self.originals.append(
            (
                case_id + "-protected-change",
                {
                    "authority_sha256": hashlib.sha256(authority_path.read_bytes()).hexdigest(),
                    "configuration_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
                    "native_policy": policy,
                },
            )
        )
        observed = self.observe(case_id, run_id="observe-" + digest(case_id)[:24])
        _require(not _accepted(observed["strict"]), "protected_change_reused_approval")
        if self.mode == "selective_reuse":
            old, current = _basis(self.baseline["strict"]), _basis(observed["strict"])
            _require(
                all(current[claim] is not None and current[claim] != old[claim] for claim in CLAIM_IDS),
                "protected_global_basis_did_not_change",
            )
        if case_id == "current-role-failure":
            _require(
                all(row["required_review_role"] == "human-review" for row in observed["strict"]["claims"]),
                "current_human_role_not_selected",
            )
        return {
            "status": "passed_limited_scope" if case_id == "controller-separation-failure" else "passed",
            "assertions": [
                "actual_coherent_protected_material_change",
                "all_old_approvals_blocked",
                "read_only_closing_custody",
            ],
            "evidence": [],
            "unavailable_branches": ["fresh_equal_controller_registration_check"]
            if case_id == "controller-separation-failure"
            else [],
            "isolation": "fresh_native_fixture_no_authority_or_history_restore",
        }

    def _race_case(self, case_id):
        target = (
            self.root / "notes/context.txt"
            if case_id == "workspace-mid-read-change"
            else Path(self.runtime.context.authority_file)
        )
        _require(target.is_file(), "native_race_input_missing")
        original = target.read_bytes()
        requests, completed = queue.Queue(), threading.Event()
        errors, writes = [], []
        opening = self.guard()

        def writer():
            try:
                while True:
                    ordinal = requests.get(timeout=10)
                    if ordinal is None:
                        return
                    if case_id == "workspace-mid-read-change":
                        data = original + ("\nObserved concurrent write " + str(ordinal) + ".\n").encode()
                    else:
                        authority = json.loads(original)
                        authority["principals"]["concurrent-reviewer"] = {
                            "controller": "concurrent-controller",
                            "roles": ["evaluator"],
                            "keys": {"concurrent-key": secrets.token_bytes(32).hex()},
                        }
                        data = canonical(authority)
                    temporary = target.with_name("concurrent-owner-input.tmp")
                    with temporary.open("xb") as stream:
                        stream.write(data)
                    temporary.chmod(0o600)
                    os.replace(temporary, target)
                    writes.append({"ordinal": ordinal, "sha256": hashlib.sha256(data).hexdigest()})
                    completed.set()
            except Exception as error:
                errors.append(type(error).__name__)
                completed.set()

        def change():
            completed.clear()
            requests.put(len(writes) + 1)
            _require(completed.wait(timeout=5) and not errors, "native_race_writer_unavailable")

        thread = threading.Thread(target=writer, name="native-input-writer", daemon=True)
        thread.start()
        revision, strict = owner("_evidence_revision"), owner("_strict_evidence")
        boundary = (
            "observe_tree"
            if case_id == "workspace-mid-read-change"
            else ("dependency_review_result" if self.mode == "selective_reuse" else "review_result")
        )
        module = revision if case_id == "workspace-mid-read-change" else strict
        actual = getattr(module, boundary)
        fired = False

        def observed_boundary(*args, **kwargs):
            nonlocal fired
            result = actual(*args, **kwargs)
            if case_id == "workspace-mid-read-change" or not fired:
                fired = True
                change()
            return result

        refusal = None
        try:
            if case_id == "workspace-mid-read-change":
                setattr(module, boundary, observed_boundary)
                try:
                    revision.capture_workspace(self.root)
                except (Exception, SystemExit) as error:
                    _require(
                        getattr(error, "error_code", None) == "EVIDENCE_REVISION_CHANGED",
                        "unexpected_capture_race_refusal",
                    )
                    refusal = {"error_code": error.error_code, "reason": _reason(error)}
            else:
                application = self.applications[("baseline", "observed-capacity", "critic")]
                with self.session(application["run_id"]) as (workspace, _session):
                    setattr(module, boundary, observed_boundary)
                    try:
                        workspace.strict.check()
                    except (Exception, SystemExit) as error:
                        _require(
                            getattr(error, "error_code", None)
                            in {"RESEARCH_REVIEW_DEPENDENCY_INPUTS_CHANGED", "RESEARCH_BINDING_CHANGED"}
                            or _reason(error) == "host_trust_changed",
                            "unexpected_authority_race_refusal",
                        )
                        refusal = {"error_code": getattr(error, "error_code", None), "reason": _reason(error)}
            _require(refusal is not None and writes and not errors, "native_race_was_not_refused")
            if case_id == "workspace-mid-read-change":
                _require(len(writes) == revision.MAX_ATTEMPTS, "native_capture_retry_attempts_not_exercised")
            closing = self.guard()
            if case_id == "workspace-mid-read-change":
                expected = copy.deepcopy(opening)
                expected["workspace_files"][target.relative_to(self.root).as_posix()] = writes[-1]["sha256"]
            else:
                expected = copy.deepcopy(opening)
                expected["authority_sha256"] = writes[-1]["sha256"]
                private = Path(self.runtime.context.state_directory)
                if target.is_relative_to(private):
                    expected["private_files"][target.relative_to(private).as_posix()] = writes[-1]["sha256"]
            _require(closing == expected, "race_added_unplanned_journal_or_output_effect")
            self.originals.append(
                (
                    case_id + "-actual-writes-and-refusal",
                    {"writes": writes, "refusal": refusal, "boundary": boundary, "original_invoked": True},
                )
            )
            return {
                "status": "passed",
                "assertions": [
                    "actual_external_writer_interleaving",
                    "original_native_capture_or_closing_refusal",
                    "no_unplanned_journal_or_output_effect",
                ],
                "evidence": [refusal, {"writes": writes}],
                "isolation": "fresh_native_fixture",
            }
        finally:
            setattr(module, boundary, actual)
            requests.put(None)
            thread.join(timeout=5)
            _require(not thread.is_alive(), "native_race_writer_not_closed")
            if case_id == "workspace-mid-read-change":
                target.write_bytes(original)
                _require(self.guard() == opening, "race_input_restore_changed_custody")

    def _counter_case(self, case_id):
        from evidence_wiki import counter_context
        from evidence_wiki.coverage_examination import check_instruction
        from evidence_wiki.coverage_observation import observe
        from evidence_wiki.review_context import ReviewContext

        application = self.applications[("baseline", "observed-capacity", "critic")]
        context = ReviewContext.restore(application["attempt"].input_document["packet"], application["review_context"])
        quote = "Service note: scheduled maintenance."
        source = next(row for row in context.document()["sources"] if quote in row["content"])
        start = source["content"].index(quote)
        proposal = {
            "schema_version": "evidence-counterevidence-proposal/v1",
            "basis_id": context.sha256,
            "findings": [
                {
                    "claim_id": context.binding()["aliases"]["claims"]["observed-capacity"],
                    "facet_id": "facet-1",
                    "kind": "opposing",
                    "query": "maintenance availability",
                    "exclusions": [],
                    "source_ids": [source["source_id"]],
                    "evidence": [
                        {
                            "source_id": source["source_id"],
                            "record_sha256": source["record_sha256"],
                            "quote": quote,
                            "start": start,
                            "end": start + len(quote),
                        }
                    ],
                    "rationale": "Maintenance can affect available capacity; assess scope.",
                }
            ],
        }
        pin = self.raw(case_id + "-proposal", canonical(proposal))
        with self.session(application["run_id"]):
            instruction = check_instruction(observe(self.root, question_slug="capacity"))
            refused = self.refused(
                case_id,
                lambda: counter_context.translate(instruction, context, proposal),
                reasons={"critic_context_version_unsupported"},
            )
        return {
            "status": "passed_limited_scope",
            "executed_scope": "allowed_native_counter_proposal_refusal_no_acceptance",
            "assertions": [
                "actual_critic_context_and_retained_exact_quote",
                "actual_native_unsupported_counter_refusal",
                "failed_call_no_write",
            ],
            "evidence": [pin, refused],
            "unavailable_branches": ["finding_only_selective_semantic_input_and_local_set_comparison"],
        }

    def _question_case(self, case_id):
        slug = "capacity" if "capacity" in case_id else "demand"
        path = self.root / "wiki/questions" / (slug + ".md")
        original, opening = path.read_bytes(), self.guard()
        context = copy.deepcopy(self.fixture["question_context"][slug])
        if case_id.endswith("-metadata"):
            context["metadata"]["owner"] = "second-analyst"
        else:
            context["body"] = "Additional retained question scope sentence.\\n"
        header = original.decode().split("---", 2)
        question = next(row["question"] for row in self.manifest["questions"] if row["slug"] == slug)
        wire = ("---" + header[1] + "---\n" + render_question_body(context, question=question)).encode()
        expected_changed = set(CLAIM_IDS)
        if self.mode == "selective_reuse":
            expected_changed = set().union(
                *(
                    _dependants(self.manifest, row["id"])
                    for row in self.manifest["claims"]
                    if row["question_slug"] == slug
                )
            )
        try:
            path.write_bytes(wire)
            current = self.observe(case_id)
            strict = current["strict"]
            _require(strict["basis_id"] != self.baseline["strict"]["basis_id"], "question_workspace_basis_unchanged")
            _require(_accepted(strict) == CLAIM_IDS - expected_changed, "question_native_acceptance_changed")
            evidence = []
            if self.mode == "selective_reuse":
                old, new = _basis(self.baseline["strict"]), _basis(strict)
                _require(
                    {claim for claim in CLAIM_IDS if old[claim] != new[claim]} == expected_changed,
                    "question_closure_basis_changed",
                )
                for claim in sorted(CLAIM_IDS - expected_changed):
                    for config in ("evidence", "critic"):
                        facts = self.retained(claim, config, case_id)
                        _require(facts["state"] == "eligible", "question_retained_service_ineligible")
                        evidence.append({"claim_id": claim, "config_ref": config, "state": facts["state"]})
            return {
                "status": "passed",
                "assertions": [
                    "actual_question_input_bytes",
                    "exact_transitive_question_acceptance_set",
                    "read_only_closing_custody",
                ],
                "evidence": evidence,
                "material_invalidated": sorted(expected_changed),
            }
        finally:
            path.write_bytes(original)
            _require(self.guard() == opening, "question_input_restore_changed_custody")

    def _residual_case(self, case_id):
        path = self.root / "notes" / ("new.txt" if case_id.endswith("addition") else "context.txt")
        existed = path.exists()
        original = path.read_bytes() if existed else None
        if case_id != "unclassified-file-addition" and not existed:
            raise AssertionError("native_baseline_residual_coordinate_absent")
        _require(not existed if case_id.endswith("addition") else existed, "residual_membership_precondition_changed")
        opening = self.guard()
        directory_existed = path.parent.exists()
        try:
            if case_id.endswith("deletion"):
                path.unlink()
            else:
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b"Retained observer context amended.\\n")
            current = self.observe(case_id)
            _require(not _accepted(current["strict"]), "global_residual_change_reused_approval")
            _require(
                current["strict"]["basis_id"] != self.baseline["strict"]["basis_id"], "global_residual_basis_unchanged"
            )
            if self.mode == "selective_reuse":
                old, new = _basis(self.baseline["strict"]), _basis(current["strict"])
                _require(
                    all(old[claim] != new[claim] and new[claim] is not None for claim in CLAIM_IDS),
                    "global_residual_claim_basis_unchanged",
                )
            return {
                "status": "passed",
                "assertions": [
                    "actual_residual_file_membership_or_bytes",
                    "global_invalidation",
                    "read_only_closing_custody",
                ],
                "evidence": [],
            }
        finally:
            if existed:
                path.write_bytes(original)
            elif path.exists():
                path.unlink()
            if not directory_existed and path.parent.exists():
                path.parent.rmdir()
            _require(self.guard() == opening, "residual_input_restore_changed_custody")
