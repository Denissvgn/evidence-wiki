"""Original owner adapters bind replayed bytes and authenticated lifetimes."""

import copy
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools import _release_decision as release
from tools import _release_decision_owners as owners
from tools import measure_integration_costs, qualify_unattended
from tools._acceptance_protocol import digest as owner_digest


@pytest.fixture
def original_owner():
    instant = datetime.now(timezone.utc)
    expires = (instant + timedelta(minutes=5)).isoformat()
    execution = {"deadline_utc": expires, "original_execution": "retained owner"}
    report = {
        "preliminary": False,
        "candidate_sha256": "a" * 64,
        "observation_class": "fixture_mechanics",
        "decision": "qualified",
        "failures": [],
        "execution_sha256": owner_digest(execution),
        "costs": {
            "automated": {"metrics": {"elapsed_seconds": {"known_subtotal": "2.125"}}},
            "coverage": {"missing_attempt_ids": [], "missing_metrics": []},
        },
        "denominators": {"terminal_tasks": 5, "started_stop_unknown_tasks": 0},
    }
    raw = release.canonical(report)
    head = owner_digest(report)
    sources = [{"category": category, "owner_ref": "original:" + category,
                "raw_sha256": hashlib.sha256(raw).hexdigest(), "source_head_sha256": head}
               for category in release.CATEGORIES]
    budgets = dict.fromkeys(release.METRICS)
    budgets["elapsed_seconds"] = "10"
    plan = release.freeze_plan(candidate_sha256=report["candidate_sha256"], host_sha256="b" * 64,
        profile="technical_fixture", required_sources=sources, budgets=budgets,
        scope={"kind": "unattended_original_roles"},
        frozen_at=(instant - timedelta(seconds=1)).isoformat(), expires_at=expires)
    return {"plan": plan, "report": report, "execution": execution, "raw": raw, "head": head,
            "path": Path("/private/tmp/retained-original-owner/report.json"), "observer": object()}


def install_owner(monkeypatch, original, *, replayed=None, execution=None, raw_reads=None):
    """Stub owner IO without keys, original jobs, or private artifact access."""
    replay = Mock(return_value=original["report"] if replayed is None else replayed)
    authenticate = Mock(return_value=original["execution"] if execution is None else execution)
    read = Mock(return_value=original["raw"], side_effect=raw_reads)
    monkeypatch.setattr(owners, "replay", replay)
    monkeypatch.setattr(owners, "_authenticated", authenticate)
    monkeypatch.setattr(owners, "read_private", read)
    return replay, authenticate, read


def make_readers(original):
    return owners.unattended_readers(original["plan"], report_path=original["path"],
        observer_key=original["observer"], expected_report_sha256=original["head"])


def test_successful_projection_replays_original_once_per_complete_read_round(monkeypatch, original_owner):
    original = original_owner
    replay, authenticate, read = install_owner(monkeypatch, original)
    readers = make_readers(original)
    result = release.decide(original["plan"], release.digest(original["plan"]), readers)
    assert result["outcome"] == "qualified"
    assert result["claim_scope"] == "fixture_mechanics"
    assert len(result["evidence"]) == len(release.CATEGORIES)
    assert replay.call_count == authenticate.call_count == 2
    assert read.call_count == 4
    for call in replay.call_args_list:
        assert call.args == (original["path"],)
        assert call.kwargs == {"observer_key": original["observer"], "expected_report_sha256": original["head"]}
    for call in authenticate.call_args_list:
        assert call.args == (original["path"].parent / "execution.json", original["observer"])
    for row in result["evidence"]:
        assert row["source_head_sha256"] == original["head"]
        assert row["raw_sha256"] == hashlib.sha256(original["raw"]).hexdigest()
        assert row["projection"]["status"] == "pass"
        expected_class = "simulated" if row["category"] in {"semantic", "integration", "conformance"} else "deterministic_verified"
        assert row["evidence_class"] == expected_class
        assert row["projection"]["judge"] is row["projection"]["business"] is None
        for citation in row["citations"]:
            assert release.canonical(citation["value"]) == release.canonical(
                release._pointer(original["report"], citation["pointer"]))
    cost = next(row for row in result["evidence"] if row["category"] == "cost")
    assert cost["projection"]["metrics"] == dict.fromkeys(release.METRICS) | {"elapsed_seconds": "2.125"}
    assert all(result[field] is False for field in ("authorized", "evidence_accepted", "release_published"))


def test_report_bytes_must_match_authenticated_replay_even_when_citations_match(monkeypatch, original_owner):
    replayed = copy.deepcopy(original_owner["report"])
    replayed["different_uncited_origin"] = "other retained report"
    install_owner(monkeypatch, original_owner, replayed=replayed)
    with pytest.raises(release.EvidenceUnavailable):
        make_readers(original_owner)[0].read()


def test_replayed_report_must_match_frozen_external_head(monkeypatch, original_owner):
    original_owner["head"] = "c" * 64
    for source in original_owner["plan"]["required_sources"]:
        source["source_head_sha256"] = original_owner["head"]
    install_owner(monkeypatch, original_owner)
    with pytest.raises(release.EvidenceUnavailable):
        make_readers(original_owner)[0].read()


def test_authenticated_execution_must_match_report_commitment(monkeypatch, original_owner):
    execution = dict(original_owner["execution"], deadline_utc="2099-01-01T00:00:00Z")
    install_owner(monkeypatch, original_owner, execution=execution)
    with pytest.raises(release.EvidenceUnavailable):
        make_readers(original_owner)[0].read()


def test_unsigned_execution_is_refused_by_original_authenticator(monkeypatch, original_owner):
    original = original_owner
    assert owners._authenticated is measure_integration_costs._authenticated
    monkeypatch.setattr(owners, "replay", Mock(return_value=original["report"]))
    monkeypatch.setattr(owners, "read_private", Mock(return_value=original["raw"]))
    original_read = Mock(return_value=release.canonical(original["execution"]))
    monkeypatch.setattr(measure_integration_costs, "read_private", original_read)
    with pytest.raises(release.EvidenceUnavailable):
        make_readers(original)[0].read()
    assert original_read.call_args.args[0] == original["path"].parent / "execution.json"


def test_original_replay_authenticates_report_instead_of_trusting_json_claims(monkeypatch, original_owner):
    original = original_owner
    assert owners.replay is qualify_unattended.replay
    assert qualify_unattended._authenticated is measure_integration_costs._authenticated
    claimed = dict(original["report"], verified=True, owner_authenticated=True)
    raw = release.canonical(claimed)
    monkeypatch.setattr(owners, "read_private", Mock(return_value=raw))
    original_read = Mock(return_value=raw)
    monkeypatch.setattr(measure_integration_costs, "read_private", original_read)
    with pytest.raises(release.EvidenceUnavailable):
        make_readers(original)[0].read()
    assert original_read.call_args.args[0] == original["path"]


def test_changed_report_bytes_after_replay_are_refused(monkeypatch, original_owner):
    changed = release.canonical(dict(original_owner["report"], changed_after_replay=True))
    install_owner(monkeypatch, original_owner, raw_reads=[original_owner["raw"], changed])
    with pytest.raises(release.EvidenceUnavailable):
        make_readers(original_owner)[0].read()


def test_closing_shared_owner_withdrawal_prevents_old_qualification(monkeypatch, original_owner):
    replay, _, _ = install_owner(monkeypatch, original_owner)
    replay.side_effect = [original_owner["report"], ValueError("original owner withdrew evidence")]
    result = release.decide(original_owner["plan"], release.digest(original_owner["plan"]), make_readers(original_owner))
    assert result["outcome"] == "inconclusive"
    assert result["evidence"] == []
    assert replay.call_count == 2


@pytest.mark.parametrize("coverage_change", ["missing_attempt_ids", "missing_metrics", "terminal_tasks", "started_stop_unknown_tasks"])
def test_incomplete_original_role_usage_remains_unknown(monkeypatch, original_owner, coverage_change):
    original = original_owner
    if coverage_change in {"missing_attempt_ids", "missing_metrics"}:
        original["report"]["costs"]["coverage"][coverage_change] = ["unobserved"]
    else:
        original["report"]["denominators"][coverage_change] = 4 if coverage_change == "terminal_tasks" else 1
    original["raw"] = release.canonical(original["report"])
    original["head"] = owner_digest(original["report"])
    for source in original["plan"]["required_sources"]:
        source["raw_sha256"] = hashlib.sha256(original["raw"]).hexdigest()
        source["source_head_sha256"] = original["head"]
    install_owner(monkeypatch, original)
    result = release.decide(original["plan"], release.digest(original["plan"]), make_readers(original))
    assert result["outcome"] == "inconclusive"
    assert any(reason["code"] == "usage_unknown_elapsed_seconds" for reason in result["reasons"])
