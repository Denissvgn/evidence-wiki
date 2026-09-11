#!/usr/bin/env python3
"""Offline snapshot verification using explicit, independently supplied authority.

Evidence artifacts are data. Verification never imports an originating
workspace, executes a recorded tool, or treats bundled public policy as a key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Any

import yaml
from _evidence_authority import EvidenceInvalid
from _market_evidence import qualify_cutoff as qualify_market_cutoff
from _market_evidence import validate_closure as validate_market
from _temporal_contract import availability_receipt, qualify_time, source_times

SCHEMA = "evidence-snapshot/v1"
MANIFEST_SCHEMA = "evidence-snapshot-manifest/v1"
SELECTION_SCHEMA = "evidence-snapshot-selection/v1"
CONTRACT = "evidence-snapshot-contract/v1"
TEMPORAL_SCHEMA = "evidence-snapshot/v2"
TEMPORAL_MANIFEST_SCHEMA = "evidence-snapshot-manifest/v2"
TEMPORAL_SELECTION_SCHEMA = "evidence-snapshot-selection/v2"
TEMPORAL_CONTRACT = "evidence-snapshot-contract/v2"
EXECUTION_SCHEMA = "evidence-snapshot/v3"
EXECUTION_MANIFEST_SCHEMA = "evidence-snapshot-manifest/v3"
EXECUTION_CONTRACT = "evidence-snapshot-contract/v3"
BOUNDS = {"selected_revisions": 32, "source_revisions": 64, "lineage_nodes": 128,
          "artifact_blobs": 512, "decoded_bytes": 8 * 1024 * 1024, "bundle_bytes": 16 * 1024 * 1024}
OUTCOMES = {"passed", "failed", "skipped", "inconclusive"}
ROLES = {"generator", "evaluator", "usage", "scrubber", "assessment", "availability", "revocation"}


class SnapshotInvalid(ValueError):
    """A content-free snapshot refusal reason."""


def require(condition: Any, reason: str) -> None:
    if not condition:
        raise SnapshotInvalid(reason)


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode()


def identifier(domain: str, value: Any) -> str:
    return "sha256:" + hashlib.sha256(domain.encode() + b"\0" + canonical(value)).hexdigest()


def binding(data: bytes) -> dict[str, Any]:
    return {"content_hash": "sha256:" + hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}


def document(data: bytes, maximum: int = BOUNDS["bundle_bytes"]) -> dict[str, Any]:
    require(isinstance(data, bytes) and 0 < len(data) <= maximum, "snapshot_byte_bound_exceeded")

    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "snapshot_duplicate_json_key")
            result[key] = value
        return result

    def constant(_value):
        raise SnapshotInvalid("snapshot_nonfinite_number")

    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        require(isinstance(result, dict), "snapshot_object_required")
        pending, visited = [(result, 0)], 0
        while pending:
            value, depth = pending.pop()
            visited += 1
            require(depth <= 64 and visited <= 250000, "snapshot_document_structure_bound_exceeded")
            if isinstance(value, dict):
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
        canonical(result)
        return result
    except (UnicodeError, ValueError, RecursionError, TypeError) as exc:
        if isinstance(exc, SnapshotInvalid):
            raise
        raise SnapshotInvalid("snapshot_invalid_json") from exc


def fields(value: Any, expected: set[str]) -> dict[str, Any]:
    require(isinstance(value, dict) and value.keys() == expected, "snapshot_object_fields_invalid")
    return value


def items(value: Any, maximum: int = 256, minimum: int = 0) -> list[Any]:
    require(isinstance(value, list) and minimum <= len(value) <= maximum, "snapshot_list_bound_exceeded")
    return value


def name(value: Any) -> str:
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}", value), "snapshot_identity_invalid")
    return value


def digest(value: Any) -> str:
    require(isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value), "snapshot_digest_invalid")
    return value


def instant(value: Any) -> datetime:
    require(isinstance(value, str) and len(value) <= 40 and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value), "snapshot_timestamp_invalid")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise SnapshotInvalid("snapshot_timestamp_invalid") from exc


def path_name(value: Any) -> str:
    require(isinstance(value, str) and 0 < len(value) <= 4096, "snapshot_path_invalid")
    parts = value.split("/")
    require(len(parts) <= 33, "snapshot_path_invalid")
    for part in parts:
        require(part and not part.startswith(".") and not part.endswith((".", " "))
                and not any(c in part for c in '\\:<>"|?*')
                and not any(ord(c) < 32 or ord(c) == 127 for c in part)
                and not re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part), "snapshot_path_invalid")
    return value


def unique(value: Any, validator=name, maximum: int = 256, minimum: int = 0) -> list[str]:
    result = [validator(item) for item in items(value, maximum, minimum)]
    require(len(result) == len(set(result)), "snapshot_duplicate_identity")
    return result


def selection(value: Any) -> dict[str, Any]:
    temporal = isinstance(value, dict) and value.get("schema_version") == TEMPORAL_SELECTION_SCHEMA
    fields(value, {"schema_version", "source_revisions", "include_negative_examples", "purpose", "consumer"}
           | ({"temporal"} if temporal else set()))
    require(value["schema_version"] in {SELECTION_SCHEMA, TEMPORAL_SELECTION_SCHEMA} and type(value["include_negative_examples"]) is bool,
            "snapshot_selection_unsupported")
    if temporal:
        settings = fields(value["temporal"], {"mode", "cutoff", "checkpoint"})
        require(settings["mode"] in {"historical-audit", "historical-available"}, "snapshot_temporal_mode_unsupported")
        instant(settings["cutoff"])
        digest(settings["checkpoint"])
    revisions = unique(value["source_revisions"], digest, BOUNDS["selected_revisions"], 1)
    name(value["purpose"])
    name(value["consumer"])
    return {**value, "source_revisions": sorted(revisions)}


def temporal_source(source: dict[str, Any], files: dict[str, bytes], proof: Any, policy: dict[str, Any],
                    settings: dict[str, Any], checkpoint_time: datetime) -> None:
    """Recheck captured clocks and independently signed publication evidence without an origin workspace."""
    cutoff = instant(settings["cutoff"])
    require(instant(source["observed_at"]) <= checkpoint_time, "snapshot_temporal_source_after_checkpoint")
    record = {**source, "files": files}
    try:
        times = source_times(record)
        qualify_time(record, times, cutoff, settings["mode"])
        prefix = source["descriptor"]["evidence_root"]
        originals = {} if prefix is None else {
            path[len(prefix) + 1:]: data for path, data in files.items() if path.startswith(prefix + "/")
        }
        if "market-record.json" in originals:
            qualify_market_cutoff(validate_market(source["source_id"], originals), times, cutoff,
                                  instant(source["observed_at"]))
        if settings["mode"] == "historical-available":
            fields(proof, {"body", "event_id", "observed_at"})
            digest(proof["event_id"])
            observed = instant(proof["observed_at"])
            require(instant(source["observed_at"]) <= observed <= checkpoint_time, "snapshot_availability_observation_invalid")
            payload = availability_receipt(proof["body"], record)
            require(proof["body"]["source_revision"] == source["source_revision"], "snapshot_availability_revision_mismatch")
            auth = authenticate(proof["body"]["receipt"], "availability", policy, observed)
            supplier = policy["principals"].get(payload["asserting_principal"])
            require(supplier is not None and supplier["controller"] != auth["controller"], "snapshot_availability_not_independent")
            require(instant(payload["available_at"]) <= instant(auth["issued_at"]), "snapshot_availability_receipt_precedes_publication")
        else:
            require(proof is None, "snapshot_unexpected_availability_proof")
    except EvidenceInvalid as exc:
        raise SnapshotInvalid(str(exc)) from exc


def public_policy(policy: dict[str, Any]) -> dict[str, Any]:
    return {**{key: value for key, value in policy.items() if key != "principals"},
            "principals": {principal: {"controller": settings["controller"], "roles": settings["roles"],
                                        "key_ids": sorted(settings["keys"])}
                           for principal, settings in policy["principals"].items()}}


def trust_policy(raw: bytes) -> dict[str, Any]:
    policy = fields(document(raw, 1024 * 1024), {
        "schema_version", "policy_id", "policy_revision", "not_before", "expires_at",
        "principals", "revoked_keys", "revoked_envelopes",
    })
    require(policy["schema_version"] == "evidence-trust-policy/v1", "snapshot_trust_unsupported")
    name(policy["policy_id"])
    name(policy["policy_revision"])
    require(instant(policy["not_before"]) < instant(policy["expires_at"]), "snapshot_trust_interval_invalid")
    require(isinstance(policy["principals"], dict) and 1 <= len(policy["principals"]) <= 128, "snapshot_principals_invalid")
    keys, controllers = set(), {}
    for principal, settings in policy["principals"].items():
        name(principal)
        fields(settings, {"controller", "roles", "keys"})
        name(settings["controller"])
        require(set(unique(settings["roles"], maximum=len(ROLES), minimum=1)) <= ROLES, "snapshot_role_invalid")
        require(isinstance(settings["keys"], dict) and 1 <= len(settings["keys"]) <= 32, "snapshot_keys_invalid")
        for key, secret in settings["keys"].items():
            name(key)
            require(key not in keys and isinstance(secret, str) and re.fullmatch(r"[0-9a-f]{64,128}", secret)
                    and len(secret) % 2 == 0, "snapshot_key_invalid")
            require(secret not in controllers or controllers[secret] == settings["controller"], "snapshot_shared_controller_key")
            keys.add(key)
            controllers[secret] = settings["controller"]
    unique(policy["revoked_keys"], maximum=4096)
    unique(policy["revoked_envelopes"], digest, maximum=4096)
    return policy


def authenticate(envelope: Any, role: str, policy: dict[str, Any], clock: datetime) -> dict[str, Any]:
    fields(envelope, {"payload", "authentication"})
    require(isinstance(envelope["payload"], dict), "snapshot_authenticated_payload_invalid")
    auth = fields(envelope["authentication"], {"schema_version", "scheme", "principal", "key_id", "role",
                                                "policy_id", "policy_revision", "issued_at", "expires_at", "signature"})
    require(auth["schema_version"] == "evidence-authentication/v1" and auth["scheme"] == "hmac-sha256"
            and auth["role"] == role, "snapshot_authentication_unsupported")
    for field in ("principal", "key_id", "policy_id", "policy_revision"):
        name(auth[field])
    issued, expiry = instant(auth["issued_at"]), instant(auth["expires_at"])
    require(instant(policy["not_before"]) <= issued <= clock < expiry <= instant(policy["expires_at"]),
            "snapshot_authentication_outside_validity")
    require(all(auth[key] == policy[key] for key in ("policy_id", "policy_revision")), "snapshot_authentication_policy_mismatch")
    settings = policy["principals"].get(auth["principal"])
    require(settings and role in settings["roles"] and auth["key_id"] in settings["keys"], "snapshot_principal_not_authorized")
    require(auth["key_id"] not in policy["revoked_keys"]
            and identifier("evidence-authenticated-payload/v1", envelope["payload"]) not in policy["revoked_envelopes"],
            "snapshot_authentication_revoked")
    require(isinstance(auth["signature"], str) and re.fullmatch(r"[0-9a-f]{64}", auth["signature"]), "snapshot_signature_invalid")
    message = b"evidence-attestation/v1\0" + canonical({"payload": envelope["payload"],
                                                        "authentication": {k: v for k, v in auth.items() if k != "signature"}})
    signature = hmac.new(bytes.fromhex(settings["keys"][auth["key_id"]]), message, hashlib.sha256).hexdigest()
    require(hmac.compare_digest(signature, auth["signature"]), "snapshot_signature_mismatch")
    return {**auth, "controller": settings["controller"]}


def validate_use(record: dict[str, Any], policy: dict[str, Any], clock: datetime, request: dict[str, Any]) -> None:
    auth = authenticate(record["grant"], "usage", policy, clock)
    grant = fields(record["grant"]["payload"], {"schema_version", "source_id", "source_revision", "permissions",
                                                "purposes", "consumers", "not_before", "expires_at", "redaction_policy", "retention"})
    require(grant["schema_version"] == "evidence-usage-grant/v1" and grant["source_id"] == record["source_id"]
            and grant["source_revision"] == record["source_revision"], "snapshot_grant_revision_mismatch")
    permissions = fields(grant["permissions"], {"retrieval", "training", "export"})
    require(all(type(value) is bool for value in permissions.values()) and permissions["training"] and permissions["export"],
            "snapshot_training_export_denied")
    require(request["purpose"] in unique(grant["purposes"], minimum=1)
            and request["consumer"] in unique(grant["consumers"], minimum=1), "snapshot_purpose_consumer_denied")
    require(instant(grant["not_before"]) <= clock < instant(grant["expires_at"]) <= instant(auth["expires_at"]),
            "snapshot_usage_outside_validity")
    require(grant["retention"] == "host-managed", "snapshot_retention_unsupported")
    redaction = fields(grant["redaction_policy"], {"id", "revision"})
    name(redaction["id"])
    name(redaction["revision"])
    scrub_auth = authenticate(record["scrub"], "scrubber", policy, clock)
    scrub = fields(record["scrub"]["payload"], {"schema_version", "sanitized_revision", "redaction_policy", "tool", "outcome", "completed_at"})
    require(scrub["schema_version"] == "evidence-scrub-receipt/v1" and scrub["sanitized_revision"] == record["source_revision"]
            and scrub["redaction_policy"] == redaction and scrub["outcome"] == "passed", "snapshot_scrub_binding_mismatch")
    fields(scrub["tool"], {"name", "version"})
    name(scrub["tool"]["name"])
    name(scrub["tool"]["version"])
    require(instant(scrub["completed_at"]) <= instant(scrub_auth["issued_at"]), "snapshot_scrub_interval_invalid")


class ClaimLoader(yaml.SafeLoader):
    """Bounded normalized metadata with unambiguous keys and no alias expansion."""

    def compose_node(self, parent, index):
        require(not self.check_event(yaml.AliasEvent), "snapshot_normalized_alias_unsupported")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            require(isinstance(key, str) and key not in result, "snapshot_normalized_key_invalid")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def normalized_claims(data: bytes, source_id: str, revision: str) -> None:
    """An original explicit denial cannot be upgraded by a later grant."""
    try:
        lines = data.decode("utf-8").splitlines()
        require(lines and lines[0] == "---" and "---" in lines[1:], "snapshot_normalized_metadata_missing")
        end = lines.index("---", 1)
        require(end <= 4096, "snapshot_normalized_metadata_bound_exceeded")
        metadata = yaml.load("\n".join(lines[1:end]), Loader=ClaimLoader)  # noqa: S506 -- restricted SafeLoader subclass
        require(isinstance(metadata, dict) and metadata.get("source_id") == source_id, "snapshot_normalized_source_mismatch")
        documents = [metadata] + [metadata[key] for key in ("metadata", "provenance") if isinstance(metadata.get(key), dict)]
        for claims in documents:
            for use in ("training", "export"):
                require(use + "_eligible" not in claims or claims[use + "_eligible"] is True,
                        "snapshot_original_usage_denied")
            require("usage_policy" not in claims, "snapshot_workspace_policy_not_authoritative")
            require("usage_revision_id" not in claims or digest(claims["usage_revision_id"]) == revision,
                    "snapshot_original_revision_mismatch")
    except yaml.YAMLError as exc:
        raise SnapshotInvalid("snapshot_normalized_metadata_invalid") from exc


def artifact(reference: Any, members: dict[str, Any], allowed: set[str]) -> str:
    fields(reference, {"path", "content_hash"})
    path = path_name(reference["path"])
    require(path in members and members[path]["role"] in allowed
            and members[path]["content_hash"] == digest(reference["content_hash"]), "snapshot_artifact_reference_invalid")
    return path


def execution(files: dict[str, bytes], source_id: str, policy: dict[str, Any], clock: datetime,
              *, historical: bool = False) -> dict[str, Any]:
    """Recalculate execution membership, record IDs, receipt scope, and authority."""
    require("execution-record.json" in files, "snapshot_execution_record_missing")
    record = fields(document(files["execution-record.json"]), {"schema_version", "profile", "source_id", "selected_record_id",
                                                               "selected_receipt_id", "artifacts", "records", "receipts"})
    require(record["schema_version"] == "execution-evidence/v1" and record["profile"] == "execution_evidence/v1"
            and record["source_id"] == source_id, "snapshot_execution_contract_invalid")
    members = {}
    for member in items(record["artifacts"], 255):
        fields(member, {"path", "content_hash", "size_bytes", "role"})
        path = path_name(member["path"])
        require(path != "execution-record.json" and path not in members and path in files
                and type(member["size_bytes"]) is int
                and {key: member[key] for key in ("content_hash", "size_bytes")} == binding(files[path]),
                "snapshot_execution_member_mismatch")
        members[path] = member
    require(set(files) == {"execution-record.json", *members}, "snapshot_execution_closure_incomplete")
    records, authentication, used = {}, {}, set()
    input_revisions, qualified_packets = [], []
    for envelope in items(record["records"], 128, 1):
        payload = fields(envelope.get("payload"), {
            "record_type", "episode_id", "task_id", "run_id", "problem_id", "input_group_id", "held_out_role",
            "generating_agent", "predecessor", "hypothesis", "inputs", "patch", "workspace", "environment", "dependencies",
            "container", "tool", "model", "parameters", "seed", "started_at", "finished_at", "outputs", "logs", "outcome",
            "units", "warnings", "limits", "verification_scope", "temporal",
        })
        require(payload["record_type"] in {"observation", "hypothesis"} and payload["outcome"] in OUTCOMES,
                "snapshot_execution_outcome_invalid")
        require(payload["record_type"] != "hypothesis" or payload["outcome"] == "inconclusive", "snapshot_hypothesis_outcome_invalid")
        for key in ("episode_id", "task_id", "run_id", "problem_id", "input_group_id", "generating_agent"):
            name(payload[key])
        require(payload["held_out_role"] in {"training", "validation", "held-out", "unspecified"}, "snapshot_held_out_role_invalid")
        require(instant(payload["started_at"]) <= instant(payload["finished_at"]), "snapshot_execution_interval_invalid")
        auth = authenticate(envelope, "generator", policy, clock)
        require(auth["principal"] == payload["generating_agent"] and instant(payload["finished_at"]) <= instant(auth["issued_at"]),
                "snapshot_generator_binding_invalid")
        rid = identifier("evidence-execution-record/v1", payload)
        require(rid not in records, "snapshot_execution_record_duplicate")
        for key, kind in (("predecessor", "observation"), ("hypothesis", "hypothesis")):
            link = payload[key]
            if link is not None:
                require(digest(link) in records and records[link]["record_type"] == kind
                        and all(records[link][field] == payload[field] for field in ("episode_id", "task_id"))
                        and instant(records[link]["finished_at"]) <= instant(payload["started_at"]), "snapshot_execution_history_invalid")
        if payload["hypothesis"] is not None:
            require(records[payload["hypothesis"]]["predecessor"] == payload["predecessor"], "snapshot_execution_history_invalid")
        inputs, references = set(), set()
        for item in items(payload["inputs"], minimum=1):
            fields(item, {"source_id", "revision_id", "artifact"})
            identity = (name(item["source_id"]), digest(item["revision_id"]))
            require(identity not in inputs, "snapshot_execution_input_duplicate")
            inputs.add(identity)
            path = artifact(item["artifact"], members, {"input", "context", "context-packet"})
            references.add(path)
            input_revisions.append({"source_id": item["source_id"], "source_revision": item["revision_id"], **binding(files[path])})
        require(payload["workspace"] == {"scope": "declared-input-artifacts", "content_hash": identifier(
            "evidence-artifact-closure/v1", {path: binding(files[path]) for path in references})}, "snapshot_workspace_binding_invalid")
        input_paths = set(references)
        for field, allowed in (("patch", {"patch"}), ("environment", {"environment"}),
                               ("dependencies", {"dependency"}), ("container", {"environment"})):
            value = payload[field]
            if not (isinstance(value, str) and value in {"not-applicable", "unknown"}):
                references.add(artifact(value, members, allowed))
        tool = fields(payload["tool"], {"name", "version", "schema_version", "artifact"})
        for key in ("name", "version", "schema_version"):
            name(tool[key])
        references.add(artifact(tool["artifact"], members, {"tool"}))
        model = fields(payload["model"], {"identity", "weights", "adapter", "quantization", "prompt", "context"})
        for key in ("identity", "weights", "adapter", "quantization"):
            name(model[key])
        for key in ("prompt", "context"):
            value = model[key]
            if not (isinstance(value, str) and value in {"not-applicable", "unknown"}):
                references.add(artifact(value, members, {"input", "context", "context-packet"}))
        require(isinstance(payload["parameters"], dict) and len(payload["parameters"]) <= 128
                and (type(payload["seed"]) is int or payload["seed"] in ("unknown", "not-applicable")), "snapshot_execution_parameters_invalid")
        require(isinstance(payload["units"], dict) and len(payload["units"]) <= 128
                and all(isinstance(k, str) and isinstance(v, str) for k, v in payload["units"].items()), "snapshot_units_invalid")
        for key, allowed in (("outputs", {"output"}), ("logs", {"log"})):
            paths = [artifact(value, members, allowed) for value in items(payload[key], minimum=1)]
            require(len(paths) == len(set(paths)), "snapshot_artifact_reference_duplicate")
            references.update(paths)
        scope = fields(payload["verification_scope"], {"suite_revision", "suite", "checks", "environment"})
        name(scope["suite_revision"])
        unique(scope["checks"], minimum=1)
        references.add(artifact(scope["suite"], members, {"suite"}))
        require(scope["environment"] == payload["environment"], "snapshot_verification_environment_mismatch")
        temporal = fields(payload["temporal"], {"mode", "cutoff", "limitations"})
        require(temporal["mode"] in {"current", "historical-audit", "historical-available"}
                and (temporal["mode"] == "current") == (temporal["cutoff"] is None), "snapshot_execution_temporal_invalid")
        if temporal["mode"] != "current":
            require(historical, "snapshot_historical_execution_unsupported")
            require(instant(temporal["cutoff"]) <= instant(payload["started_at"]), "execution_input_cutoff_after_start")
            require(all(not isinstance(model[field], dict) or model[field]["path"] in input_paths for field in ("prompt", "context")),
                    "execution_historical_context_input_required")
        for values in (payload["warnings"], payload["limits"], temporal["limitations"]):
            require(all(isinstance(v, str) and len(v) <= 4096 for v in items(values, 128)), "snapshot_execution_text_invalid")
        for path in sorted(references):
            if members[path]["role"] in {"context", "context-packet"}:
                required = members[path]["role"] == "context-packet"
                packet = document(files[path]) if required or path.endswith(".json") else {}
                if required or str(packet.get("schema_version", "")).startswith("llm-wiki-qualified-context-packet/"):
                    qualified_packets.append({"path": path, "bytes": files[path]})
        used.update(references)
        records[rid], authentication[rid] = payload, auth
    receipts = {}
    for envelope in items(record["receipts"], 128):
        payload = fields(envelope.get("payload"), {"record_type", "target_record_id", "episode_id", "task_id", "run_id", "evaluator",
                                                  "scope", "started_at", "finished_at", "outcome", "assertions", "counts", "logs", "warnings", "limits"})
        target_id = digest(payload["target_record_id"])
        require(payload["record_type"] == "verification-receipt" and target_id in records
                and records[target_id]["record_type"] == "observation", "snapshot_receipt_target_invalid")
        target = records[target_id]
        require(all(payload[key] == target[key] for key in ("episode_id", "task_id", "run_id"))
                and payload["scope"] == target["verification_scope"], "snapshot_receipt_scope_mismatch")
        require(instant(target["finished_at"]) <= instant(payload["started_at"]) <= instant(payload["finished_at"]),
                "snapshot_receipt_interval_invalid")
        auth = authenticate(envelope, "evaluator", policy, clock)
        require(auth["principal"] == payload["evaluator"] and instant(payload["finished_at"]) <= instant(auth["issued_at"])
                and auth["controller"] != authentication[target_id]["controller"], "snapshot_evaluator_not_independent")
        require(payload["outcome"] in OUTCOMES, "snapshot_receipt_outcome_invalid")
        checks, counts = set(), dict.fromkeys(sorted(OUTCOMES), 0)
        expected = set(payload["scope"]["checks"])
        for assertion in items(payload["assertions"]):
            fields(assertion, {"check_id", "outcome", "expected", "actual", "comparison"})
            check = name(assertion["check_id"])
            name(assertion["comparison"])
            require(check in expected and check not in checks and assertion["outcome"] in OUTCOMES, "snapshot_assertion_invalid")
            counts[assertion["outcome"]] += 1
            checks.add(check)
        require(isinstance(payload["counts"], dict) and all(type(v) is int for v in payload["counts"].values())
                and payload["counts"] == counts, "snapshot_assertion_counts_invalid")
        require(payload["outcome"] != "passed" or checks == expected and counts["passed"] == len(expected),
                "snapshot_passing_scope_incomplete")
        require(payload["outcome"] != "failed" or counts["failed"] > 0, "snapshot_failed_assertion_missing")
        logs = [artifact(value, members, {"log"}) for value in items(payload["logs"], minimum=1)]
        require(len(logs) == len(set(logs)), "snapshot_artifact_reference_duplicate")
        used.update(logs)
        for key in ("warnings", "limits"):
            require(all(isinstance(v, str) and len(v) <= 4096 for v in items(payload[key], 128)), "snapshot_execution_text_invalid")
        rid = identifier("evidence-verification-receipt/v1", payload)
        require(rid not in receipts, "snapshot_receipt_duplicate")
        receipts[rid] = payload
    require(used == set(members), "snapshot_execution_unreferenced_artifact")
    target = records.get(digest(record["selected_record_id"]))
    receipt = receipts.get(digest(record["selected_receipt_id"]))
    require(target is not None and target["record_type"] == "observation" and receipt is not None
            and receipt["target_record_id"] == record["selected_record_id"], "snapshot_selected_receipt_missing")
    require(target["outcome"] == receipt["outcome"] and target["outcome"] in {"passed", "failed"}, "snapshot_example_outcome_unsupported")
    return {"example": {"record_id": record["selected_record_id"], "receipt_id": record["selected_receipt_id"],
                         "label": "positive" if target["outcome"] == "passed" else "negative",
                         "outcome": target["outcome"], **{key: target[key] for key in (
                             "episode_id", "task_id", "run_id", "problem_id", "input_group_id", "held_out_role")}},
            "inputs": input_revisions, "packets": qualified_packets, "generations": list(records.values())}


def execution_input_context(sources: dict[str, dict[str, Any]], files: dict[str, dict[str, bytes]],
                            nodes: dict[str, dict[str, Any]], proofs: dict[str, Any], policy: dict[str, Any],
                            clock: datetime, checkpoint_time: datetime) -> dict[str, Any]:
    """Bind every execution input and qualify its complete ancestry at its own cutoff.

    Callers supply an accepted, currently authorized closure or independently
    verified snapshot bindings. Result availability and run authentication use
    the enclosing clock; input availability uses each generation's cutoff.
    """
    require(len(sources) <= BOUNDS["source_revisions"] and len(nodes) <= BOUNDS["lineage_nodes"]
            and set(sources) == set(files) and set(sources) <= set(nodes), "execution_input_context_bound_exceeded")
    require(sum(len(raw) for group in files.values() for raw in group.values()) <= BOUNDS["decoded_bytes"],
            "execution_input_context_bound_exceeded")
    require(checkpoint_time <= clock, "execution_input_checkpoint_after_clock")
    closures, originals, runs = {}, {}, {}
    deadlines = [instant(policy["expires_at"])]

    def ancestors(revision: str, active: set[str]) -> set[str]:
        require(revision in nodes and revision not in active and len(active) <= 64, "execution_input_lineage_incomplete_or_cyclic")
        if revision not in closures:
            result = {revision}
            for parent in nodes[revision]["parents"]:
                result.update(ancestors(parent, active | {revision}))
            closures[revision] = result
        return closures[revision]

    for revision, source in sources.items():
        prefix = source["descriptor"]["evidence_root"]
        originals[revision] = {} if prefix is None else {
            path[len(prefix) + 1:]: raw for path, raw in files[revision].items() if path.startswith(prefix + "/")
        }
        if "execution-record.json" in originals[revision]:
            runs[revision] = execution(originals[revision], source["source_id"], policy, clock, historical=True)
            captured = document(originals[revision]["execution-record.json"])
            deadlines.extend(instant(envelope["authentication"]["expires_at"])
                             for group in ("records", "receipts") for envelope in captured[group])
            for item in runs[revision]["inputs"]:
                parent = item["source_revision"]
                require(parent in ancestors(revision, set()) - {revision} and parent in sources
                        and sources[parent]["source_id"] == item["source_id"]
                        and {key: item[key] for key in ("content_hash", "size_bytes")} in (binding(raw) for raw in files[parent].values()),
                        "snapshot_execution_input_lineage_missing")
    checked = set()
    historical = False
    for result in runs.values():
        for generation in result["generations"]:
            settings = generation["temporal"]
            if settings["mode"] == "current":
                continue
            historical = True
            cutoff = instant(settings["cutoff"])
            for item in generation["inputs"]:
                for parent in ancestors(item["revision_id"], set()):
                    key = (parent, settings["mode"], cutoff)
                    if key in checked:
                        continue
                    require(len(checked) < 4096, "execution_temporal_check_bound_exceeded")
                    require(parent in sources, "execution_historical_non_source_ancestor_unsupported")
                    proof = proofs.get(parent) if settings["mode"] == "historical-available" else None
                    require(settings["mode"] != "historical-available" or proof is not None,
                            "execution_independent_availability_missing")
                    temporal_source(sources[parent], files[parent], proof, policy, settings, checkpoint_time)
                    if parent in runs:
                        execution(originals[parent], sources[parent]["source_id"], policy, cutoff, historical=True)
                    checked.add(key)
    return {"historical": historical, "complete": True, "qualified_source_cutoffs": len(checked),
            "bound": 4096, "model_training_cutoff": "not_established", "valid_until": min(deadlines).isoformat()}


def validate_files(members: Any, blobs: dict[str, bytes]) -> dict[str, bytes]:
    require(isinstance(members, dict) and 1 <= len(members) <= 256, "snapshot_source_file_bound_exceeded")
    files, entries = {}, {}
    for path, member in members.items():
        parts = path_name(path).split("/")
        fields(member, {"content_hash", "size_bytes"})
        hash_value = digest(member["content_hash"])
        require(hash_value in blobs and type(member["size_bytes"]) is int and binding(blobs[hash_value]) == member,
                "snapshot_source_file_mismatch")
        for end in range(1, len(parts) + 1):
            entry = "/".join(parts[:end])
            require(entry.casefold() not in entries or entries[entry.casefold()] == entry, "snapshot_path_collision")
            require(end == len(parts) or entry not in members, "snapshot_path_collision")
            entries[entry.casefold()] = entry
        require(len(entries) <= 512, "snapshot_source_entry_bound_exceeded")
        files[path] = blobs[hash_value]
    return files


def validity_deadline(manifest: dict[str, Any], files_by_revision: dict[str, dict[str, bytes]],
                      registration: dict[str, Any] | None = None) -> datetime:
    """Bound a completion-time check by every already validated authority interval."""
    expiries = [instant(manifest["policy"]["public_trust"]["expires_at"])]
    if registration is not None:
        expiries.append(instant(registration["authentication"]["expires_at"]))
    for source in manifest["sources"]:
        expiries.extend(instant(source[key]["authentication"]["expires_at"]) for key in ("grant", "scrub"))
        expiries.append(instant(source["grant"]["payload"]["expires_at"]))
        prefix = source["descriptor"]["evidence_root"]
        files = files_by_revision[source["source_revision"]]
        path = prefix + "/execution-record.json" if prefix is not None else None
        if path in files:
            record = document(files[path])
            expiries.extend(instant(envelope["authentication"]["expires_at"])
                            for key in ("records", "receipts") for envelope in record[key])
    return min(expiries)


def validate_snapshot(data: bytes, trust_raw: bytes, *, current_time: datetime | None = None,
                      qualify_source=None) -> dict[str, Any]:
    """Verify independent bindings; current_time is reserved for host reconciliation."""
    bundle = fields(document(data), {"schema_version", "snapshot_id", "manifest", "registration", "blobs"})
    historical_inputs = bundle["schema_version"] == EXECUTION_SCHEMA
    temporal = bundle["schema_version"] == TEMPORAL_SCHEMA or historical_inputs and "temporal" in bundle["manifest"]
    contract = EXECUTION_CONTRACT if historical_inputs else TEMPORAL_CONTRACT if temporal else CONTRACT
    manifest_schema = EXECUTION_MANIFEST_SCHEMA if historical_inputs else TEMPORAL_MANIFEST_SCHEMA if temporal else MANIFEST_SCHEMA
    require(bundle["schema_version"] in {SCHEMA, TEMPORAL_SCHEMA, EXECUTION_SCHEMA} and canonical(bundle) == data, "snapshot_encoding_unsupported")
    manifest = fields(bundle["manifest"], {"schema_version", "contract", "exporter", "selection", "captured_state", "policy",
                                          "sources", "lineage", "examples", "exclusions", "qualifications", "coverage", "bounds"}
                      | ({"temporal"} if temporal else set()) | ({"execution_inputs"} if historical_inputs else set()))
    require(manifest["schema_version"] == manifest_schema and manifest["contract"] == contract and manifest["bounds"] == BOUNDS,
            "snapshot_contract_unsupported")
    require(digest(bundle["snapshot_id"]) == identifier(manifest_schema, manifest), "snapshot_manifest_digest_mismatch")
    fields(manifest["exporter"], {"name", "version", "implementation"})
    require(manifest["exporter"]["name"] == "evidence-wiki" and manifest["exporter"]["version"] == contract,
            "snapshot_exporter_contract_unsupported")
    digest(manifest["exporter"]["implementation"])
    request = selection(manifest["selection"])
    require(request == manifest["selection"], "snapshot_selection_order_invalid")
    require(("temporal" in request) is temporal, "snapshot_temporal_contract_mismatch")
    state = fields(manifest["captured_state"], {"state_id", "workspace_binding", "checkpoint", "observed_at"})
    name(state["state_id"])
    digest(state["workspace_binding"])
    digest(state["checkpoint"])
    captured = instant(state["observed_at"])
    temporal_time, proofs = None, {}
    if temporal:
        declared = fields(manifest["temporal"], {"checkpoint_observed_at", "availability"})
        temporal_time = instant(declared["checkpoint_observed_at"])
        require(temporal_time <= captured and instant(request["temporal"]["cutoff"]) <= captured, "snapshot_temporal_clock_invalid")
        proofs = declared["availability"]
        require(isinstance(proofs, dict), "snapshot_temporal_proofs_invalid")
    clock = current_time or captured
    require(clock.tzinfo is not None and clock >= captured, "snapshot_clock_invalid")
    policy = trust_policy(trust_raw)
    declared_policy = fields(manifest["policy"], {"trust_content_hash", "public_trust", "required_uses", "retention"})
    require(declared_policy == {"trust_content_hash": binding(trust_raw)["content_hash"], "public_trust": public_policy(policy),
                                "required_uses": ["export", "training"], "retention": "host-managed"}, "snapshot_trust_basis_mismatch")
    require(instant(policy["not_before"]) <= captured < instant(policy["expires_at"]), "snapshot_trust_outside_validity")
    encoded = bundle["blobs"]
    require(isinstance(encoded, dict) and 1 <= len(encoded) <= BOUNDS["artifact_blobs"], "snapshot_blob_bound_exceeded")
    blobs, total = {}, 0
    for key, value in encoded.items():
        digest(key)
        require(isinstance(value, str) and len(value) <= BOUNDS["bundle_bytes"], "snapshot_blob_bound_exceeded")
        try:
            raw = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise SnapshotInvalid("snapshot_blob_encoding_invalid") from exc
        total += len(raw)
        require(total <= BOUNDS["decoded_bytes"], "snapshot_blob_bound_exceeded")
        require(base64.b64encode(raw).decode("ascii") == value and binding(raw)["content_hash"] == key, "snapshot_blob_digest_mismatch")
        blobs[key] = raw
    sources, files_by_revision, referenced = {}, {}, set()
    for source in items(manifest["sources"], BOUNDS["source_revisions"], 1):
        fields(source, {"source_revision", "source_id", "descriptor", "files", "grant", "scrub", "observed_at", "deposit_event_id", "authorization_event_id"})
        revision = digest(source["source_revision"])
        require(revision not in sources, "snapshot_source_duplicate")
        name(source["source_id"])
        require(instant(source["observed_at"]) <= captured, "snapshot_source_observed_after_capture")
        digest(source["deposit_event_id"])
        if source["authorization_event_id"] is not None:
            digest(source["authorization_event_id"])
        files = validate_files(source["files"], blobs)
        require(identifier("evidence-artifact-closure/v1", source["files"]) == revision, "snapshot_revision_digest_mismatch")
        require("source-record.json" in files and document(files["source-record.json"]) == source["descriptor"], "snapshot_source_descriptor_mismatch")
        descriptor = fields(source["descriptor"], {"schema_version", "source_id", "parents", "normalized_path", "evidence_root", "temporal"})
        require(descriptor["schema_version"] == "evidence-source-revision/v1" and descriptor["source_id"] == source["source_id"]
                and isinstance(descriptor["temporal"], dict), "snapshot_source_contract_invalid")
        unique(descriptor["parents"], digest)
        normalized = descriptor["normalized_path"]
        require(normalized is None or path_name(normalized) in files and normalized.endswith(".md"), "snapshot_normalized_file_missing")
        if normalized is not None:
            normalized_claims(files[normalized], source["source_id"], revision)
        evidence_root = descriptor["evidence_root"]
        if evidence_root is not None:
            require(any(path.startswith(path_name(evidence_root) + "/") for path in files), "snapshot_evidence_root_missing")
        validate_use(source, policy, clock, request)
        if temporal:
            require(revision in proofs, "snapshot_temporal_source_missing")
            temporal_source(source, files, proofs[revision], policy, request["temporal"], temporal_time)
        referenced.update(value["content_hash"] for value in source["files"].values())
        sources[revision], files_by_revision[revision] = source, files
    require(list(sources) == sorted(sources) and referenced == set(blobs), "snapshot_artifact_closure_incomplete")
    require(not temporal or set(proofs) == set(sources), "snapshot_temporal_closure_incomplete")
    if historical_inputs:
        input_proofs = fields(manifest["execution_inputs"], {"availability"})["availability"]
        require(isinstance(input_proofs, dict) and set(input_proofs) == set(sources), "snapshot_execution_input_proofs_incomplete")
    nodes = {}
    for node in items(manifest["lineage"], BOUNDS["lineage_nodes"], 1):
        fields(node, {"node_id", "kind", "parents", "source_id", "depth"})
        node_id = digest(node["node_id"])
        require(node_id not in nodes and node["kind"] in {"source", "derived", "snapshot", "dataset", "adapter", "model"}
                and type(node["depth"]) is int and 0 <= node["depth"] <= 64, "snapshot_lineage_node_invalid")
        unique(node["parents"], digest)
        if node["kind"] == "source":
            require(node_id in sources and node["source_id"] == sources[node_id]["source_id"]
                    and node["parents"] == sources[node_id]["descriptor"]["parents"], "snapshot_lineage_source_mismatch")
        else:
            require(not temporal, "snapshot_temporal_non_source_lineage_unsupported")
            require(node["source_id"] is None and node["parents"], "snapshot_lineage_node_invalid")
        nodes[node_id] = node
    require(list(nodes) == sorted(nodes) and set(sources) == {key for key, value in nodes.items() if value["kind"] == "source"},
            "snapshot_lineage_source_mismatch")
    closures = {}

    def ancestors(node_id: str, active: set[str]) -> set[str]:
        require(node_id in nodes and node_id not in active and len(active) <= 64, "snapshot_lineage_incomplete_or_cyclic")
        if node_id in closures:
            return closures[node_id]
        result = {node_id}
        depth = -1
        for parent in nodes[node_id]["parents"]:
            result.update(ancestors(parent, active | {node_id}))
            depth = max(depth, nodes[parent]["depth"])
        require(nodes[node_id]["depth"] == depth + 1, "snapshot_lineage_depth_mismatch")
        closures[node_id] = result
        return result

    selected, included = [], set()
    for example in items(manifest["examples"], BOUNDS["selected_revisions"], 1):
        fields(example, {"source_revision", "record_id", "receipt_id", "label", "outcome", "episode_id", "task_id", "run_id",
                         "problem_id", "input_group_id", "held_out_role"})
        revision = digest(example["source_revision"])
        require(revision in sources and revision in request["source_revisions"] and revision not in selected, "snapshot_example_revision_invalid")
        selected.append(revision)
        closure = ancestors(revision, set())
        included.update(closure)
        source = sources[revision]
        prefix = source["descriptor"]["evidence_root"]
        require(prefix is not None, "snapshot_execution_record_missing")
        originals = {path[len(prefix) + 1:]: raw for path, raw in files_by_revision[revision].items() if path.startswith(prefix + "/")}
        result = execution(originals, source["source_id"], policy, clock, historical=historical_inputs)
        if temporal:
            execution(originals, source["source_id"], policy, instant(request["temporal"]["cutoff"]), historical=historical_inputs)
        require(example == {"source_revision": revision, **result["example"]}, "snapshot_example_binding_mismatch")
        require(example["label"] != "negative" or request["include_negative_examples"], "snapshot_negative_not_requested")
        for item in result["inputs"]:
            parent = item["source_revision"]
            require(parent in closure - {revision} and parent in sources and sources[parent]["source_id"] == item["source_id"]
                    and {key: item[key] for key in ("content_hash", "size_bytes")} in sources[parent]["files"].values(),
                    "snapshot_execution_input_lineage_missing")
    require(selected == sorted(selected) and included == set(nodes), "snapshot_lineage_closure_incomplete")
    # Referenced executions retain their original authority even when they are
    # included as inputs instead of being selected as examples.
    for revision in sorted(set(sources) - set(selected)):
        source = sources[revision]
        prefix = source["descriptor"]["evidence_root"]
        originals = {path[len(prefix) + 1:]: raw for path, raw in files_by_revision[revision].items()
                     if prefix is not None and path.startswith(prefix + "/")}
        if "execution-record.json" in originals:
            result = execution(originals, source["source_id"], policy, clock, historical=historical_inputs)
            if temporal:
                execution(originals, source["source_id"], policy, instant(request["temporal"]["cutoff"]), historical=historical_inputs)
            for item in result["inputs"]:
                parent = item["source_revision"]
                require(parent in ancestors(revision, set()) - {revision} and parent in sources
                        and sources[parent]["source_id"] == item["source_id"]
                        and {key: item[key] for key in ("content_hash", "size_bytes")} in sources[parent]["files"].values(),
                        "snapshot_execution_input_lineage_missing")
    if historical_inputs:
        result = execution_input_context(sources, files_by_revision, nodes, input_proofs, policy, clock, captured)
        require(result["historical"], "snapshot_execution_contract_unnecessary")
    excluded = []
    for item in items(manifest["exclusions"], BOUNDS["selected_revisions"]):
        fields(item, {"source_revision", "reason"})
        excluded.append(digest(item["source_revision"]))
        name(item["reason"])
    require(excluded == sorted(set(excluded)) and not set(excluded) & set(selected)
            and set(request["source_revisions"]) == set(selected) | set(excluded), "snapshot_selection_coverage_incomplete")
    coverage = fields(manifest["coverage"], {"selection_complete", "artifact_closure_complete", "included_count",
                                            "excluded_count", "source_count", "lineage_count", "blob_count"})
    require(all(type(coverage[key]) is bool if key.endswith("_complete") else type(coverage[key]) is int
                for key in coverage), "snapshot_coverage_types_invalid")
    require(coverage == {"selection_complete": True, "artifact_closure_complete": True,
                                     "included_count": len(selected), "excluded_count": len(excluded),
                                     "source_count": len(sources), "lineage_count": len(nodes), "blob_count": len(blobs)},
            "snapshot_coverage_mismatch")
    require(qualify_source is not None, "snapshot_qualification_validator_required")
    qualifications = [{"source_revision": revision, **qualification}
                      for revision in sorted(sources)
                      for qualification in qualify_source(sources[revision], files_by_revision[revision])]
    require(canonical(manifest["qualifications"]) == canonical(qualifications), "snapshot_packet_qualifications_mismatch")
    registration = bundle["registration"]
    payload = fields(registration.get("payload"), {"schema_version", "state_id", "workspace_binding", "request_id", "expected_checkpoint", "action", "body"})
    require(payload == {"schema_version": "evidence-usage-command/v1", "state_id": state["state_id"],
                        "workspace_binding": state["workspace_binding"], "request_id": name(payload["request_id"]),
                        "expected_checkpoint": state["checkpoint"], "action": "register",
                        "body": {"node_id": bundle["snapshot_id"], "kind": "snapshot", "parents": selected}},
            "snapshot_registration_binding_mismatch")
    # A host may attest to a captured checkpoint after that checkpoint's clock.
    registration_clock = max(clock, instant(registration["authentication"]["issued_at"]))
    authenticate(registration, "usage", policy, registration_clock)
    deadline = validity_deadline(manifest, files_by_revision, registration)
    require(clock < deadline, "snapshot_authority_expired")
    return {"snapshot_id": bundle["snapshot_id"], "manifest": manifest, "registration": registration,
            "source_revisions": sorted(sources), "selected_revisions": selected, "valid_until": deadline.isoformat()}


def verify_snapshot(data: bytes, *, trust_policy_bytes: bytes, qualify_source=None) -> dict[str, Any]:
    """Return historical verification only; a host must separately authorize use."""
    try:
        result = validate_snapshot(data, trust_policy_bytes, qualify_source=qualify_source)
        return {"schema_version": "evidence-snapshot-verification/v1", "valid": True, "reason": "snapshot_historical_bindings_valid",
                "snapshot_id": result["snapshot_id"], "captured_state": result["manifest"]["captured_state"],
                "coverage": result["manifest"]["coverage"], "current_use": "not_evaluated"}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError) as exc:
        return {"schema_version": "evidence-snapshot-verification/v1", "valid": False,
                "reason": str(exc) if isinstance(exc, SnapshotInvalid) else "snapshot_invalid_contract", "current_use": "not_evaluated"}
