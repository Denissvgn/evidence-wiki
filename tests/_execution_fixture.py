"""Synthetic host and independently calculated laboratory evidence."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import yaml

NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
SOURCE_ID = "execution:lab"
KEYS = {"runner": "11" * 32, "evaluator": "22" * 32, "owner": "33" * 32}


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode()


def identifier(domain, value):
    return "sha256:" + hashlib.sha256(domain.encode() + b"\0" + canonical(value)).hexdigest()


def binding(data):
    return {"content_hash": "sha256:" + hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}


def closure(files):
    return identifier("evidence-artifact-closure/v1", {path: binding(data) for path, data in files.items()})


def authenticate(payload, principal, role):
    auth = {"schema_version": "evidence-authentication/v1", "scheme": "hmac-sha256",
            "principal": principal, "key_id": principal + "-key", "role": role,
            "policy_id": "lab-authority", "policy_revision": "1",
            "issued_at": "2026-09-10T00:10:00Z", "expires_at": "2027-01-01T00:00:00Z"}
    message = b"evidence-attestation/v1\0" + canonical({"payload": payload, "authentication": auth})
    auth["signature"] = hmac.new(bytes.fromhex(KEYS[principal]), message, hashlib.sha256).hexdigest()
    return {"payload": copy.deepcopy(payload), "authentication": auth}


def host_policy(path: Path):
    policy = {"schema_version": "evidence-trust-policy/v1", "policy_id": "lab-authority", "policy_revision": "1",
              "not_before": "2026-01-01T00:00:00Z", "expires_at": "2027-01-01T00:00:00Z",
              "principals": {principal: {"controller": principal, "roles": [role], "keys": {principal + "-key": KEYS[principal]}}
                             for principal, role in (("runner", "generator"), ("evaluator", "evaluator"), ("owner", "usage"))},
              "revoked_keys": [], "revoked_envelopes": []}
    path.write_bytes(canonical(policy))
    path.chmod(0o600)
    return policy


def independently_recalculate(files, payload):
    """Decimal reference calculation, separate from any normalization or receipt code."""
    values = [Decimal(value) for value in files[payload["inputs"][0]["artifact"]["path"]].decode().splitlines()]
    parameters = payload["parameters"]
    expected = (sum(values) * Decimal(parameters["factor"]) - Decimal(parameters["cost"])).quantize(Decimal(parameters["precision"]))
    actual = Decimal(files[payload["outputs"][0]["path"]].decode()).quantize(Decimal(parameters["precision"]))
    return str(expected), str(actual), "passed" if expected == actual else "failed"


def example(*, history=True, outcome="passed"):
    files = {"inputs.txt": b"2\n3\n", "runner.txt": b"multiply sum by factor, then subtract cost\n",
             "environment.json": canonical({"arithmetic": "decimal", "precision": "0.01"}),
             "suite.txt": b"Independently recalculate the declared sum, factor and cost.\n",
             "result.txt": b"9.50\n" if outcome == "passed" else b"10.00\n", "run.log": b"calculation finished\n",
             "evaluation.log": b"independent calculation finished\n"}

    def reference(path):
        return {"path": path, "content_hash": binding(files[path])["content_hash"]}

    payload = {"record_type": "observation", "episode_id": "lab-episode", "task_id": "weighted-sum", "run_id": "corrected",
               "problem_id": "laboratory-calibration", "input_group_id": "sample-a", "held_out_role": "held-out",
               "generating_agent": "runner", "predecessor": None, "hypothesis": None,
               "inputs": [{"source_id": "lab:measurements", "revision_id": closure({"inputs.txt": files["inputs.txt"]}), "artifact": reference("inputs.txt")}],
               "patch": "not-applicable", "workspace": {"scope": "declared-input-artifacts", "content_hash": closure({"inputs.txt": files["inputs.txt"]})},
               "environment": reference("environment.json"), "dependencies": "not-applicable", "container": "not-applicable",
               "tool": {"name": "decimal-calculator", "version": "1", "schema_version": "1", "artifact": reference("runner.txt")},
               "model": dict.fromkeys(("identity", "weights", "adapter", "quantization", "prompt", "context"), "not-applicable"),
               "parameters": {"factor": "2", "cost": "0.50", "precision": "0.01"}, "seed": "not-applicable",
               "started_at": "2026-09-10T00:00:00Z", "finished_at": "2026-09-10T00:01:00Z",
               "outputs": [reference("result.txt")], "logs": [reference("run.log")], "outcome": outcome,
               "units": {"result": "calibration-units"}, "warnings": [], "limits": ["Synthetic laboratory example."],
               "verification_scope": {"suite_revision": "decimal-check/1", "suite": reference("suite.txt"),
                                      "checks": ["weighted-sum"], "environment": reference("environment.json")},
               "temporal": {"mode": "current", "cutoff": None, "limitations": []}}
    records = []
    receipts = []

    def record(value):
        records.append(authenticate(value, "runner", "generator"))
        return identifier("evidence-execution-record/v1", value)

    def receipt(value, target):
        expected, actual, verdict = independently_recalculate(files, value)
        evidence = {"record_type": "verification-receipt", "target_record_id": target,
                    "episode_id": value["episode_id"], "task_id": value["task_id"], "run_id": value["run_id"], "evaluator": "evaluator",
                    "scope": value["verification_scope"], "started_at": "2026-09-10T00:02:00Z", "finished_at": "2026-09-10T00:03:00Z",
                    "outcome": verdict, "assertions": [{"check_id": "weighted-sum", "outcome": verdict,
                                                        "expected": expected, "actual": actual, "comparison": "decimal-equality/1"}],
                    "counts": {key: int(key == verdict) for key in ("passed", "failed", "skipped", "inconclusive")},
                    "logs": [reference("evaluation.log")], "warnings": [], "limits": ["No external tool or model execution."]}
        if value["run_id"] == "initial":
            evidence.update(started_at="2026-09-09T23:52:00Z", finished_at="2026-09-09T23:53:00Z")
        receipts.append(authenticate(evidence, "evaluator", "evaluator"))
        return identifier("evidence-verification-receipt/v1", evidence)

    roles = {"inputs.txt": "input", "runner.txt": "tool", "environment.json": "environment", "suite.txt": "suite",
             "result.txt": "output", "run.log": "log", "evaluation.log": "log"}
    if history:
        files.update({"failed-result.txt": b"10.00\n", "hypothesis.txt": b"Subtract the declared cost.\n", "fix.patch": b"result = sum * factor - cost\n"})
        roles.update({"failed-result.txt": "output", "hypothesis.txt": "output", "fix.patch": "patch"})
        failed = copy.deepcopy(payload)
        failed.update(run_id="initial", outcome="failed", outputs=[reference("failed-result.txt")],
                      started_at="2026-09-09T23:50:00Z", finished_at="2026-09-09T23:51:00Z")
        failed_id = record(failed)
        receipt(failed, failed_id)
        proposed = copy.deepcopy(payload)
        proposed.update(record_type="hypothesis", run_id="proposal", outcome="inconclusive", predecessor=failed_id,
                        outputs=[reference("hypothesis.txt")], patch=reference("fix.patch"),
                        started_at="2026-09-09T23:54:00Z", finished_at="2026-09-09T23:55:00Z")
        hypothesis_id = record(proposed)
        payload.update(predecessor=failed_id, hypothesis=hypothesis_id, patch=reference("fix.patch"))
    selected = record(payload)
    selected_receipt = receipt(payload, selected)
    document = {"schema_version": "execution-evidence/v1", "profile": "execution_evidence/v1", "source_id": SOURCE_ID,
                "selected_record_id": selected, "selected_receipt_id": selected_receipt,
                "artifacts": [{"path": path, **binding(data), "role": roles[path]} for path, data in sorted(files.items())],
                "records": records, "receipts": receipts}
    files["execution-record.json"] = canonical(document)
    return files, document


def workspace(root: Path, files=None):
    if files is None:
        files, _ = example()
    config = {"project": {"name": "Laboratory evidence"},
              "sources": {"manifest_path": "sources/manifest.jsonl", "normalized_dir": "sources/normalized"},
              "evidence_trust": {"policy_id": "lab-authority", "policy_revision": "1"}}
    record = {"id": SOURCE_ID, "kind": "execution_evidence", "title": "Laboratory calculation",
              "raw_paths": [], "raw_fingerprint": closure(files), "metadata": {}}
    folder = root / "sources/evidence/execution--lab"
    folder.mkdir(parents=True, exist_ok=True)
    for path, data in files.items():
        (folder / path).write_bytes(data)
    (root / "sources/normalized").mkdir(exist_ok=True)
    (root / "research.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (root / "sources/manifest.jsonl").write_bytes(canonical(record))
    return config, record, folder
