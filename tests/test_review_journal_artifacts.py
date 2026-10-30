"""Historical collection byte proofs remain distinct from current native authority."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki.errors import UsageError
from evidence_wiki.research_journal_contracts import (
    JOURNAL,
    canonical_record,
    canonical_records,
    detached_record,
    digest_record,
)
from tools import _review_dependency_accounting as accounting
from tools import _review_dependency_pilot as pilot
from tools import _review_dependency_runner as runner
from tools import _review_dependency_workload as native_tools
from tools import run_review_dependency_workload as workload
from tools._review_journal_artifacts import POLICY, RECEIPT, page_directory, raw_pin, read_collection, verify_receipt
from tools.replay_qualification_native import PAYLOAD_BYTES, journal_snapshot, verify_journal_closing


@pytest.fixture(autouse=True)
def no_native_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("historical collection tests cannot launch processes")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("os.system", forbidden)


def record(index=0, data=None):
    return {"schema_version": JOURNAL, "kind": "execution_native", "binding_id": "sha256:" + "1" * 64,
        "request_id": "request", "sequence": index, "previous_record_ref": None,
        "data": {"value": "café"} if data is None else data, "authentication": None,
        "event_ref": f"event-{index:04d}"}


def large_records():
    return [record(index, {"chunks": [str(index) + "x" * 59999] * 5}) for index in range(4)]


def retained(tmp_path, records=None):
    from tests._usage_fixture import require_reference_storage
    require_reference_storage()
    records = large_records() if records is None else records
    originals = native_tools.OriginalResponses(tmp_path / "observations", journal_collection=True)
    pin = originals.append_records("run-authenticated", records)
    return originals, pin, records


def receipt_for(pin, records):
    _, metadata = read_collection(pin)
    return {"schema_version": RECEIPT, "run_id": "run", **{key: metadata[key] for key in
        ("records_sha256", "records_bytes", "record_count", "physical_nodes", "expanded_bytes", "artifact_kind", "page_pins")},
        "head_ref": digest_record(records[-1]) if records else None, "source_sha256": "2" * 64,
        "state_sha256": "3" * 64, "original_pin": pin}


def test_small_writer_retains_legacy_bytes_and_explicit_artifact_metadata(tmp_path):
    rows = [record()]
    originals, pin, _ = retained(tmp_path, rows)
    assert Path(pin["path"]).read_bytes() == canonical_records(rows)
    assert len(originals.pins) == 1
    decoded, metadata = workload.read_pinned_native_records(pin)
    assert decoded == rows and metadata["artifact_kind"] == "legacy_array"
    assert metadata["artifact_bytes"] == metadata["records_bytes"] == pin["size_bytes"]
    assert metadata["page_pins"] == [] and metadata["page_inventory"] == []
    assert not page_directory(Path(pin["path"])).exists()


def test_legacy_writer_still_refuses_large_collection_and_exports_safe_cause(tmp_path):
    originals = native_tools.OriginalResponses(tmp_path)
    with pytest.raises(UsageError) as refused:
        originals.append_records("run", large_records())
    assert refused.value.error_code == "MUTATION_LIMIT"
    failure = native_tools.failure_document(refused.value)
    assert failure["error_code"] == "MUTATION_LIMIT"
    assert failure["collection"]["records_bytes"] > 1_048_576
    assert failure["collection_stage"] == "legacy_collection_encode"
    from evidence_wiki import mutation_contracts

    source = native_tools.ROOT / "src/evidence_wiki/mutation_contracts.py"
    installed = Path(mutation_contracts.__file__).resolve() != source
    expected = ("tools/_review_dependency_workload.py", "_append_records") if installed else (
        "src/evidence_wiki/mutation_contracts.py", "refuse")
    assert (failure["frames"][-1]["path"], failure["frames"][-1]["function"]) == expected
    assert all(set(frame) == {"path", "function", "line"} and type(frame["line"]) is int
               and frame["line"] > 0 for frame in failure["frames"])
    assert not originals.pins and not list(tmp_path.iterdir())


def test_paged_writer_reader_binds_complete_hash_and_every_original_page(tmp_path):
    originals, pin, rows = retained(tmp_path)
    decoded, metadata = workload.read_pinned_native_records(pin)
    whole = b"[" + b",".join(canonical_record(row) for row in rows) + b"]"
    assert decoded == rows and metadata["artifact_kind"] == "paged_collection"
    assert metadata["records_sha256"] == "sha256:" + hashlib.sha256(whole).hexdigest()
    assert metadata["records_bytes"] == len(whole) > 1_048_576
    assert metadata["artifact_bytes"] == pin["size_bytes"] < 1_048_576
    assert metadata["artifact_sha256"] == "sha256:" + pin["raw_sha256"] != metadata["records_sha256"]
    assert len(metadata["page_pins"]) == 2 and len(originals.pins) == 3
    receipt = receipt_for(pin, rows)
    assert accounting.validate("JournalReceipt", receipt) == receipt
    assert verify_receipt(receipt, original_pins=originals.pins)[0] == rows
    with pytest.raises(ValueError, match="workload_pilot_measurement_invalid"):
        verify_receipt(receipt, original_pins=originals.pins[:-1])


@pytest.mark.parametrize("change", ["missing", "extra", "tampered", "reordered", "stale_pin", "oversize", "symlink", "public"])
def test_page_custody_changes_are_refused(tmp_path, change):
    originals, pin, rows = retained(tmp_path)
    directory = page_directory(Path(pin["path"]))
    page = directory / "0000.json"
    if change == "missing":
        page.unlink()
    elif change == "extra":
        (directory / "0002.json").write_bytes(b"[]")
    elif change == "reordered":
        other = directory / "0001.json"
        first, second = page.read_bytes(), other.read_bytes()
        page.chmod(0o600); other.chmod(0o600)
        page.write_bytes(second); other.write_bytes(first)
    elif change == "symlink":
        target = tmp_path / "copy"
        target.write_bytes(page.read_bytes())
        target.chmod(0o400)
        page.unlink(); page.symlink_to(target)
    elif change == "public":
        page.chmod(0o644)
    elif change == "stale_pin":
        pin["raw_sha256"] = "0" * 64
    else:
        page.chmod(0o600)
        page.write_bytes(b"x" * 1_048_577 if change == "oversize" else page.read_bytes()[:-1])
    with pytest.raises((ValueError, OSError, UsageError)):
        workload.read_pinned_native_records(pin)


@pytest.mark.parametrize("field", ["records_sha256", "records_bytes", "physical_nodes", "expanded_bytes", "page_pins", "head_ref", "unexpected"])
def test_receipt_cannot_substitute_manifest_hash_or_hide_inventory(tmp_path, field):
    originals, pin, rows = retained(tmp_path)
    receipt = receipt_for(pin, rows)
    if field == "records_sha256":
        receipt[field] = "sha256:" + pin["raw_sha256"]
    elif field == "page_pins":
        receipt[field] = receipt[field][:-1]
    elif field == "head_ref":
        receipt[field] = "sha256:" + "0" * 64
    elif field == "unexpected":
        receipt[field] = True
    else:
        receipt[field] += 1
    with pytest.raises(ValueError):
        verify_receipt(receipt, original_pins=originals.pins)


def test_authenticated_tool_capture_selects_codec_after_original_records_owner(tmp_path, monkeypatch):
    from contextlib import nullcontext
    rows = large_records()
    originals = native_tools.OriginalResponses(tmp_path / "observations", journal_collection=True)
    session = SimpleNamespace(current=lambda: {"current": True})
    workspace = SimpleNamespace(research=SimpleNamespace(session=lambda *args, **kwargs: nullcontext(session)))
    runtime = SimpleNamespace(context=SimpleNamespace(activate=lambda: nullcontext()), policy=object(),
        execution=SimpleNamespace(records=lambda selected: rows))
    monkeypatch.setattr(native_tools.Workspace, "open", lambda root: nullcontext(workspace))
    import tools.replay_qualification_native as snapshots
    seen = []
    def capture(selected, records, *, journal_collection):
        assert records is rows and selected is session and journal_collection is True
        seen.append("source")
        return {"source_sha256": "2" * 64, "state_sha256": "3" * 64}
    monkeypatch.setattr(snapshots, "journal_snapshot", capture)
    result, receipt, snapshot, current = native_tools.authenticated_journal(tmp_path, runtime, "run", originals, current=True)
    assert result is rows and receipt["schema_version"] == RECEIPT and current == {"current": True}
    assert verify_receipt(receipt, original_pins=originals.pins)[0] == rows
    assert seen == ["source"]


def test_driver_retention_does_not_replace_primary_error(tmp_path):
    originals = native_tools.OriginalResponses(tmp_path)
    primary = ValueError("private source body")
    with pytest.raises(ValueError) as raised:
        try:
            raise primary
        finally:
            originals.finally_records("run", SimpleNamespace(records=lambda session: large_records()), object())
    assert raised.value is primary
    failure = native_tools.failure_document(primary)
    assert failure["retention_failure"]["error_code"] == "MUTATION_LIMIT"
    assert "private source body" not in json.dumps(failure)


def test_snapshot_normalizes_raw_wire_records_without_claiming_authentication(tmp_path):
    from evidence_wiki._review_dependency_wire import pack
    physical = record(data={"schema_version": "evidence-strict-authorship/v2", "basis_id": "sha256:" + "1" * 64,
        "claim_id": "claim", "claim_schema_version": "evidence-strict-claims/v1",
        "observed_workspace_basis_id": "sha256:" + "2" * 64, "semantic_material_id": "sha256:" + "3" * 64})
    physical["data"] = pack(physical["data"], "journal_data")
    runtime = detached_record(physical)
    assert physical != runtime and canonical_record(physical) == canonical_record(runtime)
    directory = tmp_path / "runs/run"
    directory.mkdir(parents=True, mode=0o700)
    event = {"event_id": physical.pop("event_ref"), "data": {"record": physical}}
    source = (json.dumps(event) + "\n").encode()
    (directory / "events.jsonl").write_bytes(source)
    (directory / "events.jsonl").chmod(0o600)
    (directory / "run-state.json").write_bytes(b"{}")
    (directory / "run-state.json").chmod(0o600)
    session = SimpleNamespace(root=tmp_path, run_id="run")
    captured = journal_snapshot(session, [runtime], journal_collection=True)
    assert captured["source_sha256"] == hashlib.sha256(source).hexdigest()
    assert captured["events"] == [event] and captured["source_bytes_bound"] == 16_777_216
    assert PAYLOAD_BYTES == 4_194_304
    verify_journal_closing([captured])
    with pytest.raises(ValueError, match="changed"):
        journal_snapshot(session, [{**runtime, "sequence": 1}], journal_collection=True)

# Reuse inert preparation builders; no fixture owner or worker is entered.
from tests.test_review_dependency_workload import plan as plan
from tests.test_selective_review_workload import cohort as cohort
from tests.test_selective_review_workload import pin as pin


def test_native_and_envelope_plans_require_explicit_prospective_collection_version(plan):
    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    selected = workload.reference_plan(bundle, observation_profile="batch", execution_profile="six-hour", journal_collection=True)
    assert selected["schema_version"] == workload.PLAN_SCHEMA_COLLECTION
    assert selected["journal_collection_policy"] == POLICY
    assert workload.validate_plan(selected, native_tools.digest(selected)) == selected
    old = workload.reference_plan(bundle, observation_profile="batch", execution_profile="six-hour")
    assert "journal_collection_policy" not in old and old["schema_version"] == workload.PLAN_SCHEMA_EXECUTION
    for change in ("remove", "retag", "alter"):
        changed = deepcopy(selected)
        if change == "remove":
            del changed["journal_collection_policy"]
        elif change == "retag":
            changed["schema_version"] = old["schema_version"]
        else:
            changed["journal_collection_policy"]["page_bytes"] += 1
        with pytest.raises(ValueError):
            workload.validate_plan(changed, native_tools.digest(changed))
    envelope = pilot.reference_plan(bundle, execution_profile="eight-hour", service_profile="extended-observational", journal_collection=True)
    assert envelope["schema_version"] == pilot.COLLECTION_OBSERVATIONAL_PLAN
    assert envelope["native_plan"]["schema_version"] == workload.PLAN_SCHEMA_OBSERVATIONAL_COLLECTION
    assert pilot.validate_plan(envelope, native_tools.digest(envelope)) == envelope
    assert workload.report_schema(envelope["native_plan"]) == workload.RESULT_SCHEMA_OBSERVATIONAL_COLLECTION
    changed = deepcopy(envelope)
    changed["schema_version"] = pilot.OBSERVATIONAL_PLAN
    with pytest.raises(ValueError):
        pilot.validate_plan(changed, native_tools.digest(changed))


def collection_formal_plan(cohort):
    from datetime import datetime, timedelta, timezone

    from tools._execution_budget import execution_policy
    plan, pair = deepcopy(cohort)
    execution = execution_policy("eight-hour")
    caps = {"setup": 1, "job": 1, "mutation": 1, "owner_read": 1, "global_close": 1500, "cleanup": 1}
    frozen = datetime.now(timezone.utc)
    plan.update(schema_version=accounting.COLLECTION_PLAN, journal_collection_policy=deepcopy(POLICY),
        execution_policy=execution, execution_policy_sha256=native_tools.digest(execution), phase_caps=caps,
        frozen_at=frozen.isoformat(), expires_at=(frozen + timedelta(seconds=execution["authority_seconds"])).isoformat(),
        formal_application_policy=workload.formal_application_policy(caps["job"]),
        elapsed_cap_seconds=workload.allocation_seconds(caps, 24) + 2 * caps["global_close"])
    plan["pair_closing_reservation"] = accounting.extended_closing_reservation(caps["global_close"])
    return plan, pair


def test_collection_formal_schema_and_report_keep_legacy_bound_closed(cohort):
    plan, pair = collection_formal_plan(cohort)
    assert accounting.validate_plan(plan, pair) == plan
    report = accounting.initialize_report(plan, plan["pair_spec_pin"], pair)
    assert report["schema_version"] == accounting.COLLECTION_REPORT and report["journal_collection_policy"] == POLICY
    assert accounting.validate_report(report, plan) == report
    for changed in (dict(plan, schema_version=accounting.EXTENDED_PLAN), {key: value for key, value in plan.items() if key != "journal_collection_policy"}):
        with pytest.raises(ValueError):
            accounting.validate_plan(changed)
    old_definition = accounting.schema_documents()["JournalReceipt"]["$defs"]["JournalReceipt"]
    assert old_definition["properties"]["records_bytes"]["maximum"] == 1_048_576
    assert "schema_version" not in old_definition["properties"]


def test_dispatch_custody_keeps_complete_collection_identity_outside_generic_document_bound(tmp_path, cohort):
    originals, pin, rows = retained(tmp_path)
    from contextlib import nullcontext
    class Executor:
        _lock = nullcontext()
        _attempts = {}
    executor = Executor()
    tracker = SimpleNamespace(originals=originals)
    # Admission mint is private; this isolates immutable custody only.
    document = {"fixture": "dispatch"}
    original_validate = accounting.validate
    try:
        accounting.validate = lambda name, value: deepcopy(value)
        observation = accounting._DispatchObservation(accounting._DISPATCH_MINT, document, pin,
            tracker=tracker, executor=executor, attempts=[], journals={"run": rows})
    finally:
        accounting.validate = original_validate
    observation.assert_original()
    observation.journals["run"][0]["data"]["chunks"][0] = "changed"
    with pytest.raises(ValueError, match="accounting_dispatch_custody_changed"):
        observation.assert_original()


def test_new_receipt_selection_is_forwarded_to_retained_native_owner(monkeypatch):
    from contextlib import nullcontext

    from evidence_wiki import execution_session
    called = []
    runtime = SimpleNamespace(context=SimpleNamespace(activate=lambda: nullcontext()), policy=object(), execution=object())
    session = object()
    workspace = SimpleNamespace(research=SimpleNamespace(session=lambda *args, **kwargs: nullcontext(session)))
    monkeypatch.setattr(runner.Workspace, "open", lambda root: nullcontext(workspace))
    monkeypatch.setattr(execution_session, "retained_review_evidence", lambda *args, **kwargs: called.append((args, kwargs)))
    obligation = {"native_run_id": "run", "coordinate": {"claim_id": "claim"}}
    runner._native_facts(Path("/fixture"), runtime, obligation, "ref")
    runner._native_facts(Path("/fixture"), runtime, obligation, "ref", journal_collection=True)
    assert called[0][1] == {} and called[1][1] == {"journal_collection": True}


def test_measurement_recipe_version_binds_collection_policy_without_native_reexecution(monkeypatch):
    from tools._execution_budget import execution_policy
    from tools._observation_freshness import observation_policy
    native = {"service_policy": workload.service_policy("extended-observational"), "observation_policy": observation_policy("batch"),
        "execution_policy": execution_policy("eight-hour"), "journal_collection_policy": deepcopy(POLICY)}
    plan = {"schema_version": pilot.COLLECTION_OBSERVATIONAL_PLAN, "candidate_sha256": "1" * 64, "native_plan": native}
    report = {"closing_current_results": {"strict": {"review_producer_id": "sha256:" + "2" * 64}}}
    worst = {key: 1 for key in workload.PILOT_CAPS}
    monkeypatch.setattr(workload, "read_pinned_artifact", lambda pin: (plan, b"{}"))
    monkeypatch.setattr(pilot, "_originals", lambda *args: (plan, report, worst, 1))
    recipe = pilot.extended_measurement_recipe({}, {}, execution_profile="eight-hour")
    assert recipe["schema_version"] == pilot.COLLECTION_EXTENDED_RECIPE and recipe["journal_collection_policy"] == POLICY
    assert pilot.completed_extended_pilot_measurements(recipe, plan["candidate_sha256"]) == (worst, 1)
    for changed in (dict(recipe, schema_version=pilot.EXTENDED_RECIPE), {key: value for key, value in recipe.items() if key != "journal_collection_policy"}):
        with pytest.raises(ValueError):
            pilot.completed_extended_pilot_measurements(changed, plan["candidate_sha256"])


@pytest.mark.parametrize("change", ["bytes", "nodes", "overlap", "gap", "extra_field", "boolean", "page_order"])
def test_manifest_preflight_refuses_before_any_page_file_is_read(tmp_path, monkeypatch, change):
    _, pin, _ = retained(tmp_path)
    path = Path(pin["path"])
    manifest = json.loads(path.read_bytes())
    if change == "bytes":
        manifest["pages"][0]["size_bytes"] = 1_048_577
    elif change == "nodes":
        manifest["physical_nodes"] = 65_536
    elif change == "overlap":
        manifest["pages"][1]["record_start"] -= 1
    elif change == "gap":
        manifest["pages"][1]["record_start"] += 1
    elif change == "extra_field":
        manifest["unrecognized"] = True
    elif change == "boolean":
        manifest["record_count"] = True
    else:
        manifest["pages"].reverse()
    raw = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    path.chmod(0o600); path.write_bytes(raw); path.chmod(0o400)
    pin = raw_pin(path, raw)
    import tools.replay_qualification_native as native
    original_read = native._private_part
    reads = []
    def read(path, maximum, **kwargs):
        assert path == Path(pin["path"]).name, "invalid manifest reached page I/O"
        reads.append(path)
        return original_read(path, maximum, **kwargs)
    monkeypatch.setattr(native, "_private_part", read)
    with pytest.raises((ValueError, UsageError)):
        read_collection(pin)
    assert reads == [path.name]


def test_reader_uses_declared_and_remaining_page_caps_before_hashing(tmp_path, monkeypatch):
    _, pin, _ = retained(tmp_path)
    manifest = json.loads(Path(pin["path"]).read_bytes())
    import tools.replay_qualification_native as native
    original_read = native._private_part
    page_caps = []
    def read(path, maximum, **kwargs):
        if str(path).startswith("000"):
            page_caps.append(maximum)
        return original_read(path, maximum, **kwargs)
    monkeypatch.setattr(native, "_private_part", read)
    read_collection(pin)
    assert page_caps == [entry["size_bytes"] for entry in manifest["pages"]]


def test_all_earlier_accounting_schema_export_bundles_are_byte_identical():
    exported = accounting.schema_documents()
    previous = {name: exported[name] for name in accounting._PRE_COLLECTION_SCHEMA_DEFS}
    assert len(previous) == 46
    assert hashlib.sha256(accounting.canonical(previous)).hexdigest() == "8ca8b4cff0976667408526de66b4a0a01b6815a02a1855b850ca587e2438fcb9"


def test_collection_native_case_selection_allocation_index_are_explicit_byte_routes(tmp_path, monkeypatch):
    # Only the current producer/completed-reader boundaries are isolated. This
    # exercises real selection, allocation, provisioning and index validators.
    from tests._review_dependency_fixture import case_evidence_report
    from tests.test_native_case_extended_policy import allocated
    from tests.test_native_case_extended_policy import selected as old_selection_fixture
    from tools import _review_dependency_case_budget as budget
    from tools import _review_dependency_case_evidence as evidence
    from tools._execution_budget import provisioning_window
    from tools._review_dependency_case_contracts import case_plan
    old_selection, old_recipe = old_selection_fixture.__wrapped__(tmp_path, monkeypatch)
    recipe = {**old_recipe, "schema_version": pilot.COLLECTION_EXTENDED_RECIPE, "journal_collection_policy": deepcopy(POLICY)}
    pin = native_tools.retain(tmp_path / "collection-measured.json", recipe)
    selected = budget.select(pin, "twenty-four-hour", old_selection["fixture_input"], "selective_reuse",
        ["unchanged-baseline"], False, journal_collection=True)
    assert selected["schema_version"] == budget.COLLECTION_SELECTION and selected["journal_collection_policy"] == POLICY
    binding, fixture = allocated(selected)
    assert binding["schema_version"] == budget.COLLECTION_ALLOCATION and binding["journal_collection_policy"] == POLICY
    seconds = binding["fixture_budget"]["fixture_run_seconds"]
    budget.validate_allocation(selected["execution_policy"], binding, seconds,
        fixture=fixture, mode="selective_reuse", worker_plan=case_plan())
    assert provisioning_window(selected["execution_policy"], binding, seconds)[1] == binding["authority_expires_at"]
    assert binding["hard_limits"]["native_worker_seconds"] == 600
    report, source = case_evidence_report()
    report.update(extended_selection=selected, fixture_sha256=selected["fixture_sha256"],
        watchdog=budget.watchdog(selected, ["unchanged-baseline"]))
    output = evidence.write_index(tmp_path / "collection-index", report, source_before=source, source_after=source, refresh=False)
    index = evidence.read_index(output, roots=[tmp_path])
    assert index["schema_version"] == budget.COLLECTION_INDEX and index["qualification"] is False
    assert index["authority"] == "byte_evidence_only"
    for change in ("retag", "policy", "recipe"):
        invalid = deepcopy(selected)
        if change == "retag":
            invalid["schema_version"] = budget.SELECTION
        elif change == "policy":
            del invalid["journal_collection_policy"]
        else:
            invalid["measured_recipe_pin"] = old_selection["measured_recipe_pin"]
        with pytest.raises(ValueError):
            budget.validate_selection(invalid)


def test_transport_cleanup_does_not_replace_primary_driver_failure(tmp_path):
    originals = native_tools.OriginalResponses(tmp_path)
    primary = ValueError("private driver value")
    def close():
        raise ValueError("private transport value")
    with pytest.raises(ValueError) as raised:
        try:
            raise primary
        finally:
            originals.finally_close(SimpleNamespace(close=close))
    assert raised.value is primary
    failure = native_tools.failure_document(primary)
    assert failure["cleanup_failure"]["kind"] == "ValueError"
    assert "private" not in json.dumps(failure)


def test_formal_failure_keeps_first_safe_cause_when_cleanup_also_fails(tmp_path):
    originals = native_tools.OriginalResponses(tmp_path, journal_collection=True)
    report = {"failure": None, "terminal_outcome": "started"}
    primary = ValueError("private source body")
    primary.error_code = "private_credential"
    runner._mark_failure(report, primary, originals)
    first = deepcopy(report["failure"])
    runner._mark_failure(report, RuntimeError("private cleanup value"), originals)
    assert report["failure"] == first and report["terminal_outcome"] == "failed"
    assert first["error_code"] is None
    assert "private" not in json.dumps(json.loads(Path(first["raw_pin"]["path"]).read_bytes()))


def test_manifest_parent_must_be_private_and_anchored_during_read(tmp_path, monkeypatch):
    _, pin, _ = retained(tmp_path)
    path = Path(pin["path"])
    path.parent.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        read_collection(pin)
    path.parent.chmod(0o700)
    import shutil

    import tools.replay_qualification_native as native
    original_read = native._private_part
    replaced = []
    def read(name, maximum, **kwargs):
        raw = original_read(name, maximum, **kwargs)
        if name == path.name and not replaced:
            held = tmp_path / "held-original-parent"
            path.parent.rename(held)
            shutil.copytree(held, path.parent)
            replaced.append(True)
        return raw
    monkeypatch.setattr(native, "_private_part", read)
    with pytest.raises(ValueError, match="changed"):
        read_collection(pin)
    assert replaced == [True]
