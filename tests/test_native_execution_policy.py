"""Closed execution bounds cannot slide original fixture authority."""

import copy
import importlib
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from evidence_wiki import _native_owner_execution as execution
from tools import _repeated_integration_host as host
from tools._release_decision import digest

START = datetime(2030, 1, 1, tzinfo=timezone.utc)


def interval(profile="extended"):
    selected = execution.execution_policy(profile)
    return selected, START.isoformat(), (START + timedelta(seconds=selected["study_seconds"])).isoformat()


def test_profiles_preserve_legacy_and_close_the_extended_schedule():
    legacy = execution.execution_policy()
    assert [legacy[key] for key in ("slot_seconds", "route_observer_seconds", "study_seconds",
                                   "native_run_seconds", "authority_seconds")] == [900, 2700, 14400, 14400, 7200]
    selected = execution.validate_execution_policy(execution.execution_policy("extended"))
    assert selected["route_observer_seconds"] == 3 * selected["slot_seconds"] == 3900
    assert 4 * selected["route_observer_seconds"] + selected["decision_seconds"] + selected["replay_seconds"] == 17400
    assert 3 * selected["route_observer_seconds"] + 1800 == 13500 < selected["authority_seconds"] == 14400
    assert selected["study_seconds"] == 21600
    for field, expected in (("owner_pass_seconds", 120), ("witness_seconds", 30), ("cleanup_seconds", 60)):
        assert legacy[field] == selected[field] == expected
    selected["slot_seconds"] = 1
    assert execution.execution_policy("extended")["slot_seconds"] == 1300


@pytest.mark.parametrize("change", [lambda p: p.update(extra=True), lambda p: p.pop("cleanup_seconds"),
    lambda p: p.update(witness_seconds=31), lambda p: p.update(slot_seconds=1300.0),
    lambda p: p.update(study_seconds=True), lambda p: p.update(profile="arbitrary")])
def test_policy_rejects_extra_missing_changed_and_wrong_type_fields(change):
    selected = execution.execution_policy("extended")
    change(selected)
    with pytest.raises(ValueError):
        execution.validate_execution_policy(selected)


def test_fresh_issuance_uses_one_end_and_remaining_original_window():
    selected, start, end = interval()
    until, seconds = execution.fixture_window(selected, start, end, current_utc=START)
    assert until == (START + timedelta(hours=4)).isoformat() and seconds == 14400
    instant = START + timedelta(hours=5, minutes=59, seconds=58, microseconds=500000)
    until, seconds = execution.fixture_window(selected, start, end, current_utc=instant)
    assert until == end and seconds == 1
    legacy, start, end = interval("legacy")
    until, seconds = execution.fixture_window(legacy, start, end, current_utc=START)
    assert until == (START + timedelta(hours=2)).isoformat() and seconds == 7200


@pytest.mark.parametrize("instant", [START - timedelta(microseconds=1), START + timedelta(hours=6),
                                    START + timedelta(hours=7), datetime(2030, 1, 1)])
def test_original_window_rejects_early_expired_or_unzoned_time(instant):
    selected, start, end = interval()
    with pytest.raises(ValueError, match="native_execution_window"):
        execution.fixture_window(selected, start, end, current_utc=instant)
    with pytest.raises(ValueError, match="native_execution_window"):
        execution.fixture_window(selected, start, (START + timedelta(hours=7)).isoformat(), current_utc=START)


def test_restore_validates_initial_authority_without_renewal():
    selected, start, end = interval()
    record = execution.fixture_authority(selected, start, end, current_utc=START + timedelta(hours=3))
    assert record["expires_at"] == end and record["run_seconds"] == 10800
    assert execution.validate_fixture_authority(record, current_utc=START + timedelta(hours=4)) == record
    for field, replacement in (("expires_at", (START + timedelta(hours=7)).isoformat()),
                               ("run_seconds", 14400), ("run_seconds", 10800.0),
                               ("provisioned_at", (START + timedelta(hours=4)).isoformat())):
        changed = copy.deepcopy(record)
        changed[field] = replacement
        with pytest.raises(ValueError):
            execution.validate_fixture_authority(changed, current_utc=START + timedelta(hours=4))
    with pytest.raises(ValueError, match="native_fixture_authority_expired"):
        execution.validate_fixture_authority(record, current_utc=START + timedelta(hours=6))


def example(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "examples/unattended-host"))
    return importlib.import_module("fixture_host"), importlib.import_module("finite_host")


@pytest.mark.parametrize("extended", [False, True])
def test_fixture_shared_authority_covers_trust_usage_authentication_and_services(tmp_path, monkeypatch, extended):
    fixture, finite = example(monkeypatch)
    base = tmp_path / "fixture"
    root = base / "workspace"
    def setup(_):
        for folder in ("sources/normalized", "raw/papers", "docs"):
            (root / folder).mkdir(parents=True, exist_ok=True)
        (root / "sources/manifest.jsonl").write_text('{"id":"source"}\n')
        (root / "sources/normalized/source.md").write_text("source")
        (root / "raw/papers/study.md").write_text("retained")
        (root / "AGENTS.md").write_text("fixture")
        (root / "docs/research-requirements.json").write_text("{}")
        (root / "research.yml").write_text(yaml.safe_dump({"strict_evidence": {"rubric": {"id": "finite", "revision": "1"}}}))
        return {"setup_ready": True}
    onboarding = SimpleNamespace(plan=lambda request: request, apply=setup)
    monkeypatch.setattr(fixture.Onboarding, "open", lambda **_: nullcontext(onboarding))
    workspace = MagicMock()
    workspace.sources.capture_revision.return_value = {"state": "captured"}
    workspace.usage.transact.return_value = {"checkpoint": "checkpoint"}
    monkeypatch.setattr(fixture.Workspace, "open", lambda _: nullcontext(workspace))
    monkeypatch.setattr(fixture, "HostContext", lambda *a, **kw: SimpleNamespace(activate=nullcontext))
    captured = {}
    runtime = SimpleNamespace(execution=SimpleNamespace(profile=SimpleNamespace(document=lambda: {"schema_version": "fixture"})))
    def services(*args, **kwargs):
        captured.update(keys=args[4], run_policy=args[5].to_dict(), until=args[7], options=kwargs)
        return runtime
    monkeypatch.setattr(finite, "provision_services", services)
    selected, start, end = interval()
    authority = execution.fixture_authority(selected, start, end, current_utc=START + timedelta(hours=3))
    monkeypatch.setattr(execution, "fixture_authority", lambda *args, **kwargs: authority)
    legacy_end = (START + timedelta(hours=2)).isoformat()
    monkeypatch.setattr(fixture, "expiry", lambda: legacy_end)
    tracker = MagicMock()
    options = {"execution_policy": selected, "study_frozen_at": start, "study_expires_at": end} if extended else {}
    fixture.provision(base, "Observe fixture.", finite=True, child_tracker=tracker, **options)
    until = authority["expires_at"] if extended else legacy_end
    assert json.loads((base / "fixture-private/authority.json").read_bytes())["expires_at"] == until
    assert captured["keys"]["expires_at"] == captured["until"] == until
    assert captured["run_policy"]["budgets"]["seconds"] == (authority["run_seconds"] if extended else 1800)
    assert captured["options"]["execution_authority"] == (authority if extended else None)
    assert captured["options"]["child_tracker"] is tracker
    envelopes = [call.args[0] for call in workspace.usage.transact.call_args_list]
    assert len(envelopes) == 2
    assert all(row["authentication"]["expires_at"] == until for row in envelopes)
    deposit = envelopes[1]["payload"]["body"]
    assert deposit["grant"]["payload"]["expires_at"] == until
    assert all(deposit[name]["authentication"]["expires_at"] == until for name in ("grant", "scrub"))


def test_fixture_meter_acknowledges_native_completion_independently_of_accounting(monkeypatch):
    fixture, _ = example(monkeypatch)
    tracker = MagicMock()
    tracker.confirm_completed.return_value = False
    assert fixture.FixtureMeter(tracker).finalize(None, {"seconds": 1})["seconds"] == 1
    tracker.confirm_completed.assert_called_once_with()
    assert fixture.FixtureMeter().finalize(None, {"seconds": 2})["seconds"] == 2


def test_finite_service_credentials_signer_and_references_share_end_without_freshness_extension(tmp_path, monkeypatch):
    _, finite = example(monkeypatch)
    support = importlib.import_module("support")
    selected = execution.execution_policy("extended")
    instant = datetime.now(timezone.utc)
    start = (instant - timedelta(seconds=1)).isoformat()
    end = (instant - timedelta(seconds=1) + timedelta(hours=6)).isoformat()
    bound = execution.fixture_authority(selected, start, end, current_utc=instant)
    for name in ("root", "private", "control", "workers"):
        (tmp_path / name).mkdir()
    policy = support.policy(tmp_path / "root", "Observe finite value.", run_seconds=bound["run_seconds"])
    keys = {name: bytes([index + 1]) * 32 for index, name in enumerate(
        ("receipt", "qualifier", "generator", "evidence", "scope", "critic", "adjudicator", "observer"))}
    original = finite.selections
    issued, retained = [], {}
    def selections(document, **kwargs):
        result = original(document, **kwargs)
        retained["signer"] = result[5]
        result[2]["cases"] = []
        def issue(*args, **options):
            issued.append(options)
            return {"state": "qualified", "expires_utc": options["expires_utc"]}
        return (*result[:4], SimpleNamespace(issue=issue), *result[5:])
    monkeypatch.setattr(finite, "selections", selections)
    monkeypatch.setattr(finite, "restore", lambda *args, **kwargs: "restored")
    assert finite.provision_services(*(tmp_path / name for name in ("root", "private", "control", "workers")),
        keys, policy, {"id": "finite", "revision": "1"}, bound["expires_at"], execution_authority=bound) == "restored"
    document = json.loads((tmp_path / "private/finite-services.json").read_bytes())
    assert document["execution_authority"] == bound
    assert document["expires_utc"] == retained["signer"].expires_utc == bound["expires_at"]
    assert len(issued) == len(document["qualification_records"]) == 4
    assert all(options["verification_seconds"] == 30 and options["expires_utc"] == bound["expires_at"] for options in issued)
    for key, value in (("expires_utc", (instant + timedelta(hours=5)).isoformat()),
                       ("policy", policy.to_dict() | {"budgets": policy.to_dict()["budgets"] | {"seconds": 14401}})):
        changed = copy.deepcopy(document)
        changed[key] = value
        with pytest.raises(ValueError, match="Finite fixture authority binding differs"):
            original(changed)


def test_extended_scenario_forwards_frozen_authority_and_completion_tracker(tmp_path, monkeypatch):
    from tests.test_repeated_integration_host import scenario_fixture

    data = scenario_fixture(monkeypatch, tmp_path, {})
    selected = execution.execution_policy("extended")
    data.plan["scope"].update(native_run_seconds=14400, execution_policy=selected,
                              execution_policy_sha256=digest(selected))
    start = datetime.fromisoformat(data.plan["frozen_at"])
    data.plan["expires_at"] = (start + timedelta(seconds=21600)).isoformat()
    for slot in data.plan["slots"]:
        slot["elapsed_cap_seconds"] = 1300
    provision = host._load_example()[0]
    options = {}
    def create(path, **kwargs):
        options.update(kwargs)
        return provision(path)
    monkeypatch.setattr(host, "_load_example", lambda: (create, None, None, None, None))
    routes = host.Routes
    tracker = MagicMock()
    def route(*args, child_tracker):
        assert child_tracker is tracker
        return routes(*args)
    monkeypatch.setattr(host, "Routes", route)
    result = host.run_scenario(data.plan, "direct", data.directory, remaining_seconds=3900, child_tracker=tracker)
    assert result["failure"] is None
    assert options == {"run_seconds": 14400, "execution_policy": selected,
        "study_frozen_at": data.plan["frozen_at"], "study_expires_at": data.plan["expires_at"], "child_tracker": tracker}
    assert tracker.confirm_completed.call_count >= 9


@pytest.mark.parametrize("change", [lambda p: p["scope"].pop("execution_policy_sha256"),
    lambda p: p["scope"].pop("execution_policy"), lambda p: p["scope"].update(native_run_seconds=1800),
    lambda p: p["slots"][0].update(elapsed_cap_seconds=900)])
def test_scenario_rejects_partial_or_inconsistent_policy_before_provisioning(tmp_path, change):
    selected, start, end = interval()
    plan = {"hosts": [{"host_id": "direct", "route": "direct"}], "frozen_at": start, "expires_at": end,
        "scope": {"native_run_seconds": 14400, "execution_policy": selected,
                  "execution_policy_sha256": digest(selected)}, "slots": [{"elapsed_cap_seconds": 1300}]}
    change(plan)
    with pytest.raises(ValueError):
        host.run_scenario(plan, "direct", tmp_path / "uncreated")
    assert not (tmp_path / "uncreated").exists()
