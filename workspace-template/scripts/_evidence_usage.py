#!/usr/bin/env python3
"""Current use authority and immutable, sanitized revisions in host-owned state."""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _assessment_contract import assessment_document, refresh_document
from _evidence_authority import (
    EvidenceInvalid,
    authority_basis,
    bounded_list,
    digest,
    exact_object,
    load_trust,
    name,
    timestamp,
    verify_attestation,
)
from _evidence_revision import canonical_bytes, content_id
from _host_evidence_store import STATE_ENV, locked_state, read_state, write_state
from _publication_context import captured_view
from _record_artifacts import artifact_path, closure_identity, json_document, validate_file_bounds
from _temporal_contract import authenticate_availability, availability_receipt
from _workspace_module_loader import load_workspace_module

STATE_SCHEMA = "evidence-usage-state/v1"
COMMAND_SCHEMA = "evidence-usage-command/v1"
GRANT_SCHEMA = "evidence-usage-grant/v1"
SCRUB_SCHEMA = "evidence-scrub-receipt/v1"
SOURCE_SCHEMA = "evidence-source-revision/v1"
MAX_EVENTS = 10000
MAX_NODES = 4096
MAX_LINEAGE_DEPTH = 64
USES = frozenset({"retrieval", "training", "export"})
CLAIM_KEYS = frozenset({"usage_revision_id", "retrieval_eligible", "training_eligible", "export_eligible", "usage_policy"})


def workspace_binding(root: Path) -> str:
    return content_id("evidence-host-workspace/v1", {"root": str(root.resolve())})


def configured(config: dict[str, Any]) -> bool:
    return "evidence_usage" in config or bool(os.environ.get(STATE_ENV))


def selection(config: dict[str, Any]) -> str:
    return name(exact_object(config.get("evidence_usage"), {"state_id"})["state_id"])


def unique_names(value: Any, *, minimum: int = 1) -> list[str]:
    values = [name(item) for item in bounded_list(value, minimum=minimum)]
    if len(set(values)) != len(values):
        raise EvidenceInvalid("duplicate_usage_identity")
    return values


def references(value: Any) -> list[str]:
    values = [digest(item) for item in bounded_list(value)]
    if len(set(values)) != len(values):
        raise EvidenceInvalid("duplicate_lineage_reference")
    return values


def authenticated(envelope: Any, role: str, trust: dict[str, Any], now: datetime) -> dict[str, Any]:
    result = verify_attestation(envelope, role, trust, now)
    if not result["authenticated"]:
        raise EvidenceInvalid(result["reason"])
    return result


def validate_grant(envelope: Any, source: str, revision: str) -> dict[str, Any]:
    exact_object(envelope, {"payload", "authentication"})
    grant = exact_object(envelope["payload"], {
        "schema_version", "source_id", "source_revision", "permissions", "purposes",
        "consumers", "not_before", "expires_at", "redaction_policy", "retention",
    })
    if grant["schema_version"] != GRANT_SCHEMA or grant["source_id"] != source or grant["source_revision"] != revision:
        raise EvidenceInvalid("usage_revision_binding_mismatch")
    permissions = exact_object(grant["permissions"], set(USES))
    if any(type(value) is not bool for value in permissions.values()):
        raise EvidenceInvalid("usage_requires_explicit_booleans")
    unique_names(grant["purposes"])
    unique_names(grant["consumers"])
    if timestamp(grant["not_before"]) >= timestamp(grant["expires_at"]):
        raise EvidenceInvalid("invalid_usage_interval")
    policy = exact_object(grant["redaction_policy"], {"id", "revision"})
    name(policy["id"])
    name(policy["revision"])
    if grant["retention"] != "host-managed":
        raise EvidenceInvalid("unsupported_retention_contract")
    return grant


def validate_scrub(envelope: Any, revision: str, grant: dict[str, Any]) -> dict[str, Any]:
    exact_object(envelope, {"payload", "authentication"})
    scrub = exact_object(envelope["payload"], {
        "schema_version", "sanitized_revision", "redaction_policy", "tool", "outcome", "completed_at",
    })
    if (scrub["schema_version"] != SCRUB_SCHEMA or scrub["sanitized_revision"] != revision
            or scrub["redaction_policy"] != grant["redaction_policy"] or scrub["outcome"] != "passed"):
        raise EvidenceInvalid("scrub_revision_binding_mismatch")
    tool = exact_object(scrub["tool"], {"name", "version"})
    name(tool["name"])
    name(tool["version"])
    timestamp(scrub["completed_at"])
    return scrub


def source_descriptor(files: dict[str, bytes]) -> dict[str, Any]:
    validate_file_bounds(files)
    if "source-record.json" not in files:
        raise EvidenceInvalid("source_revision_descriptor_missing")
    source = exact_object(json_document(files["source-record.json"]), {
        "schema_version", "source_id", "parents", "normalized_path", "evidence_root", "temporal",
    })
    if source["schema_version"] != SOURCE_SCHEMA:
        raise EvidenceInvalid("source_revision_contract_unsupported")
    name(source["source_id"])
    references(source["parents"])
    normalized = source["normalized_path"]
    if normalized is not None and (artifact_path(normalized) not in files or not normalized.endswith(".md")):
        raise EvidenceInvalid("normalized_revision_missing")
    evidence_root = source["evidence_root"]
    if evidence_root is not None:
        artifact_path(evidence_root)
        if not any(path.startswith(evidence_root + "/") for path in files):
            raise EvidenceInvalid("evidence_revision_missing")
    if not isinstance(source["temporal"], dict):
        raise EvidenceInvalid("invalid_temporal_claims")
    return source


def decode_files(encoded: Any) -> dict[str, bytes]:
    if not isinstance(encoded, dict) or not 1 <= len(encoded) <= 256:
        raise EvidenceInvalid("artifact_bound_exceeded")
    files: dict[str, bytes] = {}
    total = 0
    for path, value in encoded.items():
        artifact_path(path)
        if not isinstance(value, str) or len(value) > 24 * 1024 * 1024:
            raise EvidenceInvalid("artifact_bound_exceeded")
        total += len(value)
        if total > 64 * 1024 * 1024:
            raise EvidenceInvalid("artifact_bound_exceeded")
        try:
            files[path] = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise EvidenceInvalid("invalid_artifact_encoding") from exc
        if base64.b64encode(files[path]).decode("ascii") != value:
            raise EvidenceInvalid("noncanonical_artifact_encoding")
    validate_file_bounds(files)
    return files


class UsageState:
    """One locked event generation; authority is evaluated for each operation."""

    def __init__(self, root: Path, state_id: str, data: bytes | None) -> None:
        self.root = root
        self.state_id = state_id
        self.binding = workspace_binding(root)
        self.document = json_document(data) if data is not None else {
            "schema_version": STATE_SCHEMA, "state_id": state_id, "workspace_binding": self.binding, "events": [],
        }
        exact_object(self.document, {"schema_version", "state_id", "workspace_binding", "events"})
        if (self.document["schema_version"] != STATE_SCHEMA or self.document["state_id"] != state_id
                or self.document["workspace_binding"] != self.binding):
            raise EvidenceInvalid("host_state_binding_mismatch")
        self.events = bounded_list(self.document["events"], maximum=MAX_EVENTS)
        self.checkpoint = content_id("evidence-usage-genesis/v1", {"state_id": state_id, "workspace_binding": self.binding})
        self.requests: dict[str, dict[str, Any]] = {}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.revisions: dict[str, dict[str, Any]] = {}
        self.availability: dict[str, dict[str, Any]] = {}
        self.assessments: dict[str, dict[str, Any]] = {}
        self.assessment_invalidations: dict[str, dict[str, Any]] = {}
        self.revoked_sources: set[str] = set()
        self.revoked_revisions: set[str] = set()
        self.last_observed: datetime | None = None
        for position, event in enumerate(self.events, 1):
            self._accept(event, position)
        if data is not None and not self.events:
            raise EvidenceInvalid("host_state_not_initialized")

    def _accept(self, event: Any, position: int) -> None:
        exact_object(event, {"sequence", "previous", "event_id", "command", "accepted_at", "authority", "artifacts"})
        if type(event["sequence"]) is not int or event["sequence"] != position or event["previous"] != self.checkpoint:
            raise EvidenceInvalid("host_state_event_order_invalid")
        observed = timestamp(event["accepted_at"])
        if self.last_observed is not None and observed < self.last_observed:
            raise EvidenceInvalid("host_state_clock_regressed")
        identity = content_id("evidence-usage-event/v1", {key: value for key, value in event.items() if key != "event_id"})
        if event["event_id"] != identity:
            raise EvidenceInvalid("host_state_event_digest_mismatch")
        command = validate_command(event["command"], self.state_id, self.binding)
        request = command["request_id"]
        if request in self.requests:
            raise EvidenceInvalid("host_state_duplicate_request")
        if command["expected_checkpoint"] != (None if position == 1 else self.checkpoint):
            raise EvidenceInvalid("host_state_command_order_invalid")
        action, body = command["action"], command["body"]
        if (position == 1) != (action == "initialize"):
            raise EvidenceInvalid("host_state_initialization_invalid")
        if action == "deposit":
            files = decode_files(event["artifacts"])
            descriptor = source_descriptor(files)
            revision = closure_identity(files)
            if revision != body["source_revision"] or descriptor["source_id"] != body["source_id"]:
                raise EvidenceInvalid("source_revision_content_mismatch")
            grant = validate_grant(body["grant"], body["source_id"], revision)
            validate_scrub(body["scrub"], revision, grant)
            self._node(revision, "source", descriptor["parents"], body["source_id"])
            self.revisions[revision] = {"source_id": body["source_id"], "files": files, "descriptor": descriptor,
                                        "grant": body["grant"], "scrub": body["scrub"], "event_id": identity,
                                        "observed_at": event["accepted_at"]}
        elif event["artifacts"] != {}:
            raise EvidenceInvalid("unexpected_event_artifacts")
        elif action == "authorize":
            revision = body["source_revision"]
            if revision not in self.revisions or self.revisions[revision]["source_id"] != body["source_id"]:
                raise EvidenceInvalid("source_revision_unknown")
            grant = validate_grant(body["grant"], body["source_id"], revision)
            validate_scrub(body["scrub"], revision, grant)
            self.revisions[revision].update(grant=body["grant"], scrub=body["scrub"], authorization_event_id=identity)
        elif action == "attest-availability":
            revision = body["source_revision"]
            if revision not in self.revisions or self.revisions[revision]["source_id"] != body["source_id"]:
                raise EvidenceInvalid("source_revision_unknown")
            if revision in self.availability:
                raise EvidenceInvalid("availability_receipt_already_recorded")
            availability_receipt(body, self.revisions[revision])
            self.availability[revision] = {"body": body, "event_id": identity, "observed_at": event["accepted_at"]}
        elif action == "revoke":
            source_id = body["source_id"]
            if not any(record["source_id"] == source_id for record in self.revisions.values()):
                raise EvidenceInvalid("revocation_source_unknown")
            if body["scope"] == "source":
                self.revoked_sources.add(source_id)
            else:
                revision = body["source_revision"]
                if revision not in self.revisions or self.revisions[revision]["source_id"] != source_id:
                    raise EvidenceInvalid("revocation_revision_unknown")
                self.revoked_revisions.add(revision)
        elif action == "register":
            self._node(body["node_id"], body["kind"], body["parents"], None)
        elif action == "register-assessment":
            assessment = body["assessment"]
            identifier = assessment["assessment_id"]
            self._node(identifier, "assessment", assessment["inputs"]["ancestors"], None)
            self.assessments[identifier] = {"envelope": event["command"], "event_id": identity}
            for previous in body["supersedes"]:
                if previous not in self.assessments or previous == identifier:
                    raise EvidenceInvalid("assessment_superseded_identity_unknown")
                self.assessment_invalidations.setdefault(previous, {"event_id": identity, "superseded_by": identifier,
                                                                    "status": "superseded"})
        elif action == "invalidate-assessments":
            for entry in body["plan"]["entries"]:
                identifier = entry["assessment_id"]
                if identifier not in self.assessments:
                    raise EvidenceInvalid("assessment_invalidation_identity_unknown")
                self.assessment_invalidations.setdefault(identifier, {**entry, "event_id": identity,
                                                                       "status": "needs-reevaluation"})
        self.requests[request] = event
        self.checkpoint = identity
        self.last_observed = observed

    def _node(self, identifier: str, kind: str, parents: list[str], source: str | None) -> None:
        if identifier in self.nodes or len(self.nodes) >= MAX_NODES:
            raise EvidenceInvalid("lineage_node_duplicate_or_bound")
        if any(parent not in self.nodes for parent in parents):
            raise EvidenceInvalid("lineage_parent_unknown")
        depth = max((self.nodes[parent]["depth"] for parent in parents), default=-1) + 1
        if depth > MAX_LINEAGE_DEPTH:
            raise EvidenceInvalid("lineage_depth_exceeded")
        self.nodes[identifier] = {"node_id": identifier, "kind": kind, "parents": parents, "source_id": source, "depth": depth}

    def append(self, command: dict[str, Any], files: dict[str, bytes], trust: dict[str, Any], now: datetime) -> dict[str, Any]:
        if len(self.events) >= MAX_EVENTS:
            raise EvidenceInvalid("host_state_event_bound_exceeded")
        event = {"sequence": len(self.events) + 1, "previous": self.checkpoint, "command": command,
                 "accepted_at": now.isoformat(), "authority": authority_basis(trust),
                 "artifacts": {path: base64.b64encode(data).decode("ascii") for path, data in sorted(files.items())}}
        event["event_id"] = content_id("evidence-usage-event/v1", event)
        self._accept(event, len(self.events) + 1)
        self.events.append(event)
        return self.receipt(event)

    def receipt(self, event: dict[str, Any]) -> dict[str, Any]:
        return {"schema_version": "evidence-usage-transaction/v1", "state_id": self.state_id,
                "request_id": event["command"]["payload"]["request_id"], "event_id": event["event_id"],
                "sequence": event["sequence"], "checkpoint": event["event_id"]}


def validate_command(envelope: Any, state_id: str, binding: str) -> dict[str, Any]:
    exact_object(envelope, {"payload", "authentication"})
    command = exact_object(envelope["payload"], {
        "schema_version", "state_id", "workspace_binding", "request_id", "expected_checkpoint", "action", "body",
    })
    if (command["schema_version"] != COMMAND_SCHEMA or command["state_id"] != state_id
            or command["workspace_binding"] != binding):
        raise EvidenceInvalid("usage_command_binding_mismatch")
    name(command["request_id"])
    if command["expected_checkpoint"] is not None:
        digest(command["expected_checkpoint"])
    action = name(command["action"])
    if action == "initialize":
        exact_object(command["body"], set())
        if command["expected_checkpoint"] is not None:
            raise EvidenceInvalid("invalid_initial_checkpoint")
    elif action in {"deposit", "authorize"}:
        body = exact_object(command["body"], {"source_id", "source_revision", "grant", "scrub"})
        name(body["source_id"])
        digest(body["source_revision"])
    elif action == "attest-availability":
        body = exact_object(command["body"], {"source_id", "source_revision", "receipt"})
        name(body["source_id"])
        digest(body["source_revision"])
    elif action == "revoke":
        body = exact_object(command["body"], {"source_id", "scope", "source_revision", "reason"})
        name(body["source_id"])
        name(body["reason"])
        if body["scope"] == "revision":
            digest(body["source_revision"])
        elif body["scope"] != "source" or body["source_revision"] is not None:
            raise EvidenceInvalid("invalid_revocation_scope")
    elif action == "register":
        body = exact_object(command["body"], {"node_id", "kind", "parents"})
        digest(body["node_id"])
        references(body["parents"])
        if name(body["kind"]) not in {"derived", "snapshot", "dataset", "adapter", "model"} or not body["parents"]:
            raise EvidenceInvalid("invalid_lineage_node")
    elif action == "register-assessment":
        body = exact_object(command["body"], {"assessment", "supersedes"})
        assessment_document(body["assessment"])
        references(body["supersedes"])
    elif action == "invalidate-assessments":
        body = exact_object(command["body"], {"plan"})
        refresh_document(body["plan"])
    else:
        raise EvidenceInvalid("usage_action_unsupported")
    return command


def command_role(action: str) -> str:
    if action in {"register-assessment", "invalidate-assessments"}:
        return "assessment"
    return "revocation" if action == "revoke" else "usage"


def qualify_revision(record: dict[str, Any], revision: str, trust: dict[str, Any], now: datetime) -> dict[str, Any]:
    grant = validate_grant(record["grant"], record["source_id"], revision)
    grant_auth = authenticated(record["grant"], "usage", trust, now)
    scrub = validate_scrub(record["scrub"], revision, grant)
    scrub_auth = authenticated(record["scrub"], "scrubber", trust, now)
    if not timestamp(grant["not_before"]) <= now < timestamp(grant["expires_at"]):
        raise EvidenceInvalid("usage_outside_validity")
    if (timestamp(grant["expires_at"]) > timestamp(grant_auth["expires_at"])
            or timestamp(scrub["completed_at"]) > timestamp(scrub_auth["issued_at"])):
        raise EvidenceInvalid("usage_receipt_interval_mismatch")
    return grant


def transact(root: Path, config: dict[str, Any], envelope: dict[str, Any], files: dict[str, bytes] | None = None) -> dict[str, Any]:
    """Authenticate and scrub-bind protected bytes before any package persistence."""
    now = datetime.now(timezone.utc)
    state_id = selection(config)
    # Detach mutable caller containers before qualifying or persisting them.
    serialized = canonical_bytes(envelope)
    if len(serialized) > 1024 * 1024:
        raise EvidenceInvalid("usage_command_bound_exceeded")
    envelope = json_document(serialized)
    command = validate_command(envelope, state_id, workspace_binding(root))
    trust = load_trust(root, config, now)
    authenticated(envelope, command_role(command["action"]), trust, now)
    artifacts = dict(files) if files is not None else {}
    if command["action"] == "deposit":
        descriptor = source_descriptor(artifacts)
        revision = closure_identity(artifacts)
        body = command["body"]
        if revision != body["source_revision"] or descriptor["source_id"] != body["source_id"]:
            raise EvidenceInvalid("source_revision_content_mismatch")
        qualify_revision(body, revision, trust, now)
    elif artifacts != {}:
        raise EvidenceInvalid("unexpected_event_artifacts")
    with locked_state(root, write=True, initialize=command["action"] == "initialize") as directory:
        state = UsageState(root, state_id, read_state(directory, allow_missing=command["action"] == "initialize"))
        prior = state.requests.get(command["request_id"])
        if prior is not None:
            if prior["command"] != envelope or prior["artifacts"] != {
                path: base64.b64encode(data).decode("ascii") for path, data in sorted(artifacts.items())
            }:
                raise EvidenceInvalid("usage_request_conflict")
            return state.receipt(prior)
        expected = None if not state.events else state.checkpoint
        if command["expected_checkpoint"] != expected:
            raise EvidenceInvalid("usage_checkpoint_changed")
        if command["action"] == "authorize":
            qualify_revision(command["body"], command["body"]["source_revision"], trust, now)
        if command["action"] == "attest-availability":
            revision = command["body"]["source_revision"]
            if revision not in state.revisions:
                raise EvidenceInvalid("source_revision_unknown")
            authenticate_availability(command["body"], state.revisions[revision], trust, now)
        assessment_view = UsageView(state, trust, now) if command["action"] in {"register-assessment", "invalidate-assessments"} else None
        assessment_engine = None
        if command["action"] == "register-assessment":
            assessment_engine = load_workspace_module(Path(__file__).resolve().parent, "_assessment_engine")
            assessment_engine.validate_issue(root, config, envelope, assessment_view)
        if command["action"] == "invalidate-assessments":
            assessment_engine = load_workspace_module(Path(__file__).resolve().parent, "_assessment_refresh")
            assessment_engine.validate_apply(root, config, command["body"]["plan"], assessment_view)
        receipt = state.append(envelope, artifacts, trust, now)
        # A host policy change during validation cannot authorize the commit.
        committed_at = datetime.now(timezone.utc)
        if committed_at < now:
            raise EvidenceInvalid("host_state_clock_regressed")
        if load_trust(root, config, committed_at)["content_hash"] != trust["content_hash"]:
            raise EvidenceInvalid("host_trust_changed")
        authenticated(envelope, command_role(command["action"]), trust, committed_at)
        if command["action"] in {"deposit", "authorize"}:
            qualify_revision(command["body"], command["body"]["source_revision"], trust, committed_at)
        if command["action"] == "attest-availability":
            authenticate_availability(command["body"], state.revisions[command["body"]["source_revision"]], trust, committed_at)
        if command["action"] == "register-assessment":
            assessment_engine.closing_issue(root, config, command["body"]["assessment"], assessment_view)
        if command["action"] == "invalidate-assessments":
            assessment_engine.closing_apply(root, config, command["body"]["plan"], assessment_view)
        write_state(directory, canonical_bytes(state.document))
        return receipt


class UsageView:
    def __init__(self, state: UsageState, trust: dict[str, Any], now: datetime) -> None:
        self.state, self.trust, self.now = state, trust, now
        self.checked: dict[tuple[str, tuple[str, ...], str, str], dict[str, Any]] = {}

    def revalidate(self, root: Path, config: dict[str, Any]) -> None:
        """Refresh successful decisions at the read or publication boundary."""
        now = datetime.now(timezone.utc)
        if now < self.now:
            raise EvidenceInvalid("host_state_clock_regressed")
        if load_trust(root, config, now)["content_hash"] != self.trust["content_hash"]:
            raise EvidenceInvalid("host_trust_changed")
        self.now = now
        for (revision, uses, purpose, consumer), previous in list(self.checked.items()):
            result = self.check(revision, uses=list(uses), purpose=purpose, consumer=consumer)
            if not result["eligible"]:
                raise EvidenceInvalid(result["reasons"][0])
            previous.update(result)

    def check(self, revision: str, *, uses: list[str], purpose: str, consumer: str) -> dict[str, Any]:
        digest(revision)
        name(purpose)
        name(consumer)
        if not isinstance(uses, list) or not 1 <= len(uses) <= 3 or any(not isinstance(use, str) or use not in USES for use in uses):
            raise EvidenceInvalid("invalid_usage_request")
        result: dict[str, Any] = {"eligible": False, "source_revision": revision, "uses": sorted(set(uses)),
                                  "purpose": purpose, "consumer": consumer, "checkpoint": self.state.checkpoint,
                                  "evaluated_at": self.now.isoformat(), "authority": authority_basis(self.trust),
                                  "reasons": [], "ancestors": [], "complete": False}
        try:
            if revision not in self.state.nodes:
                raise EvidenceInvalid("lineage_node_unknown")
            pending, visited = [revision], set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                if len(visited) >= MAX_NODES:
                    raise EvidenceInvalid("lineage_bound_exceeded")
                visited.add(current)
                node = self.state.nodes[current]
                if current in self.state.assessment_invalidations:
                    raise EvidenceInvalid("assessment_invalidated")
                pending.extend(node["parents"])
                if current in self.state.revisions:
                    record = self.state.revisions[current]
                    if current in self.state.revoked_revisions or record["source_id"] in self.state.revoked_sources:
                        raise EvidenceInvalid("source_revision_revoked")
                    grant = qualify_revision(record, current, self.trust, self.now)
                    if any(not grant["permissions"][use] for use in uses):
                        raise EvidenceInvalid("usage_permission_denied")
                    if purpose not in grant["purposes"] or consumer not in grant["consumers"]:
                        raise EvidenceInvalid("usage_purpose_or_consumer_denied")
            result.update(eligible=True, complete=True, ancestors=sorted(visited - {revision}))
            self.checked[(revision, tuple(result["uses"]), purpose, consumer)] = result
        except EvidenceInvalid as exc:
            result["reasons"] = [str(exc)]
        return result

    def lineage(self, identifier: str, *, limit: int = MAX_NODES) -> dict[str, Any]:
        digest(identifier)
        if type(limit) is not int or not 1 <= limit <= MAX_NODES:
            raise EvidenceInvalid("invalid_lineage_bound")
        if identifier not in self.state.nodes:
            return {"found": False, "complete": False, "reason": "lineage_node_unknown",
                    "checkpoint": self.state.checkpoint, "nodes": [], "limit": limit}
        children: dict[str, list[str]] = {}
        for node_id, node in self.state.nodes.items():
            for parent in node["parents"]:
                children.setdefault(parent, []).append(node_id)
        pending, visited, nodes = [identifier], set(), []
        while pending and len(visited) < limit:
            node_id = pending.pop(0)
            if node_id in visited:
                continue
            visited.add(node_id)
            nodes.append(self.state.nodes[node_id])
            pending.extend(sorted(children.get(node_id, [])))
        complete = not any(item not in visited for item in pending)
        return {"found": True, "complete": complete, "reason": "complete" if complete else "lineage_results_truncated",
                "checkpoint": self.state.checkpoint, "nodes": nodes, "limit": limit}


@contextmanager
def current_view(root: Path, config: dict[str, Any], *, exclusive: bool = False) -> Iterator[UsageView]:
    """Hold one revocation generation through the caller's complete read operation."""
    borrowed = captured_view(root, config)
    if borrowed is not None:
        if exclusive:
            raise EvidenceInvalid("assessment_capture_is_read_only")
        yield borrowed
        return
    state_id = selection(config)
    with locked_state(root, write=exclusive) as directory:
        now = datetime.now(timezone.utc)
        trust = load_trust(root, config, now)
        view = UsageView(UsageState(root, state_id, read_state(directory)), trust, now)
        if view.state.last_observed is not None and now < view.state.last_observed:
            raise EvidenceInvalid("host_state_clock_regressed")
        yield view
        view.revalidate(root, config)
