"""Executor recovery joins real mutation serialization, without native approval.

The selected fixture substitutes child semantic validation. Mutation preparation,
transport, immutable intent identity, durable receipts and inspection remain real;
these tests make no claim about review authentication or native admission.
"""

import copy
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from evidence_wiki import _review_dependency_json as child_codec
from evidence_wiki import execution_session as execution
from evidence_wiki import mutation_contracts as contracts
from evidence_wiki import mutation_operations as operations
from evidence_wiki.workflow_contracts import WorkflowContractError
from tests.test_mutation_operations import host
from tests.test_mutation_operations import roots as roots
from tests.test_selective_wire_mutation import selected as selected

_encoded = child_codec.encoded


@pytest.fixture(params=["packed", "legacy"])
def registered(roots, selected, monkeypatch, request):
    root, _control = roots
    arguments = {"envelope": selected}
    selected_encoded = child_codec.encoded
    monkeypatch.setattr(child_codec, "encoded", lambda value, **options:
        selected_encoded(value) if type(value) is dict and set(value) == {"envelope"}
        else _encoded(value, **options))
    monkeypatch.setattr(operations, "_invoke", lambda *args, **kwargs: {"registration": "test owner return"})
    with host(roots).activate():
        prepared = operations.prepare_request(root, request_id="register", operation="strict.register_review", arguments=arguments)
        if request.param == "legacy":
            prepared["arguments"] = copy.deepcopy(arguments)
        receipt = operations.submit(root, prepared)
        intent = operations.inspect(root, "register")["record"]["intent"]
    assert (intent["arguments"] != arguments) == (request.param == "packed")
    assert contracts.review_envelope(intent["arguments"]["envelope"]) == selected
    return SimpleNamespace(root=root, arguments=arguments, receipt=receipt, intent=intent)


@pytest.mark.parametrize("changed", [False, True])
def test_existing_registration_receipt_matches_complete_raw_envelope(registered, roots, monkeypatch, changed):
    case = registered
    supplied = copy.deepcopy(case.arguments)
    if changed:
        supplied["envelope"]["authentication"]["signature"] = "1" * 64

    def native_call(session, identifier, operation, arguments, invoke, **options):
        assert arguments == supplied  # Execution journals keep the original logical envelope.
        return invoke(None)

    monkeypatch.setattr(execution, "_native_call", native_call)
    with host(roots).activate():
        if changed:
            with pytest.raises(WorkflowContractError) as caught:
                execution._mutation(case, "register", "strict.register_review", supplied)
            assert caught.value.error_code == "RESEARCH_REQUEST_CONFLICT"
        else:
            assert execution._mutation(case, "register", "strict.register_review", supplied) == case.receipt


@pytest.mark.parametrize("changed", [False, True])
def test_unobserved_registration_recovers_against_packed_owner_intent(registered, roots, monkeypatch, changed):
    case = registered
    arguments = copy.deepcopy(case.arguments)
    if changed:
        arguments["envelope"]["authentication"]["signature"] = "1" * 64
    records = [{"kind": "execution_native", "data": {"operation": "strict.register_review",
        "arguments": arguments, "reservation": {"attempt_id": "register"}}}]
    outcomes, unknown = [], []
    monkeypatch.setattr(execution, "retained", lambda session: records)
    monkeypatch.setattr(execution, "gate", lambda session: nullcontext())
    monkeypatch.setattr(execution, "append", lambda *args: unknown.append(args))

    def native_call(session, identifier, operation, supplied, invoke, *, recover):
        assert supplied == arguments
        outcomes.append(recover(None))

    monkeypatch.setattr(execution, "_native_call", native_call)
    with host(roots).activate():
        execution._recover_history(case)
    assert outcomes == ([] if changed else [case.receipt])
    assert len(unknown) == int(changed)
