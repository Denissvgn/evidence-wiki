"""Local producer and Linux architecture obligations for the repository gate."""

from __future__ import annotations

import hashlib
import json
import platform
import re
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "evidence-local-ci/v1"
SIGNING_NAMESPACE = "evidence-wiki-local-ci"
PYTHON_PATCHES = {"3.10": "3.10.21", "3.12": "3.12.14", "3.14": "3.14.7"}
GROUP_SIZE = 48
GROUP_TIMEOUT = 1800
LINUX_NATIVE_TARGETS = (
    "tests/test_agent_execution.py::test_process_dispatch_input_correlation_and_host_measured_receipt",
    "tests/test_agent_execution.py::test_real_cancellation_confirms_exit_and_preserves_unknown_accounting",
    "tests/test_agent_execution.py::test_background_descendant_is_stopped_after_leader_exit",
    "tests/test_native_filesystem.py",
    "tests/test_workspace_capture_portability.py",
    "tests/test_process_peak_memory.py",
    "tests/test_process_group_ownership.py",
    "tests/test_handoff_pipe.py",
    "tests/test_journal_owned_scan.py",
    "tests/test_yaml_safe.py",
    "tests/test_orchestration_host.py::OrchestrationHostTests::test_codex_runtime_resolution_fails_closed_for_missing_platform_package",
)
LINUX_NATIVE_REQUIRED = (
    *LINUX_NATIVE_TARGETS[:3],
    "tests/test_process_peak_memory.py::test_native_peak_resident_memory_is_positive",
    "tests/test_native_filesystem.py::test_native_directory_operations_and_exclusive_publication",
    "tests/test_native_filesystem.py::test_native_private_permissions_are_enforced",
    "tests/test_native_filesystem.py::test_metadata_signature_retains_changes_when_size_and_mtime_are_restored",
    "tests/test_handoff_pipe.py::test_delayed_reader_exact_payload_same_thread_and_final_completion",
)
REQUIRED_LOCAL_PROFILES = (
    "linux-3.10", "linux-3.14", "linux-native", "macos-3.12", "macos-compatibility", "packages",
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def architecture(machine):
    if type(machine) is not str:
        raise ValueError("runtime architecture is missing")
    value = machine.lower().replace("-", "_")
    if value in {"arm64", "aarch64"}:
        return "ARM64"
    if value in {"x64", "amd64", "x86_64"}:
        return "X64"
    raise ValueError("unsupported runtime architecture")


def runtime_identity():
    return {"system": platform.system(), "machine": platform.machine(),
            "architecture": architecture(platform.machine()), "python": platform.python_version(),
            "implementation": platform.python_implementation()}


def policy():
    root = Path(__file__).resolve().parent
    harness = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in
               ("_local_ci_contract.py", "local_ci.py", "verify_local_ci.py", "check_local_ci_status.py",
                "linux_architecture_probe.py", "check_ci_compatibility.py", "run_test_groups.py", "_suite_plugin.py")}
    return {"schema_version": SCHEMA, "python_patches": PYTHON_PATCHES,
            "group_size": GROUP_SIZE, "group_timeout": GROUP_TIMEOUT,
            "local_profiles": REQUIRED_LOCAL_PROFILES, "linux_native_targets": LINUX_NATIVE_TARGETS,
            "linux_native_required": LINUX_NATIVE_REQUIRED,
            "linux_common_architectures": ("X64", "ARM64"), "implementation": "CPython",
            "installation_limits": {"command": 900, "case": 1200, "journey": 5400, "workers": 3},
            "harness_sha256": harness}


def checked_runtime(value, *, system=None, python=None):
    if (type(value) is not dict or set(value) != {"system", "machine", "architecture", "python", "implementation"}
            or value["system"] not in {"Linux", "Darwin", "Windows"}
            or value["architecture"] != architecture(value["machine"])
            or value["implementation"] != "CPython"
            or not re.fullmatch(r"\d+\.\d+\.\d+", value["python"])):
        raise ValueError("invalid native runtime identity")
    if system is not None and value["system"] != system or python is not None and value["python"] != python:
        raise ValueError("runtime does not match the required profile")
    return value


def suite_result(report, *, commit, source, targets, runtime):
    """Reconcile complete local groups without inventing hosted runner metadata."""
    from tools.run_test_groups import COMBINED_MODULES, groups
    from tools.verify_test_shards import check_run

    checked_runtime(runtime)
    if (report.get("commit") != commit or report.get("source_sha256") != source
            or report.get("sources_unchanged") is not True or report.get("status") != "passed"
            or report.get("targets") != list(targets) or report.get("group_size") != GROUP_SIZE
            or report.get("timeout") != GROUP_TIMEOUT
            or report.get("shard") != {"index": 1, "count": 1}
            or report.get("python", "").split()[0] != runtime["python"]
            or report.get("expected_python") != runtime["python"]
            or report.get("runtime") != runtime):
        raise ValueError("local suite identity or complete selection differs")
    if report.get("runner"):
        raise ValueError("local evidence must not claim GitHub runner provenance")
    nodes = report["collection"]["result"]["collected"]
    if not nodes or len(nodes) != len(set(nodes)):
        raise ValueError("invalid complete collection")
    check_run(report["collection"], "collection", nodes, collect=True)
    planned = groups(nodes, GROUP_SIZE)
    if len(report["groups"]) != len(planned):
        raise ValueError("local suite has missing or extra groups")
    for number, (row, members) in enumerate(zip(report["groups"], planned, strict=True), 1):
        check_run(row, f"group-{number:03}", members)
    modules = [name for name in COMBINED_MODULES if any(node.startswith(name + "::") for node in nodes)]
    orders = [("combined-forward", modules), ("combined-reverse", modules[::-1])] if len(modules) >= 2 else []
    if len(report["combined_orders"]) != len(orders):
        raise ValueError("local combined orders are incomplete")
    for row, (label, order) in zip(report["combined_orders"], orders, strict=True):
        check_run(row, label, [node for module in order for node in nodes if node.startswith(module + "::")])
    result = {"collection_sha256": digest(nodes), "collected": len(nodes), "executed": len(nodes),
              "groups": len(planned), "combined_orders": len(orders), "status": "passed"}
    if list(targets) == list(LINUX_NATIVE_TARGETS):
        outcomes = {key: outcome for row in report["groups"] for key, outcome in row["result"].get("case_outcomes", {}).items()}
        if any(outcomes.get(node) != "passed" for node in LINUX_NATIVE_REQUIRED):
            raise ValueError("required native behavior was skipped or did not pass")
        result["native_checks"] = dict.fromkeys(LINUX_NATIVE_REQUIRED, "passed")
    return result


def linux_obligation(runtime):
    """Share functional coverage while retaining the actual machine identity."""
    checked_runtime(runtime, system="Linux")
    minor = ".".join(runtime["python"].split(".")[:2])
    if PYTHON_PATCHES.get(minor) != runtime["python"]:
        raise ValueError("Linux runtime is outside the pinned policy")
    return f"Linux/common/{minor}"


def linux_native_pair(left, right):
    """Both native architectures must execute the same probe collection."""
    for value in (left, right):
        checked_runtime(value["runtime"], system="Linux", python=PYTHON_PATCHES["3.12"])
        if (value["targets"] != list(LINUX_NATIVE_TARGETS) or value["status"] != "passed"
                or value["collected"] <= 0 or value["executed"] != value["collected"]
                or value.get("native_checks") != dict.fromkeys(LINUX_NATIVE_REQUIRED, "passed")):
            raise ValueError("native architecture probe is incomplete")
    if {left["runtime"]["architecture"], right["runtime"]["architecture"]} != {"X64", "ARM64"}:
        raise ValueError("both real Linux architectures are required")
    for field in ("commit", "tree", "collection_sha256", "collected", "executed"):
        if left[field] != right[field]:
            raise ValueError("native architecture probe coverage differs")
    return {"status": "passed", "architectures": ["X64", "ARM64"], "collection_sha256": left["collection_sha256"]}


def checked_receipt(value, *, commit, tree, base, repository, now=None):
    fields = {"schema_version", "repository", "commit", "tree", "base", "policy_sha256", "created_utc",
              "producer", "profiles", "evidence_sha256", "status"}
    if type(value) is not dict or set(value) != fields:
        raise ValueError("receipt has unknown or missing fields")
    if (value["schema_version"] != SCHEMA or value["repository"] != repository
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or value["commit"] != commit or value["tree"] != tree
            or value["base"] != base or value["policy_sha256"] != digest(policy())
            or value["status"] != "passed" or type(value["producer"]) is not str
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value["producer"])
            or not re.fullmatch(r"[a-f0-9]{64}", value["evidence_sha256"])):
        raise ValueError("receipt candidate, policy, producer or outcome differs")
    start = datetime.fromisoformat(value["created_utc"].replace("Z", "+00:00"))
    instant = datetime.now(timezone.utc) if now is None else now
    if start.tzinfo is None or instant.tzinfo is None or start > instant:
        raise ValueError("receipt creation time is invalid or in the future")
    profiles = value["profiles"]
    if type(profiles) is not dict or set(profiles) != set(REQUIRED_LOCAL_PROFILES):
        raise ValueError("required local profile is missing or duplicated")
    for name, row in profiles.items():
        fields = {"commit", "tree", "runtime", "status", "compatibility", "collection_sha256"}
        fields |= {"installations", "limits", "wheel_sha256", "sdist_sha256"} if name == "packages" else (
            {"targets", "collected", "executed", "groups", "combined_orders"} if name != "macos-compatibility" else set())
        if name == "linux-native":
            fields.add("native_checks")
        if (type(row) is not dict or set(row) != fields or row.get("status") != "passed"
                or row.get("commit") != commit or row.get("tree") != tree):
            raise ValueError("local profile is failed or bound to another candidate")
        runtime = checked_runtime(row["runtime"])
        if name.startswith("linux-"):
            checked_runtime(runtime, system="Linux", python=PYTHON_PATCHES["3.12"] if name == "linux-native"
                            else PYTHON_PATCHES[name.removeprefix("linux-")])
        elif name == "macos-3.12":
            checked_runtime(runtime, system="Darwin", python=PYTHON_PATCHES["3.12"])
        elif name == "macos-compatibility":
            checked_runtime(runtime, system="Darwin", python=PYTHON_PATCHES["3.14"])
        elif name == "packages":
            checked_runtime(runtime, system="Linux", python=PYTHON_PATCHES["3.12"])
        if name in {"linux-3.10", "linux-3.14", "macos-3.12"}:
            if (row.get("targets") != ["tests"] or type(row.get("collected")) is not int or row["collected"] < 1
                    or type(row.get("executed")) is not int or type(row.get("groups")) is not int
                    or row.get("executed") != row["collected"] or row.get("groups", 0) < 1
                    or row.get("combined_orders") != 2):
                raise ValueError("full suite or combined orders are incomplete")
        if name != "packages":
            if row.get("compatibility") != "passed" or not re.fullmatch(r"[a-f0-9]{64}", row.get("collection_sha256", "")):
                raise ValueError("profile compatibility or inventory commitment is missing")
        elif (row.get("installations") != {"wheel": "passed", "sdist": "passed"}
              or row.get("limits") != policy()["installation_limits"]
              or any(not re.fullmatch(r"[a-f0-9]{64}", row.get(key, "")) for key in ("wheel_sha256", "sdist_sha256"))):
            raise ValueError("both exact installed distributions are required")
    common_arches = {profiles[name]["runtime"]["architecture"] for name in ("linux-3.10", "linux-3.14", "linux-native", "packages")}
    if len(common_arches) != 1:
        raise ValueError("local Linux profiles use mixed native architectures")
    native = profiles["linux-native"]
    if (native.get("targets") != list(LINUX_NATIVE_TARGETS) or type(native.get("collected")) is not int
            or native["collected"] <= 0 or type(native.get("executed")) is not int
            or native["executed"] != native["collected"]
            or native.get("native_checks") != dict.fromkeys(LINUX_NATIVE_REQUIRED, "passed")):
        raise ValueError("local native architecture probe is incomplete")
    return value
