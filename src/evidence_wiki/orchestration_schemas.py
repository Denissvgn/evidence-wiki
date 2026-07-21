"""Public JSON Schemas for durable orchestration protocol artifacts.

The package host validates runner-facing data before use, while the deployed
workspace controller owns the durable state machine.  These schemas publish
the shared wire contract without importing workspace code from a deployed
project.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .orchestration import (
    ATTEMPT_ERROR_CODES,
    ORCHESTRATION_ATTEMPT_SCHEMA,
    ORCHESTRATION_RESULT_SCHEMA,
    ORCHESTRATION_SESSION_SCHEMA_VERSION,
    ORCHESTRATION_WORK_ORDER_SCHEMA_VERSION,
)

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

STABLE_ID_PATTERN = r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._-]{0,159}$"
SCOPED_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
SKILL_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
WORKSPACE_RELATIVE_PATH_PATTERN = (
    r"^(?![A-Za-z]:[\\/])(?![\\/])(?!.*(?:^|/)\.\.(?:/|$))(?!.*\\)(?!.*\u0000).{1,512}$"
)
RESULT_ARTIFACT_PATH_PATTERN = (
    r"^(?![A-Za-z]:[\\/])(?![\\/])(?!.*(?:^|/)\.\.(?:/|$))(?!.*\\)(?!.*\u0000)"
    r"(?!(?:\./)*[Rr][Uu][Nn][Ss][ .]*/[Oo][Rr][Cc][Hh][Ee][Ss][Tt][Rr][Aa][Tt][Ii][Oo][Nn][Ss][ .]*(?:/|$))"
    r".{1,512}$"
)

SESSION_STATUSES = ("active", "paused", "complete", "blocked_on_sources", "no_ship", "failed")
SESSION_PHASES = (
    "planning",
    "research",
    "discovery",
    "candidate_review",
    "acquisition",
    "verification",
    "complete",
    "blocked_on_sources",
    "no_ship",
    "failed",
    "paused",
)
WORK_ORDER_PHASES = ("research", "discovery", "candidate_review", "acquisition", "verification")
AGENT_ID_PATTERN = r"^[^\u0000-\u001F\u007F]*\S[^\u0000-\u001F\u007F]*$"


def _timestamp_schema() -> dict[str, Any]:
    return {"type": "string", "format": "date-time"}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _provider_policy_schema() -> dict[str, Any]:
    phase_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["enabled", "providers"],
        "properties": {
            "enabled": {"type": "boolean"},
            "providers": {
                "type": "array",
                "maxItems": 64,
                "uniqueItems": True,
                "items": {"type": "string", "pattern": SCOPED_ID_PATTERN},
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["discovery", "acquisition"],
        "properties": {
            "discovery": deepcopy(phase_schema),
            "acquisition": deepcopy(phase_schema),
        },
    }


def _result_schema() -> dict[str, Any]:
    """Return the strict public wire schema for an orchestration result.

    ``ORCHESTRATION_RESULT_SCHEMA`` intentionally stays within the smaller
    structured-output subset accepted by managed runner CLIs.  The public
    protocol contract must additionally describe the bounds and portable path
    rules enforced by the host after generation, otherwise an external agent
    can validate a result that EvidenceWiki will subsequently refuse.
    """

    schema = deepcopy(ORCHESTRATION_RESULT_SCHEMA)
    properties = schema["properties"]
    properties["action_id"] = {"type": "string", "pattern": STABLE_ID_PATTERN}
    properties["summary"] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 4000,
        "pattern": r"\S",
    }
    properties["artifacts"] = {
        "type": "array",
        "maxItems": 256,
        "uniqueItems": True,
        "items": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
            "pattern": RESULT_ARTIFACT_PATH_PATTERN,
        },
    }
    return schema


def _attempt_schema() -> dict[str, Any]:
    """Return the complete public contract for a retained host attempt."""

    schema = deepcopy(ORCHESTRATION_ATTEMPT_SCHEMA)
    properties = schema["properties"]
    for field_name in ("orchestration_id", "attempt_id", "action_id"):
        properties[field_name] = {"type": "string", "pattern": SCOPED_ID_PATTERN}
    properties["run_id"] = _nullable(
        {"type": "string", "pattern": SCOPED_ID_PATTERN}
    )
    for field_name in ("started_at", "updated_at"):
        properties[field_name] = {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "pattern": r"\S",
        }
    properties["work_order_identity"] = {
        "type": "string",
        "pattern": SHA256_PATTERN,
    }
    properties["result_digest"] = _nullable(
        {"type": "string", "pattern": SHA256_PATTERN}
    )
    properties["error_code"] = {"enum": [None, *sorted(ATTEMPT_ERROR_CODES)]}

    def status_requires(statuses: list[str], required_properties: dict[str, Any]) -> dict[str, Any]:
        return {
            "if": {
                "properties": {"status": {"enum": statuses}},
                "required": ["status"],
            },
            "then": {"properties": required_properties},
        }

    schema["allOf"] = [
        status_requires(
            ["running"],
            {"error_code": {"type": "null"}},
        ),
        status_requires(
            ["result_staged", "submitted"],
            {
                "result_digest": {"type": "string", "pattern": SHA256_PATTERN},
                "error_code": {"type": "null"},
            },
        ),
        *[
            status_requires(
                [status],
                {"error_code": {"type": "string", "const": error_code}},
            )
            for status, error_code in (
                ("runner_failed", "RUNNER_FAILED"),
                ("timed_out", "RUNNER_TIMEOUT"),
                ("interrupted", "RUNNER_INTERRUPTED"),
                ("control_tampered", "CONTROL_ARTIFACT_TAMPERED"),
                ("repair_acknowledged", "CONTROL_ARTIFACT_TAMPERED"),
            )
        ],
    ]
    return schema


ORCHESTRATION_SESSION_SCHEMA: dict[str, Any] = {
    "$schema": JSON_SCHEMA_DIALECT,
    "title": "EvidenceWiki orchestration session",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "artifact_type",
        "orchestration_id",
        "started_at",
        "updated_at",
        "completed_at",
        "agent_id",
        "handoff",
        "status",
        "phase",
        "verdict",
        "pause_reason",
        "pending_action_id",
        # 0.2.0 sessions predate pending_submission and recovery. Current
        # writers always emit them, but schema 1.0 readers accept their absence.
        "last_completed_action_id",
        "active_run_id",
        "child_run_ids",
        "action_count",
        "completed_action_count",
        "window_action_count",
        "window_started_at",
        "limits",
        "provider_policy",
        "failure_records",
    ],
    "properties": {
        "schema_version": {
            "type": "string",
            "enum": [ORCHESTRATION_SESSION_SCHEMA_VERSION],
        },
        "artifact_type": {"type": "string", "const": "orchestration_session"},
        "orchestration_id": {"type": "string", "pattern": STABLE_ID_PATTERN},
        "started_at": {"$ref": "#/$defs/timestamp"},
        "updated_at": {"$ref": "#/$defs/timestamp"},
        "completed_at": _nullable({"$ref": "#/$defs/timestamp"}),
        "agent_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160,
            "pattern": AGENT_ID_PATTERN,
        },
        "handoff": _nullable({"$ref": "#/$defs/handoff"}),
        "status": {"type": "string", "enum": list(SESSION_STATUSES)},
        "phase": {"type": "string", "enum": list(SESSION_PHASES)},
        "verdict": {
            "enum": [None, "paused", "complete", "blocked_on_sources", "no_ship", "failed"],
        },
        "pause_reason": _nullable({"type": "string"}),
        "pending_action_id": _nullable({"$ref": "#/$defs/stable_id"}),
        "pending_submission": _nullable({"$ref": "#/$defs/pending_submission"}),
        # Optional for read compatibility with sessions created before the
        # trusted-static-input binding was added during schema version 1.0.
        "pending_trusted_static_inputs": _nullable(
            {"$ref": "#/$defs/pending_trusted_static_inputs"}
        ),
        "recovery": _nullable({"$ref": "#/$defs/recovery"}),
        "last_completed_action_id": _nullable({"$ref": "#/$defs/stable_id"}),
        "active_run_id": _nullable({"$ref": "#/$defs/stable_id"}),
        "child_run_ids": {
            "type": "array",
            "uniqueItems": True,
            "items": {"$ref": "#/$defs/stable_id"},
        },
        "action_count": {"type": "integer", "minimum": 0},
        "completed_action_count": {"type": "integer", "minimum": 0},
        "window_action_count": {"type": "integer", "minimum": 0},
        "window_started_at": {"$ref": "#/$defs/timestamp"},
        "limits": {"$ref": "#/$defs/limits"},
        "provider_policy": {"$ref": "#/$defs/provider_policy"},
        "failure_records": {
            "type": "array",
            "items": {"$ref": "#/$defs/failure_record"},
        },
    },
    "$defs": {
        "timestamp": _timestamp_schema(),
        "stable_id": {"type": "string", "pattern": STABLE_ID_PATTERN},
        "handoff": {
            "type": "object",
            "additionalProperties": False,
            "minProperties": 1,
            "properties": {
                "task_id": {"type": "string", "minLength": 1, "pattern": r".*\S.*"},
                "requested_by": {"type": "string", "minLength": 1, "pattern": r".*\S.*"},
                "chain_run_id": {"type": "string", "minLength": 1, "pattern": r".*\S.*"},
            },
        },
        "provider_policy": _provider_policy_schema(),
        "limits": {
            "type": "object",
            "additionalProperties": False,
            "required": ["max_actions", "action_timeout_seconds", "total_timeout_seconds"],
            "properties": {
                "max_actions": {"type": "integer", "minimum": 1},
                "action_timeout_seconds": {"type": "integer", "minimum": 1},
                "total_timeout_seconds": {"type": "integer", "minimum": 1},
            },
        },
        "result": _result_schema(),
        "pending_submission": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "action_id",
                "accepted_at",
                "result",
                "result_digest",
                "next_phase",
                "completion_reason",
            ],
            "properties": {
                "action_id": {"$ref": "#/$defs/stable_id"},
                "accepted_at": {"$ref": "#/$defs/timestamp"},
                "result": {"$ref": "#/$defs/result"},
                "result_digest": {"type": "string", "pattern": SHA256_PATTERN},
                "next_phase": {"enum": [None, *SESSION_PHASES]},
                "completion_reason": _nullable({"type": "string"}),
            },
        },
        "pending_trusted_static_inputs": {
            "type": "object",
            "additionalProperties": False,
            "required": ["action_id", "fingerprint", "entry_count", "total_bytes"],
            "properties": {
                "action_id": {"$ref": "#/$defs/stable_id"},
                "fingerprint": {"type": "string", "pattern": SHA256_PATTERN},
                "entry_count": {"type": "integer", "minimum": 0, "maximum": 10000},
                "total_bytes": {"type": "integer", "minimum": 0, "maximum": 33554432},
            },
        },
        "recovery": {
            "type": "object",
            "additionalProperties": False,
            "required": ["state", "action_id", "attempt", "reason_code", "recorded_at"],
            "properties": {
                "state": {
                    "type": "string",
                    "enum": ["none", "reconcile_required", "finalizing_submission"],
                },
                "action_id": _nullable({"$ref": "#/$defs/stable_id"}),
                "attempt": _nullable({"type": "integer", "minimum": 1}),
                "reason_code": _nullable({"type": "string"}),
                "recorded_at": _nullable({"$ref": "#/$defs/timestamp"}),
            },
        },
        "failure_record": {
            "type": "object",
            "additionalProperties": False,
            "required": ["recorded_at", "action_id", "summary"],
            "properties": {
                "recorded_at": {"$ref": "#/$defs/timestamp"},
                "action_id": {"$ref": "#/$defs/stable_id"},
                "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
        },
    },
}


def _raw_tree_snapshot_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["algorithm", "file_count", "total_bytes", "fingerprint"],
        "properties": {
            "algorithm": {"type": "string", "const": "sha256-content-v1"},
            "file_count": {"type": "integer", "minimum": 0, "maximum": 10000},
            "total_bytes": {"type": "integer", "minimum": 0, "maximum": 2147483648},
            "fingerprint": {"type": "string", "pattern": SHA256_PATTERN},
        },
    }


def _postcondition_schemas() -> list[dict[str, Any]]:
    def check(name: str, properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
        fields = {"check": {"type": "string", "const": name}, **(properties or {})}
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["check", *(required or [])],
            "properties": fields,
        }

    manifest_guard = {
        "manifest_records_before": {"type": "integer", "minimum": 0},
        "manifest_digest_before": _nullable({"type": "string", "pattern": SHA256_PATTERN}),
    }
    return [
        check(
            "workspace_readiness_changed",
            {
                "allowed_verdicts": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "enum": ["in_progress", "blocked_on_sources", "complete"],
                    },
                },
            },
            ["allowed_verdicts"],
        ),
        check("child_run_state", {"expected": {"type": "string", "const": "answering"}}, ["expected"]),
        check(
            "request_scoped_candidates_increased",
            {
                "before": {"type": "integer", "minimum": 0, "maximum": 256},
            },
            ["before"],
        ),
        check("discovery_never_fetches", deepcopy(manifest_guard), list(manifest_guard)),
        check("raw_tree_unchanged", {"before": _raw_tree_snapshot_schema()}, ["before"]),
        check(
            "selected_candidate_for_request",
            {
                "selected_before": {"type": "integer", "minimum": 0, "maximum": 256},
            },
            ["selected_before"],
        ),
        check("selection_does_not_fetch", deepcopy(manifest_guard), list(manifest_guard)),
        check("request_fulfilled_with_normalized_source"),
        check("linked_blocked_questions_reopened"),
        check(
            "manifest_records_increased",
            {"before": {"type": "integer", "minimum": 0}},
            ["before"],
        ),
        check(
            "controller_integrity_baseline",
            {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "pattern": (
                        r"^runs/orchestrations/(?![^/]*\.\.)[A-Za-z0-9][A-Za-z0-9._-]{0,159}/"
                        r"trusted-inputs/(?![^/]*\.\.)[A-Za-z0-9][A-Za-z0-9._-]{0,159}"
                        r"-scope-baseline\.json$"
                    ),
                },
                "fingerprint": {"type": "string", "pattern": SHA256_PATTERN},
                "field_count": {"type": "integer", "minimum": 1, "maximum": 12},
                "entry_count": {"type": "integer", "minimum": 0, "maximum": 120000},
            },
            ["path", "fingerprint", "field_count", "entry_count"],
        ),
        check(
            "fresh_verification_bundle",
            {
                "paths": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 256,
                    "uniqueItems": True,
                    "items": {"$ref": "#/$defs/workspace_relative_path"},
                },
                "before": {
                    "type": "object",
                    "propertyNames": {"pattern": WORKSPACE_RELATIVE_PATH_PATTERN},
                    "additionalProperties": _nullable(
                        {"type": "string", "pattern": SHA256_PATTERN}
                    ),
                },
            },
            ["paths", "before"],
        ),
        check(
            "publication_readiness",
            {"expected": {"type": "string", "const": "ship"}},
            ["expected"],
        ),
    ]


ORCHESTRATION_WORK_ORDER_SCHEMA: dict[str, Any] = {
    "$schema": JSON_SCHEMA_DIALECT,
    "title": "EvidenceWiki orchestration work order",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "artifact_type",
        "orchestration_id",
        "action_id",
        "issued_at",
        "phase",
        "skill",
        "run_id",
        "agent_id",
        "scope",
        "provider_policy",
        "budgets",
        "inputs",
        "required_postconditions",
        "lease",
    ],
    "properties": {
        "schema_version": {
            "type": "string",
            "enum": [ORCHESTRATION_WORK_ORDER_SCHEMA_VERSION],
        },
        "artifact_type": {"type": "string", "const": "orchestration_work_order"},
        "orchestration_id": {"$ref": "#/$defs/stable_id"},
        "action_id": {"$ref": "#/$defs/stable_id"},
        "issued_at": {"$ref": "#/$defs/timestamp"},
        "phase": {"type": "string", "enum": list(WORK_ORDER_PHASES)},
        "skill": {"type": "string", "pattern": SKILL_ID_PATTERN},
        "run_id": _nullable({"$ref": "#/$defs/stable_id"}),
        "agent_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160,
            "pattern": AGENT_ID_PATTERN,
        },
        "scope": {"$ref": "#/$defs/scope"},
        "provider_policy": {"$ref": "#/$defs/provider_policy"},
        "budgets": {"$ref": "#/$defs/budgets"},
        "inputs": {
            "type": "array",
            "maxItems": 256,
            "uniqueItems": True,
            "items": {"$ref": "#/$defs/workspace_relative_path"},
        },
        "required_postconditions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {"oneOf": _postcondition_schemas()},
        },
        "lease": {"$ref": "#/$defs/lease"},
    },
    "allOf": [
        {
            "if": {
                "properties": {
                    "phase": {
                        "enum": ["research", "discovery", "candidate_review", "acquisition"]
                    }
                },
                "required": ["phase"],
            },
            "then": {
                "properties": {
                    "required_postconditions": {
                        "contains": {
                            "type": "object",
                            "properties": {
                                "check": {
                                    "type": "string",
                                    "const": "controller_integrity_baseline",
                                }
                            },
                            "required": ["check"],
                        },
                        "minContains": 1,
                        "maxContains": 1,
                    }
                }
            },
        }
    ],
    "$defs": {
        "timestamp": _timestamp_schema(),
        "stable_id": {"type": "string", "pattern": STABLE_ID_PATTERN},
        "scoped_id": {"type": "string", "pattern": SCOPED_ID_PATTERN},
        "workspace_relative_path": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
            "pattern": WORKSPACE_RELATIVE_PATH_PATTERN,
        },
        "scope": {
            "type": "object",
            "additionalProperties": False,
            "required": ["question_slugs", "request_ids", "candidate_ids"],
            "properties": {
                field: {
                    "type": "array",
                    "maxItems": 256,
                    "uniqueItems": True,
                    "items": {"$ref": "#/$defs/scoped_id"},
                }
                for field in ("question_slugs", "request_ids", "candidate_ids")
            },
        },
        "provider_policy": _provider_policy_schema(),
        "budgets": {
            "type": "object",
            "required": ["action_timeout_seconds"],
            "propertyNames": {"pattern": r"^[a-z][a-z0-9_]*$"},
            "additionalProperties": {"type": "integer", "minimum": 0},
        },
        "lease": {
            "type": "object",
            "additionalProperties": False,
            "required": ["duration_seconds", "expires_at", "attempt"],
            "properties": {
                "duration_seconds": {"type": "integer", "minimum": 1},
                "expires_at": {"$ref": "#/$defs/timestamp"},
                "attempt": {"type": "integer", "minimum": 1},
            },
        },
    },
}


def public_orchestration_schema_documents() -> dict[str, dict[str, Any]]:
    """Return caller-owned schema documents for the public contract payload."""

    return {
        "orchestration_session": deepcopy(ORCHESTRATION_SESSION_SCHEMA),
        "orchestration_work_order": deepcopy(ORCHESTRATION_WORK_ORDER_SCHEMA),
        "orchestration_result": {
            "$schema": JSON_SCHEMA_DIALECT,
            "title": "EvidenceWiki orchestration result",
            **_result_schema(),
        },
        "orchestration_attempt": {
            "$schema": JSON_SCHEMA_DIALECT,
            "title": "EvidenceWiki orchestration attempt",
            **_attempt_schema(),
        },
    }
