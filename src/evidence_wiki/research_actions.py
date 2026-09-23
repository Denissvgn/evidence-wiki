"""Read-only next steps over canonical state, with no implicit parent orders."""

from __future__ import annotations

import sys
from datetime import timedelta

from .agent_resources import resource_document
from .pack_discovery import owner
from .planning_contracts import digest
from .research_contracts import ACTION, GUIDANCE, checked
from .research_observation import ResearchObservation, request_row


def action(view, operation, *, slugs=(), argv=(), reasons=(), parameters=None, guide="research"):
    document = resource_document("guide/" + guide + "/v1")
    value = {
        "schema_version": ACTION,
        "operation": operation,
        "basis_id": view.basis["workspace_revision"],
        "policy_id": view.basis["policy_id"],
        "question_slugs": list(slugs),
        "parent_order_id": None,
        "host_implementation": view.basis["implementation"].removeprefix("sha256:"),
        "preconditions": ["current_workspace", "current_controls", "current_ownership", "task_authority_required"],
        "postconditions": ["owning_operation_checked", "unresolved_evidence_retained"],
        "argv": list(argv),
        "parameters": parameters or {},
        "guides": [{key: document[key] for key in ("id", "sha256", "version")}],
        "reasons": list(reasons),
        "authorized": False,
        "evidence_accepted": False,
        "expires_at": (view.now + timedelta(seconds=60)).isoformat(),
    }
    value["action_id"] = owner("_evidence_revision").content_id(ACTION, value)
    return checked(value)


def guidance(target, *, agent_id=None, run_id=None):
    view = ResearchObservation(target, run_id=run_id)
    actions, gaps = [], []
    package = lambda *args: [sys.executable, "-B", "-m", "evidence_wiki", *args]
    script = lambda stem, *args: [
        sys.executable,
        "-B",
        str(view.root / "scripts" / (stem + ".py")),
        "--project-root",
        str(view.root),
        *args,
    ]
    actor = agent_id or "CALLER_ID"
    add = lambda operation, **options: actions.append(action(view, operation, **options))
    if view.mapping["state"] == "observed":
        gaps.extend(view.mapping["result"]["gaps"])
    else:
        gaps.append("original_question_accounting_unavailable")
    if view.controls["state"] != "observed":
        gaps.append("instruction_or_policy_binding_unavailable")
    if view.managed:
        add(
            "managed_resume",
            reasons=["active_managed_session_preserved"],
            parameters={
                "sessions": [
                    {"orchestration_id": row["orchestration_id"], "action_id": row["action_id"]} for row in view.managed
                ],
                "route": "Use the existing host protocol; workers may only inspect their issued order and its postconditions.",
            },
        )
    elif not view.status.get("smoke", {}).get("ok") or view.controls["state"] != "observed":
        add(
            "repair",
            argv=package(
                *(("agent", "inspect") if not view.integrations_qualified else ("doctor",)),
                "--target",
                str(view.root),
                "--format",
                "json",
            ),
            reasons=[view.controls.get("reason", "workspace_or_controls_need_attention")],
            parameters={
                "remediation": "Inspect the named control/authority gap. Restore pinned instructions or preview the owning upgrade at a safe boundary; never force a running workspace or weaken its policy."
            },
        )
    else:
        controller = view.status.get("run_controller", {})
        if view.run is None or view.run["state"]["current"] in owner("run_controller").TERMINAL_STATES:
            if any(q["status"] == "answered" for q in view.questions):
                add(
                    "release",
                    argv=package("agent", "research-export", "--target", str(view.root)),
                    reasons=["existing_answers_require_current_release"],
                    guide="strict-evidence" if view.policy else "research",
                )
            add(
                "start",
                argv=package("agent", "start", "--target", str(view.root), "--agent-id", actor),
                reasons=["new_caller_run_required"],
            )
        elif view.run.get("caller_context") is None:
            add(
                "resume",
                reasons=["legacy_run_not_rebound"],
                parameters={
                    "run_id": view.run["run_id"],
                    "route": "Inspect through run_controller; preserve the original run and use a new caller-bound run at a safe boundary.",
                },
            )
        elif view.run["caller_context"] != view.controls.get("result"):
            add(
                "stop",
                reasons=["run_instruction_policy_or_implementation_changed"],
                parameters={"run_id": view.run["run_id"]},
            )
        elif controller.get("stale") or view.run["agent_id"] != agent_id:
            add(
                "resume",
                reasons=["run_owner_or_liveness_requires_inspection"],
                parameters={
                    "run_id": view.run["run_id"],
                    "owner": view.run["agent_id"],
                    "stale": controller.get("stale"),
                    "adoption": "Explicit run_controller adopt with a finite positive stale threshold; claim transfer is separate.",
                },
            )
        elif view.status.get("readiness", {}).get("budget_state", {}).get("should_stop"):
            add("stop", reasons=["artifact_run_budget_exhausted"], parameters={"run_id": view.run["run_id"]})
        else:
            rid = view.run["run_id"]
            if view.run["state"]["current"] == "initialized":
                add(
                    "resume",
                    argv=script(
                        "run_controller",
                        "transition",
                        "--run-id",
                        rid,
                        "--agent-id",
                        actor,
                        "--to-state",
                        "planned",
                        "--format",
                        "json",
                    ),
                    reasons=["initial_phase_requires_planning"],
                    parameters={"allowed_next_states": view.run["state"]["allowed_next_states"]},
                )
            add(
                "heartbeat",
                argv=package("agent", "heartbeat", "--target", str(view.root), "--run-id", rid, "--agent-id", actor),
                reasons=["maintain_current_run_lease"],
                parameters={
                    "heartbeat_seconds": min(60, max(1, int(controller.get("stale_threshold_hours", 4) * 1200)))
                },
            )
            held = [q for q in view.questions if q["status"] == "in_progress" and q.get("claimed_by") == agent_id]
            if held:
                q = held[0]
                add(
                    "retrieve",
                    slugs=[q["slug"]],
                    argv=script("query_index", q["question"], "--scope", "normalized", "--format", "json"),
                    reasons=["held_question_needs_evidence_or_review"],
                )
                add(
                    "resolve",
                    slugs=[q["slug"]],
                    parameters={
                        "owner": "question_resolve",
                        "outcomes": ["answer", "block", "defer", "reject", "release"],
                        "acceptance": "Strict answer requires current coverage, grounding and authenticated review; never use an allow-unclaimed/uncited bypass.",
                    },
                    reasons=["resolve_only_the_held_claim"],
                )
            else:
                other_claims = [
                    q for q in view.questions if q["status"] == "in_progress" and q.get("claimed_by") != agent_id
                ]
                if other_claims:
                    add(
                        "resume",
                        slugs=[q["slug"] for q in other_claims],
                        reasons=["questions_owned_by_other_callers"],
                        parameters={
                            "owners": [
                                {"slug": q["slug"], "agent_id": q.get("claimed_by"), "claimed_at": q.get("claimed_at")}
                                for q in other_claims
                            ],
                            "transfer": "No automatic claim transfer; inspect liveness and use the existing explicit finite-threshold owner.",
                        },
                    )
                opened = sorted(
                    [q for q in view.questions if q["status"] == "open"],
                    key=lambda q: ({"high": 0, "medium": 1, "low": 2}.get(q["priority"], 3), q["slug"]),
                )
                if opened:
                    q = opened[0]
                    add(
                        "claim",
                        slugs=[q["slug"]],
                        argv=script(
                            "question_claim", "claim", "--slug", q["slug"], "--agent-id", actor, "--format", "json"
                        ),
                        reasons=["open_unclaimed_question"],
                    )
            for request in view.requests[:32]:
                slugs = request.get("question_slugs") or []
                if request["status"] == "open":
                    add(
                        "discover",
                        slugs=slugs,
                        argv=script(
                            "source_requests", "plan-fetch", "--request-id", request["request_id"], "--format", "json"
                        ),
                        reasons=["source_request_open"],
                        parameters={
                            "request_id": request["request_id"],
                            "discovery": "Use configured discovery, then explicitly select candidates; metadata alone cannot fulfill full-text requests.",
                        },
                        guide="acquisition",
                    )
                elif request["status"] == "fulfilled" and any(
                    q["slug"] in slugs and q["status"] == "blocked" for q in view.questions
                ):
                    add(
                        "ingest",
                        slugs=slugs,
                        argv=package(
                            "agent",
                            "ingest",
                            "--target",
                            str(view.root),
                            "--run-id",
                            rid,
                            "--agent-id",
                            actor,
                            "--request-id",
                            request["request_id"],
                            "--source-id",
                            request["source_id"],
                        ),
                        reasons=["verify_current_source_before_reopen"],
                        guide="acquisition",
                    )
            if len(view.requests) > 32:
                gaps.append("action_request_selection_bounded_first_32")
            if view.policy:
                add(
                    "review",
                    argv=package("strict", "check", "--target", str(view.root)),
                    reasons=["current_independent_review_required"],
                    guide="strict-evidence",
                )
            if view.computation is not None:
                add(
                    "compute",
                    argv=package(
                        "computation",
                        "check",
                        "--target",
                        str(view.root),
                        "--as-of",
                        (view.config.get("computation", {}).get("clock", {}).get("as_of") or view.now.isoformat()),
                    ),
                    reasons=["recompute_current_inputs_and_invariants"],
                    guide="computation",
                )
                for row in (view.computation.get("clock") or {}).get("schedules", [])[:8]:
                    if row.get("state") in {"due", "overdue"}:
                        add(
                            "inspect_schedule",
                            reasons=["due_action_requires_explicit_current_result"],
                            parameters={
                                "cadence_id": row["id"],
                                "occurrence_id": row.get("occurrence_id"),
                                "result_id": view.computation.get("result_id"),
                                "definition_id": view.computation.get("definition_id"),
                                "dispatch_authorized": False,
                            },
                            guide="computation",
                        )
            add(
                "release",
                argv=package("agent", "research-export", "--target", str(view.root)),
                reasons=["completion_requires_current_original_accounting_and_release"],
                guide="strict-evidence" if view.policy else "research",
            )
    view.finish()
    result = {
        "schema_version": GUIDANCE,
        "target": str(view.root),
        "basis": view.basis,
        "observed_at": view.now.isoformat(),
        "cache": {"used": False, "written": False, "freshness": "current_owner_reads_bracketed_by_revision"},
        "status": view.status,
        "questions": view.questions,
        "requests": [request_row(row) for row in view.requests],
        "run": view.run,
        "setup": view.setup,
        "strict": view.strict_summary(),
        "computation": view.computation,
        "actions": actions,
        "gaps": gaps,
        "actions_executed": False,
        "research_complete": False,
        "limitations": [
            "Advice does not execute the proposed actions; it runs current read-only owner checks, including configured arithmetic. Refresh after every mutation and revalidate each owner precondition.",
            "Agent IDs and local run bindings coordinate callers; they do not authenticate independent review or establish host isolation.",
            "Status, stored receipts and transport acknowledgments do not confirm claims or research completion.",
        ],
    }
    # Return the current binding without accepting caller-supplied success fields.
    result["basis"]["guidance_inputs_id"] = digest(
        {"revision": view.basis["workspace_revision"], "controls": view.basis["controls"]}
    )
    return checked(result)
