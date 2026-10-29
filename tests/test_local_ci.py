"""Local provenance cannot masquerade as another architecture or incomplete CI."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from tools._local_ci_contract import (
    LINUX_NATIVE_REQUIRED,
    LINUX_NATIVE_TARGETS,
    PYTHON_PATCHES,
    REQUIRED_LOCAL_PROFILES,
    SCHEMA,
    SIGNING_NAMESPACE,
    architecture,
    canonical,
    checked_receipt,
    digest,
    linux_native_pair,
    linux_obligation,
    policy,
    runtime_identity,
    suite_result,
)
from tools.run_test_groups import main as run_groups
from tools.run_test_groups import source_identity
from tools.verify_local_ci import verify_signature


def runtime(machine="aarch64", python="3.12.14", system="Linux"):
    return {"system": system, "machine": machine, "architecture": architecture(machine),
            "python": python, "implementation": "CPython"}


def receipt():
    profiles = {}
    for name in REQUIRED_LOCAL_PROFILES:
        minor = name.removeprefix("linux-") if name in {"linux-3.10", "linux-3.14"} else "3.14" if name == "macos-compatibility" else "3.12"
        selected = runtime(python=PYTHON_PATCHES[minor], system="Darwin" if name.startswith("macos") else "Linux")
        profiles[name] = {"commit": "a" * 40, "tree": "b" * 40, "runtime": selected, "status": "passed",
                          "compatibility": "passed", "collection_sha256": "c" * 64, "collected": 14,
                          "executed": 14, "groups": 1, "combined_orders": 2, "targets": ["tests"]}
    profiles["linux-native"]["targets"] = list(LINUX_NATIVE_TARGETS)
    profiles["linux-native"]["native_checks"] = dict.fromkeys(LINUX_NATIVE_REQUIRED, "passed")
    for key in ("targets", "collected", "executed", "groups", "combined_orders"):
        profiles["macos-compatibility"].pop(key)
        profiles["packages"].pop(key)
    profiles["packages"].update(installations={"wheel": "passed", "sdist": "passed"},
                                limits=policy()["installation_limits"], wheel_sha256="d" * 64, sdist_sha256="e" * 64)
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    return {"schema_version": SCHEMA, "repository": "owner/project", "commit": "a" * 40, "tree": "b" * 40, "base": "f" * 40,
            "policy_sha256": digest(policy()), "created_utc": start.isoformat(),
            "producer": "controlled-host",
            "profiles": profiles, "evidence_sha256": "0" * 64, "status": "passed"}


@pytest.mark.parametrize("machine,expected", [("aarch64", "ARM64"), ("arm64", "ARM64"), ("x86_64", "X64"), ("AMD64", "X64")])
def test_native_architecture_aliases_are_explicit(machine, expected):
    assert architecture(machine) == expected
    assert linux_obligation(runtime(machine, "3.10.21")) == "Linux/common/3.10"


def test_shared_linux_obligation_does_not_relabel_the_native_machine():
    left, right = runtime("x86_64", "3.14.7"), runtime("aarch64", "3.14.7")
    assert linux_obligation(left) == linux_obligation(right)
    assert left["machine"] == "x86_64" and right["machine"] == "aarch64"
    assert left["architecture"] != right["architecture"]
    with pytest.raises(ValueError):
        linux_obligation(runtime("riscv64", "3.14.7"))


@pytest.mark.parametrize("damage", [None, "same-architecture", "collection", "candidate", "incomplete", "target", "mislabel", "skipped-native"])
def test_architecture_pair_requires_both_real_machines_and_equal_probe_coverage(damage):
    left = {"runtime": runtime("x86_64"), "targets": list(LINUX_NATIVE_TARGETS), "status": "passed",
            "commit": "a" * 40, "tree": "b" * 40, "collection_sha256": "c" * 64, "collected": 20, "executed": 20}
    left["native_checks"] = dict.fromkeys(LINUX_NATIVE_REQUIRED, "passed")
    right = {**copy.deepcopy(left), "runtime": runtime("aarch64")}
    if damage == "same-architecture":
        right["runtime"] = runtime("x86_64")
    elif damage == "collection":
        right["collection_sha256"] = "d" * 64
    elif damage == "candidate":
        right["commit"] = "d" * 40
    elif damage == "incomplete":
        right["executed"] -= 1
    elif damage == "target":
        right["targets"].pop()
    elif damage == "mislabel":
        right["runtime"]["architecture"] = "X64"
    elif damage == "skipped-native":
        right["native_checks"][LINUX_NATIVE_REQUIRED[0]] = "skipped"
    if damage is None:
        assert linux_native_pair(left, right)["architectures"] == ["X64", "ARM64"]
    else:
        with pytest.raises(ValueError):
            linux_native_pair(left, right)


@pytest.mark.parametrize("damage", [None, "missing-profile", "wrong-tree", "future-time", "wrong-policy", "partial-suite",
                                    "missing-order", "missing-installation", "larger-budget", "wrong-runtime", "failed", "extra"])
def test_signed_receipt_contract_retains_all_required_obligations(damage):
    value = receipt()
    if damage == "missing-profile":
        value["profiles"].pop("linux-3.10")
    elif damage == "wrong-tree":
        value["tree"] = "c" * 40
    elif damage == "future-time":
        value["created_utc"] = "2031-01-01T00:00:00+00:00"
    elif damage == "wrong-policy":
        value["policy_sha256"] = "e" * 64
    elif damage == "partial-suite":
        value["profiles"]["linux-3.10"]["targets"] = ["tests/test_one.py"]
    elif damage == "missing-order":
        value["profiles"]["linux-3.14"]["combined_orders"] = 1
    elif damage == "missing-installation":
        value["profiles"]["packages"]["installations"].pop("sdist")
    elif damage == "larger-budget":
        value["profiles"]["packages"]["limits"]["command"] += 1
    elif damage == "wrong-runtime":
        value["profiles"]["macos-3.12"]["runtime"]["python"] = "3.12.11"
    elif damage == "failed":
        value["profiles"]["linux-3.14"]["status"] = "failed"
    elif damage == "extra":
        value["approved"] = True
    options = {"commit": "a" * 40, "tree": "b" * 40, "base": "f" * 40,
               "repository": "owner/project",
               "now": datetime(2030, 1, 1, 0, 30, tzinfo=timezone.utc)}
    if damage is None:
        assert checked_receipt(value, **options) is value
    else:
        with pytest.raises((ValueError, KeyError)):
            checked_receipt(value, **options)


def test_real_detached_signature_requires_the_enrolled_key_and_exact_receipt(tmp_path):
    executable = shutil.which("ssh-keygen")
    if executable is None:
        pytest.skip("OpenSSH signing executable is unavailable")
    key, other = tmp_path / "key", tmp_path / "other"
    for path in (key, other):
        subprocess.run([executable, "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    message = tmp_path / "receipt.json"
    raw = canonical(receipt())
    message.write_bytes(raw)
    subprocess.run([executable, "-Y", "sign", "-f", str(key), "-n", SIGNING_NAMESPACE, str(message)], check=True)
    signature = message.with_suffix(".json.sig").read_bytes()
    public = key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    verify_signature(raw, signature, public)
    for changed, selected in [(raw + b" ", public), (raw, other.with_suffix(".pub").read_text(encoding="utf-8").strip())]:
        with pytest.raises(ValueError, match="does not verify"):
            verify_signature(changed, signature, selected)


def test_real_local_suite_reconciles_original_records_without_github_ids(tmp_path, monkeypatch):
    from tools import _suite_plugin

    root = tmp_path / "candidate"
    (root / "tests").mkdir(parents=True)
    (root / "tools").mkdir()
    shutil.copyfile(_suite_plugin.__file__, root / "tools/_suite_plugin.py")
    (root / "tests/test_small.py").write_text("def test_real():\n    assert 2 + 2 == 4\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Local suite", "-c", "user.email=local@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "Fixture"], check=True)
    for name in tuple(__import__("os").environ):
        if name.startswith(("GITHUB_", "RUNNER_")):
            monkeypatch.delenv(name)
    selected = runtime_identity()
    monkeypatch.setenv("EVIDENCE_WIKI_EXPECTED_PYTHON", selected["python"])
    evidence = tmp_path / "evidence"
    assert run_groups(["--root", str(root), "--output", str(evidence), "--group-size", "48", "--timeout", "1800", "tests"]) == 0
    report = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    summary = suite_result(report, commit=report["commit"], source=source_identity(root), targets=["tests"], runtime=selected)
    assert summary["collected"] == summary["executed"] == 1
    for field, changed in [("timeout", 1801), ("runner", {"GITHUB_RUN_ID": "invented"}), ("status", "failed")]:
        altered = copy.deepcopy(report)
        altered[field] = changed
        with pytest.raises(ValueError):
            suite_result(altered, commit=report["commit"], source=source_identity(root), targets=["tests"], runtime=selected)
