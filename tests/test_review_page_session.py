"""Native journal proves host assembly only; worker consumption stays unobserved."""

import ast
import importlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from evidence_wiki._pack_io import json_document
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.pack_discovery import owner
from tests import test_execution_session as reference


@pytest.fixture
def configured(tmp_path, monkeypatch):
    root, policy, execution, context = reference.fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"role_isolation": True, "coverage_examination": True}))
    operations = importlib.import_module("evidence_wiki.review_page_session")
    tree = ast.parse(execution.executor._commands["researcher"][-1])
    printed = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "print")
    generated = json_document(ast.literal_eval(printed.args[0]).encode())
    proposal = generated["proposal"]
    selected = replace(context, grants=context.grants | {"review_page_operations.prepare", "review_page_operations.page",
        "review_page_session.assembled", "coverage_observation.observe"})
    with selected.activate(), Workspace.open(root) as workspace:
        request = workspace.mutations.prepare(request_id="page-owner-admission", operation="strict.admit",
            arguments={"proposal": proposal})
        receipt = workspace.mutations.submit(request)
        assert receipt["status"] == "committed"
    return root, policy, execution, selected, proposal["inventory"]["claims"][0], operations


def test_actual_native_assembly_receipts_replay_without_worker_or_new_effect(configured, monkeypatch):
    root, policy, execution, context, claim, operations = configured
    monkeypatch.setattr(execution.executor, "invoke", lambda *_args: pytest.fail("Packet assembly dispatched a worker"))
    monkeypatch.setattr(execution.executor, "poll", lambda *_args: pytest.fail("Packet assembly polled a worker"))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="page-assembly", execution=execution) as session:
            value = operations.assembled(session, request_id="assembled", claim_id=claim["id"], page_chars=256)
            assert value["packet"]["complete"]["complete"]
            decisive = json.loads("".join(page["text"] for page in value["packet"]["pages"]
                if page.get("section_id") == "mechanical-counter"))
            assert decisive["counter_context"]["status"] == "current_native_instruction"
            assert decisive["counter_context"]["criteria"]
            assert decisive["counter_search_completeness"] == "not_established"
            assert value["worker_consumption"] == "not_observed" and not value["accepted"]
            records = execution.records(session)
            deliveries = [row for row in records if row["kind"] == "execution_review_page_delivery"]
            assert len(deliveries) == len(value["packet"]["pages"])
            assert all(row["data"]["worker_consumption"] == "not_observed" for row in deliveries)
            assert not any(row["kind"] == "execution_attempt" for row in records)
            assert operations.assembled(session, request_id="assembled", claim_id=claim["id"], page_chars=256) == value
            assert execution.records(session) == records
            source = claim["evidence"][0]["source_id"]
            config = yaml.safe_load((root / "research.yml").read_bytes())
            normalized = root / config["sources"]["normalized_dir"] / (owner("normalize_sources").safe_source_id(source) + ".md")
            normalized.write_bytes(normalized.read_bytes() + b"\nChanged decisive material.\n")
            with pytest.raises(EvidenceWikiError):
                operations.assembled(session, request_id="assembled", claim_id=claim["id"], page_chars=256)
            assert execution.records(session) == records


def test_terminal_native_run_refuses_new_packet_before_any_native_intent(configured, monkeypatch):
    root, policy, execution, context, claim, operations = configured
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="terminal-packet", execution=execution) as session:
            with native.gate(session):
                native._begin(session)
                native._finish(session, {"outcome": "abstained", "reason": "bounded_stop"})
            records = execution.records(session)
            monkeypatch.setattr(operations, "prepare", lambda *_args, **_kwargs: pytest.fail("Terminal run read new source pages"))
            with pytest.raises(EvidenceWikiError) as refused:
                operations.assembled(session, request_id="new-packet", claim_id=claim["id"])
            assert refused.value.details.get("reason") == "packet_run_terminal"
            assert execution.records(session) == records
