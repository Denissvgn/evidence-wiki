"""Actual host CLI decisions over original signed fixture evidence."""

import hashlib
import json
import os
import py_compile
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tools import decide_release as cli
from tools._release_decision import CATEGORIES, METRICS, digest
from tools.measure_integration_costs import _signed
from tools.verify_acceptance_paths import candidate_manifest

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args, timeout=15):
    return subprocess.run([str(ROOT / ".venv/bin/python"), str(ROOT / "tools/decide_release.py"), *map(str, args)],
                          cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)


def configured_host(tmp_path, *, status="pass", noise=False, stall=False):
    from tests._usage_fixture import require_reference_storage
    require_reference_storage()
    key = b"s" * 32
    key_path = tmp_path / "host.key"
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    sources = []
    for category in CATEGORIES:
        metrics = {name: None for name in METRICS}
        metrics["elapsed_seconds"] = "2.125"
        report = _signed({"schema_version": "fixture-observation/v1", "category": category,
                          "status": status if category == "conformance" else "pass",
                          "metrics": metrics if category == "cost" else None,
                          "description": "Ignored prose cannot certify a release."}, key)
        path = tmp_path / (category + ".json")
        raw = json.dumps(report, sort_keys=True).encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        sources.append({"category": category, "owner_ref": str(path),
                        "raw_sha256": hashlib.sha256(raw).hexdigest(), "source_head_sha256": digest(report)})
    factory = tmp_path / "host.py"
    factory.write_text(f'''from pathlib import Path
from datetime import datetime,timezone
import time
from tools._release_decision import OwnerReader,OwnerEvidence,OriginalCitation,digest,ExternalBlocked
from tools.measure_integration_costs import _authenticated
KEY=Path({str(key_path)!r})
def make_readers(plan):
    def read(source):
        {"time.sleep(5)" if stall else "pass"}
        {"print('unexpected host output')" if noise else "pass"}
        path=Path(source['owner_ref']);report=_authenticated(path,KEY.read_bytes())
        if digest(report)!=source['source_head_sha256']:raise ValueError('fixture_original_changed')
        if report['status']=='external_unavailable':raise ExternalBlocked('fixture external service unavailable')
        projection={{'status':report['status'],'metrics':report['metrics'],'judge':None,'business':None}}
        return OwnerEvidence(path.read_bytes(),digest(report),plan['candidate_sha256'],plan['host_sha256'],plan['scope'],datetime.now(timezone.utc).isoformat(),plan['expires_at'],'simulated' if source['category'] in ['semantic','integration','conformance'] else 'deterministic_verified',projection,(OriginalCitation('/status',report['status']),))
    return tuple(OwnerReader(s['category'],s['owner_ref'],lambda s=dict(s):read(s)) for s in plan['required_sources'])
''')
    now = datetime.now(timezone.utc)
    budgets = {name: None for name in METRICS}
    budgets["elapsed_seconds"] = "10"
    config = {"candidate_sha256": digest(candidate_manifest(ROOT)), "profile": "technical_fixture",
              "required_sources": sources, "budgets": budgets, "scope": {"kind": "signed_fixture_observations"},
              "frozen_at": now.isoformat(), "expires_at": (now + timedelta(minutes=5)).isoformat(), "judge": None}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    plan_path = tmp_path / "plan.json"
    result = run_cli("freeze", "--config", config_path, "--factory", str(factory) + ":make_readers",
                     "--output", plan_path, "--timeout-seconds", "1" if stall else "10")
    assert result.returncode == 0, result.stdout + result.stderr
    return factory, plan_path, json.loads(result.stdout)["plan_sha256"]


@pytest.mark.parametrize("status,expected", [("pass", "qualified"), ("actionable_failure", "revise"),
                                             ("critical_failure", "reject"), ("unknown", "inconclusive"),
                                             ("external_unavailable", "blocked_external")])
def test_cli_decides_and_replays_original_signed_sources(tmp_path, status, expected):
    factory, plan, pin = configured_host(tmp_path, status=status)
    output = tmp_path / "decision.json"
    args = ["--plan", plan, "--expected-plan", pin, "--factory", str(factory) + ":make_readers"]
    result = run_cli("decide", *args, "--output", output)
    assert result.returncode == (0 if expected == "qualified" else 2)
    report = json.loads(result.stdout)
    assert report["outcome"] == expected and output.exists()
    replay = run_cli("replay", *args, "--report", output)
    assert replay.returncode == result.returncode, replay.stdout + replay.stderr
    assert json.loads(replay.stdout)["outcome"] == expected
    assert report["release_published"] is False


def test_factory_source_drift_refuses_before_loading(tmp_path):
    factory, plan, pin = configured_host(tmp_path)
    factory.write_text(factory.read_text() + "\nraise RuntimeError('must not execute drifted host')\n")
    output = tmp_path / "decision.json"
    result = run_cli("decide", "--plan", plan, "--expected-plan", pin,
                     "--factory", str(factory) + ":make_readers", "--output", output)
    assert result.returncode == 2 and not output.exists()
    assert json.loads(result.stdout)["reason"] == "release_current_host_changed"


def test_reader_output_cannot_corrupt_machine_protocol(tmp_path):
    factory, plan, pin = configured_host(tmp_path, noise=True)
    output = tmp_path / "decision.json"
    result = run_cli("decide", "--plan", plan, "--expected-plan", pin,
                     "--factory", str(factory) + ":make_readers", "--output", output)
    assert result.returncode == 2 and not output.exists()
    assert json.loads(result.stdout)["reason"] == "release_host_protocol_output"
    assert result.stderr == ""


def test_host_deadline_cannot_be_swallowed_by_owner_recovery(tmp_path):
    factory, plan, pin = configured_host(tmp_path, stall=True)
    output = tmp_path / "decision.json"
    result = run_cli("decide", "--plan", plan, "--expected-plan", pin,
                     "--factory", str(factory) + ":make_readers", "--output", output, timeout=5)
    assert result.returncode == 2 and not output.exists()
    assert json.loads(result.stdout)["reason"] == "release_host_deadline"


def test_factory_executes_exact_pinned_source_despite_timestamp_cache(tmp_path):
    factory = tmp_path / "host.py"
    factory.write_text("def make_readers(plan):return 'old_code'\n")
    original = factory.stat()
    py_compile.compile(str(factory), doraise=True)
    factory.write_text("def make_readers(plan):return 'new_code'\n")
    os.utime(factory, ns=(original.st_atime_ns, original.st_mtime_ns))
    pin = cli._factory_pin(str(factory) + ":make_readers")
    assert cli._readers({}, pin) == "new_code"


@pytest.mark.parametrize("raw", ['{"scope":{},"scope":{}}', '{"budget":NaN}', '{"schema":1,"schema":2}'])
def test_all_cli_json_inputs_use_strict_decoder(tmp_path, raw):
    path = tmp_path / "input.json"
    path.write_text(raw)
    with pytest.raises(ValueError):
        cli._read(path)


@pytest.mark.parametrize("kind", ["fifo", "oversized"])
def test_factory_rejects_nonregular_or_oversized_source(tmp_path, kind):
    factory = tmp_path / "host.py"
    if kind == "fifo":
        os.mkfifo(factory)
    else:
        factory.write_bytes(b" " * 65_537)
    with pytest.raises(ValueError, match="release_factory_bound"):
        cli._factory_pin(str(factory) + ":make_readers")
