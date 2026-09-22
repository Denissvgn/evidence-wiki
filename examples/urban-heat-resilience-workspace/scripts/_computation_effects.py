"""Explicit, recoverable derived writes and local dispatch over existing owners."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

import yaml
from _computation_contract import MAX_BYTES
from _computation_contract import path as declared_path
from _computation_expression import require
from _computation_runtime import evaluate, sibling
from _computation_schedule import parsed_time
from _evidence_revision import canonical_bytes, capture_workspace

STATE_PATH = "runs/computation/state.json"
LOCK_PATH = "runs/computation/operation.lock"
MAX_STATE_BYTES = 8_388_608
MAX_WRITE_BYTES = 4_194_304
MAX_REQUESTS = 128


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def writable_path(target, config):
    sibling("_record_artifacts").artifact_path(target)
    forbidden = [*config.get("raw", {}).get("source_roots", ["raw"]),
                 config.get("sources", {}).get("normalized_dir", "sources/normalized"),
                 config.get("sources", {}).get("cards_dir", "sources/cards")]
    for root in forbidden:
        declared_path(root)
        require(target != root and not target.startswith(root + "/"), "computation_effect_overlaps_evidence")


def warning_path(target, root, config, result):
    writable_path(target, config)
    receipt = "runs/computation/receipts/" + result["result_id"].split(":")[1] + ".json"
    if target in {"index.md", "log.md", receipt}:
        return
    question_root = sibling("question_status").questions_directory(root, config).relative_to(root).as_posix() + "/"
    allowed = ["invariant-" + row["finding_id"].split(":")[1][:24] for row in result["findings"] if row["severity"] == "warning"]
    require(target.startswith(question_root) and any(re.fullmatch(re.escape(prefix) + r"(?:-[0-9]{1,4})?\.md", target[len(question_root):])
                for prefix in allowed), "computation_intake_path_invalid")


def state_from(capture):
    raw = capture.files.get(STATE_PATH)
    if raw is None:
        return {"schema_version": "evidence-computation-state/v1", "outputs": {}, "requests": {}, "occurrences": {}, "findings": {}}, None
    require(len(raw) <= MAX_STATE_BYTES, "computation_state_bound")
    state = sibling("_record_artifacts").json_document(raw)
    require(set(state) == {"schema_version", "outputs", "requests", "occurrences", "findings"}
            and state["schema_version"] == "evidence-computation-state/v1", "computation_state_invalid")
    require(all(type(state[key]) is dict for key in ("outputs", "requests", "occurrences", "findings"))
            and len(state["requests"]) <= MAX_REQUESTS and len(state["outputs"]) <= 64
            and len(state["occurrences"]) <= 128 and len(state["findings"]) <= 128, "computation_state_invalid")
    for request in state["requests"].values():
        require(type(request) is dict and set(request) == {"command", "status", "writes", "summary", "outputs", "occurrences", "findings", "started_at"},
                "computation_state_request_invalid")
        require(request["status"] in {"pending", "complete"} and type(request["writes"]) is list and len(request["writes"]) <= 64,
                "computation_state_request_invalid")
        require(type(request["command"]) is dict and set(request["command"]) == {"operation", "expected_result_id", "as_of", "cadence_id"},
                "computation_state_request_invalid")
        parsed_time(request["started_at"], aware=True)
        for write in request["writes"]:
            require(type(write) is dict and set(write) == {"path", "expected", "before", "content", "sha256"}, "computation_state_write_invalid")
            sibling("_record_artifacts").artifact_path(write["path"])
    return state, raw


def pending(root):
    state, _raw = state_from(capture_workspace(Path(root).resolve()))
    return [key for key, request in state["requests"].items() if request.get("status") == "pending"]


def escape(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\\", "\\\\").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("`", "\\`").replace("\n", " ").replace("\r", " ")


def table_model(result, kind, identifier):
    if kind == "aggregation":
        rows = [{"group": row["key"], "metric": name, "value": value["formatted"] if value["formatted"] is not None else value["value"],
                 "unit": value["unit"], "rounded": value["rounded"]}
                for row in result["aggregations"][identifier]["groups"] for name, value in sorted(row["metrics"].items())]
    else:
        rows = [{"group": [], "metric": name, "value": value["formatted"] if value["formatted"] is not None else value["value"],
                 "unit": value["unit"], "rounded": value["rounded"]}
                for name, value in sorted(result["graphs"][identifier]["outputs"].items())]
    return {"columns": ["group", "metric", "value", "unit", "rounded"], "rows": rows}


def block(result, kind, identifier):
    model = table_model(result, kind, identifier)
    lines = [f"<!-- evidence-computation:{kind}:{identifier}:begin -->", "### Computed values", "",
             "Result: `" + result["result_id"] + "`", "", "| Group | Metric | Value | Unit | Rounded |",
             "| --- | --- | --- | --- | --- |"]
    for row in model["rows"]:
        lines.append("| " + " | ".join(escape(json.dumps(row[key], ensure_ascii=False) if key == "group" else row[key]) for key in model["columns"]) + " |")
    lines += ["", "Input basis: `" + result["input_id"] + "`", ""]
    lines.extend("Source: " + escape(source["source_id"]) + " (`" + source["record_sha256"] + "`)" for source in result["sources"])
    lines += ["", "Derived from retained inputs under declared rules; domain meaning requires review.",
              f"<!-- evidence-computation:{kind}:{identifier}:end -->"]
    rendered = "\n".join(lines) + "\n"
    require(len(rendered.encode()) <= MAX_BYTES, "computation_output_bound")
    return rendered


def initial_markdown(result, observed_at, config):
    wiki = config.get("wiki", {})
    require("output" in wiki.get("allowed_page_types", []), "computation_output_metadata_required")
    timestamp = observed_at.astimezone(timezone.utc).date().isoformat()
    metadata = {"type": "output", "created": timestamp, "updated": timestamp,
                "source_ids": sorted(source["source_id"] for source in result["sources"])}
    lint = sibling("lint")
    rule = lint.normalize_frontmatter_type_rules(wiki).get("output", {})
    required_fields = [*wiki.get("frontmatter_required", []), *rule.get("required_fields", [])]
    require(all(lint.missing_reason(metadata, field) is None for field in required_fields), "computation_output_metadata_required")
    require(all(not lint.is_empty_value(metadata.get(field)) for field in rule.get("non_empty_fields", [])), "computation_output_metadata_required")
    require(all(field not in metadata or lint.frontmatter_value_matches_type(metadata[field], kind) for field, kind in rule.get("field_types", {}).items()),
            "computation_output_metadata_required")
    require(all(field not in metadata or metadata[field] in choices for field, choices in rule.get("allowed_values", {}).items()),
            "computation_output_metadata_required")
    return "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n\n# Derived computation\n\n"


def output_files(result, capture, state, observed_at, config):
    files, ownership = {}, {}
    for section, kind, field in (("aggregations", "aggregation", "output_target"), ("graphs", "graph", "output_page")):
        for identifier, declaration in result["definition"][section].items():
            target = declaration[field]
            if target is None:
                continue
            previous = state["outputs"].get(target)
            owner = {"kind": kind, "id": identifier}
            require(previous is None or {key: previous[key] for key in owner} == owner, "computation_output_owner_conflict")
            old = capture.files.get(target)
            if target.endswith(".json"):
                require(old is None or previous is not None and digest(old) == previous["content_hash"], "computation_output_user_owned")
                data = canonical_bytes({"schema_version": "evidence-computation-output/v1", "owner": owner,
                                        "result": result, "table": table_model(result, kind, identifier)})
                owned_hash = digest(data)
            else:
                generated = block(result, kind, identifier)
                begin, end = f"<!-- evidence-computation:{kind}:{identifier}:begin -->", f"<!-- evidence-computation:{kind}:{identifier}:end -->"
                text = old.decode("utf-8") if old is not None else initial_markdown(result, observed_at, config)
                if begin in text or end in text:
                    require(text.count(begin) == text.count(end) == 1 and text.index(begin) < text.index(end), "computation_output_marker_invalid")
                    start, finish = text.index(begin), text.index(end) + len(end)
                    retained = text[start:finish] + "\n"
                    require(previous is not None and digest(retained.encode()) == previous["block_hash"], "computation_output_block_edited")
                    text = text[:start] + generated.rstrip("\n") + text[finish:]
                else:
                    require(previous is None and "<!-- evidence-computation:" not in text, "computation_output_owner_conflict")
                    text = text + "\n\n" + generated
                data, owned_hash = text.encode(), digest(generated.encode())
            require(len(data) <= MAX_BYTES, "computation_output_bound")
            files[target] = data
            ownership[target] = {**owner, "content_hash": digest(data), "block_hash": owned_hash, "result_id": result["result_id"]}
    return files, ownership


def warning_files(root, result, capture, state, observed_at):
    warnings = [finding for finding in result["findings"] if finding["severity"] == "warning"]
    current = {row["finding_id"]: row for row in warnings}
    resolved = sorted(key for key in state["findings"] if key not in current)
    if not warnings:
        return {}, {}, {"created": [], "resolved_finding_ids": resolved}
    batch = {"schema_version": "1.0", "questions": [{
        "id": "invariant-" + finding["finding_id"].split(":")[1][:24],
        "question": "Review computation invariant " + finding["invariant_id"] + " finding " + finding["finding_id"].split(":")[1][:24] + ".",
        "priority": "medium", "origin": "computation",
        "context": json.dumps({"finding": finding, "result_id": result["result_id"]}, ensure_ascii=False, sort_keys=True),
        "metadata": {"computation": {"finding_id": finding["finding_id"], "invariant_id": finding["invariant_id"]}},
    } for finding in warnings]}
    # The existing intake owner decides validation, deduplication, page bodies,
    # index and log changes in a private capture. Only its resulting diff is applied.
    with tempfile.TemporaryDirectory(prefix="computation-intake-") as directory:
        staged = Path(directory) / "workspace"
        staged.mkdir(mode=0o700)
        for name, content in capture.files.items():
            destination = staged / name
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination.write_bytes(content)
            destination.chmod(0o600)
        report = sibling("intake_questions").run_intake_document(staged, batch, dry_run=False,
            from_file_label="computation findings", _observed_at=observed_at)
        after = capture_workspace(staged)
        allowed = {"index.md", "log.md", *[item["path"] for item in report["created"]]}
        files = {name: data for name, data in after.files.items() if data != capture.files.get(name)
                 and not (name == ".locks/log.lock" and data == b"")}
        require(set(files) <= allowed, "computation_intake_effect_scope_changed")
        findings = {finding["finding_id"]: {"invariant_id": finding["invariant_id"], "result_id": result["result_id"]} for finding in warnings}
        return files, findings, {"created": report["created"], "duplicates": report["skipped_duplicates"], "resolved_finding_ids": resolved}


def validate_derived_write(result, target, data, current, state, observed_at, config):
    receipt = "runs/computation/receipts/" + result["result_id"].split(":")[1] + ".json"
    if target == receipt:
        require(data == canonical_bytes(result), "computation_receipt_not_reproduced")
        return
    owners = [(kind, identifier) for section, kind, field in (("aggregations", "aggregation", "output_target"), ("graphs", "graph", "output_page"))
              for identifier, declaration in result["definition"][section].items() if declaration[field] == target]
    require(len(owners) == 1, "computation_output_owner_conflict")
    kind, identifier = owners[0]
    if target.endswith(".json"):
        expected = canonical_bytes({"schema_version": "evidence-computation-output/v1", "owner": {"kind": kind, "id": identifier},
                                    "result": result, "table": table_model(result, kind, identifier)})
        require(current is None or target in state["outputs"] or current == expected, "computation_output_user_owned")
    else:
        generated = block(result, kind, identifier)
        begin, end = f"<!-- evidence-computation:{kind}:{identifier}:begin -->", f"<!-- evidence-computation:{kind}:{identifier}:end -->"
        text = current.decode() if current is not None else initial_markdown(result, observed_at, config)
        if begin in text or end in text:
            require(text.count(begin) == text.count(end) == 1 and text.index(begin) < text.index(end), "computation_output_marker_invalid")
            start, finish = text.index(begin), text.index(end) + len(end)
            retained = text[start:finish] + "\n"
            previous = state["outputs"].get(target)
            require(retained == generated or previous is not None and digest(retained.encode()) == previous["block_hash"],
                    "computation_output_block_edited")
            text = text[:start] + generated.rstrip("\n") + text[finish:]
        else:
            require("<!-- evidence-computation:" not in text, "computation_output_owner_conflict")
            text += "\n\n" + generated
        expected = text.encode()
    require(data == expected, "computation_output_not_reproduced")


def plan(root, operation, result, capture, state, cadence_id, observed_at, config):
    files, outputs, occurrences, findings = {}, dict(state["outputs"]), dict(state["occurrences"]), dict(state["findings"])
    summary = {"operation": operation, "result_id": result["result_id"]}
    if operation == "write":
        require(result["status"] == "passed", "computation_invariants_failed")
        files, changed = output_files(result, capture, state, observed_at, config)
        require(bool(files), "computation_no_output_targets")
        outputs.update(changed)
        summary["paths"] = sorted(files)
    elif operation == "apply-warnings":
        files, findings, details = warning_files(root, result, capture, state, observed_at)
        summary.update(details)
    else:
        require(operation == "dispatch", "computation_operation_unknown")
        rows = [row for row in result["clock"]["schedules"] if row["id"] == cadence_id]
        require(len(rows) == 1 and rows[0]["state"] in {"due", "overdue"}, "computation_action_not_due")
        row = rows[0]
        if row["occurrence_id"] in occurrences:
            summary.update(replayed=True, occurrence=occurrences[row["occurrence_id"]])
            return {}, outputs, occurrences, findings, summary
        action = row["action"]
        if action["kind"] == "evaluate_graph":
            outcome = result["graphs"][action["target"]]
        elif action["kind"] == "check_invariants":
            outcome = next(item for item in result["invariants"] if item["id"] == action["target"])
        else:
            outcome = {"flag": action["target"], "value": True}
        observed = {"occurrence_id": row["occurrence_id"], "action": action, "result_id": result["result_id"], "outcome": outcome}
        occurrences[row["occurrence_id"]] = observed
        summary.update(occurrence=observed, replayed=False)
    receipt = "runs/computation/receipts/" + result["result_id"].split(":")[1] + ".json"
    files[receipt] = canonical_bytes(result)
    require(len(files) <= 64 and sum(len(data) for data in files.values()) <= MAX_WRITE_BYTES, "computation_write_bound")
    return files, outputs, occurrences, findings, summary


def apply(project_root, operation, *, expected_result_id, request_id, as_of=None, cadence_id=None, dry_run=False):
    root = Path(project_root).resolve()
    require(operation in {"write", "apply-warnings", "dispatch"}, "computation_operation_unknown")
    require(type(request_id) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", request_id) is not None,
            "computation_request_id_invalid")
    require(type(expected_result_id) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", expected_result_id) is not None,
            "computation_expected_result_required")
    command = {"operation": operation, "expected_result_id": expected_result_id, "as_of": as_of, "cadence_id": cadence_id}
    before = capture_workspace(root)
    state_from(before)
    config = sibling("_strict_evidence").configuration(root)
    usage = sibling("_evidence_usage")
    preflight = evaluate(root, as_of=as_of, config=config, capture=before)
    require(preflight["result_id"] == expected_result_id, "computation_result_changed")
    writable_path(STATE_PATH, config)
    writable_path(LOCK_PATH, config)
    manager = usage.current_view(root, config) if usage.configured(config) else nullcontext(None)
    publisher = sibling("_usage_materialization")
    if not dry_run:
        publisher.publish_file(root, LOCK_PATH, b"", None)
    guard = sibling("_workspace_locks").workspace_lock(root / LOCK_PATH, purpose="computation effect") if not dry_run else nullcontext(None)
    with guard as lock, manager as view:
        require(dry_run or lock.locked, "computation_lock_required")
        capture = capture_workspace(root)
        state, state_bytes = state_from(capture)
        result = evaluate(root, as_of=as_of, config=config, capture=capture, view=view)
        require(result["result_id"] == expected_result_id, "computation_result_changed")
        prior = state["requests"].get(request_id)
        if prior is not None:
            require(prior["command"] == command, "computation_request_conflict")
            if prior["status"] == "complete":
                if operation == "write":
                    for target in prior["summary"]["paths"]:
                        writable_path(target, config)
                        current = capture.files.get(target)
                        require(current is not None, "computation_output_missing")
                        validate_derived_write(result, target, current, current, state, parsed_time(prior["started_at"], aware=True), config)
                return {**prior["summary"], "replayed": True, "dry_run": dry_run}
        require(not any(key != request_id and row["status"] == "pending" for key, row in state["requests"].items()),
                "computation_recovery_required")
        if prior is None:
            require(len(state["requests"]) < MAX_REQUESTS, "computation_request_bound")
            started_at = datetime.now(timezone.utc).replace(microsecond=0)
            files, outputs, occurrences, findings, summary = plan(root, operation, result, capture, state, cadence_id, started_at, config)
            writes = [{"path": name, "expected": digest(capture.files[name]) if name in capture.files else None,
                       "before": base64.b64encode(capture.files[name]).decode() if name in capture.files else None,
                       "content": base64.b64encode(data).decode(), "sha256": digest(data)} for name, data in sorted(files.items())]
            prior = {"command": command, "status": "pending", "writes": writes, "summary": summary,
                     "outputs": outputs, "occurrences": occurrences, "findings": findings, "started_at": started_at.isoformat()}
        elif operation == "apply-warnings":
            restored = dict(capture.files)
            for write in prior["writes"]:
                warning_path(write["path"], root, config, result)
                content = capture.files.get(write["path"])
                require((digest(content) if content is not None else None) in {write["expected"], write["sha256"]},
                        "computation_recovery_destination_changed")
                before_content = base64.b64decode(write["before"], validate=True) if write["before"] is not None else None
                require((digest(before_content) if before_content is not None else None) == write["expected"], "computation_recovery_snapshot_invalid")
                if before_content is None:
                    restored.pop(write["path"], None)
                else:
                    restored[write["path"]] = before_content
            original = replace(capture, files=MappingProxyType(restored))
            files, outputs, occurrences, findings, summary = plan(root, operation, result, original, state, cadence_id, parsed_time(prior["started_at"], aware=True), config)
            require(set(files) == {item["path"] for item in prior["writes"]}, "computation_intake_plan_changed")
            for write in prior["writes"]:
                require(base64.b64decode(write["content"], validate=True) == files[write["path"]], "computation_intake_not_reproduced")
            prior.update(outputs=outputs, occurrences=occurrences, findings=findings, summary=summary)
        if dry_run:
            require(capture_workspace(root).revision_id == before.revision_id, "computation_inputs_changed")
            return {**prior["summary"], "dry_run": True, "planned_paths": [row["path"] for row in prior["writes"]]}
        def persist():
            nonlocal state_bytes
            data = canonical_bytes(state)
            require(len(data) <= MAX_STATE_BYTES, "computation_state_bound")
            publisher.publish_file(root, STATE_PATH, data, digest(state_bytes) if state_bytes is not None else None)
            state_bytes = data
        state["requests"][request_id] = prior
        allowed = {value[field] for section, field in (("aggregations", "output_target"), ("graphs", "output_page"))
                   for value in result["definition"][section].values() if value[field] is not None}
        question_root = sibling("question_status").questions_directory(root, config).relative_to(root).as_posix() + "/"
        inputs = {"research.yml", config.get("sources", {}).get("manifest_path", "sources/manifest.jsonl")}
        roots = [config.get("sources", {}).get("normalized_dir", "sources/normalized"), *config.get("raw", {}).get("source_roots", ["raw"])]
        def input_files(snapshot):
            return {name: digest(data) for name, data in snapshot.files.items()
                    if name in inputs or any(name == prefix or name.startswith(prefix.rstrip("/") + "/") for prefix in roots)}
        expected_inputs = input_files(capture)
        def closing():
            require(input_files(capture_workspace(root)) == expected_inputs, "computation_inputs_changed")
            require(sibling("_selected_publication").producer_identity() == result["engine_id"], "computation_engine_changed")
            require(sibling("_computation_schedule").zone_identity(result["definition"]["clock"]["timezone"])[1] == result["clock"]["timezone"],
                    "computation_clock_basis_changed")
            if view is not None:
                view.revalidate(root, config)
        closing()
        persist()
        for write in prior["writes"]:
            target = write["path"]
            writable_path(target, config)
            if operation == "apply-warnings":
                warning_path(target, root, config, result)
            authorized = target in allowed if operation == "write" else operation == "apply-warnings" and (target in {"index.md", "log.md"} or target.startswith(question_root) and target.endswith(".md"))
            receipt = "runs/computation/receipts/" + expected_result_id.split(":")[1] + ".json"
            require(authorized or target == receipt, "computation_effect_path_invalid")
            data = base64.b64decode(write["content"], validate=True)
            require(len(data) <= MAX_BYTES and digest(data) == write["sha256"], "computation_effect_bytes_invalid")
            if operation == "write" or target == receipt:
                validate_derived_write(result, target, data, capture_workspace(root).files.get(target), state,
                                       parsed_time(prior["started_at"], aware=True), config)
            publisher.publish_file(root, target, data, write["expected"], before_publish=closing)
        closing()
        state.update(outputs=prior["outputs"], occurrences=prior["occurrences"], findings=prior["findings"])
        prior["status"], prior["writes"] = "complete", []
        persist()
        return {**prior["summary"], "replayed": prior["summary"].get("replayed", False), "dry_run": False}
