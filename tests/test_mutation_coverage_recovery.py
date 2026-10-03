"""Real coverage owners, exact staged postconditions and interrupted publication."""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
import yaml

from evidence_wiki import Workspace
from evidence_wiki import mutation_coverage_recovery as recovery
from evidence_wiki import mutation_operations as operations
from evidence_wiki._pack_io import canonical
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_contracts import make_intent
from evidence_wiki.mutation_store import MutationStore, capture_revision
from evidence_wiki.pack_discovery import owner
from evidence_wiki.pack_revisions import apply, plan
from tests.test_mutation_operations import GRANTS, host, template
from tests.test_mutation_operations import roots as roots
from tests.test_mutation_store import state
from tests.test_pack_revisions import candidate as candidate
from tests.test_pack_revisions import migration, request
from tests.test_pack_revisions import template as revision_template
from tests.test_pack_revisions import workspace as workspace


@pytest.fixture(autouse=True)
def no_ambient_selection(monkeypatch):
    monkeypatch.delenv("EVIDENCE_WIKI_AUTHORITY_FILE", raising=False)
    monkeypatch.delenv("EVIDENCE_WIKI_STATE_DIR", raising=False)


@pytest.fixture
def evaluated_root(roots):
    root, _control = roots
    with Workspace.open(root) as handle:
        handle.coverage.init("sample", template=template())
    return roots


@pytest.fixture
def migration_root(workspace, candidate, tmp_path):
    coverage = owner("coverage_manifest")
    coverage.run_init_document(workspace, coverage.load_config(workspace), slug="q1", template=revision_template())
    changed = candidate / "claims.md"
    changed.write_text(changed.read_text() + "\nRequire a complete independently checked observation.\n")
    prepared = plan(request(workspace, candidate))
    apply(canonical(prepared))
    wanted = migration(prepared)
    wanted["template"]["required_facets"][0]["min_sources"] = 2
    control = tmp_path / "mutation-control"
    control.mkdir(mode=0o700)
    return workspace.resolve(), control.resolve(), wanted


def frozen_intent(root, operation, arguments, request_id="request"):
    request_value = operations.prepare_request(root, request_id=request_id, operation=operation, arguments=arguments)
    observed = capture_revision(root, excluded=(".locks", "raw/.locks"))
    return make_intent(request_value, observed["workspace_binding"])


def seed_state(store, value):
    store.prepare(value)
    store.write(state(value, "prepared"))


def staged_plan(store, basis):
    return json.loads(store.read_stage(basis["plan"]))


def test_real_evaluation_recovery_proves_exact_desired_state_after_lost_return(evaluated_root, monkeypatch):
    root, control = evaluated_root
    with host(evaluated_root).activate():
        request_value = operations.prepare_request(root, request_id="evaluate", operation="coverage.evaluate", arguments={"slug": "sample"})
        original = operations._invoke

        def lost_return(*args):
            original(*args)
            raise RuntimeError("synthetic loss after canonical publication")

        monkeypatch.setattr(operations, "_invoke", lost_return)
        receipt = operations.submit(root, request_value)
        assert receipt["status"] == "commit_unknown"
        inspected = operations.inspect(root, "evaluate")
        value = inspected["record"]["intent"]
        with MutationStore(root, control, value).locked() as store:
            expected = staged_plan(store, inspected["record"]["state"]["owner_basis"])
            actual = (root / expected["outputs"][0]["path"]).read_bytes()
            assert actual == store.read_stage(expected["outputs"][0]["blob"])
        proof = operations.reconcile(root, "evaluate")
        assert proof["desired_state_proved"] is True and proof["completion_proved"] is False
        assert proof["recovery_supported"] is True
        assert operations.inspect(root, "evaluate")["record"]["receipt"] == receipt
        assert operations.submit(root, request_value) == receipt


def test_prepared_clock_is_reused_and_plan_reads_do_not_re_evaluate(evaluated_root, monkeypatch):
    root, control = evaluated_root
    with host(evaluated_root).activate():
        value = frozen_intent(root, "coverage.evaluate", {"slug": "sample"})
        with MutationStore(root, control, value).locked() as store:
            seed_state(store, value)
            basis = recovery.prepare(root, value, store)
            original = staged_plan(store, basis)
            monkeypatch.setattr(owner("coverage_manifest"), "prepare_evaluation", lambda *a, **k: pytest.fail("re-rendered staged plan"))
            assert recovery.prepare(root, value, store) == basis
            assert staged_plan(store, basis)["observed_at"] == original["observed_at"]
            result = recovery.invoke(root, value, basis, store)
            assert result["manifest"]["updated_at"] == datetime.fromisoformat(original["observed_at"]).strftime("%Y-%m-%dT%H:%M:%SZ")
            proof = recovery.prove(root, value, basis, store)
            assert proof["desired"] and not proof["completion"]


@pytest.mark.parametrize("kind", ["source", "provenance", "candidate", "jurisdiction", "other_coverage", "directory"])
def test_reader_input_or_directory_drift_blocks_desired_proof(evaluated_root, kind):
    root, control = evaluated_root
    with host(evaluated_root).activate():
        value = frozen_intent(root, "coverage.evaluate", {"slug": "sample"})
        with MutationStore(root, control, value).locked() as store:
            seed_state(store, value)
            basis = recovery.prepare(root, value, store)
            recovery.invoke(root, value, basis, store)
            targets = {"source": "raw/web/source.html", "provenance": "raw/web/source.html.provenance.yml",
                "candidate": "sources/discovery/candidates.jsonl", "jurisdiction": "sources/discovery/jurisdictions.yml",
                "other_coverage": "sources/coverage/other.yml", "directory": "sources/coverage/unrelated"}
            changed = root / targets[kind]
            changed.parent.mkdir(parents=True, exist_ok=True)
            changed.mkdir() if kind == "directory" else changed.write_bytes(b"synthetic changed dependency\n")
            proof = recovery.prove(root, value, basis, store)
            assert proof["present"] and not proof["desired"] and not proof["completion"]


def test_inner_evaluation_lock_rechecks_prior_manifest_before_write(evaluated_root):
    root, control = evaluated_root
    with host(evaluated_root).activate():
        value = frozen_intent(root, "coverage.evaluate", {"slug": "sample"})
        with MutationStore(root, control, value).locked() as store:
            seed_state(store, value)
            basis = recovery.prepare(root, value, store)
            path = root / "sources/coverage/sample.yml"
            document = yaml.safe_load(path.read_bytes())
            document["coverage_profile"] = "changed-after-plan"
            path.write_text(yaml.safe_dump(document))
            changed = path.read_bytes()
            with pytest.raises(EvidenceWikiError):
                recovery.invoke(root, value, basis, store)
            assert path.read_bytes() == changed


def test_real_revision_recovers_three_exact_artifacts_after_lost_return(migration_root, monkeypatch):
    root, control, wanted = migration_root
    selected = HostContext([root], GRANTS, "caller", [root], mutation_control_directory=control)
    with selected.activate():
        request_value = operations.prepare_request(root, request_id="migration", operation="coverage.revision", arguments={"request": wanted})
        original = operations._invoke

        def lost_return(*args):
            original(*args)
            raise RuntimeError("synthetic loss after migration publication")

        monkeypatch.setattr(operations, "_invoke", lost_return)
        receipt = operations.submit(root, request_value)
        assert receipt["status"] == "commit_unknown"
        observed = operations.inspect(root, "migration")["record"]
        with MutationStore(root, control, observed["intent"]).locked() as store:
            expected = staged_plan(store, observed["state"]["owner_basis"])
            assert len(expected["outputs"]) == 3
            assert all((root / row["path"]).read_bytes() == store.read_stage(row["blob"]) for row in expected["outputs"])
            archived = json.loads(store.read_stage(expected["outputs"][0]["blob"]))
            assert archived["request"] == wanted
        proof = operations.reconcile(root, "migration")
        assert proof["desired_state_proved"] and not proof["completion_proved"]
        assert operations.inspect(root, "migration")["record"]["receipt"] == receipt


@pytest.mark.parametrize("partial", ["archive_only", "archive_question"])
def test_partial_revision_artifacts_do_not_prove_completion(migration_root, partial):
    root, control, wanted = migration_root
    selected = HostContext([root], GRANTS, "caller", [root], mutation_control_directory=control)
    with selected.activate():
        value = frozen_intent(root, "coverage.revision", {"request": wanted})
        with MutationStore(root, control, value).locked() as store:
            seed_state(store, value)
            basis = recovery.prepare(root, value, store)
            expected = staged_plan(store, basis)
            for row in expected["outputs"][:1 if partial == "archive_only" else 2]:
                path = root / row["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(store.read_stage(row["blob"]))
            proof = recovery.prove(root, value, basis, store)
            assert proof["present"] and not proof["desired"] and not proof["completion"]


def test_question_prior_hash_is_checked_inside_owning_lock(migration_root, monkeypatch):
    root, control, wanted = migration_root
    selected = HostContext([root], GRANTS, "caller", [root], mutation_control_directory=control)
    with selected.activate():
        value = frozen_intent(root, "coverage.revision", {"request": wanted})
        with MutationStore(root, control, value).locked() as store:
            seed_state(store, value)
            basis = recovery.prepare(root, value, store)
            resolver = owner("coverage_manifest").load_sibling_module("_coverage_revision").sibling("question_resolve")
            claims = resolver.load_sibling_module("question_claim")
            original = claims.question_lock
            question = root / "wiki/questions/q1.md"

            @contextmanager
            def changed_after_lock(path):
                with original(path):
                    question.write_bytes(question.read_bytes() + b"\nConcurrent question edit.\n")
                    yield

            monkeypatch.setattr(claims, "question_lock", changed_after_lock)
            with pytest.raises(EvidenceWikiError):
                recovery.invoke(root, value, basis, store)
            assert question.read_bytes().endswith(b"Concurrent question edit.\n")
            assert recovery.prove(root, value, basis, store)["desired"] is False


@pytest.mark.parametrize("kind", ["unrelated_lock", "unrelated_directory", "control_content"])
def test_exact_nested_control_admission_does_not_hide_other_changes(migration_root, kind):
    root, control, wanted = migration_root
    selected = HostContext([root], GRANTS, "caller", [root], mutation_control_directory=control)
    with selected.activate():
        value = frozen_intent(root, "coverage.revision", {"request": wanted})
        with MutationStore(root, control, value).locked() as store:
            seed_state(store, value)
            basis = recovery.prepare(root, value, store)
            expected = staged_plan(store, basis)
            assert expected["controls"]["files"] == ["wiki/questions/.locks/q1.lock"]
            assert not (root / "wiki/questions/.locks").exists()
            recovery.invoke(root, value, basis, store)
            assert recovery.prove(root, value, basis, store)["desired"]
            if kind == "unrelated_lock":
                (root / "wiki/questions/.locks/other.lock").write_bytes(b"")
            elif kind == "unrelated_directory":
                (root / "wiki/questions/.locks/other").mkdir()
            else:
                (root / "wiki/questions/.locks/q1.lock").write_bytes(b"unowned control content")
            proof = recovery.prove(root, value, basis, store)
            assert proof["present"] and not proof["desired"] and not proof["completion"]


def test_absolute_wiki_root_refuses_coordinated_coverage_before_effects(migration_root):
    root, control, wanted = migration_root
    config_path = root / "research.yml"
    config = yaml.safe_load(config_path.read_bytes())
    config["wiki"]["root"] = str(root / "wiki")
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    selected = HostContext([root], GRANTS, "caller", [root], mutation_control_directory=control)
    with selected.activate():
        value = frozen_intent(root, "coverage.revision", {"request": wanted})
        with MutationStore(root, control, value).locked() as store:
            seed_state(store, value)
            before = (root / "wiki/questions/q1.md").read_bytes()
            with pytest.raises(Exception) as error:
                recovery.prepare(root, value, store)
            assert getattr(error.value, "error_code", None) == "WORKSPACE_PATH_REFUSED"
            assert (root / "wiki/questions/q1.md").read_bytes() == before
            assert store.read()["status"] == "prepared"


def test_frozen_clock_cannot_be_replaced_after_plan(evaluated_root):
    root, control = evaluated_root
    with host(evaluated_root).activate():
        value = frozen_intent(root, "coverage.evaluate", {"slug": "sample"})
        with MutationStore(root, control, value).locked() as store:
            seed_state(store, value)
            basis = recovery.prepare(root, value, store)
            changed = copy.deepcopy(staged_plan(store, basis))
            changed["observed_at"] = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
            altered = store.stage_blob("altered-plan", canonical(changed))
            with pytest.raises(EvidenceWikiError):
                recovery.invoke(root, value, {**basis, "plan": altered}, store)


def test_staged_desired_proof_rejects_redirected_operation_target(evaluated_root):
    root, control = evaluated_root
    with host(evaluated_root).activate():
        value = frozen_intent(root, "coverage.evaluate", {"slug": "sample"})
        with MutationStore(root, control, value).locked() as store:
            seed_state(store, value)
            basis = recovery.prepare(root, value, store)
            altered = copy.deepcopy(staged_plan(store, basis))
            altered["outputs"][0]["path"] = "sources/coverage/unrelated.yml"
            descriptor = store.stage_blob("redirected-plan", canonical(altered))
            with pytest.raises(EvidenceWikiError):
                recovery.prove(root, value, {**basis, "plan": descriptor}, store)


def test_quota_projection_refuses_near_limit_report_before_effect(evaluated_root, monkeypatch):
    root, control = evaluated_root
    with host(evaluated_root).activate():
        value = frozen_intent(root, "coverage.evaluate", {"slug": "sample"})
        with MutationStore(root, control, value).locked() as store:
            seed_state(store, value)
            module = owner("coverage_manifest")
            original = module.prepare_evaluation

            def large_report(*args, **kwargs):
                prepared = original(*args, **kwargs)
                prepared["report"]["expanded"] = {str(index): "x" * 65000 for index in range(17)}
                return prepared

            monkeypatch.setattr(module, "prepare_evaluation", large_report)
            before = (root / "sources/coverage/sample.yml").read_bytes()
            with pytest.raises(EvidenceWikiError):
                recovery.prepare(root, value, store)
            assert (root / "sources/coverage/sample.yml").read_bytes() == before
            assert store.read()["status"] == "prepared"
            assert store.read()["owner_basis"]["preparing"]
