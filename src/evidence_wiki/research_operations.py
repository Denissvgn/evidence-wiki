"""Explicit caller operations composing canonical run and source owners."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

from ._pack_io import canonical
from .pack_catalog import _outside_assets
from .pack_discovery import owner
from .planning_contracts import refuse
from .research_actions import guidance
from .research_observation import ResearchObservation
from .source_commands import _no_secret_values
from .source_inputs import SourceView
from .source_readiness import inspect_sources


@contextlib.contextmanager
def coordinated(target):
    root = Path(target).expanduser().resolve(strict=True)
    _outside_assets(root, additional_roots=(Path(__file__).parent,))
    # A single outer coordinator serializes this route above existing owner locks.
    with owner("_workspace_locks").workspace_lock(
        root / ".locks/caller-research.lock", timeout_seconds=0, purpose="caller research coordination"
    ) as lock:
        if not lock.locked or lock.backend not in {"fcntl", "msvcrt"}:
            refuse("caller_native_coordination_unavailable", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
        yield root


def controller_args(root, operation, *, agent_id, run_id=None):
    values = ["--project-root", str(root), operation, "--agent-id", agent_id, "--format", "json"]
    if run_id:
        values += ["--run-id", run_id]
    if operation == "start":
        values.append("--caller")
    return owner("run_controller").parse_args(values)


def run_operation(target, operation, *, agent_id, run_id=None):
    _no_secret_values({"agent_id": agent_id, "run_id": run_id})
    if operation == "resume":
        view = ResearchObservation(target, run_id=run_id)
        view.run_guard(agent_id)
        return guidance(view.root, agent_id=agent_id, run_id=run_id)
    with coordinated(target) as root:
        view = ResearchObservation(root, run_id=run_id if operation != "start" else None)
        if view.managed:
            refuse("managed_session_requires_existing_protocol", "ONBOARDING_OWNERSHIP_CONFLICT")
        if operation == "start":
            if view.run and view.run["state"]["current"] not in owner("run_controller").TERMINAL_STATES:
                refuse("active_run_requires_resume_or_explicit_recovery", "ONBOARDING_OWNERSHIP_CONFLICT")
            if not view.status.get("smoke", {}).get("ok") or view.controls["state"] != "observed":
                refuse("caller_workspace_requires_repair", "ONBOARDING_CHECK_FAILED")
            view.finish()
            result = owner("run_controller").run_start(
                root, controller_args(root, operation, agent_id=agent_id, run_id=run_id)
            )
        elif operation == "heartbeat":
            view.run_guard(agent_id)
            result = owner("run_controller").run_heartbeat(
                root, controller_args(root, operation, agent_id=agent_id, run_id=run_id)
            )
        elif operation == "resume":
            view.run_guard(agent_id)
            return guidance(root, agent_id=agent_id, run_id=run_id)
        else:
            refuse("caller_run_operation_unknown")
    return {
        "schema_version": "evidence-caller-run-result/v1",
        "operation": operation,
        "run": result,
        "authority": "caller_declared; owner-checked coordination only",
        "research_complete": False,
        "next": guidance(root, agent_id=agent_id, run_id=result["run_id"]),
    }


def event(root, run_id, agent_id, kind, data):
    module = owner("run_controller")
    args = module.parse_args(
        [
            "--project-root",
            str(root),
            "event",
            "--run-id",
            run_id,
            "--agent-id",
            agent_id,
            "--event-type",
            kind,
            "--message",
            "Caller research operation observation",
            "--data-json",
            canonical(data).decode(),
        ]
    )
    return module.run_event(root, args)


def selected_request(view, request_id):
    rows = [row for row in view.requests if row["request_id"] == request_id]
    if len(rows) != 1 or rows[0]["status"] not in {"open", "fulfilled"}:
        refuse("caller_request_unavailable")
    request = rows[0]
    known = {q["slug"] for q in view.questions}
    if not request.get("question_slugs") or not set(request["question_slugs"]) <= known:
        refuse("caller_request_question_scope_missing")
    return request


def acquire(target, *, agent_id, run_id, request_id, url):
    _no_secret_values({"agent_id": agent_id, "run_id": run_id, "request_id": request_id, "url": url})
    with coordinated(target) as root:
        view = ResearchObservation(root, run_id=run_id)
        view.run_guard(agent_id)
        request = selected_request(view, request_id)
        if request["status"] != "open":
            refuse("caller_request_already_fulfilled", "ONBOARDING_OWNERSHIP_CONFLICT")
        # Selection is an explicit exact URL; candidate IDs have their separate owner.
        if request["query_or_identifier"] != url or request["kind"] not in {"web", "other"}:
            refuse("caller_web_selection_requires_exact_request")
        requirements = view.capture.files.get("docs/research-requirements.json")
        if requirements is not None:
            from ._pack_io import json_document
            from .host_capabilities import scope_allows

            authority = json_document(requirements)["request"]["payload"]["authority"]
            scope = {
                "scope": [
                    {"kind": "uri_prefix", "value": value} for value in authority["source_scope"] if "://" in value
                ]
            }
            if "acquisition" not in authority["allowed_actions"] or not scope_allows(scope, url):
                refuse("caller_acquisition_outside_declared_authority")
        fetch = owner("fetch_sources")
        args = fetch.parse_args(
            [
                "--project-root",
                str(root),
                "--format",
                "json",
                "--run-id",
                run_id,
                "web",
                "get",
                "--url",
                url,
                "--request-id",
                request_id,
            ]
        )
        fetch.require_host_intake(view.config)
        context = fetch.acquisition_context(root, view.config, "web", 1, run_id=run_id, registered=())
        destination = fetch.resolve_web_target_root(root, context["acquisition"]) / fetch.web_target_filename(url)
        if destination.exists() or destination.with_name(destination.name + ".provenance.yml").exists():
            refuse("caller_capture_exists_inspect_and_ingest", "ONBOARDING_OWNERSHIP_CONFLICT")
        view.finish()
        try:
            result = fetch.run_web_get(root, context, args)
        except (Exception, SystemExit):
            event(
                root,
                run_id,
                agent_id,
                "fetch_failed",
                {"request_id": request_id, "provider": "web", "reason": "owner_fetch_failed"},
            )
            raise
        event(
            root,
            run_id,
            agent_id,
            "acquisition_completed",
            {
                "request_id": request_id,
                "provider": "web",
                "path": result["target_path"],
                "bytes": result["byte_count"],
                "observation": "capture_only",
            },
        )
        return {
            "schema_version": "evidence-caller-acquisition/v1",
            "capture": result,
            "request_fulfilled": False,
            "next": {"operation": "agent ingest", "request_id": request_id, "source_path": result["target_path"]},
            "evidence_accepted": False,
            "limits": [
                "Configured web transport enforced its policy; scope/normalized usability still require ingestion."
            ],
        }


def ingest(target, *, agent_id, run_id, request_id, source_id=None, source_path=None, needs_complete=True):
    _no_secret_values(
        {
            "agent_id": agent_id,
            "run_id": run_id,
            "request_id": request_id,
            "source_id": source_id,
            "source_path": source_path,
        }
    )
    with coordinated(target) as root:
        view = ResearchObservation(root, run_id=run_id)
        view.run_guard(agent_id)
        request = selected_request(view, request_id)
        if bool(source_id) == bool(source_path):
            refuse("caller_ingest_requires_one_source_selector")
        try:
            if source_path:
                from ._pack_io import relative_path

                relative_path(source_path)
                roots = view.config["raw"]["source_roots"]
                if not any(source_path.startswith(value.rstrip("/") + "/") for value in roots):
                    refuse("caller_ingest_outside_raw_roots")
                inventory = owner("source_inventory")
                args = inventory.parse_args(["--project-root", str(root)])
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    if inventory.run_inventory(args):
                        refuse("caller_inventory_failed", "ONBOARDING_CHECK_FAILED")
                source_view = SourceView(root)
                records = source_view.manifest()
                matches = [row["id"] for row in records.values() if source_path in (row.get("raw_paths") or [])]
                if len(matches) != 1:
                    refuse("caller_delivered_source_ambiguous_or_missing", "ONBOARDING_CHECK_FAILED")
                source_id = matches[0]
            if request["status"] == "fulfilled" and request["source_id"] != source_id:
                refuse("caller_request_fulfillment_mismatch", "ONBOARDING_OWNERSHIP_CONFLICT")
            normalizer = owner("normalize_sources")
            configured = owner("_normalization_config").normalization_config(view.config)
            record = SourceView(root).manifest().get(source_id)
            if record is None:
                refuse("caller_source_missing_from_inventory", "ONBOARDING_CHECK_FAILED")
            if configured["adapters"] and record.get("kind") not in owner("_normalization_config").NATIVE_SOURCE_KINDS:
                refuse("caller_normalizer_requires_explicit_qualification")
            args = normalizer.parse_args(["--project-root", str(root), "--source-id", source_id, "--format", "json"])
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                if normalizer.run_normalization(args):
                    refuse("caller_normalization_failed", "ONBOARDING_CHECK_FAILED")
            source_view = SourceView(root)
            rows = inspect_sources(source_view, source_ids=[source_id])
            if len(rows) != 1 or rows[0]["usability"] not in ({"usable"} if needs_complete else {"usable", "partial"}):
                refuse("caller_source_not_usable", "ONBOARDING_CHECK_FAILED")
            row = rows[0]
            # Discovery/link metadata is never accepted as requested full text.
            if row.get("kind") == "link" or needs_complete and not row["complete"]:
                refuse("caller_full_text_not_demonstrated", "ONBOARDING_CHECK_FAILED")
            source_view.finish()
            owner("run_controller").require_caller_context(
                root, owner("run_controller").load_run_state(root, run_id), agent_id
            )
            requests = owner("source_requests")
            requests.check_fulfill_scope(
                request,
                source_id,
                requests.source_provenance_scope(root, view.config, source_id),
                {},
                require_scope=True,
            )
            args = requests.parse_args(
                [
                    "--project-root",
                    str(root),
                    "fulfill",
                    "--request-id",
                    request_id,
                    "--source-id",
                    source_id,
                    "--require-scope",
                    "--format",
                    "json",
                ]
            )
            fulfilled = requests.run_fulfill(args)
            source_view.finish()
            reopened, pending_questions = [], []
            q = owner("question_status")
            current = {r["slug"]: r for r in q.collect_questions(q.questions_directory(root, view.config))}
            for slug in request["question_slugs"]:
                if current[slug]["status"] == "blocked":
                    try:
                        reopened.append(
                            owner("question_resolve").run_reopen(
                                root,
                                slug=slug,
                                agent_id=agent_id,
                                source_id=[source_id],
                                request_id=[request_id],
                                require_decisive_scope=True,
                            )
                        )
                    except Exception as error:
                        if getattr(error, "error_code", None) not in {
                            "QUESTION_BLOCKERS_UNFULFILLED",
                            "SOURCE_NOT_NORMALIZED",
                            "REQUEST_SCOPE_MISMATCH",
                            "REQUEST_SCOPE_UNDECIDED",
                        }:
                            raise
                        pending_questions.append({"slug": slug, "reason": error.error_code, "status": "blocked"})
            source_view.finish()
            event(
                root,
                run_id,
                agent_id,
                "acquisition_completed",
                {
                    "observation": "source_verified",
                    "request_id": request_id,
                    "source_id": source_id,
                    "normalized_sha256": row.get("normalized_sha256"),
                    "question_slugs": request["question_slugs"],
                },
            )
            return {
                "schema_version": "evidence-caller-ingestion/v1",
                "source": row,
                "request": fulfilled,
                "reopened": reopened,
                "pending_questions": pending_questions,
                "semantic_adequacy": "not_evaluated",
                "evidence_accepted": False,
                "research_complete": False,
            }
        except (Exception, SystemExit):
            event(
                root,
                run_id,
                agent_id,
                "verification_failed",
                {"request_id": request_id, "source_id": source_id, "reason": "source_ingestion_incomplete"},
            )
            raise
