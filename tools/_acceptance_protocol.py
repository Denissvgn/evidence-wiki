"""Finite acceptance evidence joins; this gate supplies no native authority."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import random
import re
from pathlib import Path

MAX_CASES = 32
MAX_NODES = 512
MAX_FILES = 8192
MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024
REQUIRED_CATEGORIES = frozenset({
    "stale-review", "forged-review", "prompt-injection", "cross-workspace-authority",
    "replay", "request-race", "permission-revocation", "output-substitution", "cancellation",
    "context-overload", "unsupported-format", "hostile-evaluator-generator", "forged-role",
    "colluding-output", "hidden-answer-leakage", "threshold-editing", "judge-order", "infinite-repair",
})
AUTOMATIC_CATEGORIES = frozenset({
    "supported-claim", "reversed-quote", "missing-evidence", "judge-disagreement", "arithmetic-consensus",
    "unqualified-judge", "human-policy", "spoofed-principal", "source-drift", "threshold-or-key-access",
    "provider-timeout", "repair-oscillation", "durable-mutation-death", "unattended-transports",
    "simulated-approval", "absent-usage", "ungranted-delivery", "truncated-context",
})


def canonical_json(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode()


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def hash_file(path):
    path = Path(path)
    before = path.stat()
    if not path.is_file() or before.st_size > MAX_INPUT_BYTES:
        raise ValueError("acceptance_file_bound")
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) != (
            after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size):
        raise ValueError("acceptance_input_changed_while_reading")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def failure_source_locations(root, paths):
    """Index only explicitly selected frozen source declarations, without imports."""
    locations = {}
    for relative in sorted(set(paths)):
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py" or len(relative.encode()) > 256:
            continue
        source = Path(root) / path
        if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_RECORD_BYTES:
            continue
        raw = source.read_bytes()
        try:
            tree = ast.parse(raw)
        except (SyntaxError, ValueError, UnicodeError):
            continue
        functions = [[node.name, min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)]),
                      node.lineno, node.end_lineno] for node in ast.walk(tree)
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        locations[relative] = {"sha256": hashlib.sha256(raw).hexdigest(), "functions": functions}
    return locations


def validate_failure_diagnostic(value, *, nodeid, phase, run_nonce=None, capture_pid=None, source_locations=None):
    """Validate bounded diagnostic identifiers; they supply no acceptance proof."""
    value = bounded_json(value, 32768)
    fields = {"schema_version", "nodeid", "phase", "run_nonce", "capture_pid", "exception_type",
              "exception_module", "error_code", "reason", "error_path", "frames", "frame_count",
              "frames_truncated", "redacted", "full_trace_replay", "exception_identity_sha256"}
    private = ("credential", "password", "secret", "token", "authentication", "private_key", "signing_key")
    def token(item, pattern):
        return item is None or (type(item) is str and re.fullmatch(pattern, item) is not None
            and not any(word in item.lower() for word in private))
    if (type(value) is not dict or set(value) != fields or value["schema_version"] != "acceptance-failure-diagnostic/v1"
            or value["nodeid"] != nodeid or value["phase"] != phase or phase not in {"setup", "call", "teardown"}
            or type(value["capture_pid"]) is not int or value["capture_pid"] <= 0
            or type(value["run_nonce"]) is not str or re.fullmatch(r"[a-f0-9]{32}", value["run_nonce"]) is None
            or (run_nonce is not None and value["run_nonce"] != run_nonce)
            or (capture_pid is not None and value["capture_pid"] != capture_pid)
            or any(value[name] is not None for name in ("error_code", "reason", "error_path"))
            or type(value["exception_identity_sha256"]) is not str
            or re.fullmatch(r"[a-f0-9]{64}", value["exception_identity_sha256"]) is None
            or (value["exception_type"] is None) != (value["exception_module"] is None)
            or (value["exception_type"] is not None and (value["exception_module"] != "builtins"
                or value["exception_type"] not in {"AssertionError", "AttributeError", "EOFError", "Exception",
                    "FileNotFoundError", "ImportError", "IndexError", "KeyError", "LookupError", "ModuleNotFoundError",
                    "NotImplementedError", "OSError", "PermissionError", "RecursionError", "RuntimeError", "StopIteration",
                    "SyntaxError", "SystemExit", "TimeoutError", "TypeError", "UnicodeError", "ValueError", "ZeroDivisionError"}
                or value["exception_identity_sha256"] != digest(["builtins", value["exception_type"]])))
            or type(value["frames"]) is not list or type(value["frame_count"]) is not int or value["frame_count"] < 0
            or len(value["frames"]) != min(32, value["frame_count"])
            or type(value["frames_truncated"]) is not bool or value["frames_truncated"] != (value["frame_count"] > 32)
            or value["redacted"] is not True or value["full_trace_replay"] != "unqualified"):
        raise ValueError("acceptance_failure_diagnostic_binding")
    for frame in value["frames"]:
        if (type(frame) is not dict or set(frame) != {"path", "source_sha256", "line", "function", "location_sha256"}
                or type(frame["location_sha256"]) is not str or re.fullmatch(r"[a-f0-9]{64}", frame["location_sha256"]) is None
                or (frame["path"] is None and any(frame[name] is not None for name in ("source_sha256", "line", "function")))
                or (frame["path"] is not None and (type(frame["path"]) is not str or len(frame["path"].encode()) > 256
                    or Path(frame["path"]).is_absolute() or ".." in Path(frame["path"]).parts
                    or type(frame["line"]) is not int or frame["line"] < 1
                    or not token(frame["function"], r"[A-Za-z_][A-Za-z0-9_.-]{0,255}")
                    or type(frame["source_sha256"]) is not str or re.fullmatch(r"[a-f0-9]{64}", frame["source_sha256"]) is None))):
            raise ValueError("acceptance_failure_diagnostic_frame")
        if frame["path"] is not None and source_locations is not None:
            source = source_locations.get(frame["path"])
            if source is None or source["sha256"] != frame["source_sha256"] or not any(
                    name == frame["function"] and start <= frame["line"] <= end
                    for name, _first, start, end in source["functions"]):
                raise ValueError("acceptance_failure_diagnostic_source")
    return value


def bounded_json(value, maximum=MAX_RECORD_BYTES):
    """Reject cyclic, deep and oversized observations before serialization."""
    pending, count, size = [(value, 0, frozenset())], 0, 0
    while pending:
        item, depth, ancestors = pending.pop()
        count += 1
        if count > 32768 or depth > 16:
            raise ValueError("acceptance_observation_bound")
        if type(item) in {dict, list, tuple}:
            if id(item) in ancestors or len(item) > 4096:
                raise ValueError("acceptance_observation_bound")
            parents = ancestors | {id(item)}
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    raise ValueError("acceptance_observation_shape")
                children = [*item.keys(), *item.values()]
            else:
                children = item
            pending.extend((child, depth + 1, parents) for child in children)
        elif type(item) is str:
            size += len(item.encode())
            if size > maximum:
                raise ValueError("acceptance_observation_bound")
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("acceptance_observation_shape")
        elif type(item) is int:
            if item.bit_length() > 128:
                raise ValueError("acceptance_observation_bound")
        elif item is not None and type(item) is not bool:
            raise ValueError("acceptance_observation_shape")
    raw = canonical_json(value)
    if len(raw) > maximum:
        raise ValueError("acceptance_observation_bound")
    return json.loads(raw)


def policy(catalog="attack"):
    """Return detached fixed rules; challenge data cannot change thresholds."""
    if catalog not in {"attack", "automatic"}:
        raise ValueError("acceptance_catalog_profile")
    value = {"version": "acceptance-policy/v2", "critical_bypasses_allowed": 0,
            "execution_order": "seeded-case-priority-module-blocks/v1",
            "all_exact_nodes_required": True, "all_assertions_required": True,
            "retry_budget": 0, "workers": 1, "max_cases": MAX_CASES, "max_nodes": MAX_NODES,
            "max_files": MAX_FILES, "max_input_bytes": MAX_INPUT_BYTES,
            "output_limit_bytes": MAX_RECORD_BYTES, "required_categories": sorted(REQUIRED_CATEGORIES),
            "all_challenges_critical": True, "max_artifacts_per_node": 32,
            "max_native_bytes_per_node": 8 * MAX_RECORD_BYTES, "max_native_file_bytes": MAX_RECORD_BYTES,
            "max_projected_records": 32, "max_owner_result_bytes": 32768, "max_event_bytes": 65536}
    if catalog == "automatic":
        value.update(catalog_profile="automatic", required_categories=sorted(AUTOMATIC_CATEGORIES))
    return value


def catalog_profile(cases):
    """Recognize only the two fixed complete obligation families."""
    categories = {category for case in cases for category in case["categories"]}
    if categories == REQUIRED_CATEGORIES:
        return "attack"
    if categories == AUTOMATIC_CATEGORIES:
        return "automatic"
    raise ValueError("acceptance_category_inventory_incomplete")


def validate_cases(cases):
    cases = bounded_json(cases)
    if type(cases) is not list or not 1 <= len(cases) <= MAX_CASES:
        raise ValueError("acceptance_case_inventory")
    identifiers, categories, selectors = set(), set(), set()
    for case in cases:
        if (type(case) is not dict or set(case) != {"id", "categories", "severity", "evidence_requirement", "selectors"}
                or type(case["id"]) is not str or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", case["id"]) is None
                or case["id"] in identifiers or type(case["severity"]) is not str
                or case["severity"] != "critical"
                or type(case["evidence_requirement"]) is not str
                or case["evidence_requirement"] not in {"assertions", "owner_receipt", "native_artifact"}
                or type(case["categories"]) is not list or not case["categories"]
                or any(type(category) is not str for category in case["categories"])
                or len(set(case["categories"])) != len(case["categories"])
                or not set(case["categories"]) <= REQUIRED_CATEGORIES | AUTOMATIC_CATEGORIES
                or type(case["selectors"]) is not list or not 1 <= len(case["selectors"]) <= MAX_NODES):
            raise ValueError("acceptance_case_shape")
        identifiers.add(case["id"])
        categories.update(case["categories"])
        for selector in case["selectors"]:
            if (type(selector) is not str or len(selector.encode()) > 1024
                    or re.fullmatch(r"tests/[A-Za-z0-9_/.-]+\.py(?:::[^\s]+)+", selector) is None
                    or ".." in Path(selector.split("::")[0]).parts or selector in selectors):
                raise ValueError("acceptance_selector_shape_or_duplicate")
            selectors.add(selector)
    catalog_profile(cases)
    return cases


def selector_matches(selector, nodeid):
    return nodeid == selector or nodeid.startswith(selector + "[") or nodeid.startswith(selector + "::")


def freeze_manifest(cases, nodeids, *, inputs, seed=0, timeout_seconds=1200):
    """Expand every selector into a unique exact-node obligation before running."""
    cases = validate_cases(cases)
    if (type(seed) is not int or not 0 <= seed <= 2**32 - 1 or type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 3600):
        raise ValueError("acceptance_execution_budget")
    if (type(nodeids) is not list or not 1 <= len(nodeids) <= MAX_NODES
            or any(type(node) is not str or not node or len(node.encode()) > 1024 for node in nodeids)
            or len(set(nodeids)) != len(nodeids)):
        raise ValueError("acceptance_exact_inventory")
    membership = {}
    for node in nodeids:
        matches = [case["id"] for case in cases if any(selector_matches(s, node) for s in case["selectors"])]
        if len(matches) != 1:
            raise ValueError("acceptance_node_membership")
        membership[node] = matches[0]
    for case in cases:
        for selector in case["selectors"]:
            if not any(selector_matches(selector, node) for node in nodeids):
                raise ValueError("acceptance_unresolved_selector")
    order = [case["id"] for case in cases]
    random.Random(seed).shuffle(order)  # noqa: S311 -- reproducible challenge ordering, never secret material.
    priority_nodes = [node for identifier in order for node in nodeids if membership[node] == identifier]
    modules = list(dict.fromkeys(node.split("::", 1)[0] for node in priority_nodes))
    exact = [node for module in modules for node in priority_nodes if node.split("::", 1)[0] == module]
    value = {"schema_version": "acceptance-manifest/v2", "cases": cases, "policy": policy(catalog_profile(cases)),
             "inputs": bounded_json(inputs), "seed": seed, "challenge_priority": order, "module_order": modules,
             "timeout_seconds": timeout_seconds, "collected_nodeids": list(nodeids),
             "exact_nodeids": exact, "membership": membership}
    value["manifest_sha256"] = digest(value)
    return value


def validate_manifest(value):
    """Reconstruct closed obligations and order; a self-consistent hash is insufficient."""
    value = bounded_json(value)
    fields = {"schema_version", "cases", "policy", "inputs", "seed", "challenge_priority", "module_order", "timeout_seconds",
              "collected_nodeids", "exact_nodeids", "membership", "manifest_sha256"}
    if type(value) is not dict or set(value) != fields or type(value["inputs"]) is not dict:
        raise ValueError("acceptance_manifest_shape")
    expected = freeze_manifest(value["cases"], value["collected_nodeids"], inputs=value["inputs"],
                               seed=value["seed"], timeout_seconds=value["timeout_seconds"])
    if value != expected:
        raise ValueError("acceptance_manifest_reconstruction_conflict")
    return expected


def grade_evidence(manifest, events, *, drift=False, process_complete=False, evidence_intact=True):
    """Join one ordered actual-node lifecycle; known failures dominate gaps."""
    failures, gaps = [], []
    supplied = manifest
    if type(supplied) is dict:
        try:
            if supplied.get("policy") != policy(catalog_profile(supplied["cases"])):
                failures.append("policy_drift")
        except (ValueError, TypeError, KeyError):
            failures.append("policy_drift")
    try:
        manifest = validate_manifest(supplied)
    except (ValueError, TypeError, KeyError, UnicodeError):
        gaps.append("manifest_invalid")
        manifest = {"cases": [], "exact_nodeids": [], "membership": {}}
    expected = manifest["exact_nodeids"]
    if drift:
        failures.append("input_drift")
    if not evidence_intact:
        failures.append("retained_evidence_drift")
    if not process_complete:
        gaps.append("process_completion_unqualified")
    reports, starts, finishes, receipts, artifacts, assertions = {}, [], [], {}, {}, {}
    states, seen_events, seen_artifacts = {}, set(), set()
    active = None
    critical_nodes = {node for node in expected if any(case["severity"] == "critical"
                      and manifest["membership"].get(node) == case["id"] for case in manifest["cases"])}
    for event in events:
        try:
            event = bounded_json(event, 65536)
        except (ValueError, TypeError, UnicodeError):
            gaps.append("malformed_event")
            continue
        if type(event) is not dict or type(event.get("nodeid")) is not str or event["nodeid"] not in expected:
            gaps.append("malformed_or_extra_event")
            continue
        node, kind = event["nodeid"], event.get("event")
        fingerprint = digest(event)
        if fingerprint in seen_events:
            gaps.append("duplicate_event")
            continue
        seen_events.add(fingerprint)
        state = states.get(node, "unstarted")
        if kind == "started":
            if set(event) != {"event", "nodeid"} or state != "unstarted" or active is not None:
                gaps.append("invalid_start_order")
                continue
            starts.append(node)
            active, states[node] = node, "setup"
            continue
        if active != node or state in {"unstarted", "finished"}:
            gaps.append("event_outside_active_node")
            continue
        if kind == "report":
            required = {"event", "nodeid", "phase", "outcome", "wasxfail", "assertion_failure"}
            if (not required <= set(event) or set(event) - required - {"failure_sha256", "failure_diagnostic"}
                    or type(event["phase"]) is not str or event["phase"] not in {"setup", "call", "teardown"}
                    or type(event["outcome"]) is not str or event["outcome"] not in {"passed", "failed", "skipped"}
                    or type(event["assertion_failure"]) is not bool
                    or (event["wasxfail"] is not None and type(event["wasxfail"]) is not str)):
                gaps.append("malformed_phase")
                continue
            if "failure_diagnostic" in event:
                try:
                    if event["outcome"] != "failed" or re.fullmatch(r"[a-f0-9]{64}", event.get("failure_sha256", "")) is None:
                        raise ValueError("acceptance_failure_diagnostic_phase")
                    validate_failure_diagnostic(event["failure_diagnostic"], nodeid=node, phase=event["phase"])
                except (ValueError, TypeError, KeyError):
                    gaps.append("malformed_failure_diagnostic")
            phase = event["phase"]
            allowed = {"setup": {"setup"}, "call": {"call", "asserted"}, "teardown": {"teardown", "setup_failed"}}
            if state not in allowed[phase]:
                gaps.append("invalid_phase_order")
                continue
            reports.setdefault((node, phase), []).append(event)
            if phase == "setup":
                states[node] = "call" if event["outcome"] == "passed" else "setup_failed"
            elif phase == "call":
                states[node] = "teardown"
                if event["outcome"] == "failed":
                    if event["assertion_failure"]:
                        failures.append("native_assertion_failure:" + node)
                    elif node in critical_nodes:
                        failures.append("critical_call_failure:" + node)
            else:
                states[node] = "artifacts"
        elif kind == "owner_result":
            if (state != "call" or set(event) != {"event", "nodeid", "owner", "result", "result_sha256", "owner_result_sha256"}
                    or type(event["owner"]) is not str or not 1 <= len(event["owner"].encode()) <= 256
                    or type(event["result"]) is not dict or event["result_sha256"] != digest(event["result"])
                    or type(event["owner_result_sha256"]) is not str
                    or re.fullmatch(r"[a-f0-9]{64}", event["owner_result_sha256"]) is None):
                gaps.append("malformed_or_misordered_owner_receipt")
                continue
            receipts.setdefault(node, []).append(event)
        elif kind == "assertions":
            if (state != "call" or set(event) != {"event", "nodeid", "observed"}
                    or type(event["observed"]) is not int or not 0 <= event["observed"] <= 32768):
                gaps.append("malformed_or_misordered_assertions")
                continue
            assertions.setdefault(node, []).append(event["observed"])
            states[node] = "asserted"
        elif kind == "artifacts":
            refs = event.get("refs")
            if (state != "artifacts" or set(event) != {"event", "nodeid", "refs"}
                    or type(refs) is not list or len(refs) > 32):
                gaps.append("malformed_or_misordered_artifacts")
                continue
            valid = True
            for ref in refs:
                if (type(ref) is not dict or set(ref) != {"path", "sha256", "native_sha256", "changed_since_setup"}
                        or type(ref["changed_since_setup"]) is not bool
                        or type(ref["path"]) is not str or re.fullmatch(r"[a-f0-9]{64}\.json", ref["path"]) is None
                        or ref["path"] in seen_artifacts
                        or any(type(ref[field]) is not str or re.fullmatch(r"[a-f0-9]{64}", ref[field]) is None
                               for field in ("sha256", "native_sha256"))):
                    valid = False
                    break
                seen_artifacts.add(ref["path"])
            if not valid:
                gaps.append("malformed_duplicate_or_cross_node_artifact")
                continue
            artifacts[node] = refs
            states[node] = "finish"
        elif kind == "finished":
            if state != "finish" or set(event) != {"event", "nodeid"}:
                gaps.append("invalid_finish_order")
                continue
            finishes.append(node)
            active, states[node] = None, "finished"
        else:
            gaps.append("unsupported_event")
    if starts != expected or finishes != expected:
        gaps.append("missing_extra_duplicate_or_reordered_execution")
    case_rows = []
    for case in manifest["cases"]:
        nodes = [node for node in expected if manifest["membership"][node] == case["id"]]
        passed = 0
        for node in nodes:
            joined = [reports.get((node, phase), []) for phase in ("setup", "call", "teardown")]
            native_pass = all(len(rows) == 1 and rows[0]["outcome"] == "passed" and rows[0]["wasxfail"] is None for rows in joined)
            checked = len(assertions.get(node, [])) == 1 and assertions[node][0] > 0
            requirement = case["evidence_requirement"]
            owner_ok = (requirement == "assertions"
                        or (requirement == "owner_receipt" and (bool(receipts.get(node))
                            or any(ref["changed_since_setup"] for ref in artifacts.get(node, []))))
                        or (requirement == "native_artifact" and bool(artifacts.get(node))))
            if native_pass and checked and owner_ok and states.get(node) == "finished":
                passed += 1
            else:
                gaps.append("incomplete_node:" + node)
        case_failed = any(reason in {"native_assertion_failure:" + node, "critical_call_failure:" + node}
                          for reason in failures for node in nodes)
        case_rows.append({"case_id": case["id"], "severity": case["severity"], "expected_nodes": len(nodes),
                          "passed_nodes": passed, "status": "FAIL" if case_failed else "PASS" if passed == len(nodes) > 0 else "ABSTAIN"})
    status = "FAIL" if failures else "ABSTAIN" if gaps or not expected else "PASS"
    return {"schema_version": "acceptance-grade/v1", "status": status, "failures": sorted(set(failures)),
            "gaps": sorted(set(gaps)), "cases": case_rows,
            "denominators": {"challenges": len(case_rows), "exact_nodes": len(expected), "started_nodes": len(starts),
                             "finished_nodes": len(finishes), "native_assertion_failures": sum(reason.startswith("native_assertion_failure:") for reason in set(failures)),
                             "failed_checks": sum(reason.startswith(("native_assertion_failure:", "critical_call_failure:")) for reason in set(failures))},
            "native_authorization": None, "answer_success": None, "monetary_cost": None, "model_calls": None, "tokens": None}
