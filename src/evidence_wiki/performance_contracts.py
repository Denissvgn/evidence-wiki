"""Frozen native performance workloads and deterministic qualification rules."""

import math

from .mutation_contracts import MutationError, _shape, canonical, detached, digest
from .pack_discovery import owner
from .research_session_contracts import refuse

PLAN = "research-performance-selection/v1"
AUDIT = "research-performance-audit/v1"
REPORT = "research-performance-qualification/v1"
THRESHOLDS = {"validation_seconds": 1.0, "serialization_seconds": 1.0,
    "traced_peak_bytes": 64*1024*1024, "probe_wall_seconds": 30.0, "process_peak_rss_bytes": 256*1024*1024, "samples_per_workload": 4}


def limits():
    from . import _pack_io
    from .review_packet_contracts import schema_document
    packet = schema_document("research-paged-review/v1")
    return {"strict_claims": {"units": owner("_strict_contract").MAX_CLAIMS, "bytes": owner("_strict_contract").MAX_BYTES},
        "assessment_changes": {"units": 128, "bytes": owner("_assessment_contract").MAX_BYTES,
            "continuation_page": owner("_assessment_contract").MAX_ASSESSMENTS},
        "packet_sources": {"units": packet["properties"]["sources"]["maxItems"], "bytes": 524288},
        "output_bytes": {"units": _pack_io.MAX_FILE, "bytes": _pack_io.MAX_FILE}}


def catalog(policy, *, seed):
    if type(seed) is not int or not 0 <= seed <= 2**32-1:
        refuse("PERFORMANCE_SEED_INVALID")
    frozen = policy.to_dict()
    rows = []
    for capability, bound in limits().items():
        maximum = bound["units"]
        small = 1024 if capability == "output_bytes" else max(1, min(maximum, frozen["budgets"]["claims" if capability == "strict_claims" else "sources"]))
        small = min(small, maximum//2 or 1)
        for label, size in (("small", small), ("boundary", maximum), ("overflow", maximum+1)):
            row = {"capability": capability, "variant": label, "size": size, "seed": seed,
                "expected": "refused" if label == "overflow" else "validated", "limits": bound}
            row["workload_id"] = digest(row)
            rows.append(row)
    return rows


def schemas():
    def obj(**fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    sha = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    text = {"type": "string", "minLength": 1, "maxLength": 256}
    selection = obj(schema_version={"const": PLAN}, catalog_id=sha,
        workload_ids={"type": "array", "minItems": 12, "maxItems": 12, "uniqueItems": True, "items": sha})
    audit = obj(schema_version={"const": AUDIT}, measurement_id=sha, complete={"type": "boolean"},
        issues={"type": "array", "maxItems": 32, "items": text})
    report = obj(schema_version={"const": REPORT}, request_id=text, binding_id=sha, policy_id=sha,
        status={"enum": ["passed", "failed", "incomplete"]}, complete={"type": "boolean"},
        performance_qualified={"type": "boolean"}, reason=text,
        hardware={"type": "object", "additionalProperties": True}, catalog_id=sha,
        workloads={"type": "array", "maxItems": 12, "items": {"type": "object", "additionalProperties": True}},
        measurements={"type": "array", "maxItems": 12, "items": {"type": "object", "additionalProperties": True}},
        thresholds={"type": "object", "additionalProperties": True},
        agent_accounting={"type": "array", "maxItems": 2, "items": {"type": "object", "additionalProperties": True}},
        critic={"anyOf": [audit, {"type": "null"}]},
        violations={"type": "array", "maxItems": 128, "items": text},
        measurement_scope={"const": "synthetic_native_contract_validation; not evidence acceptance or owner publication"},
        historical={"type": "boolean"}, authorized={"const": False}, evidence_accepted={"const": False})
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}
        for name, schema in ((PLAN, selection), (AUDIT, audit), (REPORT, report))}


def checked(name, value):
    value = detached(value)
    try:
        _shape(value, schemas()[name])
    except (MutationError, KeyError):
        refuse("PERFORMANCE_DOCUMENT_INVALID")
    if len(canonical(value)) > 1_048_576:
        refuse("LIMIT")
    if name == REPORT:
        if value["catalog_id"] != digest(value["workloads"]) or value["thresholds"] != THRESHOLDS:
            refuse("PERFORMANCE_RESULT_INVALID")
        reproduced = gate(value["workloads"], value["measurements"], value["critic"], value["hardware"], value["agent_accounting"])
        if any(value[key] != reproduced[key] for key in reproduced):
            refuse("PERFORMANCE_RESULT_INVALID")
    return value


def select(rows, proposed):
    proposed = checked(PLAN, proposed)
    if proposed["catalog_id"] != digest(rows) or set(proposed["workload_ids"]) != {row["workload_id"] for row in rows}:
        refuse("PERFORMANCE_SELECTION_INCOMPLETE")
    selected = {row["workload_id"]: row for row in rows}
    return [selected[identifier] for identifier in proposed["workload_ids"]]


def partition(row):
    if row["expected"] != "refused":
        return None
    return {"kind": "explicit_partition", "capability": row["capability"], "max_units": row["limits"]["units"],
        "max_bytes": row["limits"]["bytes"], "complete": False,
        "constraint": "Each partition needs its own native scope and fresh full review; partitions cannot reuse approval."}


def gate(rows, measured, audit, hardware, accounting):
    failures, complete = [], True
    if (any(type(hardware.get(key)) is not str or not hardware[key] or hardware[key] in {"unknown", "unobserved"} for key in ("machine", "platform", "python"))
            or type(hardware.get("cpu_count")) is not int or hardware["cpu_count"] <= 0):
        failures.append("hardware_unknown"); complete = False
    if len(measured) != len(rows) or {row.get("workload_id") for row in measured} != {row["workload_id"] for row in rows}:
        failures.append("measurement_coverage_incomplete"); complete = False
    expected = {row["workload_id"]: row for row in rows}
    for result in measured:
        original = expected.get(result.get("workload_id"))
        if original is None or result.get("complete") is not True or result.get("truncated") is not False:
            failures.append("measurement_incomplete"); complete = False; continue
        samples = result.get("samples", [])
        if len(samples) != THRESHOLDS["samples_per_workload"] or [sample.get("state") for sample in samples] != ["cold", "warm", "updated", "updated"]:
            failures.append("sample_coverage_incomplete"); complete = False
        for sample in samples:
            if sample.get("outcome") != original["expected"]:
                failures.append("native_boundary_not_reproduced")
            for field in ("validation_seconds", "serialization_seconds", "traced_peak_bytes"):
                value = sample.get(field)
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    failures.append("measurement_unknown"); complete = False
                elif value > THRESHOLDS[field]:
                    failures.append("threshold_exceeded:"+field)
            if type(sample.get("serialized_bytes")) is not int or sample["serialized_bytes"] <= 0:
                failures.append("serialization_unknown"); complete = False
        if [sample.get("revision") for sample in samples] != [0, 0, 1, 2]:
            failures.append("update_coverage_incomplete"); complete = False
        if samples and (samples[0].get("input_sha256") != samples[1].get("input_sha256")
                or len({sample.get("input_sha256") for sample in samples}) != 3):
            failures.append("material_updates_not_reproduced"); complete = False
        rss = result.get("process_peak_rss_bytes")
        if type(rss) is not int or rss <= 0:
            failures.append("rss_unknown"); complete = False
        elif rss > THRESHOLDS["process_peak_rss_bytes"]:
            failures.append("threshold_exceeded:process_peak_rss_bytes")
        if result.get("partition") != partition(original):
            failures.append("partition_contract_not_reproduced")
        if type(result.get("probe_wall_seconds")) not in (int, float) or not math.isfinite(result["probe_wall_seconds"]):
            failures.append("owner_time_unknown"); complete = False
        elif result["probe_wall_seconds"] > THRESHOLDS["probe_wall_seconds"]:
            failures.append("threshold_exceeded:probe_wall_seconds")
    def accounted(row):
        usage = row.get("observed_usage")
        if row.get("complete") is not True or type(usage) is not dict:
            return False
        for field in ("seconds", "model_calls", "tool_calls", "tokens", "cost"):
            try:
                value = usage[field]
                if value is None or type(value) is bool or not math.isfinite(float(value)) or float(value) < 0:
                    return False
            except (KeyError, TypeError, ValueError):
                return False
        return type(usage["model_calls"]) is int and usage["model_calls"] > 0
    if len(accounting) != 2 or any(not accounted(row) for row in accounting):
        failures.append("agent_accounting_unknown"); complete = False
    if audit is None or audit.get("measurement_id") != digest(measured) or audit.get("complete") is not True:
        failures.append("critic_accounting_incomplete"); complete = False
    elif audit["issues"]:
        failures.append("critic_issues")
    return {"status": "incomplete" if not complete else "failed" if failures else "passed",
        "complete": complete, "performance_qualified": complete and not failures,
        "violations": sorted(set(failures))}
