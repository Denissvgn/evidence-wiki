"""Historical cutoffs qualify exact observed or independently available revisions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki import verify_snapshot
from evidence_wiki.errors import SourceError
from tests._execution_fixture import authenticate, canonical, identifier
from tests._script_loader import load_isolated_module
from tests._temporal_fixture import TemporalFixture, clock_claim

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template" / "scripts"


@pytest.fixture
def host(tmp_path, monkeypatch):
    return TemporalFixture(tmp_path, monkeypatch)


def selected(report):
    return [item["source_revision"] for item in report["result"]["selected"]]


def analysis(source="lab:sample", value="17.50"):
    policy = "pack:observations/fresh"
    return {"query": "observations", "domain_pack": {"name": "observations", "policy_vocabularies": {
            "freshness_policy": {policy: "Observation must be recent."}}, "policy_rules": {policy: {
            "all_of": [{"max_age": {"field": "provenance/retrieved_at", "hours": 48}}]}}},
            "question_frontmatter": {}, "facets": [{"id": facet, "source_ids": [source], "policy_ids": [policy]}
                                                   for facet in ("first", "second")],
            "grounding": [{"id": "value", "source_id": source, "pointer": "/measurement/value", "expected": value}]}


@pytest.mark.parametrize("source", ["lab:sample", "filing:issuer"])
def test_one_cutoff_across_selection_query_facets_and_grounding(host, source):
    first, _ = host.captured(source)
    request = host.request(source, analysis=analysis(source))
    before = {path.relative_to(host.root): path.read_bytes() for path in host.root.rglob("*") if path.is_file()}
    state = (host.host / "evidence-state.json").read_bytes()
    report = host.evaluate(request)
    assert selected(report) == [first["source_revision"]]
    assert report["result"]["complete"]
    assert {facet["evaluated_at"] for facet in report["result"]["facets"]} == {"2026-09-10T01:00:00+00:00"}
    assert all(facet["outcome"] == "pass" for facet in report["result"]["facets"])
    assert report["result"]["grounding"][0]["value"] == "17.50"
    assert report["result"]["grounding"][0]["outcome"] == "pass"
    assert report["result"]["retrieval"]["indexed_documents"] == 1
    assert not report["current_use"]["authorized"]
    assert not report["result"]["retrieval"]["cache_used"]
    assert {path.relative_to(host.root): path.read_bytes() for path in host.root.rglob("*") if path.is_file()} == before
    assert (host.host / "evidence-state.json").read_bytes() == state


def test_later_correction_cannot_replace_cutoff_revision_or_leak_cached_answer(host):
    first, _ = host.captured()
    old = host.request(analysis=analysis())
    original = host.evaluate(old)
    host.set_time("2026-09-10T02:00:00Z")
    correction, _ = host.captured(value="FUTURE_SENTINEL", available="2026-09-10T01:30:00Z",
                                 supersedes=first["source_revision"], normalized=b"---\nsource_id: lab:sample\n---\nFUTURE_SENTINEL observations\n")
    (host.root / "wiki").mkdir()
    (host.root / "wiki" / "answer.md").write_text("FUTURE_SENTINEL observations")
    (host.root / ".query-index.json").write_text('{"answer":"FUTURE_SENTINEL"}')
    host.set_time("2026-09-10T03:00:00Z")
    replay = host.evaluate(old)
    assert replay["result_id"] == original["result_id"] and replay["result"] == original["result"]
    request = host.request(analysis=analysis())
    report = host.evaluate(request)
    assert selected(report) == [first["source_revision"]]
    assert "FUTURE_SENTINEL" not in json.dumps(report)
    assert report["result"]["exclusions"][0]["reason"] == "temporal_future_publication"
    current = host.evaluate(host.request(mode="current", analysis=analysis(value="FUTURE_SENTINEL")))
    assert selected(current) == [correction["source_revision"]]
    assert current["result"]["grounding"][0]["outcome"] == "pass"


@pytest.mark.parametrize("mode,expected", [("historical-audit", False), ("historical-available", True)])
def test_late_retrieval_requires_independent_public_availability(host, mode, expected):
    host.set_time("2026-09-10T02:00:00Z")
    body, files = host.captured()
    host.availability(body, files)
    report = host.evaluate(host.request(mode=mode))
    assert report["result"]["complete"] is expected
    assert bool(selected(report)) is expected
    if expected:
        assert report["result"]["selected"][0]["availability_event_id"] == host.checkpoint
    else:
        assert report["result"]["exclusions"][0]["reason"] == "temporal_not_host_observed_at_cutoff"


def test_provider_claim_alone_does_not_qualify_historical_public_time(host):
    host.captured()
    report = host.evaluate(host.request(mode="historical-available"))
    assert not report["result"]["complete"]
    assert report["result"]["exclusions"][0]["reason"] == "temporal_independent_availability_missing"


@pytest.mark.parametrize("corruption", ["signature", "proof", "source", "time", "controller", "role"])
def test_availability_authentication_refuses_forgery_before_event_commit(host, corruption):
    body, files = host.captured()
    receipt_body = host.availability(body, files, commit=False)
    if corruption == "controller":
        host.policy["principals"]["evaluator"]["controller"] = "runner"
        host.save_policy()
    elif corruption == "role":
        host.policy["principals"]["evaluator"]["roles"] = ["evaluator"]
        host.save_policy()
    elif corruption == "signature":
        receipt_body["receipt"]["authentication"]["signature"] = "0" * 64
    elif corruption == "proof":
        receipt_body["receipt"]["payload"]["proof_artifacts"][0]["content_hash"] = "sha256:" + "0" * 64
    elif corruption == "time":
        receipt_body["receipt"]["payload"]["available_at"] = "2026-09-01T00:00:00Z"
    else:
        receipt_body["receipt"]["payload"]["source_id"] = "lab:different"
    before = (host.host / "evidence-state.json").read_bytes()
    with pytest.raises(SourceError):
        host.transact(host.module, "attest-availability", receipt_body)
    assert (host.host / "evidence-state.json").read_bytes() == before


def test_availability_idempotence_checkpoint_and_current_key_revocation(host):
    body, files = host.captured()
    receipt = host.availability(body, files, commit=False)
    before = host.checkpoint
    command = host.command("attest-availability", receipt)
    first = host.workspace.usage.transact(command)
    host.checkpoint = first["checkpoint"]
    assert host.workspace.usage.transact(command) == first
    assert not host.evaluate(host.request(mode="historical-available", checkpoint=before))["result"]["complete"]
    assert host.evaluate(host.request(mode="historical-available"))["result"]["complete"]
    with pytest.raises(SourceError) as refused:
        host.transact(host.module, "attest-availability", receipt)
    assert refused.value.details["reason"] == "availability_receipt_already_recorded"
    host.policy["revoked_keys"].append("evaluator-key")
    host.save_policy()
    report = host.evaluate(host.request(mode="historical-available"))
    assert not report["result"]["complete"]


@pytest.mark.parametrize("change,reason", [
    ({"available_at": clock_claim(None)}, "temporal_availability_unknown"),
    ({"published_at": clock_claim("2026-09-09")}, "timestamp_requires_explicit_timezone"),
    ({"published_at": clock_claim("2026-09-09T10:00:00")}, "timestamp_requires_explicit_timezone"),
    ({"published_at": clock_claim("2026-09-10T01:00:00.000001Z"), "available_at": clock_claim("2026-09-10T01:00:00.000001Z")}, "temporal_future_publication"),
    ({"expires_at": clock_claim("2026-09-10T01:00:00Z")}, "temporal_expired_at_cutoff"),
    ({"expires_at": clock_claim(None)}, "temporal_expiry_unknown"),
    ({"measurement": {"start": clock_claim("2026-09-10T00:00:00Z"), "end": clock_claim("2026-09-10T01:00:01Z")}}, "temporal_measurement_incomplete_at_cutoff"),
    ({"effective": {"start": clock_claim("2026-09-09T00:00:00Z"), "end": clock_claim("2026-09-10T01:00:00Z")}}, "temporal_outside_effective_interval"),
    ({"claimed_retrieved_at": clock_claim("2026-09-10T02:00:00Z")}, "temporal_claim_exceeds_host_observation"),
])
def test_unknown_invalid_and_boundary_times_are_explicit_gaps(host, change, reason):
    host.captured(temporal=change)
    report = host.evaluate()
    assert not report["result"]["complete"] and not selected(report)
    assert report["result"]["exclusions"][0]["reason"] == reason


def test_exact_cutoff_and_explicitly_open_effective_interval_pass(host):
    body, _ = host.captured(published="2026-09-10T01:00:00Z", available="2026-09-10T01:00:00Z",
                            temporal={"effective": {"start": clock_claim("2026-09-10T01:00:00Z"), "end": None}})
    assert selected(host.evaluate()) == [body["source_revision"]]


@pytest.mark.parametrize("change", ["cutoff", "checkpoint", "unknown-checkpoint", "future-cutoff", "duplicate", "oversized", "nan"])
def test_invalid_request_refused_without_state_change(host, change):
    host.captured()
    request = host.request()
    if change in {"cutoff", "checkpoint"}:
        request["mode"] = "current"
        request["cutoff" if change == "checkpoint" else "checkpoint"] = None
    elif change == "unknown-checkpoint":
        request["checkpoint"] = "sha256:" + "0" * 64
    elif change == "future-cutoff":
        request["cutoff"] = "2027-01-01T00:00:00Z"
    elif change == "duplicate":
        request["source_ids"] *= 2
    elif change == "nan":
        request["analysis"]["question_frontmatter"]["number"] = float("nan")
    else:
        request["analysis"]["query"] = "x" * (256 * 1024)
    before = (host.host / "evidence-state.json").read_bytes()
    with pytest.raises(SourceError):
        host.evaluate(request)
    assert (host.host / "evidence-state.json").read_bytes() == before


def test_legacy_unsupported_and_restricted_bytes_do_not_reach_analysis(host):
    legacy, files = host.source()
    host.transact(host.module, "deposit", legacy, files)
    report = host.evaluate()
    assert report["result"]["exclusions"][0]["reason"] == "temporal_metadata_unsupported"
    denied, _ = host.captured("lab:denied", retrieval=False, value="DENIED_SENTINEL")
    request = host.request("lab:denied", analysis=analysis("lab:denied", "unavailable"))
    report = host.evaluate(request)
    assert not selected(report)
    assert "DENIED_SENTINEL" not in json.dumps(report)
    assert report["result"]["grounding"][0]["value"] is None
    assert all(facet["outcome"] == "fail" for facet in report["result"]["facets"])
    assert denied["source_revision"] == report["result"]["exclusions"][0]["source_revision"]


def test_old_checkpoint_cannot_bypass_current_revocation(host):
    body, _ = host.captured()
    request = host.request()
    assert selected(host.evaluate(request))
    host.revoke(host.module, body)
    report = host.evaluate(request)
    assert not selected(report)
    assert report["result"]["exclusions"][0]["reason"] == "source_revision_revoked"


def test_forked_corrections_are_ambiguous_and_missing_sources_are_visible(host):
    first, _ = host.captured()
    host.captured(value="18.00", supersedes=first["source_revision"])
    host.captured(value="19.00", supersedes=first["source_revision"])
    report = host.evaluate(host.request("lab:sample", "lab:missing"))
    assert not report["result"]["complete"]
    assert {row["reason"] for row in report["result"]["gaps"]} == {"temporal_revision_chain_ambiguous", "temporal_no_qualified_revision"}


def test_future_ancestor_cannot_be_hidden_by_derived_record_timestamp(host):
    parent, _ = host.captured("lab:future", published="2026-09-10T02:00:00Z", available="2026-09-10T02:00:00Z")
    host.captured(parents=[parent["source_revision"]])
    report = host.evaluate()
    assert not selected(report)
    assert report["result"]["exclusions"][0]["reason"] == "temporal_future_publication"


def test_revision_bound_is_a_refusal_not_a_successful_prefix(host, monkeypatch):
    host.captured()
    host.captured(value="18.00")
    bounds = host.script.evaluate.__globals__["BOUNDS"]
    monkeypatch.setitem(bounds, "revisions", 1)
    with pytest.raises(SourceError) as refused:
        host.evaluate()
    assert refused.value.details["reason"] == "temporal_revision_bound_exceeded"


def test_replay_rechecks_grant_expiry_at_read_boundary(host, monkeypatch):
    body, files = host.source()
    # The final byte closure and scrub receipt are signed only after adding temporal metadata.
    body["grant"]["payload"]["expires_at"] = "2026-09-10T02:00:00Z"
    body["grant"] = authenticate(body["grant"]["payload"], "owner", "usage")
    descriptor = json.loads(files["source-record.json"])
    _, temporal_files = host.captured("lab:shape")
    descriptor["temporal"] = json.loads(temporal_files["source-record.json"])["temporal"]
    descriptor["temporal"].update(record_path=None, provenance_path=None)
    files["source-record.json"] = canonical(descriptor)
    host.deposit_files(body, files)
    function = host.script.evaluate.__globals__["evaluate_analysis"]

    def cross_boundary(*args):
        result = function(*args)
        host.set_time("2026-09-10T02:00:00Z")
        return result

    monkeypatch.setitem(host.script.evaluate.__globals__, "evaluate_analysis", cross_boundary)
    with pytest.raises(SourceError) as refused:
        host.evaluate()
    assert refused.value.details["reason"] == "usage_outside_validity"


def test_real_cli_matches_api_and_rejects_oversized_stdin(host):
    host.captured()
    request = host.request(analysis=analysis())
    expected = host.evaluate(request)
    command = [sys.executable, "-m", "evidence_wiki.cli", "temporal", "evaluate", "--target", str(host.root)]
    result = subprocess.run(command, input=canonical(request), capture_output=True, check=False, timeout=30, env=os.environ.copy())
    assert result.returncode == 0, result.stdout + result.stderr
    actual = json.loads(result.stdout)
    assert actual["result_id"] == expected["result_id"] and actual["result"] == expected["result"]
    refused = subprocess.run(command, input=b"x" * (256 * 1024 + 1), capture_output=True, check=False, timeout=30)
    assert refused.returncode == 2
    assert json.loads(refused.stderr)["error_code"] == "EVIDENCE_TEMPORAL_REFUSED"


def test_unqualified_future_correction_cannot_invalidate_past_chain(host):
    first, _ = host.captured()
    host.set_time("2026-09-10T03:00:00Z")
    host.captured(value="future", published="2026-09-10T02:00:00Z", available="2026-09-10T02:00:00Z",
                  supersedes="sha256:" + "0" * 64)
    assert selected(host.evaluate()) == [first["source_revision"]]
    assert not host.evaluate(host.request(mode="current"))["result"]["complete"]


def test_backdated_correction_cannot_bypass_future_predecessor(host):
    predecessor, _ = host.captured(published="2026-09-10T02:00:00Z", available="2026-09-10T02:00:00Z")
    host.captured(supersedes=predecessor["source_revision"])
    report = host.evaluate()
    assert not selected(report)
    assert report["result"]["gaps"][0]["reason"] == "temporal_revision_chain_ambiguous"


@pytest.mark.parametrize("mode", ["historical-audit", "historical-available"])
def test_temporal_snapshot_repeats_exact_bytes_and_verifies_without_origin(host, mode, monkeypatch):
    if mode == "historical-available":
        host.set_time("2026-09-10T02:00:00Z")
    body, _, selection = host.execution_source(mode=mode)
    report = host.evaluate(host.request(body["source_id"], mode=mode))
    assert report["result"]["complete"] and report["result"]["lineage"]
    data, preparation, command, exported = host.export(selection)
    assert preparation["manifest"]["selection"]["temporal"] == selection["temporal"]
    assert json.loads(data)["schema_version"] == "evidence-snapshot/v2"
    host.set_time("2026-09-10T02:00:00Z")
    repeated = host.workspace.snapshots.export(selection, registration_request_id=command["payload"]["request_id"])
    assert not repeated["created"] and (host.root / repeated["path"]).read_bytes() == data
    policy = host.policy_path.read_bytes()
    assert host.workspace.snapshots.check(data)["current_use"] == "authorized"
    host.revoke(host.module, host.parent)
    assert host.workspace.snapshots.check(data)["current_use"] == "denied"
    host.root.rename(host.root.with_name("origin-unavailable"))
    host.host.rename(host.host.with_name("host-unavailable"))
    monkeypatch.delenv("EVIDENCE_WIKI_AUTHORITY_FILE")
    monkeypatch.delenv("EVIDENCE_WIKI_STATE_DIR")
    verified = verify_snapshot(data, trust_policy_bytes=policy)
    assert verified["valid"] and verified["current_use"] == "not_evaluated"
    assert verified["snapshot_id"] == exported["snapshot_id"]


@pytest.mark.parametrize("invalid", ["future-parent", "unknown-parent", "late-parent", "future-execution", "unknown-checkpoint", "future-cutoff"])
def test_temporal_export_refuses_unsupported_or_future_closure(host, invalid):
    changes = {"published_at": clock_claim("2026-09-10T02:00:00Z"), "available_at": clock_claim("2026-09-10T02:00:00Z")} if invalid == "future-parent" else (
        {"available_at": clock_claim(None)} if invalid == "unknown-parent" else None)
    if invalid == "late-parent":
        host.set_time("2026-09-10T02:00:00Z")
    _, _, selection = host.execution_source(parent_times=changes)
    if invalid == "unknown-checkpoint":
        selection["temporal"]["checkpoint"] = "sha256:" + "0" * 64
    elif invalid == "future-cutoff":
        selection["temporal"]["cutoff"] = "2027-01-01T00:00:00Z"
    elif invalid == "future-execution":
        selection["temporal"]["cutoff"] = "2026-09-10T00:00:00Z"
    before = (host.host / "evidence-state.json").read_bytes()
    with pytest.raises(SourceError):
        host.workspace.snapshots.prepare(selection)
    assert (host.host / "evidence-state.json").read_bytes() == before
    assert not (host.root / "exports").exists()


@pytest.mark.parametrize("change", ["proof-signature", "proof-time", "missing-proof", "cutoff", "source-observation", "extra-proof"])
def test_independent_temporal_verifier_rejects_host_resealed_false_claims(host, change):
    _, _, selection = host.execution_source(mode="historical-available")
    data, _, _, _ = host.export(selection)
    bundle = json.loads(data)
    manifest = bundle["manifest"]
    revision = host.parent["source_revision"]
    proofs = manifest["temporal"]["availability"]
    if change == "proof-signature":
        proofs[revision]["body"]["receipt"]["authentication"]["signature"] = "0" * 64
    elif change == "proof-time":
        proofs[revision]["body"]["receipt"]["payload"]["available_at"] = "2026-09-01T00:00:00Z"
    elif change == "missing-proof":
        del proofs[revision]
    elif change == "extra-proof":
        proofs["sha256:" + "0" * 64] = proofs[revision]
    elif change == "cutoff":
        manifest["selection"]["temporal"]["cutoff"] = "2026-09-08T00:00:00Z"
    else:
        next(source for source in manifest["sources"] if source["source_revision"] == revision)["observed_at"] = "2026-09-10T01:00:01Z"
    bundle["snapshot_id"] = identifier("evidence-snapshot-manifest/v2", manifest)
    payload = bundle["registration"]["payload"]
    payload["body"]["node_id"] = bundle["snapshot_id"]
    bundle["registration"] = authenticate(payload, "owner", "usage")
    report = verify_snapshot(canonical(bundle), trust_policy_bytes=host.policy_path.read_bytes())
    assert not report["valid"]


@pytest.mark.parametrize("entry", ["coverage_manifest", "_evidence_policies"])
def test_current_coverage_uses_one_clock_across_multiple_facets(monkeypatch, tmp_path, entry):
    module = load_isolated_module("coverage_clock", SCRIPTS / (entry + ".py"))
    policies = module if entry == "_evidence_policies" else module.load_sibling_module("_evidence_policies")
    observed = []

    class Clock:
        calls = 0

        @classmethod
        def now(cls, zone):
            cls.calls += 1
            return datetime(2026, 9, 10, 1, tzinfo=timezone.utc) + timedelta(seconds=cls.calls)

    def evaluation(facet, inputs, *, question_slug, now=None):
        observed.append(now)
        return [SimpleNamespace(to_dict=lambda: {"verdict": "ok" if now == observed[0] else "fail"})]

    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(policies, "evaluate_facet_policies", evaluation)
    manifest = {"question_slug": "sample", "required_facets": [{"facet_id": key, "accepted_source_ids": ["lab:sample"]}
                 for key in ("first", "second")], "optional_facets": []}
    if entry == "coverage_manifest":
        monkeypatch.setattr(policies, "load_policy_inputs", lambda *args: object())
        result, _ = module.evaluate_policy_results_for_manifest(tmp_path, {}, manifest)
    else:
        result = module.evaluate_coverage_manifest_policies(manifest, object())
    assert Clock.calls == 1 and len(observed) == 2 and observed[0] is not None and len(set(observed)) == 1
    assert all(facet["policy_results"][0]["verdict"] == "ok" for facet in result["facets"])
