"""Public setup, protected native readers and exact revision ownership boundaries."""

import copy
import hashlib
import json

import pytest
import yaml

from evidence_wiki import Onboarding, RunPolicy, Workspace
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.mutation_store import MutationStore
from evidence_wiki.pack_discovery import owner
from evidence_wiki.requirements_projection import ENVELOPE, PROPOSAL, criteria_identity
from tests._execution_fixture import identifier
from tests._usage_fixture import UsageFixture
from tests.test_mutation_operations import GRANTS as MUTATION_GRANTS
from tests.test_requirements_owner import roots as roots
from tests.test_requirements_owner import setup as setup
from tests.test_research_planning import request
from tests.test_research_session import GRANTS as SESSION_GRANTS
from tests.test_run_policy import policy_document


def sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def revision_inputs(base):
    addition = {"id": "derived1", "text": "What contrary primary evidence addresses the same jurisdiction?", "original_ids": ["q1"],
                "facet_refs": [{"original_id": "q1", "facet_id": "primary"}]}
    envelope = {"schema_version": ENVELOPE, "policy_ref": "host-frozen-scope", "base_sha256": digest(base),
        "criteria_sha256": criteria_identity(base), "allowed_original_ids": ["q1"],
        "allowed_questions": [{key: addition[key] for key in ("text", "original_ids", "facet_refs")}],
        "max_additions": 1, "max_total_questions": 10}
    proposal = {"schema_version": PROPOSAL, "request_id": "append", "parent_sha256": digest(base), "additions": [addition]}
    return proposal, envelope


def grants(envelope):
    return MUTATION_GRANTS | SESSION_GRANTS | {"requirements_operations.current", "requirements_operations.prepare_revision",
        "requirements_operations.revise", "onboarding.setup_status", "requirements.envelope." + digest(envelope)[7:]}


def test_private_plan_argument_cannot_publish_even_inside_a_real_workspace_gate(setup):
    from evidence_wiki import requirements_operations
    root, control, proposal, envelope, context, raw = setup
    before = owner("_evidence_revision").capture_workspace(root)
    forged = {"proposal_sha256": digest(proposal), "envelope_sha256": digest(envelope),
              "outputs": [{"path": "docs/research-requirements.json", "data": b"{}\n", "prior_sha256": sha(raw)}]}
    with context.activate(), MutationStore(root, control).workspace_locked():
        with pytest.raises((TypeError, EvidenceWikiError, ValueError)):
            requirements_operations.revise(root, proposal, envelope, _plan=forged)
    after = owner("_evidence_revision").capture_workspace(root)
    assert dict(after.files) == dict(before.files) and after.directories == before.directories


def test_real_onboarding_protected_revision_and_native_instruction_consumers(tmp_path, monkeypatch):
    monkeypatch.delenv("EVIDENCE_WIKI_AUTHORITY_FILE", raising=False)
    monkeypatch.delenv("EVIDENCE_WIKI_STATE_DIR", raising=False)
    declared = request(tmp_path)
    root = tmp_path / "workspace"
    declared["request"]["payload"]["authority"]["source_scope"] = [str(root / "raw")]
    with Onboarding.open(allowed_roots=[tmp_path], allow=["apply"]) as onboarding:
        prepared_setup = onboarding.plan(declared)
        setup_result = onboarding.apply(prepared_setup)
    assert setup_result["setup_ready"] and not setup_result["claims_verified"]
    raw = (root / "docs/research-requirements.json").read_bytes()
    base = json.loads(raw)
    proposal, envelope = revision_inputs(base)

    # Bind the host's protected policy before research/revision. This is fixture
    # provisioning, with no policy changes after the original instruction hash.
    authority_dir = tmp_path / "authority"
    authority_dir.mkdir()
    authority = UsageFixture(authority_dir, monkeypatch)
    authority.root = root
    config = yaml.safe_load((root / "research.yml").read_bytes())
    strict_policy = copy.deepcopy(config["strict_evidence"])
    strict_policy["instructions"]["docs/research-requirements.json"] = sha(raw)
    config.update(authority.config)
    config["strict_evidence"] = strict_policy
    (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    authority.binding = identifier("evidence-host-workspace/v1", {"root": str(root)})
    authority.policy["strict_workspaces"] = {authority.binding: strict_policy}
    authority.save_policy()
    authority.config = config
    authority.transact(owner("_evidence_usage"), "initialize")
    policy_bytes = authority.policy_path.read_bytes()
    config_bytes = (root / "research.yml").read_bytes()
    control = tmp_path / "mutation-control"
    control.mkdir(mode=0o700)
    context = HostContext([root], grants(envelope), "caller", [root], mutation_control_directory=control,
        authority_file=authority.policy_path, state_directory=authority.host)
    run_policy = policy_document()
    run_policy["scope"]["question_ids"] = ["q1"]
    run_policy["scope"]["source_scope"] = ["source-concrete"]
    run_policy = RunPolicy(run_policy)
    with context.activate(), Workspace.open(root) as workspace:
        session = workspace.research.session(run_policy, run_id="before-append")
        old_basis = session.current()["basis_id"]
        frozen_request = workspace.research.requirements.prepare_revision(proposal, envelope)
        result = workspace.research.requirements.revise(frozen_request)
        assert result["status"] == "committed" and result["result"]["original_binding"] == "protected_instruction"
        view = workspace.research.requirements.current()
        assert view["originals"][0]["question_ids"] == ["q1", "derived1"]
        assert view["originals"][0]["disposition"] == "blocked"
        assert not view["evidence_accepted"] and not view["authorized"]
        assert view["basis"]["workspace_revision"] != old_basis
        assert (root / "research.yml").read_bytes() == config_bytes and authority.policy_path.read_bytes() == policy_bytes
        assert workspace.research.requirements.revise(frozen_request) == result
        with pytest.raises(EvidenceWikiError):
            session.current()
        session.close()

        caller = owner("_caller_context")
        controls = caller.capture(root)
        assert controls["files"]["docs/research-requirements.json"] == hashlib.sha256((root / "docs/research-requirements.json").read_bytes()).hexdigest()
        mapping = caller.original_questions(root, config)
        assert mapping["originals"][0]["question_ids"] == ["q1", "derived1"] and mapping["gaps"] == []
        bridge = owner("_requirements_bridge")
        assert bridge.instruction_matches(root, bridge.PATH, (root / bridge.PATH).read_bytes(), sha(raw))

        originals = [{"original_id": "q1", "text": base["request"]["payload"]["questions"][0]["text"], "question_slugs": ["q1", "derived1"]}]
        inventory = {"schema_version": "evidence-strict-claims/v1", "claims": [], "questions": [
            {"slug": "q1", "original_id": "q1", "question": base["request"]["payload"]["questions"][0]["text"].strip()},
            {"slug": "derived1", "original_id": "q1", "question": proposal["additions"][0]["text"]}]}
        (root / strict_policy["claims_path"]).write_bytes(owner("_evidence_revision").canonical_bytes(inventory))
        capture, _, before_basis = owner("_strict_evidence").selection_inputs(root, config, strict_policy)
        assert capture.files[bridge.PATH] != raw
        draft = {"schema_version": "evidence-draft-proposal/v1", "originals": originals, "inventory": inventory}
        scopes = owner("_draft_admission").originals(capture, root, config, strict_policy, draft)
        assert scopes == [(root / "raw", True)]

        # Native status/evidence fields can evolve while immutable constraints
        # and the original protected instruction hash remain fixed.
        page = root / "wiki/questions/derived1.md"
        page.write_text(page.read_text().replace("status: open", "status: deferred"))
        coverage_path = root / "sources/coverage/derived1.yml"
        manifest = yaml.safe_load(coverage_path.read_bytes())
        manifest["required_facets"][0].update(accepted_source_ids=["unqualified-source"], facet_verdict="pending")
        coverage_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        after = workspace.research.requirements.current()
        assert after["originals"][0]["disposition"] == "blocked"
        assert after["question_plans"][0]["criteria"] == base["decisions"]["question_plans"][0]["criteria"]
        assert owner("_strict_evidence").selection_inputs(root, config, strict_policy)[2] != before_basis
        assert strict_policy["instructions"][bridge.PATH] == sha(raw)
        assert authority.policy_path.read_bytes() == policy_bytes


def test_template_inheritance_has_a_finite_unsupported_outcome_without_new_files(setup):
    from evidence_wiki.requirements_projection import revision_decision
    root, _, proposal, envelope, _, raw = setup
    base = json.loads(raw)
    base["decisions"]["question_plans"][0]["template"] = "declared-template"
    proposal["parent_sha256"] = digest(base)
    envelope["base_sha256"], envelope["criteria_sha256"] = digest(base), criteria_identity(base)
    before = owner("_evidence_revision").capture_workspace(root)
    with pytest.raises(ValueError, match="inherited_template_snapshot_unsupported"):
        revision_decision(base, base, proposal, envelope)
    assert dict(owner("_evidence_revision").capture_workspace(root).files) == dict(before.files)


def test_onboarding_postconditions_recognize_protected_revision_while_old_snapshot_stays_historical(tmp_path, monkeypatch):
    from evidence_wiki.setup_application import postconditions
    monkeypatch.delenv("EVIDENCE_WIKI_AUTHORITY_FILE", raising=False)
    monkeypatch.delenv("EVIDENCE_WIKI_STATE_DIR", raising=False)
    setup_context = HostContext([tmp_path], {"onboarding.plan", "onboarding.apply"}, "caller", [tmp_path])
    with setup_context.activate(), Onboarding.open(allowed_roots=[tmp_path], allow=["apply"]) as onboarding:
        prepared = onboarding.plan(request(tmp_path))
        setup_result = onboarding.apply(prepared)
    root = tmp_path / "workspace"
    raw = (root / "docs/research-requirements.json").read_bytes()
    proposal, envelope = revision_inputs(json.loads(raw))
    control = tmp_path.parent / (tmp_path.name + "-mutation-control")
    control.mkdir(mode=0o700)
    context = HostContext([root], grants(envelope), "caller", [root], mutation_control_directory=control)
    with context.activate(), Workspace.open(root) as workspace:
        receipt = workspace.research.requirements.revise(workspace.research.requirements.prepare_revision(proposal, envelope))
        assert receipt["status"] == "committed"
        postconditions(root, prepared, ["initialize", "intake", "coverage"])
    readonly_context = HostContext([tmp_path], grants(envelope), "caller", mutation_control_directory=control)
    with readonly_context.activate(), Onboarding.open(allowed_roots=[tmp_path]) as onboarding:
        status = onboarding.setup_status(prepared)
    assert not status["research_complete"] and not status["authority_verified"]
    assert status["status"] == "complete", status["diagnostics"]
    assert status["ownership_valid"] and status["snapshot_matches"] is False
    assert status["recovery"] == "recheck"
    assert setup_result["setup_ready"] and setup_result["claims_verified"] is False


def test_terminal_run_with_unsettled_native_job_cannot_revise_requirements(tmp_path):
    from evidence_wiki import requirements_operations
    from evidence_wiki.agent_execution import ExecutionError
    from tests.test_caller_research import workspace as caller_workspace
    from tests.test_research_session import policy
    root = caller_workspace.__wrapped__(tmp_path)
    raw = (root / "docs/research-requirements.json").read_bytes()
    proposal, envelope = revision_inputs(json.loads(raw))
    control = tmp_path / "mutation-control"
    control.mkdir(mode=0o700)
    context = HostContext([root], grants(envelope), "caller", [root], mutation_control_directory=control,
        controller_refs={"host_worker": "worker", "evaluator": "independent"})
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy(paid=True), run_id="reserved") as session:
            question = session.current()["accounting"]["original_outcomes"][0]["original_text"]
            job = session.host_job(request_id="reserved-job", operation="generate", payload={"question": question})
            assert job["max_attempts"] == 1
            with MutationStore(root, control).workspace_locked():
                owner("run_controller").run_session_finish(root, run_id="reserved", agent_id="caller", final_verdict="failed", reason="No host dispatch performed.")
        before = owner("_evidence_revision").capture_workspace(root)
        with pytest.raises(ExecutionError, match="pending_work_authentication_unavailable"):
            requirements_operations._idle(root, before)
        request_value = workspace.research.requirements.prepare_revision(proposal, envelope)
        with pytest.raises(EvidenceWikiError):
            workspace.research.requirements.revise(request_value)
        assert dict(owner("_evidence_revision").capture_workspace(root).files) == dict(before.files)
        assert (root / "docs/research-requirements.json").read_bytes() == raw
