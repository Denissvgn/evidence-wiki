#!/usr/bin/env python3
"""Explicit coverage migration with immutable evidence history and current checks."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from contextlib import nullcontext
from pathlib import Path
from types import MappingProxyType

import yaml
from _evidence_revision import canonical_bytes, capture_workspace
from _pack_revision_guard import idle, inputs, latest, require, sibling


def validate_basis(value):
    require(isinstance(value, dict) and set(value) == {"revision_id", "migration_id", "rationale", "archive", "archive_sha256", "request_replacements"}, "coverage_revision_basis_invalid")
    require(all(isinstance(value[key], str) and value[key] for key in value if key != "request_replacements"), "coverage_revision_basis_invalid")
    require(isinstance(value["request_replacements"], dict) and all(isinstance(k, str) and isinstance(v, str)
            for k, v in value["request_replacements"].items()), "coverage_revision_replacements_invalid")
    for key in ("revision_id", "migration_id", "archive_sha256"):
        require(len(value[key]) == 71 and value[key].startswith("sha256:")
                and all(c in "0123456789abcdef" for c in value[key][7:]), "coverage_revision_identity_invalid")


def replacement_document(coverage, config, slug, request, old, needed, *, observed_at=None):
    """Share criteria and preserved-evidence mapping with preparation and migration."""
    _observed_at = observed_at
    replacement = request["request_replacements"]
    template = coverage.normalize_template_document(request["template"], policy_vocabularies=coverage.merged_policy_vocabularies(config))
    document = (coverage.build_manifest(slug, None, template) if _observed_at is None
                else coverage.build_manifest(slug, None, template, _observed_at=_observed_at))
    old_facets = {f["facet_id"]: f for f in coverage.all_facets(old)} if old else {}
    new_ids = {f["facet_id"] for f in coverage.all_facets(document)}
    require(set(old_facets) - new_ids == set(request["retired_facets"]), "coverage_revision_retirements_unconfirmed")
    evidence_fields = {"accepted_source_ids", "blocking_request_ids", "facet_verdict"}
    changed_policies = set(needed["changed_policies"])
    for facet in coverage.all_facets(document):
        # A template is criteria, never evidence or an approval.
        require(not facet["accepted_source_ids"] and not facet["blocking_request_ids"]
                and facet["facet_verdict"] == "pending", "coverage_revision_template_contains_evidence")
        before = old_facets.get(facet["facet_id"])
        unchanged = before and {k: v for k, v in before.items() if k not in evidence_fields} == {k: v for k, v in facet.items() if k not in evidence_fields}
        if unchanged and not changed_policies.intersection(facet.get(k) for k in ("source_policy", "freshness_policy", "identity_policy")):
            for key in evidence_fields:
                facet[key] = copy.deepcopy(before[key])
        if before:
            facet["blocking_request_ids"] = [replacement.get(key, key) for key in before["blocking_request_ids"]]
        if facet["required"]:
            facet["blocking_request_ids"] = sorted(set(facet["blocking_request_ids"]) | set(replacement.values()))
    if old:
        document["created_at"] = old["created_at"]
    return document


def migrate(root, request, *, _plan=None, _observed_at=None, _prepare_only=False):
    """Only an explicit reviewed requirement template can clear a revision hold."""
    require(not Path(root).expanduser().is_symlink(), "revision_target_link")
    root = Path(root).expanduser().resolve(strict=True)
    coverage = sibling("coverage_manifest")
    lifecycle = sibling("_domain_pack_lifecycle")
    required = {"schema_version", "revision_id", "slug", "rationale", "template", "retired_facets", "computation_migrations", "request_replacements"}
    require(isinstance(request, dict) and set(request) == required and request["schema_version"] == "evidence-pack-reevaluation/v1",
            "coverage_revision_request_invalid")
    require(isinstance(request["rationale"], str) and 1 <= len(request["rationale"].strip()) <= 4096, "coverage_revision_rationale_required")
    slug = coverage.validate_slug(request["slug"])
    require(isinstance(request["retired_facets"], list) and all(isinstance(v, str) for v in request["retired_facets"]), "coverage_revision_retirements_invalid")
    migration_id = sibling("_pack_revision_impact").digest(request)
    lifecycle.load_state(root)
    idle(root)
    with sibling("_workspace_locks").workspace_lock(root / ".locks/domain-pack-refresh.lock", purpose="coverage revision") as lock:
        require(lock.locked, "revision_native_lock_required")
        if _plan is not None or _prepare_only:
            require(lock.backend in {"fcntl", "win32", "msvcrt"}, "revision_native_lock_required")
        capture = capture_workspace(root)
        idle(root, capture=capture)
        if _plan is not None:
            return commit_migration(root, request, _plan)
        state = lifecycle.load_state(root)
        record = latest(state, slug)
        require(record is not None and record["revision_id"] == request["revision_id"], "coverage_revision_changed")
        impact = next(row for row in record["impact"]["questions"] if row["slug"] == slug)
        needed = impact.get("migration_requirements", {
            "removed_computations": record["impact"]["computation"]["removed_definitions"],
            "changed_policies": record["impact"]["policy_ids"],
            "removed_requests": [row for row in record["impact"]["requests"] if row["removed"] and slug in row["question_slugs"]]})
        computations = request["computation_migrations"]
        require(isinstance(computations, dict) and set(computations) == set(needed["removed_computations"])
                and all(value is None or value in record["impact"]["computation"]["current_definitions"] for value in computations.values()),
                "coverage_computation_migration_required")
        config = coverage.load_config(root)
        require(sibling("_caller_context").builtin_integrations(config), "revision_provider_qualification_required")
        question = coverage.ensure_question_exists(root, config, slug)
        fm, _ = sibling("verify_quotes").split_page(capture.files[question.relative_to(root).as_posix()].decode())
        path = coverage.selected_manifest_path(root, config, slug, fm.get("coverage_manifest"))
        relative = path.relative_to(root).as_posix()
        prior = capture.files.get(relative)
        old = yaml.safe_load(prior) if prior is not None else None
        replacement = request["request_replacements"]
        removed = {row["request_id"]: row for row in needed["removed_requests"]}
        require(isinstance(replacement, dict) and set(replacement) == set(removed), "coverage_request_migration_required")
        requests_owner = sibling("source_requests")
        requests = {row["request_id"]: row for row in requests_owner.load_requests(requests_owner.requests_path(root, config))}
        for previous, new_id in replacement.items():
            require(previous in requests and "requirement_sha256" in removed[previous]
                    and sibling("_pack_revision_impact").request_basis(requests[previous]) == removed[previous]["requirement_sha256"], "coverage_original_request_changed_or_unbound")
            require(new_id in requests and new_id != previous and slug in requests[new_id].get("question_slugs", [])
                    and requests[new_id].get("scope") == requests.get(previous, {}).get("scope")
                    and requests[new_id].get("kind") in sibling("_request_kinds").valid_kinds(config), "coverage_request_replacement_invalid")
        if old and old.get("revision_basis", {}).get("migration_id") == migration_id:
            require(sibling("_pack_revision_guard").pending_question(root, slug, old) is None, "coverage_revision_archive_unavailable")
            if _prepare_only:
                return prepare_existing(root, request, capture, config, path, question, old, migration_id, needed, _observed_at)
            return {"status": "already_migrated", "revision_id": record["revision_id"], "slug": slug,
                    "coverage": coverage.coverage_summary_for_question(root, config, slug, fm), "release_accepted": False}
        document = replacement_document(coverage, config, slug, request, old, needed, observed_at=_observed_at)
        archive = "runs/pack-revisions/" + record["revision_id"][7:] + "/" + slug + "-" + migration_id[7:] + ".json"
        document["revision_basis"] = {"revision_id": record["revision_id"], "migration_id": migration_id,
            "rationale": request["rationale"], "archive": archive, "request_replacements": replacement}
        if old:
            document["created_at"] = old["created_at"]
        question_relative = question.relative_to(root).as_posix()
        original_question = capture.files[question_relative].decode()
        if archive in capture.files:
            retained = json.loads(capture.files[archive])
            require(retained["request"] == request and retained["manifest_path"] == relative
                    and retained["manifest"] == (prior.decode() if prior is not None else None)
                    and retained["question_path"] == question_relative, "coverage_revision_archive_conflict")
            original_question = retained["question"]
        resolver = sibling("question_resolve")
        expected_question = resolver.render_revision_page(original_question, record["revision_id"])
        require(capture.files[question_relative].decode() in {original_question, expected_question}, "coverage_revision_question_changed")
        archived = canonical_bytes({"schema_version": "evidence-coverage-history/v1", "request": request,
            "manifest_path": relative, "manifest": prior.decode() if prior is not None else None,
            "question_path": question_relative, "question": original_question,
            "authority": "local_unauthenticated_history"})
        document["revision_basis"]["archive_sha256"] = "sha256:" + hashlib.sha256(archived).hexdigest()
        coverage.validate_manifest(document, expected_slug=slug, policy_vocabularies=coverage.merged_policy_vocabularies(config))
        if _observed_at is None:
            policy_results, by_facet = coverage.evaluate_policy_results_for_manifest(root, config, document)
            private_view = None
            coverage.evaluate_manifest(document, by_facet)
        else:
            policy_results, by_facet, private_view = coverage.recovery_policy_results(root, config, capture, document, _observed_at)
            coverage.evaluate_manifest(document, by_facet, _observed_at=_observed_at)
        if _prepare_only:
            return prepared_migration(root, request, capture, config, relative, question_relative, archive, archived,
                                      expected_question.encode(), document, migration_id, policy_results, private_view, _observed_at)
        publisher = sibling("_usage_materialization")
        require(inputs(capture_workspace(root)) == inputs(capture), "coverage_revision_inputs_changed")
        publisher.publish_file(root, archive, archived, None)
        # A failed final write leaves the old manifest on hold and an immutable archive;
        # retry publishes that same archive idempotently, then attempts the owned write.
        after_archive = capture_workspace(root)
        expected_files = dict(capture.files)
        expected_files[archive] = archived
        require(dict(after_archive.files) == expected_files, "coverage_revision_inputs_changed")
        resolver.reopen_for_revision(root, slug, record["revision_id"], original_question)
        after_reopen = capture_workspace(root)
        expected_files[question_relative] = expected_question.encode()
        require({k: v for k, v in after_reopen.files.items() if ".locks" not in Path(k).parts}
                == {k: v for k, v in expected_files.items() if ".locks" not in Path(k).parts}, "coverage_revision_inputs_changed")
        rendered = yaml.safe_dump(document, sort_keys=False).encode()

        def closing():
            current = capture_workspace(root)
            extra = set(current.files) - set(after_reopen.files)
            require(len(extra) == 1, "coverage_revision_inputs_changed")
            temporary = next(iter(extra))
            require(Path(temporary).parent == Path(relative).parent and re.fullmatch(r"\.normalized-[0-9a-f]{32}", Path(temporary).name)
                    and current.files[temporary] == rendered
                    and all(current.files.get(k) == v for k, v in after_reopen.files.items()), "coverage_revision_inputs_changed")

        publisher.publish_file(root, relative, rendered,
            "sha256:" + hashlib.sha256(prior).hexdigest() if prior is not None else None,
            before_publish=closing)
        return {"status": "migrated", "revision_id": record["revision_id"], "slug": slug, "archive": archive,
            "coverage": coverage.coverage_summary_for_question(root, config, slug, fm), "policy_results": policy_results,
            "release_accepted": False, "next": "Recheck sources, computation, human/independent reviews and final publication through their owners."}


def migration_summary(root, config, capture, outputs, slug, observed_at, private_view):
    """Read the exact planned post-state without touching the live workspace."""
    coverage = sibling("coverage_manifest")
    revision = sibling("_evidence_revision")
    files = dict(capture.files)
    files.update({row["path"]: row["data"] for row in outputs})
    directories = set(capture.directories) | set(coverage.recovery_directories(capture, list(files)))
    candidate = revision.WorkspaceRevision(MappingProxyType(files), tuple(sorted(directories)))
    usage, publication = sibling("_evidence_usage"), sibling("_publication_context")
    with usage.current_view(root, config) if usage.configured(config) else nullcontext(None) as view:
        require(coverage.recovery_private_view(view) == private_view, "coverage_revision_private_inputs_changed")
        frozen = usage.UsageView(view.state, view.trust, observed_at) if view is not None else None
        mapped, candidate = coverage.recovery_materialized_inputs(root, config, candidate)
        with candidate.materialize() as target, publication.authorized_capture(target, mapped, frozen):
            question = coverage.ensure_question_exists(target, mapped, slug)
            frontmatter, _ = sibling("verify_quotes").split_page(question.read_bytes().decode())
            return coverage.coverage_summary_for_question(target, mapped, slug, frontmatter, _observed_at=observed_at)


def prepared_migration(root, request, capture, config, manifest_path, question_path, archive, archived,
                       reopened, document, migration_id, policies, private_view, observed_at, *, already=False):
    coverage, revision = sibling("coverage_manifest"), sibling("_evidence_revision")
    slug = coverage.validate_slug(request["slug"])
    outputs = [{"path": path, "data": data,
                "prior_sha256": "sha256:" + hashlib.sha256(capture.files[path]).hexdigest() if path in capture.files else None}
        for path, data in ((archive, archived), (question_path, reopened),
                          (manifest_path, capture.files[manifest_path] if already else yaml.safe_dump(document, sort_keys=False).encode()))]
    targets = [row["path"] for row in outputs]
    question_lock = sibling("question_claim").question_lock_path(root / question_path).relative_to(root).as_posix()
    lock_parent = Path(question_lock).parent.as_posix()
    controls = {"files": [question_lock], "new_directories": [lock_parent] if lock_parent not in capture.directories else []}
    sibling("_owner_paths").checked_path(root, question_lock, field="coverage_question_lock", kind="file")
    sibling("_owner_paths").checked_path(root, lock_parent, field="coverage_question_lock_directory", kind="directory")
    plan = {"schema_version": "evidence-coverage-plan/v1", "kind": "coverage.revision", "slug": slug,
        "observed_at": observed_at.isoformat(), "request_sha256": sibling("_pack_revision_impact").digest(request),
        "configuration_sha256": revision.content_id("coverage-config", config),
        "dependencies_sha256": coverage.recovery_inputs(capture, targets, controls=controls), "controls": controls,
        "new_directories": coverage.recovery_directories(capture, targets), "private_view": private_view,
        "outputs": outputs, "original_question": json.loads(archived)["question"], "already": already,
        "report": {"status": "already_migrated" if already else "migrated", "revision_id": request["revision_id"],
            "slug": slug, "archive": archive, "release_accepted": False,
            "coverage": migration_summary(root, config, capture, outputs, slug, observed_at, private_view),
            "policy_results": policies,
            "next": "Recheck sources, computation, human/independent reviews and final publication through their owners."}}
    current = coverage.check_recovery_inputs(root, plan)
    require(all(current.files.get(row["path"]) == capture.files.get(row["path"]) for row in outputs), "coverage_revision_inputs_changed")
    return plan


def prepare_existing(root, request, capture, config, path, question, current, migration_id, needed, observed_at):
    """An existing migration still must retain the exact criteria and reopened page."""
    coverage = sibling("coverage_manifest")
    slug = coverage.validate_slug(request["slug"])
    basis = current["revision_basis"]
    archive = basis["archive"]
    archived = capture.files.get(archive)
    require(archived is not None and "sha256:" + hashlib.sha256(archived).hexdigest() == basis["archive_sha256"], "coverage_revision_archive_conflict")
    history = sibling("_record_artifacts").json_document(archived)
    relative, question_relative = path.relative_to(root).as_posix(), question.relative_to(root).as_posix()
    require(history["request"] == request and history["manifest_path"] == relative and history["question_path"] == question_relative,
            "coverage_revision_archive_conflict")
    previous = yaml.safe_load(history["manifest"]) if history["manifest"] is not None else None
    expected = replacement_document(coverage, config, slug, request, previous, needed, observed_at=observed_at)
    clean = lambda document: {key: [{k: v for k, v in facet.items() if k != "facet_verdict"} for facet in value]
        if key in {"required_facets", "optional_facets"} else value for key, value in document.items()
        if key not in {"created_at", "updated_at", "coverage_verdict", "revision_basis"}}
    require(clean(current) == clean(expected), "coverage_revision_criteria_changed")
    require(basis["rationale"] == request["rationale"] and basis["request_replacements"] == request["request_replacements"],
            "coverage_revision_archive_conflict")
    reopened = sibling("question_resolve").render_revision_page(history["question"], request["revision_id"]).encode()
    require(capture.files.get(question_relative) == reopened, "coverage_revision_question_changed")
    policies, _facets, private_view = coverage.recovery_policy_results(root, config, capture, current, observed_at)
    return prepared_migration(root, request, capture, config, relative, question_relative, archive, archived, reopened,
                              current, migration_id, policies, private_view, observed_at, already=True)


def prepare_migration(root, request, *, _observed_at):
    coverage = sibling("coverage_manifest")
    slug = coverage.validate_slug(request["slug"])
    root, config = coverage._checked_coverage_config(root, slug)
    coverage._checked_policy_paths(root, config)
    capture_workspace(root)
    return migrate(root, request, _observed_at=coverage.observed_clock(_observed_at), _prepare_only=True)


def commit_migration(root, request, plan):
    """The caller holds the canonical revision lock before exact baseline checks."""
    coverage = sibling("coverage_manifest")
    slug = coverage.validate_slug(request["slug"])
    require(plan.get("schema_version") == "evidence-coverage-plan/v1" and plan.get("kind") == "coverage.revision"
        and plan.get("request_sha256") == sibling("_pack_revision_impact").digest(request)
        and plan.get("slug") == slug and len(plan.get("outputs", [])) == 3, "coverage_revision_plan_changed")
    capture = coverage.check_recovery_inputs(root, plan)
    config = coverage.load_config(root)
    question = coverage.ensure_question_exists(root, config, request["slug"])
    fm, _ = sibling("verify_quotes").split_page(capture.files[question.relative_to(root).as_posix()].decode())
    manifest = coverage.selected_manifest_path(root, config, request["slug"], fm.get("coverage_manifest")).relative_to(root).as_posix()
    archive = "runs/pack-revisions/" + request["revision_id"][7:] + "/" + slug + "-" + plan["request_sha256"][7:] + ".json"
    require([row["path"] for row in plan["outputs"]] == [archive, question.relative_to(root).as_posix(), manifest], "coverage_revision_plan_changed")
    for row in plan["outputs"]:
        actual = capture.files.get(row["path"])
        require(("sha256:" + hashlib.sha256(actual).hexdigest() if actual is not None else None) == row["prior_sha256"], "coverage_revision_target_changed")
    if plan["already"]:
        require(all(capture.files.get(row["path"]) == row["data"] for row in plan["outputs"]), "coverage_revision_target_changed")
        return copy.deepcopy(plan["report"])
    publisher = sibling("_usage_materialization")
    history, page, coverage_output = plan["outputs"]
    publisher.publish_file(root, history["path"], history["data"], history["prior_sha256"],
        before_publish=lambda: coverage.check_recovery_inputs(root, plan, publishing=(history["path"], history["data"])))
    coverage.check_recovery_inputs(root, plan)
    sibling("question_resolve").reopen_for_revision(root, request["slug"], request["revision_id"], plan["original_question"],
                                                  _expected_sha256=page["prior_sha256"])
    require(capture_workspace(root).files.get(page["path"]) == page["data"], "coverage_revision_question_changed")
    publisher.publish_file(root, coverage_output["path"], coverage_output["data"], coverage_output["prior_sha256"],
        before_publish=lambda: coverage.check_recovery_inputs(root, plan, publishing=(coverage_output["path"], coverage_output["data"])))
    current = coverage.check_recovery_inputs(root, plan)
    require(all(current.files.get(row["path"]) == row["data"] for row in plan["outputs"]), "coverage_revision_inputs_changed")
    document = yaml.safe_load(coverage_output["data"])
    require(sibling("_pack_revision_guard").pending_question(root, request["slug"], document) is None, "coverage_revision_archive_unavailable")
    return copy.deepcopy(plan["report"])
