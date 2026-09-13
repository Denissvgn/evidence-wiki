"""Authenticated assessments refuse stale authority independently of notifications."""

import contextlib
import copy
import io
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import evidence_wiki
from evidence_wiki.cli import main
from evidence_wiki.errors import EvidenceWikiError
from tests._assessment_fixture import SOURCE_ID, AssessmentFixture
from tests._execution_fixture import authenticate, identifier
from tests._script_loader import load_isolated_module

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template/scripts"
USAGE = load_isolated_module("assessment_usage", SCRIPTS / "_evidence_usage.py")


@pytest.fixture
def host(tmp_path, monkeypatch):
    def initialize(profile):
        with contextlib.redirect_stdout(io.StringIO()):
            assert main(["init", "--profile", str(profile)]) == 0
    fixture = AssessmentFixture(tmp_path, monkeypatch, initialize)
    fixture.transact(USAGE, "initialize")
    fixture.body, fixture.files = fixture.temporal_source()
    fixture.transact(USAGE, "deposit", fixture.body, fixture.files)
    return fixture


def issue(host, workspace, **changes):
    prepared = workspace.assessments.prepare(host.request(**changes))
    envelope = host.sign(prepared["registration"])
    host.checkpoint = workspace.assessments.issue(envelope)["checkpoint"]
    return envelope


def state_bytes(host):
    return (host.host / "evidence-state.json").read_bytes()


def reseal(envelope):
    payload = envelope["payload"]["body"]["assessment"]
    payload["assessment_id"] = identifier("evidence-assessment/v1", {key: value for key, value in payload.items() if key != "assessment_id"})
    return authenticate(envelope["payload"], "owner", "assessment")


def test_prepare_issue_and_consume_nonfinancial_assessment(host):
    with evidence_wiki.Workspace.open(host.root) as workspace:
        prepared = workspace.assessments.prepare(host.request())
        payload = prepared["assessment"]
        assert payload["publication"]["verdict"] == "ship", payload
        assert payload["gaps"] == []
        assert payload["inputs"]["selected"] == {SOURCE_ID: host.body["source_revision"]}
        assert payload["expires_at"] == "2026-12-01T00:00:00+00:00"
        envelope = host.sign(prepared["registration"])
        receipt = workspace.assessments.issue(envelope)
        host.checkpoint = receipt["checkpoint"]
        assert workspace.assessments.issue(envelope) == receipt
        checked = workspace.assessments.check(envelope)
        assert checked["eligible"], checked
        assert checked["external_action_authorized"] is False
        plan = workspace.assessments.plan_refresh(host.refresh())["plan"]
        assert plan["entries"] == []
        assert plan["coverage"]["complete"]


@pytest.mark.parametrize("layout", ["sidecar", "directory"])
def test_assessment_accepts_approved_raw_layouts(host, layout):
    manifest = host.root / "sources/manifest.jsonl"
    record = json.loads(manifest.read_text())
    capture = host.raw_path
    if layout == "directory":
        capture = host.raw_path.with_suffix("")
        capture.mkdir()
        (capture / "index.html").write_bytes(host.raw_path.read_bytes())
        host.raw_path.unlink()
        record["raw_paths"] = [capture.relative_to(host.root).as_posix()]
    sidecar = capture.with_name(capture.name + ".provenance.yml")
    sidecar.write_text(yaml.safe_dump(record["provenance"]))
    record["provenance"]["sidecar_path"] = sidecar.relative_to(host.root).as_posix()
    members = list(capture.rglob("*")) if capture.is_dir() else [capture]
    evidence = {path.relative_to(host.root).as_posix(): path.read_bytes() for path in [*members, sidecar]}
    body, files = host.temporal_source(supersedes=host.body["source_revision"], evidence=evidence)
    host.transact(USAGE, "deposit", body, files)
    record["usage_revision_id"] = body["source_revision"]
    manifest.write_text(json.dumps(record) + "\n")
    with evidence_wiki.Workspace.open(host.root) as workspace:
        envelope = issue(host, workspace)
        assert workspace.assessments.check(envelope)["eligible"]


def test_whole_envelope_tampering_and_unknown_capabilities_preserve_state(host, subtests):
    with evidence_wiki.Workspace.open(host.root) as workspace:
        original = host.sign(workspace.assessments.prepare(host.request())["registration"])
        mutations = {
            "answer": lambda p: p["publication"]["export"]["questions"][0].update(answer_summary="Unapproved answer"),
            "source": lambda p: p["inputs"]["selected"].update({SOURCE_ID: "sha256:" + "0" * 64}),
            "verdict": lambda p: p["publication"].update(verdict="no_ship"),
            "cutoff": lambda p: p["request"]["temporal"].update(mode="historical-audit", cutoff="2026-09-10T00:00:00Z"),
            "expiry": lambda p: p.update(expires_at="2026-12-31T00:00:00Z"),
            "capability": lambda p: p["required_capabilities"].append("unsupported/v1"),
            "schema": lambda p: p.update(schema_version="evidence-assessment/v999"),
        }
        before = state_bytes(host)
        for label, mutation in mutations.items():
            with subtests.test(label=label):
                changed = copy.deepcopy(original)
                mutation(changed["payload"]["body"]["assessment"])
                with pytest.raises(EvidenceWikiError) as exc:
                    workspace.assessments.issue(changed)
                assert exc.value.error_code == "EVIDENCE_ASSESSMENT_REFUSED"
                assert state_bytes(host) == before
        forged = copy.deepcopy(original)
        forged["authentication"]["signature"] = "0" * 64
        with pytest.raises(EvidenceWikiError):
            workspace.assessments.issue(forged)
        assert state_bytes(host) == before


def test_registration_independently_recomputes_host_signed_proof(host):
    with evidence_wiki.Workspace.open(host.root) as workspace:
        envelope = host.sign(workspace.assessments.prepare(host.request())["registration"])
        envelope["payload"]["body"]["assessment"]["publication"]["readiness"]["coverage_summary"]["host_claim"] = "fabricated"
        before = state_bytes(host)
        with pytest.raises(EvidenceWikiError):
            workspace.assessments.issue(reseal(envelope))
        assert state_bytes(host) == before


@pytest.mark.parametrize("mode", ["pending", "unknown-validity", "replay"])
def test_ineligible_history_is_preserved_but_cannot_be_consumed(host, mode):
    changes = {}
    if mode == "pending":
        changes["review"] = "pending"
    elif mode == "replay":
        changes["temporal"] = {"mode": "historical-audit", "cutoff": datetime.now(timezone.utc).isoformat()}
    else:
        body, files = host.temporal_source(expires=None, supersedes=host.body["source_revision"])
        host.transact(USAGE, "deposit", body, files)
        # Exact bytes may have multiple revisions; use an explicit local identity.
        # A revision cannot contain its own digest: the manifest supplies the binding.
        manifest = host.root / "sources/manifest.jsonl"
        records = [json.loads(line) for line in manifest.read_text().splitlines()]
        records[0]["usage_revision_id"] = body["source_revision"]
        manifest.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    with evidence_wiki.Workspace.open(host.root) as workspace:
        envelope = issue(host, workspace, **changes)
        assert not workspace.assessments.check(envelope)["eligible"]
        assert envelope["payload"]["body"]["assessment"]["recorded_eligible"] is False
        assert json.loads(state_bytes(host))["events"][-1]["command"] == envelope


def test_correction_without_materialization_or_notification_blocks_old_evidence(host):
    with evidence_wiki.Workspace.open(host.root) as workspace:
        envelope = issue(host, workspace)
        correction, files = host.temporal_source(supersedes=host.body["source_revision"], normalized=host.normalized_path.read_bytes() + b"\nCorrected observation.\n")
        host.transact(USAGE, "deposit", correction, files)
        checked = workspace.assessments.check(envelope)
        assert not checked["eligible"] and checked["reasons"] == ["assessment_source_superseded"]
        host.revoke(USAGE, correction)
        assert not workspace.assessments.check(envelope)["eligible"]
        prepared = workspace.assessments.plan_refresh(host.refresh())
        assert prepared["plan"]["entries"][0]["assessment_id"] == envelope["payload"]["body"]["assessment"]["assessment_id"]
        assert prepared["plan"]["coverage"]["complete"]


def test_changed_missing_and_unapproved_bytes_are_detected_without_notification(host):
    with evidence_wiki.Workspace.open(host.root) as workspace:
        envelope = issue(host, workspace)
        original = host.normalized_path.read_bytes()
        host.normalized_path.write_bytes(original + b"\nUnapproved replacement.\n")
        assert not workspace.assessments.check(envelope)["eligible"]
        host.normalized_path.unlink()
        assert not workspace.assessments.check(envelope)["eligible"]
        host.normalized_path.write_bytes(original)
        (host.root / "raw/web/unapproved.html").write_text("Not approved for export")
        with pytest.raises(EvidenceWikiError):
            workspace.assessments.prepare(host.request())
        assert not workspace.assessments.check(envelope)["eligible"]


def test_revocation_refresh_is_atomic_idempotent_and_does_not_erase_history(host):
    with evidence_wiki.Workspace.open(host.root) as workspace:
        envelope = issue(host, workspace)
        host.revoke(USAGE, host.body)
        assert not workspace.assessments.check(envelope)["eligible"]
        prepared = workspace.assessments.plan_refresh(host.refresh())
        command = host.sign(prepared["application"])
        receipt = workspace.assessments.apply_refresh(command)
        host.checkpoint = receipt["checkpoint"]
        before = state_bytes(host)
        assert workspace.assessments.apply_refresh(command) == receipt
        assert state_bytes(host) == before
        assert workspace.assessments.check(envelope)["reasons"] == ["assessment_invalidated"]
        assert workspace.assessments.plan_refresh(host.refresh())["plan"]["entries"] == []
        recorded = json.loads(before)["events"]
        assert recorded[2]["command"] == envelope
        assert recorded[-1]["command"]["payload"]["body"]["plan"]["entries"][0]["reevaluation_id"]


def test_clock_only_forecast_pagination_resume_and_supersession(host):
    with evidence_wiki.Workspace.open(host.root) as workspace:
        first = issue(host, workspace)
        second = issue(host, workspace)
        horizon = "2026-12-02T00:00:00Z"
        prepared = workspace.assessments.plan_refresh(host.refresh(limit=1, evaluation_time=horizon))
        plan = prepared["plan"]
        assert not plan["coverage"]["complete"] and plan["coverage"]["truncated"]
        assert plan["entries"][0]["reasons"] == ["assessment_expired"]
        host.checkpoint = workspace.assessments.apply_refresh(host.sign(prepared["application"]))["checkpoint"]
        resumed = workspace.assessments.plan_refresh(host.refresh(limit=1, evaluation_time=horizon, cursor=plan["next_cursor"]))
        assert resumed["plan"]["coverage"]["previous_pages_not_included"]
        assert resumed["plan"]["next_cursor"] is None
        assert resumed["plan"]["entries"][0]["assessment_id"] != plan["entries"][0]["assessment_id"]
        host.checkpoint = workspace.assessments.apply_refresh(host.sign(resumed["application"]))["checkpoint"]
        assert not workspace.assessments.check(first)["eligible"]
        assert not workspace.assessments.check(second)["eligible"]
        replacement = workspace.assessments.prepare(host.request())
        replacement["registration"]["body"]["supersedes"] = [first["payload"]["body"]["assessment"]["assessment_id"]]
        new = host.sign(replacement["registration"])
        host.checkpoint = workspace.assessments.issue(new)["checkpoint"]
        assert workspace.assessments.check(new)["eligible"]
        assert not workspace.assessments.check(first)["eligible"]


def test_changed_policy_and_stale_checkpoint_prevent_out_of_order_apply(host):
    with evidence_wiki.Workspace.open(host.root) as workspace:
        envelope = issue(host, workspace)
        original = workspace.assessments.plan_refresh(host.refresh())
        host.policy["principals"]["owner"]["controller"] = "new-owner-controller"
        host.save_policy()
        assert workspace.assessments.check(envelope)["reasons"] == ["assessment_policy_changed"]
        before = state_bytes(host)
        with pytest.raises(EvidenceWikiError):
            workspace.assessments.apply_refresh(host.sign(original["application"]))
        assert state_bytes(host) == before
        fresh = workspace.assessments.plan_refresh(host.refresh())
        assert fresh["plan"]["entries"][0]["reasons"] == ["assessment_policy_changed"]
        stale = host.sign(fresh["application"])
        host.revoke(USAGE, host.body)
        before = state_bytes(host)
        with pytest.raises(EvidenceWikiError):
            workspace.assessments.apply_refresh(stale)
        assert state_bytes(host) == before


def test_scope_and_capture_authorization_do_not_leak_to_legacy_publication(host):
    with evidence_wiki.Workspace.open(host.root) as workspace:
        envelope = issue(host, workspace)
        unrelated = host.root / "wiki/questions/unrelated.md"
        unrelated.write_text("---\ntype: question\nid: unrelated\nstatus: open\nquestion: Another question\n---\nAnother inquiry.\n")
        assert workspace.assessments.check(envelope)["eligible"]
        assert workspace.assessments.plan_refresh(host.refresh())["plan"]["entries"] == []
        with pytest.raises(EvidenceWikiError):
            workspace.publish_selected(["vendor-product-spec"])


def test_cli_prepare_issue_check_and_bounded_duplicate_json_refusal(host):
    args = [sys.executable, "-m", "evidence_wiki.cli", "assessments"]
    prepared = subprocess.run([*args, "prepare", "--target", str(host.root)], input=json.dumps(host.request()), text=True, capture_output=True, timeout=60)
    assert prepared.returncode == 0, prepared.stderr
    envelope = host.sign(json.loads(prepared.stdout)["registration"])
    issued = subprocess.run([*args, "issue", "--target", str(host.root)], input=json.dumps(envelope), text=True, capture_output=True, timeout=60)
    assert issued.returncode == 0, issued.stderr
    checked = subprocess.run([*args, "check", "--target", str(host.root)], input=json.dumps(envelope), text=True, capture_output=True, timeout=60)
    assert checked.returncode == 0 and json.loads(checked.stdout)["eligible"], checked.stderr
    invalid = subprocess.run([*args, "prepare", "--target", str(host.root)], input='{"schema_version":1,"schema_version":2}', text=True, capture_output=True, timeout=60)
    assert invalid.returncode == 2 and json.loads(invalid.stderr)["error_code"] == "EVIDENCE_ASSESSMENT_REFUSED"


def test_expiry_during_final_capture_cannot_return_eligible_evidence(host, monkeypatch):
    with evidence_wiki.Workspace.open(host.root) as workspace:
        deadline = datetime.now(timezone.utc) + timedelta(hours=1)
        envelope = issue(host, workspace, expires_at=deadline.isoformat())
        engine = workspace._script("evidence_assessments").check.__globals__
        usage = engine["current_view"].__wrapped__.__globals__
        original = engine["capture_workspace"]

        class Clock:
            value = datetime.now(timezone.utc)

            @classmethod
            def now(cls, zone):
                return cls.value

        def closing_capture(root):
            captured = original(root)
            Clock.value = deadline
            return captured

        monkeypatch.setitem(usage, "datetime", Clock)
        monkeypatch.setitem(engine, "capture_workspace", closing_capture)
        checked = workspace.assessments.check(envelope)
        assert not checked["eligible"]
        assert checked["reasons"] == ["assessment_expired_during_evaluation"]


def test_configuration_race_refuses_before_private_materialization(host, monkeypatch):
    with evidence_wiki.Workspace.open(host.root) as workspace:
        engine = workspace._script("evidence_assessments").prepare.__globals__
        original = engine["run_selected_publication"]

        def changed(root, selected, **kwargs):
            config = root / "research.yml"
            config.write_text(config.read_text() + "\nchanged_configuration: true\n")
            return original(root, selected, **kwargs)

        monkeypatch.setitem(engine, "run_selected_publication", changed)
        before = state_bytes(host)
        with pytest.raises(EvidenceWikiError):
            workspace.assessments.prepare(host.request())
        assert state_bytes(host) == before


@pytest.mark.parametrize("after_write", [False, True])
def test_interrupted_apply_retries_without_duplicate_events(host, monkeypatch, after_write):
    with evidence_wiki.Workspace.open(host.root) as workspace:
        envelope = issue(host, workspace)
        host.revoke(USAGE, host.body)
        prepared = workspace.assessments.plan_refresh(host.refresh())
        command = host.sign(prepared["application"])
        before = state_bytes(host)
        usage = workspace._script("evidence_assessments").transact.__globals__
        original = usage["write_state"]

        def interrupted(directory, data):
            if after_write:
                original(directory, data)
            raise OSError("submission interrupted")

        monkeypatch.setitem(usage, "write_state", interrupted)
        with pytest.raises(EvidenceWikiError):
            workspace.assessments.apply_refresh(command)
        assert (state_bytes(host) == before) is (not after_write)
        monkeypatch.setitem(usage, "write_state", original)
        receipt = workspace.assessments.apply_refresh(command)
        assert workspace.assessments.apply_refresh(command) == receipt
        assert len(json.loads(state_bytes(host))["events"]) == len(json.loads(before)["events"]) + 1
        assert workspace.assessments.check(envelope)["reasons"] == ["assessment_invalidated"]


def test_read_lock_prevents_concurrent_invalidation_and_derived_use_is_denied(host, monkeypatch):
    with evidence_wiki.Workspace.open(host.root) as workspace:
        envelope = issue(host, workspace)
        assessment_id = envelope["payload"]["body"]["assessment"]["assessment_id"]
        node_id = identifier("host-derived-record/v1", {"assessment_id": assessment_id})
        host.transact(USAGE, "register", {"node_id": node_id, "kind": "derived", "parents": [assessment_id]})
        prepared = workspace.assessments.plan_refresh(host.refresh(evaluation_time="2026-12-02T00:00:00Z"))
        command = host.sign(prepared["application"])
        engine = workspace._script("evidence_assessments").check.__globals__
        original = engine["capture_workspace"]
        attempts = []

        def concurrent(root):
            with pytest.raises(EvidenceWikiError):
                workspace.assessments.apply_refresh(command)
            attempts.append(True)
            return original(root)

        monkeypatch.setitem(engine, "capture_workspace", concurrent)
        assert workspace.assessments.check(envelope)["eligible"]
        assert attempts
        monkeypatch.setitem(engine, "capture_workspace", original)
        workspace.assessments.apply_refresh(command)
        with USAGE.current_view(host.root, host.config) as view:
            result = view.check(node_id, uses=["export"], purpose="research", consumer="evidence-wiki")
            assert not result["eligible"] and result["reasons"] == ["assessment_invalidated"]


@pytest.mark.parametrize("changes", [{"limit": 0}, {"limit": 33}, {"limit": True},
    {"cursor": {"basis": "sha256:" + "0" * 64, "after": "sha256:" + "1" * 64}}])
def test_refresh_bounds_and_unknown_cursor_refuse_without_writes(host, changes):
    before = state_bytes(host)
    with evidence_wiki.Workspace.open(host.root) as workspace, pytest.raises(EvidenceWikiError):
        workspace.assessments.plan_refresh(host.refresh(**changes))
    assert state_bytes(host) == before
