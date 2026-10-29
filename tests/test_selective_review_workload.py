"""Complete service accounting never turns unknown custody into saved work."""

import copy
from datetime import datetime, timedelta, timezone

import pytest

from tools import _review_dependency_accounting as accounting
from tools import _review_dependency_runner as runner
from tools import run_review_dependency_workload as legacy
from tools._execution_budget import execution_policy
from tools._observation_freshness import observation_policy
from tools._review_dependency_workload import digest, drive_selective_services, journal_run_id


@pytest.mark.parametrize("mode", ["legacy_full", "selective_reuse"])
def test_prospective_policy_matches_existing_root_without_leaving_runtime_directories(tmp_path, mode):
    from tests._review_dependency_fixture import fixture_document
    from tools._review_dependency_workload import compile_fixture_policy

    parent = tmp_path.resolve()
    base = parent / "runtime" / "mode" / "fixture"
    fixture = fixture_document()
    prospective, policy = compile_fixture_policy(base, fixture, mode)
    assert not (parent / "runtime").exists()
    base.mkdir(parents=True)
    existing, expected = compile_fixture_policy(base, fixture, mode)
    assert prospective["profile"] == existing["profile"]
    assert {key: value for key, value in prospective["bindings"].items() if key != "target"} == {
        key: value for key, value in existing["bindings"].items() if key != "target"}
    assert policy == expected
    assert base.is_dir() and list(base.iterdir()) == []


def test_pair_policy_preparation_keeps_both_mode_targets_absent(tmp_path):
    import hashlib
    import json

    from evidence_wiki.pack_discovery import owner
    from tests._review_dependency_fixture import fixture_document
    from tools._review_dependency_workload import retain

    parent = tmp_path.resolve()
    fixture_pin = retain(parent / "fixture.json", {"fixture": fixture_document()})
    runtime = parent / "runtime"
    policies = runner.compile_pair_policies(fixture_pin, parent / "policies", runtime_directory=runtime)
    assert not runtime.exists()
    assert policies["legacy_policy"]["schema_version"] == "evidence-strict-policy/v1"
    assert policies["selective_policy"]["schema_version"] == "evidence-strict-policy/v2"
    original = runner._read(policies["legacy_policy"]["raw_pin"])
    selective = runner._read(policies["selective_policy"]["raw_pin"])
    assert {key: value for key, value in selective.items() if key != "instructions"} == {
        **{key: value for key, value in original.items() if key != "instructions"},
        "schema_version": "evidence-strict-policy/v2", "review_basis_mode": "claim_dependencies_v1"}
    for ordinal, name, policy in ((0, "legacy", original), (2, "selective", selective)):
        compiled = json.loads((parent / "policies" / f"{ordinal}-{name}-compiled-profile.json").read_bytes())
        raw = owner("init_research_workspace").frozen_requirements_bytes(compiled["profile"]["workspace_init"])
        assert policy["instructions"]["docs/research-requirements.json"] == "sha256:" + hashlib.sha256(raw).hexdigest()


def test_prospective_policy_removes_only_created_directories_on_refusal(tmp_path, monkeypatch):
    from tests._review_dependency_fixture import fixture_document
    from tools import _review_dependency_workload as workload

    parent = tmp_path.resolve()
    marker = parent / "keep.txt"
    marker.write_text("Existing caller data")

    def refuse(_self, _request):
        raise ValueError("planning refused")

    monkeypatch.setattr(workload.Onboarding, "plan", refuse)
    with pytest.raises(ValueError, match="planning refused"):
        workload.compile_fixture_policy(parent / "runtime" / "fixture", fixture_document(), "legacy_full")
    assert sorted(path.name for path in parent.iterdir()) == ["keep.txt"]
    assert marker.read_text() == "Existing caller data"


def test_planning_cleanup_preserves_unexpected_files(tmp_path, monkeypatch):
    from pathlib import Path

    from tests._review_dependency_fixture import fixture_document
    from tools import _review_dependency_workload as workload

    base = tmp_path.resolve() / "runtime" / "fixture"

    def changed(_self, request):
        root = Path(request["request"]["payload"]["target"]["writable_root"])
        (root / "unexpected.txt").write_text("Do not delete")
        raise ValueError("planning refused")

    monkeypatch.setattr(workload.Onboarding, "plan", changed)
    with pytest.raises(OSError):
        workload.compile_fixture_policy(base, fixture_document(), "legacy_full")
    assert (base / "unexpected.txt").read_text() == "Do not delete"


def test_prospective_policy_refuses_symlinked_parent(tmp_path):
    from tests._review_dependency_fixture import fixture_document
    from tools._review_dependency_workload import compile_fixture_policy

    parent = tmp_path.resolve()
    target = parent / "target"
    target.mkdir()
    alias = parent / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="workload_planning_target_not_canonical"):
        compile_fixture_policy(alias / "fixture", fixture_document(), "legacy_full")
    assert list(target.iterdir()) == []
    assert alias.is_symlink()


@pytest.fixture
def directory_budget_binding(tmp_path, monkeypatch):
    import hashlib
    import json
    from types import SimpleNamespace

    from evidence_wiki import _review_producer

    root = tmp_path.resolve()
    originals = {
        "tools/_review_dependency_workload.py": "LIMIT = 60\n\ndef compile_fixture_policy():\n    return 'old'\n",
        "tools/_review_dependency_pilot.py": "LIMIT = 60\n\ndef completed_extended_pilot_measurements():\n    return 'old'\n",
        "tools/run_review_dependency_workload.py": "LIMIT = 60\n\ndef _completed_pilot_measurements():\n    return 'old'\n",
        "tools/_review_dependency_accounting.py": "LIMIT = 60\n\ndef dispatch_snapshot():\n    return 'old'\n",
        "tools/_review_dependency_pair_closing.py": "LIMIT = 60\n\ndef _executor():\n    return 'old'\n",
    }
    before = {**originals, "tests/test_selective_review_workload.py": "def test_existing():\n    pass\n"}

    def manifest(values):
        return {name: {"sha256": hashlib.sha256(raw.encode()).hexdigest(), "bytes": len(raw.encode())}
                for name, raw in values.items()}

    for name, raw in before.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw.replace("'old'", "'repaired'") + ("\n# Additional checks\n" if name.startswith("tests/") else ""))
    (root / "tools/_review_budget_source.py").write_text('"""Component source-binding fixture."""\n')

    def current_manifest(_root):
        return manifest({path.relative_to(root).as_posix(): path.read_text()
                         for folder in ("tools", "tests") for path in (root / folder).rglob("*.py")})

    def retained(name, value):
        path = root / name
        raw = json.dumps(value, sort_keys=True).encode()
        path.write_bytes(raw)
        return {"path": str(path), "raw_sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}

    old_manifest = manifest(before)
    recipe = {"schema_version": "selective-complete-pilot-measurement/v5",
              "candidate_sha256": digest(old_manifest), "review_producer_id": "sha256:" + "1" * 64,
              "phase_caps": {"setup": 30, "job": 60}}
    document = {"schema_version": "review-directory-budget-source/v3",
                "scope": "unchanged_budgets_after_directory_and_setup_custody_repairs", "qualification": False,
                "historical_recipe_pin": retained("recipe.json", recipe),
                "historical_manifest_pin": retained("historical.json", old_manifest),
                "current_manifest_pin": retained("current.json", current_manifest(root)),
                "historical_sources_pin": retained("sources.json", originals)}
    monkeypatch.setattr(legacy, "ROOT", root)
    monkeypatch.setattr(legacy, "candidate_manifest", current_manifest)
    generation = SimpleNamespace(producer_id=recipe["review_producer_id"])
    monkeypatch.setattr(_review_producer, "capture", lambda: generation)
    monkeypatch.setattr(_review_producer, "assert_current", lambda selected: selected is generation or pytest.fail("wrong producer"))

    def refresh():
        document["current_manifest_pin"] = retained("current.json", current_manifest(root))
        return retained("binding.json", document)

    return SimpleNamespace(root=root, recipe=recipe, generation=generation, document=document,
        originals=originals, retained=retained, refresh=refresh,
        current_id=lambda: digest(current_manifest(root)), binding_pin=refresh())


def test_directory_budget_binding_preserves_original_recipe_and_current_identity(directory_budget_binding):
    from tools._review_budget_source import directory_budget_source, historical_plan_candidate, measurement_candidate

    fixture = directory_budget_binding
    before = (fixture.root / "recipe.json").read_bytes()
    with directory_budget_source(fixture.binding_pin, pair=("direct", "independent-claim-text")):
        current = fixture.current_id()
        historical = measurement_candidate(fixture.recipe, current)
        assert historical == fixture.recipe["candidate_sha256"] and historical != current
        assert historical_plan_candidate(historical, current) == historical
        changed = copy.deepcopy(fixture.recipe)
        changed["phase_caps"]["job"] += 1
        with pytest.raises(ValueError, match="budget_source"):
            measurement_candidate(changed, current)
        with pytest.raises(ValueError, match="budget_source"):
            measurement_candidate(fixture.recipe, "0" * 64)
    assert (fixture.root / "recipe.json").read_bytes() == before
    with pytest.raises(ValueError, match="budget_source"):
        measurement_candidate(fixture.recipe, fixture.current_id())


@pytest.mark.parametrize("change", ["runtime_constant", "additional_function", "additional_class", "extra_file", "deleted_file", "historical_bytes", "producer"])
def test_directory_budget_binding_refuses_unrelated_or_unproved_source_changes(directory_budget_binding, change):
    from tools._review_budget_source import directory_budget_source

    fixture = directory_budget_binding
    path = fixture.root / "tools/run_review_dependency_workload.py"
    if change == "runtime_constant":
        path.write_text(path.read_text().replace("LIMIT = 60", "LIMIT = 61"))
    elif change == "additional_function":
        path.write_text(path.read_text() + "\ndef unrelated():\n    return True\n")
    elif change == "additional_class":
        path.write_text(path.read_text() + "\nclass Unrelated:\n    pass\n")
    elif change == "extra_file":
        (fixture.root / "tools/unrelated.py").write_text("changed = True\n")
    elif change == "deleted_file":
        path.unlink()
    elif change == "historical_bytes":
        changed = dict(fixture.originals)
        changed["tools/run_review_dependency_workload.py"] += "\n# Unproven history\n"
        fixture.document["historical_sources_pin"] = fixture.retained("sources.json", changed)
    else:
        fixture.generation.producer_id = "sha256:" + "2" * 64
    with pytest.raises(ValueError, match="budget_source"):
        with directory_budget_source(fixture.refresh(), pair=("scoped_mcp", "independent-claim-limitations")):
            pytest.fail("incompatible source admitted")


def test_directory_budget_binding_expires_copied_context_and_rejects_other_routes(directory_budget_binding):
    from contextvars import copy_context

    from tools._review_budget_source import directory_budget_source, measurement_candidate

    fixture = directory_budget_binding
    with pytest.raises(ValueError, match="budget_source"):
        with directory_budget_source(fixture.binding_pin, pair=("direct", "independent-claim-limitations")):
            pytest.fail("unselected route admitted")
    with directory_budget_source(fixture.binding_pin, pair=("direct", "independent-claim-text")):
        copied = copy_context()
    with pytest.raises(ValueError, match="budget_source"):
        copied.run(measurement_candidate, fixture.recipe, fixture.current_id())


def test_directory_budget_binding_refuses_source_drift_at_exit(directory_budget_binding):
    from tools._review_budget_source import directory_budget_source

    fixture = directory_budget_binding
    with pytest.raises(ValueError, match="budget_source"):
        with directory_budget_source(fixture.binding_pin, pair=("direct", "independent-claim-text")):
            path = fixture.root / "tools/run_review_dependency_workload.py"
            path.write_text(path.read_text() + "\n# Changed while selected\n")


@pytest.fixture
def repaired_producer_budget_binding(directory_budget_binding, monkeypatch):
    import hashlib
    from types import SimpleNamespace

    from evidence_wiki import _review_producer
    from tools import _review_budget_source as binding

    prior = directory_budget_binding
    root = prior.root
    added = {
        "tools/_execution_budget.py": "LIMIT = 60\n\ndef provisioning_window():\n    return 'old'\n",
        "tools/_review_dependency_runner.py": "LIMIT = 60\n\ndef _capture_completed_mode():\n    return 'old'\n",
        "src/evidence_wiki/research_journal_contracts.py": "JOURNAL = 'stable'\n\ndef _canonical_owned_record():\n    return 'old'\n\ndef canonical_record():\n    return 'old'\n",
    }
    tests = {path: "def test_existing():\n    pass\n" for path in binding._CAPTURE_TEST_SOURCES}
    originals = {**prior.originals, **added}
    before = {**originals, **tests}
    for path, raw in {**added, **tests}.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(raw.replace("'old'", "'repaired'") + ("\n# Additional checks\n" if path in tests else ""))
    product = root / "src/evidence_wiki/research_journal_contracts.py"
    product.write_text(product.read_text() + "\nclass _RecordCodecMemo:\n    pass\n\ndef _record_codec_key():\n    return None\n")

    def manifest(values):
        return {path: {"sha256": hashlib.sha256(raw.encode()).hexdigest(), "bytes": len(raw.encode())}
                for path, raw in values.items()}

    def current_manifest(_root):
        return manifest({path.relative_to(root).as_posix(): path.read_text()
            for folder in ("tools", "tests", "src") for path in (root / folder).rglob("*.py")})

    old_source = manifest(before)
    source = "src/evidence_wiki/research_journal_contracts.py"
    member = "package/evidence_wiki/research_journal_contracts.py"

    def producer(source_manifest):
        row = source_manifest[source]
        return {"schema_version": "evidence-review-producer-manifest/v1", "members": [
            {"path": member, "sha256": "sha256:" + row["sha256"], "size_bytes": row["bytes"]},
            {"path": "package/evidence_wiki/unchanged.py", "sha256": "sha256:" + "3" * 64, "size_bytes": 12}]}

    def identity(value):
        return _review_producer.content_id("evidence-review-producer/v1", {
            "schema_version": "evidence-review-producer/v1",
            "manifest_id": _review_producer.content_id("evidence-review-producer-manifest/v1", value)})

    old_producer, new_producer = producer(old_source), producer(current_manifest(root))
    recipe = copy.deepcopy(prior.recipe)
    recipe.update(candidate_sha256=digest(old_source), review_producer_id=identity(old_producer))
    generation = SimpleNamespace(producer_id=identity(new_producer), document=lambda: {"manifest": new_producer})
    document = {**prior.document, "schema_version": "review-directory-budget-source/v4",
        "scope": "unchanged_budgets_after_capture_and_codec_repairs",
        "historical_recipe_pin": prior.retained("recipe.json", recipe),
        "historical_manifest_pin": prior.retained("historical.json", old_source),
        "historical_sources_pin": prior.retained("sources.json", originals),
        "historical_producer_manifest_pin": prior.retained("old-producer.json", old_producer),
        "current_producer_manifest_pin": prior.retained("new-producer.json", new_producer)}
    monkeypatch.setattr(legacy, "candidate_manifest", current_manifest)
    monkeypatch.setattr(_review_producer, "capture", lambda: generation)
    monkeypatch.setattr(_review_producer, "assert_current", lambda selected: selected is generation or pytest.fail("wrong producer"))

    def refresh():
        document["current_manifest_pin"] = prior.retained("current.json", current_manifest(root))
        return prior.retained("binding.json", document)

    return SimpleNamespace(root=root, recipe=recipe, generation=generation, document=document,
        old_producer=old_producer, new_producer=new_producer, retained=prior.retained,
        current_id=lambda: digest(current_manifest(root)), refresh=refresh, binding_pin=refresh())


def test_repaired_producer_uses_historical_caps_with_exact_current_identity(repaired_producer_budget_binding):
    from tools._review_budget_source import directory_budget_source, execution_producer, measurement_candidate

    fixture = repaired_producer_budget_binding
    original = copy.deepcopy(fixture.recipe)
    with directory_budget_source(fixture.binding_pin, pair=("scoped_mcp", "independent-claim-limitations")):
        candidate = fixture.current_id()
        assert measurement_candidate(fixture.recipe, candidate) == fixture.recipe["candidate_sha256"]
        assert execution_producer(fixture.recipe, candidate) == fixture.generation.producer_id
        assert runner._budget_producer(fixture.recipe, candidate, fixture.generation.producer_id)
        assert not runner._budget_producer(fixture.recipe, candidate, fixture.recipe["review_producer_id"])
        changed = {**fixture.recipe, "phase_caps": {**fixture.recipe["phase_caps"], "job": 61}}
        with pytest.raises(ValueError, match="budget_source"):
            execution_producer(changed, candidate)
    assert fixture.recipe == original
    with pytest.raises(ValueError, match="budget_source"):
        execution_producer(fixture.recipe, fixture.current_id())


@pytest.mark.parametrize("change", ["old_producer", "unrelated_member", "source_member", "generation", "outside_definition", "old_schema"])
def test_repaired_producer_refuses_unbound_changes(repaired_producer_budget_binding, change):
    from tools._review_budget_source import directory_budget_source

    fixture = repaired_producer_budget_binding
    if change == "old_producer":
        fixture.old_producer["members"][0]["size_bytes"] += 1
        fixture.document["historical_producer_manifest_pin"] = fixture.retained("old-producer.json", fixture.old_producer)
    elif change in {"unrelated_member", "source_member"}:
        fixture.new_producer["members"][1 if change == "unrelated_member" else 0]["size_bytes"] += 1
        fixture.document["current_producer_manifest_pin"] = fixture.retained("new-producer.json", fixture.new_producer)
    elif change == "generation":
        fixture.generation.producer_id = fixture.recipe["review_producer_id"]
    elif change == "outside_definition":
        path = fixture.root / "src/evidence_wiki/research_journal_contracts.py"
        path.write_text(path.read_text().replace("JOURNAL = 'stable'", "JOURNAL = 'changed'"))
    else:
        fixture.document["schema_version"] = "review-directory-budget-source/v3"
    with pytest.raises(ValueError, match="budget_source"):
        with directory_budget_source(fixture.refresh(), pair=("direct", "independent-claim-text")):
            pytest.fail("unbound producer admitted")


@pytest.mark.parametrize("change", [None, "historical_producer", "changed_caps"])
def test_provisioning_uses_the_same_exact_current_producer_guard(
        repaired_producer_budget_binding, cohort, pin, monkeypatch, change):
    from tools import _review_control_ceiling as ceiling
    from tools import _review_dependency_pilot as pilot
    from tools._execution_budget import provisioning_window
    from tools._review_budget_source import directory_budget_source

    fixture = repaired_producer_budget_binding
    plan, _pair = copy.deepcopy(cohort)
    policy = execution_policy("forty-eight-hour")
    observation = observation_policy("batch-extended")
    caps = {"setup": 600, "job": 1000, "mutation": 1600, "owner_read": 2200,
        "global_close": 2625, "cleanup": 60}
    frozen = datetime.now(timezone.utc) - timedelta(seconds=2)
    plan.update(ceiling.binding(ceiling.OWNER_PROFILE), schema_version=accounting.OWNER_PLAN,
        execution_policy=policy, execution_policy_sha256=digest(policy),
        observation_policy=observation, observation_policy_sha256=digest(observation),
        phase_caps=caps, formal_application_policy=legacy.formal_application_policy(caps["job"]),
        pair_closing_reservation=accounting.extended_closing_reservation(caps["global_close"]),
        journal_collection_policy=dict(accounting.JOURNAL_COLLECTION_POLICY),
        candidate_sha256=fixture.current_id(), review_producer_id=fixture.generation.producer_id,
        elapsed_cap_seconds=legacy.allocation_seconds(caps, 24) + 2 * caps["global_close"],
        frozen_at=frozen.isoformat(), expires_at=(frozen + timedelta(seconds=policy["authority_seconds"])).isoformat())
    fixture.recipe.update(ceiling.binding(ceiling.OWNER_PROFILE), formal_execution_policy=policy,
        phase_caps=copy.deepcopy(caps), formal_application_policy=copy.deepcopy(plan["formal_application_policy"]),
        group_overhead_cap_seconds=0)
    fixture.document["historical_recipe_pin"] = fixture.retained("recipe.json", fixture.recipe)
    original_read = runner._read
    monkeypatch.setattr(runner, "_read", lambda value, **kwargs: {"host_id": "direct"}
        if value == pin else original_read(value, **kwargs))
    monkeypatch.setattr(legacy, "read_pinned_artifact", lambda value: (fixture.recipe, b"explicit historical-owner stub"))
    monkeypatch.setattr(pilot, "completed_extended_pilot_measurements", lambda *args: ({}, 0))
    if change == "historical_producer":
        plan["review_producer_id"] = fixture.recipe["review_producer_id"]
    elif change == "changed_caps":
        plan["phase_caps"] = {**caps, "setup": caps["setup"] + 1}
        plan["elapsed_cap_seconds"] += 1
    accounting.validate_plan(plan)
    assignments = [{**row["coordinate"], **{key: row[key] for key in (
        "original_id", "controller_ref", "execution_role", "task_role", "required", "journal_key", "native_run_id")},
        "attempt_id": None, "application_event_ref": None} for row in plan["obligations"]]
    allocation = runner._allocation_binding(plan, assignments)
    before = copy.deepcopy((fixture.recipe, plan, allocation))
    with directory_budget_source(fixture.refresh(), pair=("direct", "independent-claim-text")):
        if change:
            with pytest.raises(ValueError, match="execution_runtime_binding_invalid"):
                provisioning_window(policy, allocation, plan["elapsed_cap_seconds"])
        else:
            begin, end = provisioning_window(policy, allocation, plan["elapsed_cap_seconds"])
            assert frozen <= datetime.fromisoformat(begin) < datetime.fromisoformat(end)
            assert end == plan["expires_at"]
    assert before == (fixture.recipe, plan, allocation)


@pytest.fixture
def completed_dispatch(tmp_path, monkeypatch, cohort, pin):
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
    """Real executor/child custody; journal-owner inputs are explicit stubs."""
    import subprocess
    import sys
    from types import SimpleNamespace

    from evidence_wiki import BoundedSubprocessExecutor
    from tests.test_agent_execution import LocalMeter, attempt, profile
    from tools import _review_dependency_workload as workload

    plan, _pair = cohort
    worker = tmp_path / "worker.py"
    worker.write_text("import time\ntime.sleep(0.03)\nprint('{}')\n")
    host = BoundedSubprocessExecutor(profile(),
        commands={"researcher": (sys.executable, str(worker))}, cwd=tmp_path, meter=LocalMeter())
    selected = attempt(host.profile)
    runtime = SimpleNamespace(execution=SimpleNamespace(executor=host))
    originals = workload.OriginalResponses(tmp_path / "observations")
    tracker = workload.OwnedChildren(originals, digest(plan))
    tracker.register_review_executor(host, "local_review_worker")
    tracker.bind_review_dispatch(plan, runtime)

    def journal(_root, _runtime, run_id, _originals):
        receipt = {"run_id": run_id, "records_sha256": "sha256:" + "1" * 64,
            "records_bytes": 0, "record_count": 0, "head_ref": None,
            "source_sha256": "2" * 64, "state_sha256": "3" * 64, "original_pin": pin}
        return [], receipt, None, None

    monkeypatch.setattr(workload, "authenticated_journal", journal)
    # Restore the real Popen after the isolated fixture's closed launch guard.
    monkeypatch.setattr(subprocess, "Popen", subprocess.Popen)
    tracker.open()
    try:
        first = host.invoke(selected)
        running = host._attempts[selected.attempt_id]
        assert running.finished.wait(4) and running.snapshot["final_accounting"] is True
        tracker.confirm_completed()
        yield SimpleNamespace(host=host, selected=selected, running=running, first=first,
            tracker=tracker, capability=SimpleNamespace(runtime=runtime),
            pins={"plan": plan, "runtime": runtime, "root": tmp_path})
    finally:
        assert tracker.close()["status"] == "confirmed"


def test_dispatch_collector_authenticates_retained_receipt_without_polling(completed_dispatch, monkeypatch):
    fixture = completed_dispatch

    def forbidden(*_args):
        pytest.fail("collection must not invoke, poll or signal a process")

    sequence = fixture.running.sequence
    with monkeypatch.context() as guard:
        for name in ("invoke", "poll", "cancel"):
            guard.setattr(fixture.host, name, forbidden)
        guard.setattr(fixture.running.process, "poll", forbidden)
        observed = accounting.dispatch_snapshot(fixture.tracker, fixture.host, "opening", fixture.pins)
    assert observed.document["births"][0]["pid"] == fixture.running.process.pid
    assert fixture.running.sequence == sequence
    assert fixture.host.authenticate(fixture.host._dispatch_observation(fixture.selected), fixture.selected) == fixture.first
    # An unrelated unit-test worker cannot acquire a review obligation.
    assert observed.document["state"] == "unknown"


@pytest.mark.parametrize("mutation", ["missing", "unsigned", "tampered"])
def test_dispatch_collector_refuses_missing_or_unauthenticated_receipt(completed_dispatch, mutation):
    import json

    from evidence_wiki.agent_execution import ExecutionError

    fixture = completed_dispatch
    if mutation == "missing":
        fixture.running.dispatch_observation = None
    elif mutation == "unsigned":
        fixture.running.dispatch_observation = json.dumps(fixture.running.snapshot).encode()
    else:
        changed = copy.deepcopy(fixture.first)
        changed["authentication"] = "0" * 64
        fixture.running.dispatch_observation = json.dumps(changed).encode()
    with pytest.raises((ValueError, ExecutionError)):
        accounting.dispatch_snapshot(fixture.tracker, fixture.host, "opening", fixture.pins)


def test_pair_collector_authenticates_final_observations_and_preserves_stable_identity(completed_dispatch, monkeypatch):
    import json

    from tools import _review_dependency_pair_closing as closing

    fixture = completed_dispatch
    authority = fixture.selected.document()

    def forbidden(*_args):
        pytest.fail("closing must not dispatch, signal or poll a process")

    with monkeypatch.context() as guard:
        for name in ("invoke", "cancel"):
            guard.setattr(fixture.host, name, forbidden)
        guard.setattr(fixture.running.process, "poll", forbidden)
        first = closing._executor(fixture.capability)
        assert closing._executor(fixture.capability) == first
    final = json.loads(first)[0]["final"]
    assert final["state"] == "succeeded" and final["final_accounting"] is True
    assert final["attempt_id"] == fixture.selected.attempt_id
    assert fixture.selected.document() == authority and len(fixture.host._attempts) == 1
    fixture.running.snapshot["output"] = {"changed": True}
    assert closing._executor(fixture.capability) != first


@pytest.mark.parametrize("mutation", ["unsigned", "tampered", "incomplete"])
def test_pair_collector_refuses_unauthenticated_or_incomplete_final(completed_dispatch, monkeypatch, mutation):
    from evidence_wiki.agent_execution import ExecutionError
    from tools import _review_dependency_pair_closing as closing

    fixture = completed_dispatch
    observed = fixture.host.poll(fixture.selected)
    if mutation == "incomplete":
        fixture.running.finished.clear()
    else:
        if mutation == "unsigned":
            observed = copy.deepcopy(fixture.running.snapshot)
        else:
            observed["authentication"] = "0" * 64
        monkeypatch.setattr(fixture.host, "poll", lambda _attempt: observed)
    with pytest.raises((ValueError, ExecutionError)):
        closing._executor(fixture.capability)


@pytest.fixture(scope="module")
def native_setup_custody(tmp_path_factory):
    """One real native setup with its twelve bounded child processes."""
    import subprocess
    from types import SimpleNamespace

    from evidence_wiki import BoundedSubprocessExecutor, Onboarding, setup_application
    from tests._review_dependency_fixture import fixture_document
    from tests.test_agent_execution import profile
    from tools import _review_dependency_workload as workload

    root = tmp_path_factory.mktemp("native-setup-custody").resolve()
    base = root / "fixture"
    base.mkdir()
    pin = workload.retain(root / "unit-pin.json", {})
    plan, _pair = cohort.__wrapped__(pin)
    originals = workload.OriginalResponses(root / "observations")
    tracker = workload.OwnedChildren(originals, digest(plan))
    host = BoundedSubprocessExecutor(profile(), commands={}, cwd=root)
    runtime = SimpleNamespace(execution=SimpleNamespace(executor=host))
    tracker.register_review_executor(host, "local_review_worker")
    original_popen, original_owner = subprocess.Popen, setup_application._execute_bounded
    tracker.open()
    try:
        with Onboarding.open(allowed_roots=[base], allow=["apply"]) as onboarding:
            native_plan = onboarding.plan(workload.fixture_setup_request(base, fixture_document()))
            with workload._setup_child_custody(tracker, native_plan):
                setup = onboarding.apply(native_plan)
        assert setup["setup_ready"] is True
        assert setup_application._execute_bounded is original_owner
        assert len(tracker.owned) == len(tracker._setup_custody) == len(tracker._completed) == 12
        tracker.bind_review_dispatch(plan, runtime)
    finally:
        try:
            cleanup = tracker.close()
            assert cleanup["status"] == "confirmed"
        finally:
            subprocess.Popen = original_popen
    return SimpleNamespace(root=root, pin=pin, host=host, tracker=tracker, native_plan=native_plan,
        pins={"plan": plan, "runtime": runtime, "root": root})


@pytest.fixture
def setup_snapshot(native_setup_custody, monkeypatch):
    from tools import _review_dependency_workload as workload

    fixture = native_setup_custody

    def journal(_root, _runtime, run_id, _originals):
        # Only unrelated journal-owner inputs are stubbed; setup and child
        # ownership above are real. This grants no native comparison credit.
        return [], {"run_id": run_id, "records_sha256": "sha256:" + "1" * 64,
            "records_bytes": 0, "record_count": 0, "head_ref": None,
            "source_sha256": "2" * 64, "state_sha256": "3" * 64, "original_pin": fixture.pin}, None, None

    monkeypatch.setattr(workload, "authenticated_journal", journal)
    return lambda: accounting.dispatch_snapshot(fixture.tracker, fixture.host, "cleanup_closed", fixture.pins)


def test_setup_custody_joins_all_real_native_children(native_setup_custody, setup_snapshot):
    from evidence_wiki.setup_worker import STEPS

    fixture = native_setup_custody
    observed = setup_snapshot().document
    assert observed["state"] == "complete" and observed["guard_closed"] is True
    assert len(observed["births"]) == 12
    operations = []
    for row in observed["births"]:
        assert row["kind"] == "setup_or_owner_worker"
        assert row["attempt_id"] is None and row["obligation_id"] is None
        assert row["completion_pin"] is not None
        command = runner._read(row["command_manifest_pin"])
        receipt = runner._read(command["owner_receipt_pin"])
        assert receipt["pid"] == row["pid"] and receipt["ordinal"] == row["ordinal"]
        assert receipt["spawn_pin"] == row["spawn_pin"] and receipt["completion_pin"] == row["completion_pin"]
        assert receipt["plan_id"] == fixture.native_plan["plan_id"]
        assert receipt["argv"] == command["argv"]
        operations.append(receipt["operation"])
    assert tuple(operations) == STEPS
    assert fixture.host._attempts == {}


def test_missing_setup_association_stays_unknown(native_setup_custody, setup_snapshot, monkeypatch):
    tracker = native_setup_custody.tracker
    monkeypatch.setattr(tracker, "_setup_custody", tracker._setup_custody[1:])
    observed = setup_snapshot().document
    assert observed["state"] == "unknown"
    assert observed["births"][0]["kind"] == "unclassified"


@pytest.mark.parametrize("mutation", ["copied_capability", "raw_document", "missing_completion", "changed_command", "foreign_tracker"])
def test_setup_custody_refuses_reconstructed_or_changed_joins(native_setup_custody, setup_snapshot, monkeypatch, mutation):
    from dataclasses import replace

    tracker = native_setup_custody.tracker
    custody = tracker._setup_custody[0]
    if mutation == "copied_capability":
        monkeypatch.setattr(tracker, "_setup_custody", (replace(custody), *tracker._setup_custody[1:]))
    elif mutation == "raw_document":
        monkeypatch.setattr(tracker, "_setup_custody", ({"pid": custody.process.pid},))
    elif mutation == "missing_completion":
        monkeypatch.delitem(tracker._completed, id(custody.process))
    elif mutation == "changed_command":
        monkeypatch.setattr(custody.process, "args", ["unrelated-command"])
    else:
        monkeypatch.setattr(tracker, "_setup_custody", (replace(custody, tracker=object()),))
    with pytest.raises(ValueError, match="setup_custody"):
        setup_snapshot()


def test_setup_custody_refuses_wrong_call_and_restores_owner(tmp_path, monkeypatch, native_setup_custody):
    import subprocess
    import sys

    from evidence_wiki import setup_application
    from tools import _review_dependency_workload as workload

    original = setup_application._execute_bounded
    originals = workload.OriginalResponses(tmp_path / "observations")
    tracker = workload.OwnedChildren(originals, "a" * 64)
    monkeypatch.setattr(subprocess, "Popen", subprocess.Popen)
    tracker.open()
    try:
        with pytest.raises(ValueError, match="setup_custody_call_changed"):
            with workload._setup_child_custody(tracker, native_setup_custody.native_plan):
                setup_application._execute_bounded([sys.executable, "-c", "raise SystemExit(99)"],
                    cwd=tmp_path, stdin_text='{"operation":"unrelated","plan":{},"clock":"unused"}',
                    timeout_seconds=1, capture_limit=65536, preserve_stdout=True)
        assert setup_application._execute_bounded is original
        assert tracker.owned == [] and tracker._setup_custody == ()
    finally:
        assert tracker.close()["status"] == "confirmed"


@pytest.mark.parametrize("journal_change", [None, "missing", "duplicate", "attempt_changed", "claim_changed"])
def test_dispatch_joins_canonical_claim_from_exact_authenticated_attempt(tmp_path, monkeypatch, cohort, pin, journal_change):
    import subprocess
    import sys
    from types import SimpleNamespace

    from evidence_wiki import BoundedSubprocessExecutor, ExecutionAttempt, ExecutionRole
    from tests.test_agent_execution import LocalMeter, profile
    from tests.test_workflow_contracts import job
    from tools import _review_dependency_workload as workload

    plan, _pair = cohort
    obligation = plan["obligations"][0]
    canonical_claim = obligation["coordinate"]["claim_id"]
    run_id = obligation["native_run_id"]
    selected = profile(roles=(ExecutionRole("evidence", "evaluator", "evidence-controller", "local-process", "prompt/v1"),))
    now = datetime.now(timezone.utc)
    declared = job("evaluate")
    declared.update(controller_ref="evidence-controller", issued_utc=(now - timedelta(seconds=1)).isoformat(),
                    deadline_utc=(now + timedelta(seconds=10)).isoformat(), timeout_seconds=5)
    assert declared["payload"]["claim_id"] != canonical_claim
    attempt = ExecutionAttempt.create(attempt_id="canonical-join", nonce="canonical-nonce", run_id=run_id,
        binding_id="binding-1", profile=selected, config_ref="evidence", job=declared,
        input_document={"packet": "bounded-evidence"})
    reservation = {"kind": "execution_attempt", "data": {"attempt": attempt.document(), "claim_id": canonical_claim}}
    records = [reservation]
    if journal_change == "missing":
        records = []
    elif journal_change == "duplicate":
        records.append(copy.deepcopy(reservation))
    elif journal_change == "attempt_changed":
        reservation["data"]["attempt"]["nonce"] = "different"
    elif journal_change == "claim_changed":
        reservation["data"]["claim_id"] = "unrelated"

    def journal(_root, _runtime, actual_run, _originals):
        # The native journal owner is stubbed for this identity-join test;
        # process launch and executor authentication remain real.
        rows = records if actual_run == run_id else []
        return rows, {"run_id": actual_run, "records_sha256": "sha256:" + "1" * 64,
            "records_bytes": len(accounting.canonical(rows)), "record_count": len(rows), "head_ref": None,
            "source_sha256": "2" * 64, "state_sha256": "3" * 64, "original_pin": pin}, None, None

    worker = tmp_path / "worker.py"
    worker.write_text("print('{}')\n")
    host = BoundedSubprocessExecutor(selected, commands={"evidence": (sys.executable, str(worker))},
        cwd=tmp_path, meter=LocalMeter())
    runtime = SimpleNamespace(execution=SimpleNamespace(executor=host))
    tracker = workload.OwnedChildren(workload.OriginalResponses(tmp_path / "observations"), digest(plan))
    tracker.register_review_executor(host, "local_review_worker")
    tracker.bind_review_dispatch(plan, runtime)
    monkeypatch.setattr(workload, "authenticated_journal", journal)
    monkeypatch.setattr(subprocess, "Popen", subprocess.Popen)
    tracker.open()
    try:
        first = host.invoke(attempt)
        assert host._attempts[attempt.attempt_id].finished.wait(4)
        assert host.authenticate(first, attempt) == first
        tracker.confirm_completed()
        observed = accounting.dispatch_snapshot(tracker, host, "opening", {"plan": plan, "runtime": runtime, "root": tmp_path})
        birth = observed.document["births"][0]
        if journal_change is None:
            assert observed.document["state"] == "complete"
            assert birth["kind"] == "local_review_worker" and birth["obligation_id"] == obligation["obligation_id"]
            assert observed.attempts[0]["obligation_id"] == obligation["obligation_id"]
        else:
            assert observed.document["state"] == "unknown"
            assert birth["kind"] == "unclassified" and birth["obligation_id"] is None
    finally:
        assert tracker.close()["status"] == "confirmed"


def test_normalization_rejects_substituted_execution_documents(monkeypatch):
    # Isolate the binding check; native capability admission is tested separately.
    monkeypatch.setattr(accounting, "validate", lambda _name, value: value)
    pair = {"schema_version": accounting.PAIR, "candidate": "original"}
    plans = {"legacy_full": {"schema_version": accounting.PLAN, "plan": "original"}}
    reports = {"legacy_full": {"schema_version": accounting.REPORT, "stage_observations": [{"accepted": True}], "global_closing_utc": "close", "application": "original"}}
    proof = accounting._NativeNormalization(accounting._NORMALIZATION_MINT,
        evidence={"pair_spec_id": accounting.document_id(pair)}, claims={}, publications={},
        stages={"legacy_full": reports["legacy_full"]["stage_observations"]}, owners={"legacy_full": "close"},
        pair=pair, plans=plans, reports=reports)
    proof.assert_binding(pair, plans, reports)
    for target, key in ((pair, "candidate"), (plans["legacy_full"], "plan"), (reports["legacy_full"], "application")):
        original = target[key]
        target[key] = "substituted"
        with pytest.raises(ValueError, match="accounting_normalization_execution_changed"):
            proof.assert_binding(pair, plans, reports)
        target[key] = original


def test_native_report_binding_excludes_only_recursive_manifest():
    report = {"original_response_manifest_pin": "before", "global_closing_utc": "close", "cost": {"elapsed_seconds": 1}}
    identity = runner._report_source_identity(report)
    report["original_response_manifest_pin"] = "after"
    assert runner._report_source_identity(report) == identity
    report["cost"]["elapsed_seconds"] = 2
    assert runner._report_source_identity(report) != identity


def test_candidate_close_rejects_source_changes(monkeypatch):
    monkeypatch.setattr(legacy, "candidate_manifest", lambda _root: {"source": "before"})
    plan = {"candidate_sha256": digest({"source": "before"})}
    runner._assert_candidate(plan)
    monkeypatch.setattr(legacy, "candidate_manifest", lambda _root: {"source": "after"})
    with pytest.raises(ValueError, match="accounting_candidate_changed"):
        runner._assert_candidate(plan)


@pytest.fixture
def pin(tmp_path):
    return {"path": str(tmp_path / "original.json"), "raw_sha256": "a" * 64, "size_bytes": 1}


@pytest.fixture
def cohort(pin):
    host, case = "direct", "independent-claim-text"
    assignments = []
    originals = {claim: "demand" if claim in {"observed-demand", "independent-note"} else "capacity" for claim in accounting.CLAIMS}
    manifest = {"questions": [{"slug": name, "original_id": name} for name in ("capacity", "demand")],
                "claims": [{"id": claim, "question_slug": original} for claim, original in originals.items()]}
    for cycle in ("baseline", "first-change"):
        for claim in accounting.CLAIMS:
            for config in ("evidence", "critic"):
                key = "/".join((host, case, cycle, claim))
                assignments.append({"host_id": host, "case_id": case, "cycle": cycle, "claim_id": claim, "config_ref": config,
                    "original_id": originals[claim], "execution_role": "evaluator", "task_role": "critic" if config == "critic" else "evaluator",
                    "controller_ref": config + "-controller", "journal_key": key, "native_run_id": journal_run_id(key), "required": True,
                    "attempt_id": None, "application_event_ref": None})
    obligations = accounting.obligations(assignments, manifest)
    policy = {"schema_version": "evidence-strict-policy/v1", "raw_pin": pin, "content_id": "sha256:" + "1" * 64}
    selective_policy = {**policy, "schema_version": "evidence-strict-policy/v2"}
    pair = {"schema_version": accounting.PAIR, "candidate_sha256": "a" * 64, "candidate_manifest_pin": pin,
        "host_id": host, "case_id": case, "changed_cycle": "first-change", "fixture_pin": pin, "mutation_pin": pin,
        "verdict_table_pin": pin, "normalization_contract_id": accounting.document_id(accounting.NORMALIZATION_CONTRACT),
        "clock_contract_pin": pin, "legacy_policy": policy, "selective_policy": selective_policy,
        "expected_legacy_refresh_ids": list(accounting.CLAIMS), "expected_selective_refresh_ids": ["observed-demand", "independent-note", "adequacy", "margin"],
        "frozen_case_count": 99, "frozen_case_manifest_pin": pin, "prospective_case_manifest_pin": pin,
        "scope": "local_bundled_deterministic_worker_fixture", "minimum_avoided_completed_review_applications": 1, "normalization_contract_pin": pin}
    execution, observation = execution_policy("six-hour"), observation_policy("batch")
    begin = datetime(2026, 10, 1, tzinfo=timezone.utc)
    slots = [{"slot_id": run, "native_run_id": run, "kind": "review", "cycle": cycle, "claim_id": claim}
             for cycle in ("baseline", "first-change") for claim in accounting.CLAIMS
             for run in [journal_run_id("/".join((host, case, cycle, claim)))]]
    control = "control-" + accounting.hashlib.sha256((host + "/" + case).encode()).hexdigest()[:24]
    slots.append({"slot_id": control, "native_run_id": control, "kind": "changed_basis_control", "cycle": "first-change", "claim_id": None})
    plan = {"schema_version": accounting.PLAN, "study": "full", "mode": "selective_reuse", "pair_spec_pin": pin,
        "pair_spec_id": accounting.document_id(pair), "candidate_sha256": "a" * 64, "candidate_manifest_pin": pin,
        "host_identity_pin": pin, "input_manifest_pin": pin, "obligation_source_pin": pin, "obligations": obligations,
        "journal_slots": slots, "protected_policy": selective_policy, "claim_schema_version": "evidence-strict-claims/v2",
        "review_producer_id": "sha256:" + "2" * 64, "producer_manifest_pin": pin, "phase_caps": {name: 1 for name in ("setup", "job", "mutation", "owner_read", "global_close", "cleanup")},
        "group_overhead_cap_seconds": 0, "elapsed_cap_seconds": 60, "measured_recipe_pin": pin, "execution_policy": execution,
        "execution_policy_sha256": digest(execution), "observation_policy": observation, "observation_policy_sha256": digest(observation),
        "source_protocol_limits_pin": pin, "frozen_at": begin.isoformat(), "expires_at": (begin + timedelta(hours=6)).isoformat(),
        "control": "reserved_native_attempt_changed_basis_no_dispatch_no_application",
        "scope": "all_six_claims_two_questions_global_policy_sources_computation", "stdin": "closed"}
    return plan, pair


def test_runtime_ships_all_closed_definitions_and_fixed_normalization():
    schemas = accounting.schema_documents()
    assert len(schemas) == 56
    assert {"ControlPhaseCeilingPolicy", "LongPhaseCaps", "LongPlan", "LongReport"} <= schemas.keys()
    assert {"CollectionJournalReceipt", "CollectionPlan", "CollectionReport"} <= schemas.keys()
    assert accounting.document_id(accounting.NORMALIZATION_CONTRACT) == "sha256:2e715b3ce7b3ede7b7aac9c891871dc620654861cacf64d8362959ba5ca4e581"
    assert all(schema["$ref"].startswith("#/$defs/") for schema in schemas.values())


def test_initial_report_preserves_every_unknown_and_every_denominator(cohort, pin):
    plan, pair = cohort
    report = accounting.initialize_report(plan, pin, pair)
    accounting.validate_report(report, plan)
    assert len(report["resolutions"]) == 24
    assert len(report["journal_observations"]) == 13
    assert [row["round"] for row in report["owner_passes"]] == list(accounting.ROUNDS)
    assert report["counters"]["status_counts"]["unstarted"] == 24
    assert report["counters"]["fresh_completed_baseline_applications"] is None
    assert report["counters"]["validated_reused_changed_obligations"] is None
    assert all(row["additional_review_dispatches"] is None for row in report["resolutions"])
    assert all(row["model_calls"] is None for row in [report["cost"]])
    assert sum(row["planned_action"] == "retained_candidate" for row in report["resolutions"]) == 4


@pytest.mark.parametrize("mutation", ["drop_obligation", "duplicate_slot", "drop_owner", "claim_counter_success", "pretend_completed"])
def test_missing_or_self_asserted_completion_never_qualifies(cohort, pin, mutation):
    plan, pair = cohort
    report = accounting.initialize_report(plan, pin, pair)
    if mutation == "drop_obligation":
        report["resolutions"].pop()
    elif mutation == "duplicate_slot":
        report["journal_observations"][-1] = copy.deepcopy(report["journal_observations"][0])
    elif mutation == "drop_owner":
        report["owner_passes"].pop()
    elif mutation == "claim_counter_success":
        report["counters"]["fresh_completed_changed_applications"] = 0
    else:
        report["terminal_outcome"] = "completed"
    with pytest.raises(ValueError):
        accounting.validate_report(report, plan)


@pytest.mark.parametrize("malformed", [b'{"path":"/a","path":"/b","raw_sha256":"' + b'a' * 64 + b'","size_bytes":1}',
                                      b'{"path":"/a","raw_sha256":"' + b'a' * 64 + b'","size_bytes":true}',
                                      b'{"path":"/a","raw_sha256":"' + b'a' * 64 + b'","size_bytes":NaN}'])
def test_closed_codec_rejects_duplicates_boolean_counts_and_nonfinite(malformed):
    with pytest.raises(ValueError):
        accounting.decode("Pin", malformed)


def test_obligation_identifiers_bind_full_service_coordinate(cohort):
    plan, _pair = cohort
    evidence, critic = plan["obligations"][:2]
    assert evidence["obligation_id"] != critic["obligation_id"]
    assert evidence["native_run_id"] == critic["native_run_id"]
    changed = next(row for row in plan["obligations"] if row["coordinate"]["cycle"] == "first-change" and row["coordinate"]["claim_id"] == evidence["coordinate"]["claim_id"] and row["coordinate"]["config_ref"] == "evidence")
    assert changed["baseline_obligation_id"] == evidence["obligation_id"]
    broken = copy.deepcopy(plan)
    broken["obligations"][0]["controller_ref"] = "critic-controller"
    with pytest.raises(ValueError):
        accounting.validate_plan(broken)


def test_empty_fresh_service_selection_never_opens_a_native_session():
    result = drive_selective_services(None, object(), "direct", "run", object(), case_id="independent-claim-text", obligations=[])
    assert result["steps"] == []


def test_raw_dispatch_snapshot_dict_cannot_prove_zero(cohort):
    plan, _pair = cohort
    with pytest.raises(ValueError, match="receipt_not_live"):
        accounting.zero_dispatch(plan, plan["obligations"][-1], {}, {}, {})


def test_completed_mode_and_popen_handoff_cannot_be_minted_from_pins():
    with pytest.raises(ValueError):
        runner._admit_handoff({"closed": True, "cleanup": "confirmed"})
    with pytest.raises(ValueError):
        runner._CompletedMode(None, plan={}, report={"terminal_outcome": "completed"}, runtime=None, root=None, tracker=None,
                              fixture={}, sources={}, native={}, originals=None)


def test_legacy_cli_and_allocation_routes_keep_their_closed_versions():
    assert legacy.PLAN_SCHEMA_EXECUTION == "review-dependency-workload-plan/v4"
    assert legacy.RESULT_SCHEMA_EXECUTION == "review-dependency-workload-observation/v4"
    assert accounting.PLAN == "review-dependency-workload-plan/v5"


def test_offline_report_values_cannot_mint_native_comparison_credit(cohort, pin, tmp_path):
    from tools._review_dependency_workload import OriginalResponses
    selective_plan, pair = cohort
    legacy_plan = copy.deepcopy(selective_plan)
    legacy_plan.update(mode="legacy_full", protected_policy=pair["legacy_policy"])
    plans = {"legacy_full": legacy_plan, "selective_reuse": selective_plan}
    reports = {mode: accounting.initialize_report(plan, pin, pair) for mode, plan in plans.items()}
    for report in reports.values():
        for stage in report["stage_observations"]:
            stage.update(status="returned", native_claim_ids=list(accounting.CLAIMS), accepted_claim_ids=list(accounting.CLAIMS),
                         accepted_original_ids=["capacity", "demand"])
    result = accounting.differential(pair, plans, reports, pair_pin=pin, plan_pins=dict.fromkeys(plans, pin),
        report_pins=dict.fromkeys(plans, pin), originals=OriginalResponses(tmp_path / "comparison"))
    assert result["outcome"] == "unknown"
    assert len(result["claim_joins"]) == 6 and len(result["changed_obligation_joins"]) == 12
    assert result["legacy_changed_completed_applications"] is None
    assert result["avoided_completed_applications"] is None
    assert result["all_current_owners_validated"] is False
    assert all(row["legacy_after_accepted"] is None and row["selective_after_accepted"] is None for row in result["claim_joins"])
    assert all(row["saved_completed_application"] is None for row in result["changed_obligation_joins"])


@pytest.mark.parametrize("profile,hours", [("eight-hour", 8), ("sixteen-hour", 16), ("twenty-four-hour", 24)])
def test_extended_accounting_requires_typed_policy_and_exact_allocation(cohort, profile, hours):
    plan, pair = cohort
    caps = dict(plan["phase_caps"], job=1000, global_close=1500)
    execution = execution_policy(profile)
    plan.update(schema_version=accounting.EXTENDED_PLAN, execution_policy=execution,
        execution_policy_sha256=digest(execution), phase_caps=caps,
        formal_application_policy=legacy.formal_application_policy(1000),
        pair_closing_reservation=accounting.extended_closing_reservation(caps["global_close"]),
        expires_at=(datetime.fromisoformat(plan["frozen_at"]) + timedelta(hours=hours)).isoformat(),
        elapsed_cap_seconds=legacy.allocation_seconds(caps, 24) + 2 * caps["global_close"])
    accounting.validate_plan(plan, pair)
    report = accounting.initialize_report(plan, plan["pair_spec_pin"], pair)
    assert report["schema_version"] == accounting.EXTENDED_REPORT
    report["cost"]["elapsed_seconds"] = 22000
    accounting.validate_report(report, plan)
    changed = copy.deepcopy(plan)
    changed["elapsed_cap_seconds"] += 1
    with pytest.raises(ValueError, match="accounting_allocation_invalid"):
        accounting.validate_plan(changed)
    changed = copy.deepcopy(plan)
    changed["formal_application_policy"]["application_lease"]["seconds"] -= 1
    with pytest.raises(ValueError, match="workload_application_policy_invalid"):
        accounting.validate_plan(changed)
    changed = copy.deepcopy(plan)
    changed["schema_version"] = accounting.PAIR_PLAN
    with pytest.raises(ValueError):
        accounting.validate_plan(changed)


def test_extended_schema_keeps_old_caps_and_costs_closed():
    schemas = accounting.schema_documents()
    assert schemas["Plan"]["$defs"]["PhaseCaps"]["properties"]["job"]["maximum"] == 600
    assert schemas["PairPlan"]["$defs"]["PhaseCaps"]["properties"]["job"]["maximum"] == 600
    assert schemas["ExtendedPlan"]["$defs"]["ExtendedPhaseCaps"]["properties"]["job"]["maximum"] == 3600
    assert schemas["ExtendedCost"]["$defs"]["ExtendedCost"]["properties"]["elapsed_seconds"]["anyOf"][0]["maximum"] == 86400
    assert schemas["Cost"]["$defs"]["Cost"]["properties"]["elapsed_seconds"]["anyOf"][0]["maximum"] == 21600


@pytest.mark.parametrize("selected_run", [None, "last"])
@pytest.mark.parametrize("failure", [None, "current", "qualification"])
def test_current_owner_binds_each_actual_session_and_unwinds(monkeypatch, selected_run, failure):
    from contextlib import contextmanager, nullcontext
    from types import SimpleNamespace

    import evidence_wiki
    from evidence_wiki import _review_dependency_inputs as inputs
    from evidence_wiki import execution_session as native

    active, sessions, closed, bound, authenticated, current_calls = [], [], [], [], [], []
    live_qualification = {"state": "qualified"}

    @contextmanager
    def session_factory(*args, run_id, **kwargs):
        assert not active
        session = SimpleNamespace(run_id=run_id)
        sessions.append(session)
        try:
            yield session
        finally:
            assert not active
            closed.append(session)

    @contextmanager
    def bind(session):
        assert not active
        active.append(session)
        bound.append(session)
        try:
            yield session
        finally:
            assert active.pop() is session

    def global_read():
        assert authenticated == ["first", "last"]
        assert active == ([] if selected_run is None else [sessions[0]])
        return {"global": "unchanged"}

    @contextmanager
    def facts(session, *, operation):
        assert operation == "current" and active == [session]
        current_calls.append(session)
        yield
        assert active == [session]

    def current(session):
        assert active == [session] and current_calls[-1] is session
        if failure == "current":
            raise RuntimeError("current owner refused")
        return {"run_id": session.run_id, "qualification": {"state": "qualified"}}

    def witness(session):
        assert active == [session]
        if failure == "qualification":
            live_qualification["state"] = "unknown"
        return {"run_id": session.run_id, "observed_utc": datetime.now(timezone.utc).isoformat()}

    def journal(_root, _runtime, run_id, _originals):
        authenticated.append(run_id)
        return [], {"run_id": run_id}, {"run_id": run_id}, None

    workspace = SimpleNamespace(research=SimpleNamespace(session=session_factory),
        strict=SimpleNamespace(check=global_read, export=global_read))
    monkeypatch.setattr(legacy, "Workspace", SimpleNamespace(open=lambda root: nullcontext(workspace)))
    monkeypatch.setattr(legacy, "authenticated_journal", journal)
    monkeypatch.setattr(inputs, "bind_session", bind)
    monkeypatch.setattr(native, "_current_facts", facts)
    monkeypatch.setattr(native, "_current_view", current)
    monkeypatch.setattr(native, "current_qualifications", lambda session: dict(live_qualification))
    monkeypatch.setattr(evidence_wiki, "host_qualification", witness)
    runtime = SimpleNamespace(context=SimpleNamespace(activate=nullcontext), policy=object(), execution=object())
    if failure is not None:
        expected = RuntimeError if failure == "current" else ValueError
        with pytest.raises(expected, match="current owner refused" if failure == "current" else "workload_owner_current_unavailable"):
            legacy._owner_read("inert-root", runtime, ["first", "last"], [], selective_run_id=selected_run)
    else:
        result, _, _ = legacy._owner_read("inert-root", runtime, ["first", "last"], [], selective_run_id=selected_run)
        assert [row["run_id"] for row in result["native_current"]] == ["first", "last"]
    assert not active and closed == sessions
    assert bound == sessions
    assert [session.run_id for session in current_calls] == (["first", "last"] if failure is None else ["first"])
    assert len({id(session) for session in sessions}) == len(sessions)


@pytest.mark.parametrize("context_version", ["complete", "dependencies"], ids=["legacy", "dependencies"])
def test_actual_session_input_holder_admission_and_cleanup_without_authority(tmp_path, monkeypatch, context_version):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from evidence_wiki import _review_dependency_inputs as inputs
    from evidence_wiki import host_context, mutation_operations
    from evidence_wiki.host_context import HostContext
    from evidence_wiki.research_execution import ResearchExecution
    from evidence_wiki.research_session import ResearchSession
    from evidence_wiki.workflow_contracts import WorkflowContractError

    # Only holder admission is real; execution provenance and policy are inert
    # collaborators. No runtime, credential, journal or qualification is minted.
    (tmp_path / "research.yml").write_text("controlled: true\n")
    context = HostContext((tmp_path,), frozenset(), "no-authority-observer")
    execution = ResearchExecution.__new__(ResearchExecution)
    execution._profile = SimpleNamespace(review_delivery=SimpleNamespace(context_version=context_version),
        document=lambda: {"context_version": context_version})
    session = ResearchSession.__new__(ResearchSession)
    session._root, session._context, session._execution = tmp_path, context, execution
    session._policy = SimpleNamespace(to_dict=lambda: {"scope": "unqualified-holder-test"})
    session._closed, session._owner_workspace = False, None
    monkeypatch.setattr(mutation_operations, "_generation", lambda: "controlled-generation")
    monkeypatch.setattr(mutation_operations, "_host_selection", lambda: "controlled-host-selection")
    monkeypatch.setattr(ResearchExecution, "identity", lambda self: {"identity": "unqualified-controlled-collaborator"})
    producer_calls = []

    @contextmanager
    def producer_binding():
        producer_calls.append("enter")
        try:
            yield None
        finally:
            producer_calls.append("exit")

    monkeypatch.setattr(host_context, "review_producer_binding", producer_binding)
    with context.activate():
        assert inputs.current_inputs() is None
        with inputs.bind_session(session) as holder:
            assert type(holder) is inputs._Inputs and holder._session is session
            assert inputs.current_inputs() is holder
            inputs.assert_current(holder)
            with inputs.bind_session(session) as nested:
                assert nested is holder
        assert inputs.current_inputs() is None and not holder._valid
        with pytest.raises(WorkflowContractError) as caught:
            inputs.assert_current(holder)
        assert caught.value.error_code.endswith("REVIEW_DEPENDENCY_INPUTS_CHANGED")
    assert producer_calls == (["enter", "exit"] if context_version == "dependencies" else [])
    assert context.authority_file is None and not context.grants


@pytest.mark.parametrize("outcome", ["ready", "nonready", "current", "current_exit", "bind_enter", "bind_exit"])
def test_host_preflight_binding_lifetime_and_refusal_retention(monkeypatch, outcome):
    from contextlib import contextmanager, nullcontext
    from types import SimpleNamespace

    import evidence_wiki
    from evidence_wiki import _review_dependency_inputs as inputs
    from evidence_wiki import execution_session as native
    from evidence_wiki.errors import ConfigError
    from tools import _review_dependency_workload as workload

    active, events, retained = [], [], []
    session = SimpleNamespace(run_id="fixture-read-preflight")
    witness = {"credentials": {"state": "unknown" if outcome == "nonready" else "ready"},
        "authorized": False, "evidence_accepted": False}

    def refusal():
        return ConfigError("HOST_CONTEXT_REFUSED", "private controlled failure",
            details={"reason": "host_operation_not_granted", "private": "credential-secret"})

    @contextmanager
    def session_factory(*args, run_id, **kwargs):
        assert run_id == session.run_id and not active
        try:
            yield session
        finally:
            assert not active
            events.append("session_exit")

    @contextmanager
    def bind(actual):
        assert actual is session and not active
        events.append("bind_enter")
        if outcome == "bind_enter":
            raise refusal()
        active.append(actual)
        try:
            yield
        finally:
            assert active.pop() is session
            events.append("bind_exit")
            if outcome == "bind_exit":
                raise refusal()

    @contextmanager
    def facts(actual, *, operation):
        assert actual is session and operation == "current" and active == [session]
        events.append("current_enter")
        if outcome == "current":
            raise refusal()
        yield
        events.append("current_exit")
        if outcome == "current_exit":
            raise refusal()

    def qualify(actual):
        assert actual is session and active == [session]
        events.append("witness")
        return witness

    workspace = SimpleNamespace(research=SimpleNamespace(session=session_factory))
    runtime = SimpleNamespace(context=SimpleNamespace(activate=nullcontext), policy=object(), execution=object())
    monkeypatch.setattr(workload, "Workspace", SimpleNamespace(open=lambda root: nullcontext(workspace)))
    monkeypatch.setattr(inputs, "bind_session", bind)
    monkeypatch.setattr(native, "_current_facts", facts)
    monkeypatch.setattr(evidence_wiki, "host_qualification", qualify)
    if outcome == "ready":
        workload.host_read_preflight("inert-root", runtime, retained)
    else:
        expected = ValueError if outcome == "nonready" else ConfigError
        with pytest.raises(expected):
            workload.host_read_preflight("inert-root", runtime, retained)
    assert not active and events[-1] == "session_exit"
    if outcome in {"ready", "nonready"}:
        assert retained == [("host-read-preflight", witness)]
        assert events.index("current_exit") < events.index("bind_exit")
    else:
        assert [label for label, _ in retained] == ["host-read-preflight-failure"]
        failure = retained[0][1]
        assert failure["error_code"] == "HOST_CONTEXT_REFUSED"
        assert failure["reason"] == "host_operation_not_granted"
        assert "credential-secret" not in str(retained) and "private controlled" not in str(retained)


@pytest.fixture
def helper_reuse_producer_guard(monkeypatch, request):
    import hashlib
    from types import SimpleNamespace

    from evidence_wiki import _review_producer
    from tools import _review_budget_source as source

    paths = {
        "package/evidence_wiki/research_journal_contracts.py": "src/evidence_wiki/research_journal_contracts.py",
        "assets/workspace-template/scripts/_evidence_policies.py": "workspace-template/scripts/_evidence_policies.py"}
    protocol = getattr(request, "param", "review-directory-budget-source/v5")
    if protocol in {"review-directory-budget-source/v6", "review-directory-budget-source/v7"}:
        paths["package/evidence_wiki/research_journal_collections.py"] = "src/evidence_wiki/research_journal_collections.py"
    if protocol == "review-directory-budget-source/v7":
        paths["package/evidence_wiki/_review_dependency_custody.py"] = "src/evidence_wiki/_review_dependency_custody.py"
        paths["assets/workspace-template/scripts/_strict_evidence.py"] = "workspace-template/scripts/_strict_evidence.py"
    historical, current = {}, {}
    old_rows, new_rows = [], []
    for member, path in paths.items():
        for manifest, rows, raw in ((historical, old_rows, (path + " old").encode()),
                                   (current, new_rows, (path + " current").encode())):
            digest = hashlib.sha256(raw).hexdigest()
            manifest[path] = {"sha256": digest, "bytes": len(raw)}
            rows.append({"path": member, "sha256": "sha256:" + digest, "size_bytes": len(raw)})
    unchanged = {"path": "package/evidence_wiki/unchanged.py", "sha256": "sha256:" + "1" * 64, "size_bytes": 17}
    before = {"schema_version": "evidence-review-producer-manifest/v1", "members": sorted([*old_rows, unchanged], key=lambda row: row["path"])}
    after = {"schema_version": "evidence-review-producer-manifest/v1", "members": sorted([*new_rows, copy.deepcopy(unchanged)], key=lambda row: row["path"])}

    def identity(manifest):
        return _review_producer.content_id("evidence-review-producer/v1", {
            "schema_version": "evidence-review-producer/v1",
            "manifest_id": _review_producer.content_id("evidence-review-producer-manifest/v1", manifest)})

    binding = {"schema_version": protocol, "qualification": False,
        "historical_producer_manifest_pin": {"path": "explicit-historical-manifest-stub"},
        "current_producer_manifest_pin": {"path": "explicit-current-manifest-stub"}}
    manifests = {binding["historical_producer_manifest_pin"]["path"]: before,
                 binding["current_producer_manifest_pin"]["path"]: after}
    monkeypatch.setattr(runner, "_read", lambda pin: copy.deepcopy(manifests[pin["path"]]))
    recipe = {"review_producer_id": identity(before), "retained_original": "unchanged"}

    def invoke(*, producer_id=None, document=None):
        generation = SimpleNamespace(producer_id=identity(after) if producer_id is None else producer_id,
            document=lambda: {"manifest": copy.deepcopy(after if document is None else document)})
        return source._current_producer(binding, recipe, historical, current, generation)

    return SimpleNamespace(invoke=invoke, binding=binding, recipe=recipe, before=before, after=after,
        historical=historical, current=current, paths=paths, identity=identity)


def test_helper_reuse_producer_guard_accepts_exact_two_changes_without_relabeling_history(helper_reuse_producer_guard):
    fixture = helper_reuse_producer_guard
    saved = copy.deepcopy((fixture.binding, fixture.recipe, fixture.before, fixture.after,
        fixture.historical, fixture.current))
    assert fixture.invoke() == fixture.identity(fixture.after)
    assert (fixture.binding, fixture.recipe, fixture.before, fixture.after,
        fixture.historical, fixture.current) == saved
    assert fixture.binding["qualification"] is False
    fixture.binding["schema_version"] = "review-directory-budget-source/v4"
    with pytest.raises(ValueError, match="review_budget_source_incompatible"):
        fixture.invoke()


@pytest.mark.parametrize("change", ["missing_policy", "unchanged_policy", "added_unrelated_member",
    "changed_unrelated_member", "source_hash", "source_bytes", "historical_identity", "current_identity", "generation_document"])
def test_helper_reuse_producer_guard_refuses_every_undeclared_or_mismatched_binding(helper_reuse_producer_guard, change):
    fixture = helper_reuse_producer_guard
    policy_member = "assets/workspace-template/scripts/_evidence_policies.py"
    policy_source = fixture.paths[policy_member]
    if change == "missing_policy":
        fixture.after["members"][:] = [row for row in fixture.after["members"] if row["path"] != policy_member]
    elif change == "unchanged_policy":
        old = next(row for row in fixture.before["members"] if row["path"] == policy_member)
        index = next(i for i, row in enumerate(fixture.after["members"]) if row["path"] == policy_member)
        fixture.after["members"][index] = copy.deepcopy(old)
    elif change == "added_unrelated_member":
        fixture.after["members"].append({"path": "package/evidence_wiki/extra.py", "sha256": "sha256:" + "2" * 64, "size_bytes": 7})
    elif change == "changed_unrelated_member":
        fixture.after["members"][-1]["sha256"] = "sha256:" + "2" * 64
    elif change == "source_hash":
        fixture.current[policy_source]["sha256"] = "0" * 64
    elif change == "source_bytes":
        fixture.current[policy_source]["bytes"] += 1
    elif change == "historical_identity":
        fixture.recipe["review_producer_id"] = fixture.identity(fixture.after)
    arguments = ({"producer_id": fixture.identity(fixture.before)} if change == "current_identity"
        else {"document": fixture.before} if change == "generation_document" else {})
    with pytest.raises(ValueError, match="review_budget_source_incompatible"):
        fixture.invoke(**arguments)


@pytest.mark.parametrize("helper_reuse_producer_guard", ["review-directory-budget-source/v6"], indirect=True)
def test_preparation_producer_guard_requires_exact_three_declared_members(helper_reuse_producer_guard):
    fixture = helper_reuse_producer_guard
    saved = copy.deepcopy((fixture.recipe, fixture.before, fixture.after, fixture.historical, fixture.current))
    assert fixture.invoke() == fixture.identity(fixture.after)
    assert (fixture.recipe, fixture.before, fixture.after, fixture.historical, fixture.current) == saved
    fixture.binding["schema_version"] = "review-directory-budget-source/v5"
    with pytest.raises(ValueError, match="review_budget_source_incompatible"):
        fixture.invoke()


@pytest.mark.parametrize("helper_reuse_producer_guard", ["review-directory-budget-source/v6"], indirect=True)
@pytest.mark.parametrize("change", ["missing", "unchanged", "source_hash", "source_bytes", "unrelated"])
def test_preparation_producer_guard_rejects_unbound_collection_changes(helper_reuse_producer_guard, change):
    fixture = helper_reuse_producer_guard
    member = "package/evidence_wiki/research_journal_collections.py"
    source = fixture.paths[member]
    if change == "missing":
        fixture.after["members"][:] = [row for row in fixture.after["members"] if row["path"] != member]
    elif change == "unchanged":
        old = next(row for row in fixture.before["members"] if row["path"] == member)
        index = next(i for i, row in enumerate(fixture.after["members"]) if row["path"] == member)
        fixture.after["members"][index] = copy.deepcopy(old)
    elif change == "source_hash":
        fixture.current[source]["sha256"] = "0" * 64
    elif change == "source_bytes":
        fixture.current[source]["bytes"] += 1
    else:
        fixture.after["members"].append({"path": "package/evidence_wiki/unrelated.py", "sha256": "sha256:" + "2" * 64, "size_bytes": 1})
    with pytest.raises(ValueError, match="review_budget_source_incompatible"):
        fixture.invoke()


def test_preparation_source_guard_preserves_other_definitions_and_import_bindings():
    from tools import _review_budget_source as source

    path = "src/evidence_wiki/research_journal_collections.py"
    old = 'from .research_journal_contracts import canonical_record, detached_record, expanded_size\nLIMIT=17\ndef _prepared(value):\n return value\ndef other(value):\n return value\n'
    current = old.replace('canonical_record, detached_record', '_canonical_owned_record').replace('def _prepared(value):\n return value', 'def _prepared(value):\n return _canonical_owned_record(value)')
    key = lambda value: source._unchanged_module(value, path, source._PREPARATION_RUNTIME_EDITS)
    assert key(current) == key(old)
    for changed in (current.replace('LIMIT=17', 'LIMIT=18'),
                    current.replace('def other(value):\n return value', 'def other(value):\n return None'),
                    current.replace('_canonical_owned_record, expanded_size', '_canonical_owned_record as replacement, expanded_size')):
        assert key(changed) != key(old)


@pytest.mark.parametrize("helper_reuse_producer_guard", ["review-directory-budget-source/v7"], indirect=True)
def test_custody_producer_guard_requires_exact_five_declared_members(helper_reuse_producer_guard):
    fixture = helper_reuse_producer_guard
    saved = copy.deepcopy((fixture.recipe, fixture.before, fixture.after, fixture.historical, fixture.current))
    assert fixture.invoke() == fixture.identity(fixture.after)
    assert (fixture.recipe, fixture.before, fixture.after, fixture.historical, fixture.current) == saved
    fixture.binding["schema_version"] = "review-directory-budget-source/v6"
    with pytest.raises(ValueError, match="review_budget_source_incompatible"):
        fixture.invoke()


@pytest.mark.parametrize("helper_reuse_producer_guard", ["review-directory-budget-source/v7"], indirect=True)
@pytest.mark.parametrize("member", ["package/evidence_wiki/_review_dependency_custody.py", "assets/workspace-template/scripts/_strict_evidence.py"])
@pytest.mark.parametrize("change", ["missing", "unchanged", "source_hash", "source_bytes", "unrelated"])
def test_custody_producer_guard_refuses_undeclared_or_mismatched_changes(helper_reuse_producer_guard, member, change):
    fixture = helper_reuse_producer_guard
    source = fixture.paths[member]
    if change == "missing":
        fixture.after["members"][:] = [row for row in fixture.after["members"] if row["path"] != member]
    elif change == "unchanged":
        old = next(row for row in fixture.before["members"] if row["path"] == member)
        index = next(i for i, row in enumerate(fixture.after["members"]) if row["path"] == member)
        fixture.after["members"][index] = copy.deepcopy(old)
    elif change == "source_hash":
        fixture.current[source]["sha256"] = "0" * 64
    elif change == "source_bytes":
        fixture.current[source]["bytes"] += 1
    else:
        fixture.after["members"].append({"path": "package/evidence_wiki/unrelated.py", "sha256": "sha256:" + "2" * 64, "size_bytes": 1})
    with pytest.raises(ValueError, match="review_budget_source_incompatible"):
        fixture.invoke()


def test_custody_source_guard_changes_only_the_named_method():
    from tools import _review_budget_source as source

    path = "src/evidence_wiki/_review_dependency_custody.py"
    old = 'LIMIT=17\nclass _Original:\n def assert_current(self):\n  return True\n def original_snapshot(self):\n  self.assert_current()\n  return {}\n\ndef resolve_original():\n return None\n'
    current = old.replace('\ndef resolve_original', ' def original_material(self, envelope):\n  self.assert_current()\n  return {}, {}\n\ndef resolve_original')
    key = lambda value: source._unchanged_module(value, path, source._CUSTODY_RUNTIME_EDITS)
    assert key(current) == key(old)
    for changed in (current.replace('LIMIT=17', 'LIMIT=18'),
                    current.replace('return True', 'return False'),
                    current.replace('return {}', 'return None', 1),
                    current.replace('class _Original:', 'class _Original(object):'),
                    current.replace('def resolve_original():\n return None', 'def resolve_original():\n return True')):
        assert key(changed) != key(old)
