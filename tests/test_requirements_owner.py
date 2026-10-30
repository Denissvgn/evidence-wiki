"""Actual owner rendering, private staged receipts and historical lineage proof."""

import copy
import json

import pytest
import yaml

from evidence_wiki import mutation_operations as mutations
from evidence_wiki import mutation_requirements_recovery as recovery
from evidence_wiki import requirements_operations as operations
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.mutation_contracts import canonical, make_intent
from evidence_wiki.mutation_store import MutationStore, capture_revision
from evidence_wiki.pack_discovery import owner
from evidence_wiki.requirements_projection import BASE, ENVELOPE, PROPOSAL, criteria_identity, digest
from tests.test_mutation_operations import GRANTS, host
from tests.test_mutation_operations import roots as roots
from tests.test_research_planning import request


@pytest.fixture
def setup(roots):
    root, control = roots
    (root / "wiki/questions/sample.md").unlink()
    value = request(root.parent)
    from evidence_wiki.planning_contracts import normalize
    value, _ = normalize(value)
    frozen = {**value, "schema_version": BASE}
    raw = canonical(frozen) + b"\n"
    (root / "docs").mkdir()
    (root / operations.REQUIREMENTS).write_bytes(raw)
    intake = owner("intake_questions")
    intake.run_intake_document(root, {"schema_version": "1.0", "questions": [{"id": "q1", "question": frozen["request"]["payload"]["questions"][0]["text"],
        "origin": "parent_agent", "metadata": {"research_id": "q1", "original_text": frozen["request"]["payload"]["questions"][0]["text"], "original_ids_json": '["q1"]'}}]}, dry_run=False, from_file_label="setup")
    coverage = owner("coverage_manifest")
    coverage.run_init_inline(root, slug="q1", template={"required_facets": frozen["decisions"]["question_plans"][0]["facets"], "optional_facets": []})
    addition = {"id": "derived1", "text": "What contrary primary evidence exists?", "original_ids": ["q1"], "facet_refs": [{"original_id": "q1", "facet_id": "primary"}]}
    envelope = {"schema_version": ENVELOPE, "policy_ref": "frozen-host-policy", "base_sha256": digest(frozen),
        "criteria_sha256": criteria_identity(frozen), "allowed_original_ids": ["q1"],
        "allowed_questions": [{key: addition[key] for key in ("text", "original_ids", "facet_refs")},
            {"text": "What additional declared primary evidence exists?", "original_ids": ["q1"], "facet_refs": addition["facet_refs"]}], "max_additions": 2, "max_total_questions": 10}
    proposal = {"schema_version": PROPOSAL, "request_id": "append1", "parent_sha256": digest(frozen), "additions": [addition]}
    grants = GRANTS | {"requirements_operations.revise", "requirements_operations.prepare_revision", "requirements_operations.current", "requirements.envelope." + digest(envelope).removeprefix("sha256:")}
    return root, control, proposal, envelope, host(roots, grants=grants), raw


def submit(setup):
    root, _, proposal, envelope, selected, _ = setup
    with selected.activate():
        value = mutations.prepare_request(root, request_id="append1", operation="requirements.revise", arguments={"proposal": proposal, "envelope": envelope})
        receipt = mutations.submit(root, value)
    assert receipt["status"] == "committed", receipt
    return value, receipt


def test_actual_native_transaction_and_historical_reader(setup):
    root, _, _, _, selected, raw = setup
    _, receipt = submit(setup)
    with selected.activate():
        current = operations.verifylineage(root, json.loads((root / operations.REQUIREMENTS).read_bytes()))
        assert current["base"] == json.loads(raw)
        assert len(current["effective"]["decisions"]["question_plans"]) == 2
        assert operations.instruction_matches(root, (root / operations.REQUIREMENTS).read_bytes(), operations._hash(raw))
        assert current["owner_authentication"] == "protected_mutation_receipts"
    assert receipt["result"]["eligibility_invalidated"] and not receipt["authorized"]
    assert (root / "wiki/questions/derived1.md").exists()


def test_status_links_log_and_index_can_change_after_receipt(setup):
    root, _, _, _, selected, _ = setup
    submit(setup)
    page = root / "wiki/questions/derived1.md"
    page.write_text(page.read_text().replace("status: open", "status: answered"))
    coverage_path = root / "sources/coverage/derived1.yml"
    document = yaml.safe_load(coverage_path.read_bytes())
    document["required_facets"][0].update(accepted_source_ids=["source-a"], facet_verdict="accepted")
    coverage_path.write_text(yaml.safe_dump(document))
    (root / "log.md").write_text((root / "log.md").read_text() + "\nLater owner event.\n")
    (root / "index.md").write_text((root / "index.md").read_text() + "\nLater summary.\n")
    with selected.activate():
        assert len(operations.read_effective(root)["request"]["payload"]["derived_questions"]) == 1


@pytest.mark.parametrize("target", ["original", "derived"])
def test_other_coverage_writer_cannot_weaken_frozen_facet_definition(setup, target):
    root, _, _, _, selected, _ = setup
    submit(setup)
    path = root / "sources/coverage" / ("q1.yml" if target == "original" else "derived1.yml")
    document = yaml.safe_load(path.read_bytes())
    document["required_facets"][0]["min_sources"] = 0
    path.write_text(yaml.safe_dump(document))
    with selected.activate(), pytest.raises(ExecutionError, match="criteria_changed"):
        operations.read_effective(root)


def test_unreceipted_complete_bytes_do_not_authorize_lineage(setup):
    root, control, proposal, envelope, selected, _ = setup
    with selected.activate():
        value = mutations.prepare_request(root, request_id="append1", operation="requirements.revise", arguments={"proposal": proposal, "envelope": envelope})
        captured = capture_revision(root, excluded=(".locks", "raw/.locks"))
        intent = make_intent(value, captured["workspace_binding"])
        store = MutationStore(root, control, intent)
        with store.locked():
            store.prepare(intent)
            from tests.test_mutation_store import state
            store.write(state(intent, "prepared"))
            basis = recovery.prepare(root, intent, store)
            checkpoint = store.read()
            checkpoint["owner_basis"] = basis
            checkpoint["status"] = "executing"
            store.write(checkpoint, expected_sha256=store.state_sha256())
            recovery.invoke(root, intent, basis, store)
            assert recovery.prove(root, intent, basis, store)["desired"]
        with pytest.raises(ExecutionError, match="receipt_missing"):
            operations.read_effective(root)


def test_changed_envelope_argument_cannot_create_host_authority(setup):
    root, _, proposal, envelope, selected, raw = setup
    changed = copy.deepcopy(envelope)
    changed["max_additions"] = 20
    with selected.activate():
        value = mutations.prepare_request(root, request_id="append1", operation="requirements.revise", arguments={"proposal": proposal, "envelope": changed})
        from evidence_wiki.errors import EvidenceWikiError
        with pytest.raises(EvidenceWikiError):
            mutations.submit(root, value)
    assert (root / operations.REQUIREMENTS).read_bytes() == raw


@pytest.mark.parametrize("failure_at", [1, 2, 5])
def test_interrupted_prefix_never_blindly_redispatches_or_accepts(setup, monkeypatch, failure_at):
    root, _, proposal, envelope, selected, raw = setup
    writer = owner("_usage_materialization")
    actual = writer.publish_file
    calls = 0
    def interrupted(*args, **kwargs):
        nonlocal calls
        result = actual(*args, **kwargs)
        calls += 1
        if calls == failure_at:
            raise OSError("Controlled native publication interruption")
        return result
    with selected.activate():
        value = mutations.prepare_request(root, request_id="append1", operation="requirements.revise", arguments={"proposal": proposal, "envelope": envelope})
        monkeypatch.setattr(writer, "publish_file", interrupted)
        receipt = mutations.submit(root, value)
        assert receipt["status"] in {"partial", "commit_unknown"}
        monkeypatch.setattr(writer, "publish_file", lambda *a, **k: pytest.fail("blind redispatch"))
        assert mutations.submit(root, value) == receipt
        if failure_at < 5:
            assert (root / operations.REQUIREMENTS).read_bytes() == raw
        else:
            with pytest.raises(ExecutionError, match="receipt_missing"):
                operations.read_effective(root)
        reconciled = mutations.reconcile(root, "append1")
        assert reconciled["status"] == ("reconciled" if failure_at == 5 else "partial")
        if failure_at == 5:
            assert operations.read_effective(root)["request"]["payload"]["derived_questions"]


def test_second_revision_borrows_gate_preserves_historical_prefix_and_exhausts_allowance(setup):
    root, _, proposal, envelope, selected, _ = setup
    submit(setup)
    with selected.activate():
        effective = operations.read_effective(root)
        second = {**proposal, "request_id": "append2", "parent_sha256": digest(effective), "additions": [
            {**proposal["additions"][0], "id": "derived2", "text": "What additional declared primary evidence exists?"}]}
        value = mutations.prepare_request(root, request_id="append2", operation="requirements.revise", arguments={"proposal": second, "envelope": envelope})
        receipt = mutations.submit(root, value)
        assert receipt["status"] == "committed", receipt
        lineage = operations.verifylineage(root, json.loads((root / operations.REQUIREMENTS).read_bytes()))
        assert len(lineage["revisions"]) == 2 and len(lineage["effective"]["decisions"]["question_plans"]) == 3
        third = {**second, "request_id": "append3", "parent_sha256": digest(lineage["effective"])}
        with pytest.raises(ExecutionError, match="allowance_exhausted"):
            operations.prepare_revision(root, third, envelope)


def test_foreign_output_conflict_during_commit_keeps_requirements_unpublished(setup, monkeypatch):
    root, _, proposal, envelope, selected, raw = setup
    writer = owner("_usage_materialization")
    actual = writer.publish_file
    def conflict(*args, **kwargs):
        if args[1] == "index.md":
            (root / "index.md").write_text("Foreign concurrent index change")
        return actual(*args, **kwargs)
    with selected.activate():
        value = mutations.prepare_request(root, request_id="append1", operation="requirements.revise", arguments={"proposal": proposal, "envelope": envelope})
        monkeypatch.setattr(writer, "publish_file", conflict)
        receipt = mutations.submit(root, value)
        assert receipt["status"] in {"partial", "commit_unknown"}
        assert (root / operations.REQUIREMENTS).read_bytes() == raw
        assert (root / "index.md").read_text() == "Foreign concurrent index change"
        assert mutations.reconcile(root, "append1")["status"] == "partial"


def test_claimed_question_is_not_idle_even_without_a_run(setup):
    root, _, proposal, envelope, selected, raw = setup
    page = root / "wiki/questions/q1.md"
    page.write_text(page.read_text().replace("status: open", "status: in_progress"))
    with selected.activate():
        value = mutations.prepare_request(root, request_id="append1", operation="requirements.revise", arguments={"proposal": proposal, "envelope": envelope})
        from evidence_wiki.errors import EvidenceWikiError
        with pytest.raises(EvidenceWikiError):
            mutations.submit(root, value)
    assert (root / operations.REQUIREMENTS).read_bytes() == raw


@pytest.mark.parametrize("slug", ["q1", "derived1"])
def test_actual_closed_planner_metadata_cannot_be_augmented_to_override_criteria(setup, slug):
    root, _, _, _, selected, _ = setup
    submit(setup)
    page = root / "wiki/questions" / (slug + ".md")
    text = page.read_text()
    page.write_text(text.replace("metadata:\n", "metadata:\n  evidence_criteria_json: '[]'\n"))
    assert "evidence_criteria_json" in page.read_text()
    with selected.activate(), pytest.raises(ExecutionError, match="question_contract_changed"):
        operations.read_effective(root)


def test_public_frozen_revision_request_and_current_native_view(setup):
    root, _, proposal, envelope, selected, _ = setup
    with selected.activate():
        prepared = operations.prepare_revision(root, proposal, envelope)
        assert prepared["operation"] == "requirements.revise" and prepared["arguments"] == {"proposal": proposal, "envelope": envelope}
        view = operations.current(root)
        assert view["originals"][0]["original_id"] == "q1" and view["originals"][0]["disposition"] == "blocked"
        assert view["question_plans"][0]["criteria"][0]["stopping"] == "Every required facet has reviewed support or an explicit gap"
        assert not view["evidence_accepted"] and not view["authorized"]
