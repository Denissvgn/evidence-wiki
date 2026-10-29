"""Fresh-interpreter native codec measurements over synthetic bounded fixtures."""

import hashlib
import random
import sys
import time
import tracemalloc

from ._pack_io import canonical, json_document
from .mutation_contracts import digest
from .pack_discovery import owner
from .performance_contracts import limits, partition
from .research_session_contracts import refuse
from .review_packet_contracts import schema_document, validate


def _process_peak_rss_bytes():
    if sys.platform == "win32":
        import ctypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32),
                *[(name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessMemoryCounters), ctypes.c_uint32]
        kernel.K32GetProcessMemoryInfo.restype = ctypes.c_int
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not kernel.K32GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        rss = counters.PeakWorkingSetSize
    else:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            rss *= 1024
    if type(rss) is not int or rss <= 0:
        raise OSError("Process peak resident memory is unavailable")
    return rss


def stub(schema):
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return stub(schema["anyOf"][0])
    kind = schema.get("type")
    if kind == "object":
        return {key: stub(value) for key, value in schema.get("properties", {}).items() if key in schema.get("required", [])}
    if kind == "array":
        return [stub(schema["items"]) for _ in range(schema.get("minItems", 0))]
    if kind in {"integer", "number"}:
        return schema.get("minimum", 0)
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    if "sha256:" in schema.get("pattern", ""):
        return "sha256:"+"a"*64
    return "x"*max(1, schema.get("minLength", 0))


def fixture(row, revision):
    count, capability = row["size"], row["capability"]
    rng = random.Random(row["seed"]+revision)  # noqa: S311 - reproducible synthetic codec values, never secrets
    if capability == "strict_claims":
        native = owner("_strict_contract")
        value = {"schema_version": native.CLAIMS_SCHEMA, "questions": [{"slug": "q", "original_id": "q", "question": "Synthetic measurement?"}],
            "claims": [{"id": "claim-"+str(i), "question_slug": "q", "qualification": "attributed", "text": "Synthetic value "+str(rng.random()),
                "scope": "synthetic", "time": "2026-10-07", "units": "ratio", "evidence": [], "premises": [],
                "derivation": None, "limitations": ["Synthetic codec fixture; no approval."]} for i in range(count)]}
        return value, native.claims_document
    if capability == "assessment_changes":
        native = owner("_assessment_contract")
        value = {"schema_version": native.REFRESH_REQUEST, "changed_sources": [{"source_id": "source-"+str(i),
            "source_revision": "sha256:"+format(rng.getrandbits(256), '064x')} for i in range(count)],
            "evaluation_time": None, "limit": native.MAX_ASSESSMENTS, "cursor": None}
        return value, native.refresh_request
    if capability == "packet_sources":
        value = stub(schema_document("research-paged-review/v1"))
        shape = schema_document("research-paged-review/v1")["properties"]["sources"]["items"]
        value["sources"] = [{**stub(shape), "source_id": "source-"+str(i), "record_sha256": "sha256:"+format(rng.getrandbits(256), '064x')} for i in range(count)]
        return value, lambda data: validate("research-paged-review/v1", data)
    value = {"chunks": [], "revision": revision}
    while len(canonical(value)) < count:
        remaining = count-len(canonical(value))
        if not value["chunks"] or len(value["chunks"][-1]) >= 65536:
            value["chunks"].append("")
        width = min(65536-len(value["chunks"][-1]), count-len(canonical(value)))
        if width > 0:
            value["chunks"][-1] += "x"*width
        elif remaining <= 3:
            value["chunks"].pop()
            value["chunks"][-1] = value["chunks"][-1][:-4]
    return value, lambda data: json_document(canonical(data))


def run(row):
    if type(row) is not dict or set(row) != {"capability", "variant", "size", "seed", "expected", "limits", "workload_id"}:
        refuse("PERFORMANCE_WORKLOAD_INVALID")
    bounds = limits()
    if (row["capability"] not in bounds or type(row["size"]) is not int
            or not 1 <= row["size"] <= bounds[row["capability"]]["units"]+1
            or type(row["seed"]) is not int or not 0 <= row["seed"] <= 2**32-1
            or row["limits"] != bounds[row["capability"]]
            or row["workload_id"] != digest({key: value for key, value in row.items() if key != "workload_id"})):
        refuse("PERFORMANCE_WORKLOAD_INVALID")
    started = time.perf_counter()
    samples = []
    for index, state in enumerate(("cold", "warm", "updated", "updated")):
        tracemalloc.start()
        before = time.perf_counter()
        value, validator = fixture(row, max(0, index-1))
        construction = time.perf_counter()-before
        begin = time.perf_counter(); raw = canonical(value); serialized = time.perf_counter()-begin
        begin = time.perf_counter(); reason = None
        try:
            validator(value)
            outcome = "validated"
        except (Exception, SystemExit) as error:
            outcome = "refused"
            reason = getattr(error, "details", {}).get("reason") or getattr(error, "details", {}).get("field")
            if reason is None and error.args and type(error.args[0]) is str:
                reason = error.args[0]
            known = {"strict_claims": "strict_schema_array_bound", "assessment_changes": "invalid_list_bound",
                "packet_sources": "packet_contract_invalid", "output_bytes": "pack_document_bound"}
            if reason != known[row["capability"]]:
                outcome = "unknown"
                reason = type(error).__name__
        elapsed = time.perf_counter()-begin
        _current, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
        samples.append({"state": state, "revision": max(0, index-1), "construction_seconds": construction,
            "validation_seconds": elapsed, "serialization_seconds": serialized, "serialized_bytes": len(raw),
            "traced_peak_bytes": peak, "input_sha256": "sha256:"+hashlib.sha256(raw).hexdigest(), "outcome": outcome, "refusal": reason})
    rss = _process_peak_rss_bytes()
    return {"workload_id": row["workload_id"], "samples": samples, "complete": all(sample["outcome"] != "unknown" for sample in samples), "truncated": False,
        "probe_wall_seconds": time.perf_counter()-started, "process_peak_rss_bytes": rss,
        "memory_scope": "per-call Python tracemalloc peak; child-process RSS high-water",
        "cache_scope": "fresh interpreter and descriptor validation before first call; same-process warm/update calls", "partition": partition(row)}


def main():
    row = json_document(sys.stdin.buffer.read(65536))
    sys.stdout.buffer.write(canonical(run(row))+b"\n")


if __name__ == "__main__":
    main()
