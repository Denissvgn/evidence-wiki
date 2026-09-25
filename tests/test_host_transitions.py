"""Host changes preserve fixed sessions and reconcile interrupted parent creation."""

import json

import pytest

from evidence_wiki._pack_io import canonical
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_transitions import apply, plan
from evidence_wiki.local_journal import Journal
from evidence_wiki.pack_discovery import owner
from tests.test_pack_migrations import initialize


def request(tmp_path, target):
    control = tmp_path / "host"
    control.mkdir(exist_ok=True)
    return {"schema_version": "evidence-host-transition-request/v1", "transition_id": "next-research",
        "control_root": str(control), "target": str(target), "previous_session": None,
        "next_session": {"agent_id": "host", "orchestration_id": "next-session"}, "setup_plan": None,
        "revision_plan": None, "reevaluations": []}


def test_parent_start_has_correlation_and_replays_without_duplicate(tmp_path):
    target = initialize(tmp_path / "workspace")
    value = plan(canonical(request(tmp_path, target)))
    result = apply(canonical(value))
    assert result["status"] == "complete" and not result["release_accepted"]
    session_path = target / "runs/orchestrations/next-session/session.json"
    before = session_path.read_bytes()
    session = json.loads(before)
    from evidence_wiki.orchestration_schemas import host_session_schema
    from tests.test_orchestration_contract_schemas import assert_matches_schema

    assert session["schema_version"] == "1.1"
    assert_matches_schema(session, host_session_schema())
    assert owner("orchestration_controller").load_session(target, "next-session") == session
    assert session["host_transition_id"] == value["plan_id"][7:]
    assert session["requirement_basis"] == owner("_pack_revision_guard").controls(target)
    assert apply(canonical(value))["status"] == "already_complete"
    assert session_path.read_bytes() == before


def test_active_work_is_not_rebound(tmp_path):
    target = initialize(tmp_path / "workspace")
    pending = target / "runs/orchestrations/active"
    pending.mkdir(parents=True)
    (pending / "session.json").write_text(json.dumps({"status": "active", "pending_action_id": "pending"}))
    with pytest.raises(EvidenceWikiError):
        plan(canonical(request(tmp_path, target)))
    assert not (tmp_path / "host/host-transitions").exists()


def test_interrupted_parent_creation_reconciles_owned_correlation(tmp_path, monkeypatch):
    target = initialize(tmp_path / "workspace")
    value = plan(canonical(request(tmp_path, target)))
    write = Journal.write
    def interrupt(self, state):
        if state["status"] == "complete":
            raise KeyboardInterrupt
        return write(self, state)
    with monkeypatch.context() as selected:
        selected.setattr(Journal, "write", interrupt)
        with pytest.raises(KeyboardInterrupt):
            apply(canonical(value))
    assert (target / "runs/orchestrations/next-session/session.json").is_file()
    assert apply(canonical(value))["status"] == "complete"
    assert len(list((target / "runs/orchestrations").glob("*/session.json"))) == 1


def test_modified_workspace_controller_is_not_executed(tmp_path):
    target = initialize(tmp_path / "workspace")
    (target / "scripts/orchestration_controller.py").write_text("raise RuntimeError('untrusted controller')\n")
    with pytest.raises(EvidenceWikiError) as caught:
        plan(canonical(request(tmp_path, target)))
    assert caught.value.details["field"] == "host_workspace_runtime_not_the_installed_generation"


def test_modified_journal_cannot_skip_owner_revision(tmp_path, monkeypatch):
    from tests.test_pack_migrations import ROOT, resolved
    from tests.test_pack_migrations import request as migration_request

    target = initialize(tmp_path / "workspace")
    selected = request(tmp_path, target)
    selected["revision_plan"] = resolved(migration_request(target, ROOT / "domain-packs/general-science"))
    value = plan(canonical(selected))
    write = Journal.write
    def interrupt(self, state):
        write(self, state)
        raise KeyboardInterrupt
    with monkeypatch.context() as changed:
        changed.setattr(Journal, "write", interrupt)
        with pytest.raises(KeyboardInterrupt):
            apply(canonical(value))
    path = tmp_path / "host/host-transitions/next-research/state.json"
    state = json.loads(path.read_text())
    state["completed"] = ["revision"]
    state["results"] = [{"step": "revision", "result": {"status": "applied"}}]
    path.write_bytes(canonical(state))
    with pytest.raises(EvidenceWikiError):
        apply(canonical(value))
    assert not (target / "runs/orchestrations/next-session/session.json").exists()


def test_revision_then_parent_start_and_replay_use_new_basis(tmp_path):
    from tests.test_pack_migrations import ROOT, resolved
    from tests.test_pack_migrations import request as migration_request

    target = initialize(tmp_path / "workspace")
    selected = request(tmp_path, target)
    selected["revision_plan"] = resolved(migration_request(target, ROOT / "domain-packs/general-science"))
    value = plan(canonical(selected))
    assert apply(canonical(value))["status"] == "complete"
    assert apply(canonical(value))["status"] == "already_complete"
    assert owner("_domain_pack_lifecycle").inspect_workspace(target)["state"] == "current"


@pytest.mark.parametrize("field", ["agent_id", "orchestration_id"])
def test_completed_transition_checks_recorded_session_and_agent_identity(tmp_path, field):
    target = initialize(tmp_path / "workspace")
    prepared = plan(canonical(request(tmp_path, target)))
    apply(canonical(prepared))
    path = target / "runs/orchestrations/next-session/session.json"
    session = json.loads(path.read_text())
    session[field] = "changed"
    path.write_bytes(canonical(session))
    with pytest.raises(EvidenceWikiError) as error:
        apply(canonical(prepared))
    assert error.value.error_code == "ONBOARDING_PLAN_STALE"


def test_host_session_cannot_be_relabelled_as_an_ordinary_session(tmp_path):
    target = initialize(tmp_path / "workspace")
    apply(canonical(plan(canonical(request(tmp_path, target)))))
    path = target / "runs/orchestrations/next-session/session.json"
    session = json.loads(path.read_text())
    session["schema_version"] = "1.0"
    path.write_bytes(canonical(session))
    with pytest.raises(Exception) as error:
        owner("orchestration_controller").load_session(target, "next-session")
    assert error.value.error_code == "ORCHESTRATION_STATE_INVALID"


def test_no_change_revision_can_resume_interrupted_parent_start(tmp_path, monkeypatch):
    from evidence_wiki import orchestration, pack_revisions
    from tests.test_pack_migrations import ROOT

    target = initialize(tmp_path / "workspace", "general-science")
    selected = request(tmp_path, target)
    selected["revision_plan"] = pack_revisions.plan({"schema_version": "evidence-pack-revision-request/v1", "target": str(target),
        "path": str(ROOT / "domain-packs/general-science"), "catalog": None, "revision": None,
        "rationale": "Retain the already selected guidance", "keep_local": [], "accept_pack": []})
    assert selected["revision_plan"]["owner_plan"]["status"] == "no_changes"
    value = plan(canonical(selected))
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    with monkeypatch.context() as changed:
        changed.setattr(orchestration, "protocol_start", interrupt)
        with pytest.raises(KeyboardInterrupt):
            apply(canonical(value))
    assert apply(canonical(value))["status"] == "complete"
