"""Application construction shares native facts only before its first journal effect."""

import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Thread
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_session as native
from evidence_wiki.errors import UsageError
from evidence_wiki.execution_disagreement import FINDINGS
from evidence_wiki.review_context import create_review_context
from evidence_wiki.workflow_contracts import JOB_V2, JOB_V3, WorkflowContractError
from tests.test_review_context import BASIS, inputs


@pytest.fixture
def application(monkeypatch, tmp_path):
    material = inputs()
    context = create_review_context(**material)
    origin = datetime.now(timezone.utc)
    state = SimpleNamespace(now=origin, reads=0, source_reads=0, prepares=0, strict_reads=0,
        assembly_reads=0, checks=[], effects=[], held=False, source_changed=False, denied=False,
        controls={"context_id": "owned"}, qualified=True, withdrawn=False, revoked=False,
        strict_changed=False, after_sign=lambda: None, after_append=lambda: None,
        before_effect=lambda: None, signed=[], verified=[])
    prepared = {"snapshot": material["owner_snapshot"], "rubric": material["rubric"], "result": {"basis_id": BASIS}}
    attempt = SimpleNamespace(attempt_id="review", config_ref="profile-a",
        job={"schema_version": JOB_V3, "operation": "evaluate", "basis_id": BASIS,
            "deadline_utc": (origin + timedelta(seconds=60)).isoformat(),
            "payload": {"packet_id": context.packet_id}, "task_role": "evaluator"},
        input_document={"packet": context.document(), "complete_review_delivery": {"packet": "owned"}})
    row = {"attempt": {"attempt_id": attempt.attempt_id}, "purpose": "evaluate", "claim_id": "private-generator-claim",
        "original_id": "original", "review_context": context.binding(), "review_assembly_request_id": "assembly"}
    observation = {"state": "succeeded", "sequence": 1, "final_accounting": True,
        "output": {"verdicts": dict.fromkeys(material["rubric"]["criteria"], "pass"), "rationale": "Retained source review."}}
    records = [{"kind": "execution_attempt", "data": row},
        {"kind": "execution_observation", "data": {"attempt_id": "review", "observation": observation}}]
    qualification = lambda: {"state": "qualified" if state.qualified else "unknown", "profiles": []}

    def check(operation, *, write=False):
        state.checks.append((operation, write))
        if operation != "advance" or not write or state.denied:
            raise UsageError("AUTHORITY_CHANGED", "Current write grant unavailable.")

    def finish():
        if state.source_changed:
            raise UsageError("SOURCE_CHANGED", "Current source changed.")

    def strict():
        return {"state": "observed", "result": {"basis_id": BASIS,
            "claims": [{"accepted": state.strict_changed}], "evaluated_at": state.now.isoformat()}}

    def observe(_session):
        state.reads += 1
        state.source_reads += 1
        finish()
        observed = SimpleNamespace(config={}, controls={"result": copy.deepcopy(state.controls)},
            policy={"rubric": material["rubric"]}, integrations_qualified=True, strict=strict(),
            capture=SimpleNamespace(files={}), finish=finish)
        view = {"basis_id": BASIS, "qualification": qualification(), "usage": {"claims": 1, "sources": 2},
            "accounting": {"original_outcomes": []},
            "execution": {"accounting": {"overrun": []}, "source_bindings": []}}
        return view, observed, copy.deepcopy(material["sources"]), copy.deepcopy(records)

    def sources(_session):
        state.reads += 1
        state.source_reads += 1
        finish()
        return copy.deepcopy(material["sources"]), []

    def prepare(*_args):
        state.prepares += 1
        return copy.deepcopy(prepared)

    def strict_check(*_args):
        state.strict_reads += 1
        return strict()["result"]

    def sign(_prepared, verdicts, rationale, *_args, **_kwargs):
        assert state.held
        state.signed.append((copy.deepcopy(verdicts), rationale))
        state.after_sign()
        return {"payload": {"body": {"review": {"generator": {"role": "generator"}}}}}

    def trust(*_args):
        if state.revoked:
            raise UsageError("AUTHORITY_CHANGED", "Current signing authority revoked.")
        return {}

    def verify(envelope, role, *_args):
        state.verified.append(role)
        return {"authenticated": True, "controller": "reviewer" if role == "evaluator" else "generator"}

    def revalidate(*_args):
        if state.withdrawn:
            raise UsageError("SOURCE_WITHDRAWN", "Current source use withdrawn.")

    @contextmanager
    def live(*_args):
        revalidate()
        yield SimpleNamespace(revalidate=revalidate)

    owners = {
        "_caller_context": SimpleNamespace(capture=lambda *_: copy.deepcopy(state.controls)),
        "_evidence_usage": SimpleNamespace(configured=lambda *_: True, current_view=live),
        "_publication_usage": SimpleNamespace(qualify_capture=lambda *_a, **_k: {
            "complete": True, "selected": [source["source_id"] for source in material["sources"]]},
            EvidenceInvalid=ValueError),
        "strict_evidence": SimpleNamespace(prepare_review=prepare, run_operation=strict_check),
        "_evidence_authority": SimpleNamespace(load_trust=trust, verify_attestation=verify),
        "verify_quotes": SimpleNamespace(load_config=lambda *_: {}),
    }
    session = SimpleNamespace(root=tmp_path, _check=check,
        policy=SimpleNamespace(to_dict=lambda: {"budgets": {"claims": 10, "sources": 10}}),
        execution=SimpleNamespace(attempt=lambda _: attempt, authenticate=lambda value, _: value,
            profile=SimpleNamespace(calibration=None, review_services=None, input_handoff=None,
                disagreement=SimpleNamespace(), role=lambda _: SimpleNamespace(controller_ref="reviewer")),
            review_signer=SimpleNamespace(registration=sign, generator=SimpleNamespace(controller_ref="generator"))))

    @contextmanager
    def gate(_session):
        check("advance", write=True)
        assert not state.held
        state.held = True
        try:
            yield
        finally:
            state.held = False

    def append(_session, kind, request_id, data):
        assert state.held and native._CURRENT_FACTS.get() is None
        records.append({"kind": kind, "request_id": request_id, "data": copy.deepcopy(data)})
        if kind == "execution_application":
            state.after_append()

    def mutate(_session, _identifier, operation, _arguments, **options):
        assert native._CURRENT_FACTS.get() is None
        state.before_effect()
        options["effect_precondition"]()
        state.effects.append(operation)

    from evidence_wiki import review_packet_execution

    def assembly(*_args, **_kwargs):
        state.assembly_reads += 1
        return {"packet": "owned"}

    class Clock:
        @staticmethod
        def now(_zone):
            return state.now
        fromisoformat = staticmethod(datetime.fromisoformat)

    monkeypatch.setattr(native, "datetime", Clock)
    monkeypatch.setattr(native, "now_utc", lambda: state.now.isoformat())
    monkeypatch.setattr(native, "gate", gate)
    monkeypatch.setattr(native, "retained", lambda _: copy.deepcopy(records))
    monkeypatch.setattr(native, "_observe_current", observe)
    monkeypatch.setattr(native, "current", lambda selected: observe(selected)[0])
    monkeypatch.setattr(native, "_sources", sources)
    monkeypatch.setattr(native, "_qualifications", lambda *_a, **_k: qualification())
    monkeypatch.setattr(native, "owner", owners.__getitem__)
    monkeypatch.setattr(native, "append", append)
    monkeypatch.setattr(native, "_mutation", mutate)
    monkeypatch.setattr(review_packet_execution, "current_assembly", assembly)
    return session, state, row, attempt, observation, records


def test_ordinary_complete_application_shares_inputs_then_revalidates_fresh(application):
    session, state, _, attempt, _, records = application
    native._apply_owned(session, attempt.attempt_id)
    # The assembly owner is counted separately; its internal native reads remain fresh.
    assert state.reads == 3 and state.source_reads == 3 and state.prepares == 2
    assert state.strict_reads == 1 and state.assembly_reads == 3
    assert state.effects == ["strict.register_review"]
    assert state.verified == ["evaluator", "generator", "evaluator", "generator"]
    assert set(state.checks) == {("advance", True)}
    assert [row["kind"] for row in records][-2:] == ["execution_application", "execution_applied"]
    assert native._CURRENT_FACTS.get() is None


@pytest.mark.parametrize("change", ["source", "journal", "controls", "grant", "withdrawal", "authority",
    "qualification", "strict_time", "deadline", "wait", "effect"])
def test_construction_changes_refuse_before_application_append(application, change):
    session, state, _, attempt, _, records = application

    def changed():
        if change == "source":
            state.source_changed = True
        elif change == "journal":
            records.append({"kind": "changed"})
        elif change == "controls":
            state.controls = {"context_id": "changed"}
        elif change == "grant":
            state.denied = True
        elif change == "withdrawal":
            state.withdrawn = True
        elif change == "authority":
            state.revoked = True
        elif change == "qualification":
            state.qualified = False
        elif change == "strict_time":
            state.strict_changed = True
        elif change == "deadline":
            state.now += timedelta(seconds=61)
        elif change == "wait":
            from evidence_wiki.research_execution import bounded_host_call
            assert bounded_host_call(lambda: None, (), 1) == (None, None)
        else:
            native.invalidate_current_facts()

    state.after_sign = changed
    with pytest.raises((UsageError, WorkflowContractError)):
        native._apply_owned(session, attempt.attempt_id)
    assert not state.effects and not any(row["kind"] == "execution_application" for row in records)
    assert native._CURRENT_FACTS.get() is None


@pytest.mark.parametrize("moment", ["after_append", "before_effect"])
def test_application_append_never_extends_facts_into_native_precondition(application, moment):
    session, state, _, attempt, _, records = application
    setattr(state, moment, lambda: setattr(state, "qualified", False))
    with pytest.raises(WorkflowContractError):
        native._apply_owned(session, attempt.attempt_id)
    assert any(row["kind"] == "execution_application" for row in records)
    assert not state.effects and native._CURRENT_FACTS.get() is None


@pytest.mark.parametrize("boundary", ["forged", "session", "thread", "closed"])
def test_advance_inputs_require_exact_native_epoch_custody(application, boundary):
    session, _, _, attempt, observed, _ = application
    if boundary == "forged":
        token = native._CURRENT_FACTS.set({"basis_id": BASIS})
        try:
            with pytest.raises(WorkflowContractError):
                native._validate_application(session, attempt, observed, None)
        finally:
            native._CURRENT_FACTS.reset(token)
        return
    with native._current_facts(session, operation="advance") as facts:
        if boundary == "session":
            with pytest.raises(UsageError):
                native._validate_application(copy.copy(session), attempt, observed, None)
        elif boundary == "thread":
            errors = []
            from contextvars import copy_context
            selected = copy_context()
            def foreign():
                try:
                    native._validate_application(session, attempt, observed, None)
                except UsageError as error:
                    errors.append(error)
            thread = Thread(target=lambda: selected.run(foreign))
            thread.start()
            thread.join(timeout=2)
            assert not thread.is_alive() and len(errors) == 1
    if boundary == "closed":
        token = native._CURRENT_FACTS.set(facts)
        try:
            with pytest.raises(UsageError):
                native._validate_application(session, attempt, observed, None)
        finally:
            native._CURRENT_FACTS.reset(token)


@pytest.mark.parametrize("excluded", ["retained", "legacy", "service", "semantic", "adjudication", "retry"])
def test_unrelated_application_routes_keep_original_validation(application, excluded):
    session, _, row, attempt, _, _ = application
    saved = None
    if excluded == "retained":
        saved = {"attempt_id": attempt.attempt_id}
    elif excluded == "legacy":
        attempt.job["schema_version"] = JOB_V2
    elif excluded == "service":
        session.execution.profile.review_services = SimpleNamespace()
    elif excluded == "semantic":
        row["semantic_binding"] = {}
    elif excluded == "adjudication":
        row["adjudication_binding"] = {}
    else:
        row["retry_purpose"] = "evaluate"
    assert not native._ordinary_contextual_review(session, row, attempt, saved)


def test_material_objection_conflict_still_refuses_before_signing(application):
    session, state, row, attempt, observation, records = application
    alias = next(iter(row["review_context"]["aliases"]["sources"].values()))
    observation["output"].update(schema_version=FINDINGS, objections=[{"criterion": "counterevidence",
        "source_refs": [alias], "rationale": "Retained contrary passage.", "material": True}])
    with pytest.raises(WorkflowContractError, match="refused"):
        native._apply_owned(session, attempt.attempt_id)
    assert not state.signed and not state.effects
    assert not any(row["kind"] == "execution_application" for row in records)


def test_actual_complete_context_application_and_replay_keep_native_registration_unique(tmp_path, monkeypatch):
    import ast
    from dataclasses import replace

    from evidence_wiki import Workspace
    from evidence_wiki._pack_io import json_document
    from tests import test_execution_session as reference
    from tests.test_review_packet_execution import (
        test_actual_child_receives_every_required_page_and_native_review_application,
    )

    root, policy, execution, context = reference.fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"role_isolation": True, "coverage_examination": True,
            "simulated_model_usage": True, "complete_packet_capture": True, "review_delivery": True,
            "review_delivery_limits": {"context_version": "complete"}, "disagreement": True}))
    tree = ast.parse(execution.executor._commands["researcher"][-1])
    printed = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "print")
    proposal = json_document(ast.literal_eval(printed.args[0]).encode())["proposal"]
    context = replace(context, grants=context.grants | {"review_page_operations.prepare", "review_page_operations.page",
        "review_page_session.assembled", "review_packet_execution.bind", "review_packet_execution.reserve", "coverage_observation.observe"})
    with context.activate(), Workspace.open(root) as workspace:
        request = workspace.mutations.prepare(request_id="packet-admission", operation="strict.admit", arguments={"proposal": proposal})
        assert workspace.mutations.submit(request)["status"] == "committed"
    selected = []
    eligible = native._ordinary_contextual_review

    def ordinary(session, row, attempt, application):
        result = eligible(session, row, attempt, application)
        if result:
            selected.append(attempt.input_document["packet"]["schema_version"])
            assert "review_assembly_request_id" in row
            assert session.execution.profile.disagreement is not None
        return result

    monkeypatch.setattr(native, "_ordinary_contextual_review", ordinary)
    test_actual_child_receives_every_required_page_and_native_review_application(
        (root, policy, execution, context, proposal["inventory"]["claims"][0], tmp_path / "workers/complete-input.json"))
    assert selected and set(selected) == {"research-review-context/v3"}
