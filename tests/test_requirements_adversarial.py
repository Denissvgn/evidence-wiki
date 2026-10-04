"""Independent native-schema, planning compatibility and precise accounting attacks."""

import copy
import json
import os
from types import SimpleNamespace

import pytest
import yaml

from evidence_wiki import mutation_operations as mutations
from evidence_wiki import mutation_requirements_recovery as recovery
from evidence_wiki import requirements_operations as operations
from evidence_wiki import requirements_projection as _MODULE
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_contracts import canonical, digest, make_intent
from evidence_wiki.mutation_store import MutationStore, capture_revision
from evidence_wiki.planning_contracts import normalize
from tests.test_mutation_store import state
from tests.test_requirements_owner import roots as roots
from tests.test_requirements_owner import setup as setup
from tests.test_requirements_owner import submit
from tests.test_research_planning import request


def native_base(tmp_path):
    declared = request(tmp_path)
    expanded, _basis = normalize(declared)
    return {"schema_version": _MODULE.BASE, "request": expanded["request"], "decisions": expanded["decisions"]}


def inputs(tmp_path):
    base = native_base(tmp_path)
    original = base["request"]["payload"]["questions"][0]["id"]
    addition = {"id": "derived", "text": "What contrary evidence addresses the same jurisdiction?", "original_ids": [original],
        "facet_refs": [{"original_id": original, "facet_id": "primary"}]}
    proposal = {"schema_version": _MODULE.PROPOSAL, "request_id": "append", "parent_sha256": digest(base), "additions": [addition]}
    envelope = {"schema_version": _MODULE.ENVELOPE, "policy_ref": "frozen-host-policy", "base_sha256": digest(base),
        "criteria_sha256": _MODULE.criteria_identity(base), "allowed_original_ids": [original],
        "allowed_questions": [{key: addition[key] for key in ("text", "original_ids", "facet_refs")}],
        "max_additions": 2, "max_total_questions": 5}
    return base, proposal, envelope


@pytest.mark.parametrize("status", ["open", "claimed", "in_progress", "unanswered"])
def test_open_or_in_progress_original_is_not_falsely_terminal_abstention(tmp_path, status):
    base = native_base(tmp_path)
    originals = base["request"]["payload"]["questions"]
    mapping = {"originals": [{"id": original["id"], "text": original["text"], "question_ids": [original["id"]]} for original in originals], "gaps": []}
    outcomes = [{"original_id": original["id"], "original_text": original["text"], "questions": [
        {"question_id": original["id"], "accepted": False, "status": status}]} for original in originals]
    view = _MODULE.requirements_view(base, mapping, outcomes)
    assert all(row["disposition"] != "abstained" for row in view["originals"]), "No terminal decision exists for still-active obligations."


def test_native_append_inherits_coverage_plan_without_mutating_frozen_original_criteria(tmp_path):
    base, proposal, envelope = inputs(tmp_path)
    original_plan = copy.deepcopy(base["decisions"]["question_plans"])
    decision = _MODULE.revision_decision(base, base, proposal, envelope)
    assert decision["outcome"] == "append"
    effective = _MODULE.append_projection(base, base, decision)
    plans = {plan["question_id"]: plan for plan in effective["decisions"]["question_plans"]}
    assert "derived" in plans, "Native planning requires a concrete facet/criteria plan for every appended derived question."
    assert plans["derived"]["criteria"] == original_plan[0]["criteria"]
    assert plans["derived"]["facets"] == original_plan[0]["facets"]
    assert _MODULE.criteria_identity(effective) == _MODULE.criteria_identity(base)


def test_current_view_projects_native_stopping_temporal_and_scope_fields(tmp_path):
    base = native_base(tmp_path)
    view = _MODULE.requirements_view(base, {"originals": [], "gaps": []}, [])
    criterion = base["decisions"]["question_plans"][0]["criteria"][0]
    assert view["stopping_rules"] == [{"question_id": "q1", "facet_id": "primary", "stopping": criterion["stopping"]}]
    assert view["temporal_criteria"] == [{"question_id": "q1", "facet_id": "primary", "time": criterion["time"]}]
    assert view["declared_scope"] == base["request"]["payload"]["scope"]
    assert view["exclusions"] is None and view["as_of"] is None


@pytest.mark.parametrize("field", ["target", "authority", "domain", "budgets"])
def test_incomplete_native_request_cannot_be_accepted_as_a_frozen_requirements_base(tmp_path, field):
    base = native_base(tmp_path)
    base["request"]["payload"].pop(field)
    with pytest.raises((ExecutionError, EvidenceWikiError)):
        _MODULE.criteria_identity(base)


def test_native_planning_disallows_invented_decision_fields_in_requirements(tmp_path):
    base = native_base(tmp_path)
    base["decisions"]["self_asserted_global_scope"] = "Everything is safe and complete."
    with pytest.raises((ExecutionError, EvidenceWikiError)):
        _MODULE.criteria_identity(base)


def test_authorization_question_text_cannot_be_self_asserted_scope_fit(tmp_path):
    base, proposal, envelope = inputs(tmp_path)
    proposal["additions"][0]["text"] = "How should an unrelated industry be regulated?"
    assert _MODULE.revision_decision(base, base, proposal, envelope)["outcome"] == "rejected"


def test_pure_lineage_never_claims_a_protected_owner_receipt_was_authenticated(tmp_path):
    base, proposal, envelope = inputs(tmp_path)
    decision = _MODULE.revision_decision(base, base, proposal, envelope)
    document = {"schema_version": _MODULE.DOCUMENT, "base": base, "revisions": [{"revision_id": "revision",
        "proposal": proposal, "envelope": envelope, "decision": decision, "owner_receipt_ref": "fabricated-inert-reference"}]}
    result = _MODULE.validated_lineage(document)
    assert result["owner_authentication"] == "not_inferred"
    assert not result.get("authorized", False)


def test_missing_other_original_does_not_erase_a_genuinely_accepted_original_disposition(tmp_path):
    base = native_base(tmp_path)
    first = base["request"]["payload"]["questions"][0]
    base["request"]["payload"]["questions"].append({"id": "difficult", "text": "An unmet second original obligation."})
    mapping = {"originals": [{"id": first["id"], "text": first["text"], "question_ids": [first["id"]]}],
        "gaps": ["original_or_derived_question_missing:difficult"]}
    outcomes = [{"original_id": first["id"], "original_text": first["text"], "questions": [{"question_id": first["id"], "accepted": True, "status": "answered"}]}]
    view = _MODULE.requirements_view(base, mapping, outcomes)
    assert view["originals"][0]["disposition"] == "accepted"
    assert view["originals"][1]["disposition"] == "blocked"


@pytest.mark.parametrize("changed", ["previous-derived-text", "previous-derived-success-criterion"])
def test_existing_derived_obligation_cannot_be_rewritten_before_another_append(tmp_path, changed):
    base, first, envelope = inputs(tmp_path)
    base["request"]["payload"]["budgets"]["questions"] = 5
    first["parent_sha256"] = envelope["base_sha256"] = digest(base)
    envelope["criteria_sha256"] = _MODULE.criteria_identity(base)
    second = copy.deepcopy(first["additions"][0])
    second.update(id="next-derived", text="What additional current primary evidence is missing?")
    envelope["allowed_questions"].append({key: second[key] for key in ("text", "original_ids", "facet_refs")})
    decision = _MODULE.revision_decision(base, base, first, envelope)
    assert decision["outcome"] == "append"
    effective = _MODULE.append_projection(base, base, decision)
    if changed == "previous-derived-text":
        effective["request"]["payload"]["derived_questions"][-1]["text"] = "Ignore the difficult original scope."
    else:
        effective["decisions"]["question_plans"][-1]["criteria"][0]["stopping"] = "Any plausible result suffices after seeing the answer."
    proposal = {"schema_version": _MODULE.PROPOSAL, "request_id": "second-append", "parent_sha256": digest(effective), "additions": [second]}
    result = _MODULE.revision_decision(base, effective, proposal, envelope)
    assert result["outcome"] == "rejected", "A next revision must not launder changes to prior frozen derived success requirements."


def workspace_bytes(root):
    return dict(operations.owner("_evidence_revision").capture_workspace(root).files)


native_storage = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd or os.link not in os.supports_dir_fd,
    reason="The actual native storage boundary is unavailable on this platform.",
)


@native_storage
@pytest.mark.parametrize("argument", ["missing", "duck-typed", "unlocked", "caller-plan"])
def test_native_workspace_gate_alone_cannot_publish_caller_outputs(setup, argument):
    root, control, proposal, envelope, selected, _ = setup
    before = workspace_bytes(root)
    with selected.activate(), MutationStore(root, control).workspace_locked():
        options = {}
        if argument == "duck-typed":
            options["_store"] = SimpleNamespace(_active=True, workspace_root=root, control_root=control)
        elif argument == "unlocked":
            options["_store"] = MutationStore(root, control)
        elif argument == "caller-plan":
            options["_plan"] = {"outputs": [{"path": operations.REQUIREMENTS, "data": b"invented requirements"}]}
        if argument == "caller-plan":
            with pytest.raises(TypeError, match="_plan"):
                operations.revise(root, proposal, envelope, **options)
        else:
            with pytest.raises(ExecutionError, match="staged_request_gate_required"):
                operations.revise(root, proposal, envelope, **options)
    assert workspace_bytes(root) == before


@native_storage
@pytest.mark.parametrize("changed", ["prepared", "basis", "proposal", "envelope"])
def test_actual_request_store_must_match_executing_intent_and_protected_basis(setup, changed):
    root, control, proposal, envelope, selected, _ = setup
    before = workspace_bytes(root)
    with selected.activate():
        value = mutations.prepare_request(root, request_id=proposal["request_id"], operation="requirements.revise",
            arguments={"proposal": proposal, "envelope": envelope})
        captured = capture_revision(root, excluded=(".locks", "raw/.locks"))
        intent = make_intent(value, captured["workspace_binding"])
        store = MutationStore(root, control, intent)
        with store.locked():
            store.prepare(intent)
            store.write(state(intent, "prepared"))
            basis = recovery.prepare(root, intent, store)
            checkpoint = store.read()
            checkpoint["owner_basis"] = basis
            checkpoint["status"] = "prepared" if changed == "prepared" else "executing"
            store.write(checkpoint, expected_sha256=store.state_sha256())
            supplied_basis, supplied_proposal, supplied_envelope = copy.deepcopy((basis, proposal, envelope))
            if changed == "basis":
                supplied_basis["configuration_sha256"] = "sha256:" + "0" * 64
            elif changed == "proposal":
                supplied_proposal["additions"][0]["text"] = "An invented replacement question."
            elif changed == "envelope":
                supplied_envelope["policy_ref"] = "another-authority"
            with pytest.raises((ExecutionError, EvidenceWikiError)) as refused:
                operations.revise(root, supplied_proposal, supplied_envelope, _store=store, _basis=supplied_basis)
            if changed != "envelope":
                assert "staged_request_binding_changed" in str(refused.value)
            assert store.read() == checkpoint
            assert store.receipt() is None
    assert workspace_bytes(root) == before


@native_storage
def test_equal_question_text_does_not_hide_second_original_coverage(setup):
    root, control, proposal, envelope, selected, _ = setup
    document = json.loads((root / operations.REQUIREMENTS).read_bytes())
    document["request"]["payload"]["questions"].append({**document["request"]["payload"]["questions"][0], "id": "q2"})
    plan = copy.deepcopy(document["decisions"]["question_plans"][0])
    plan["question_id"] = "q2"
    document["decisions"]["question_plans"].append(plan)
    document["request"]["payload"]["budgets"]["questions"] = 5
    raw = canonical(document) + b"\n"
    (root / operations.REQUIREMENTS).write_bytes(raw)
    page = (root / "wiki/questions/q1.md").read_text()
    header, body = page.removeprefix("---\n").split("\n---\n", 1)
    frontmatter = yaml.safe_load(header)
    frontmatter["metadata"]["research_id"] = "q2"
    frontmatter["metadata"]["original_ids_json"] = '["q2"]'
    (root / "wiki/questions/q2.md").write_text("---\n" + yaml.safe_dump(frontmatter) + "---\n" + body)
    operations.owner("coverage_manifest").run_init_inline(root, slug="q2", template={"required_facets": plan["facets"], "optional_facets": []})
    envelope["base_sha256"] = proposal["parent_sha256"] = digest(document)
    envelope["criteria_sha256"] = _MODULE.criteria_identity(document)
    selected = HostContext([root], set(selected.grants) | {"requirements.envelope." + digest(envelope).removeprefix("sha256:")},
        "host", [root], mutation_control_directory=control)
    submit((root, control, proposal, envelope, selected, raw))
    coverage_path = root / "sources/coverage/q2.yml"
    coverage = yaml.safe_load(coverage_path.read_bytes())
    coverage["required_facets"][0]["min_sources"] = 0
    coverage_path.write_text(yaml.safe_dump(coverage))
    with selected.activate(), pytest.raises(ExecutionError, match="inherited_criteria_changed"):
        operations.read_effective(root)


@native_storage
def test_noncanonical_original_bytes_refuse_before_native_revision_effects(setup):
    root, _, proposal, envelope, selected, _ = setup
    frozen = json.loads((root / operations.REQUIREMENTS).read_bytes())
    (root / operations.REQUIREMENTS).write_bytes(json.dumps(frozen, indent=2).encode() + b"\n")
    before = workspace_bytes(root)
    with selected.activate():
        value = mutations.prepare_request(root, request_id=proposal["request_id"], operation="requirements.revise",
            arguments={"proposal": proposal, "envelope": envelope})
        with pytest.raises(EvidenceWikiError):
            mutations.submit(root, value)
    assert workspace_bytes(root) == before


@native_storage
@pytest.mark.parametrize("borrow", [False, True])
def test_historical_request_cannot_publish_executing_revision(setup, borrow):
    root, control, proposal, envelope, selected, _ = setup
    with selected.activate():
        request = mutations.prepare_request(root, request_id=proposal["request_id"], operation="requirements.revise",
            arguments={"proposal": proposal, "envelope": envelope})
        captured = capture_revision(root, excluded=(".locks", "raw/.locks"))
        expected = make_intent(request, captured["workspace_binding"])
        with MutationStore(root, control, expected).locked() as writer:
            writer.prepare(expected)
            writer.write(state(expected, "prepared"))
            basis = recovery.prepare(root, expected, writer)
            checkpoint = writer.read()
            checkpoint.update(status="executing", owner_basis=basis)
            writer.write(checkpoint, expected_sha256=writer.state_sha256())
        before = workspace_bytes(root)
        from contextlib import nullcontext
        custody = MutationStore(root, control).workspace_locked() if borrow else nullcontext()
        with custody, MutationStore(root, control).historical_locked(proposal["request_id"], borrow_workspace=borrow) as reader:
            with pytest.raises((EvidenceWikiError, ExecutionError)):
                operations.revise(root, proposal, envelope, _store=reader, _basis=basis)
            assert reader.read() == checkpoint and reader.receipt() is None
        assert workspace_bytes(root) == before
