"""Original acquisition authority remains narrower than configured web domains."""

import json
import time
from datetime import datetime, timezone

import pytest

from evidence_wiki import Workspace
from evidence_wiki._pack_io import canonical
from evidence_wiki.acquisition_dispatch import native
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.pack_discovery import owner
from tests import test_execution_session as execution_cases
from tests.test_acquisition_dispatch import selected
from tests.test_acquisition_transport import FakeResponse
from tests.test_draft_admission import update_policy


@pytest.fixture
def configured(tmp_path, monkeypatch):
    value = selected(execution_cases.fixture.__wrapped__(tmp_path, monkeypatch))
    path = value[0] / "docs/research-requirements.json"
    path.write_bytes(canonical(json.loads(path.read_bytes())) + b"\n")
    update_policy(value[0])
    return value


def test_allowed_domain_redirect_outside_original_uri_refuses_before_retaining_content(configured, monkeypatch):
    root, policy, execution, context = configured
    transport = owner("_acquisition_transport")
    actual = transport.execute_planned_request
    response = FakeResponse(b"Outside original authorized URI scope", url="https://example.org/unapproved",
        headers={"Content-Type":"text/plain"})

    def local(plan, **values):
        return actual(plan, **values, opener=lambda *_: response, resolve_hostnames=False)

    monkeypatch.setattr(transport, "execute_planned_request", local)
    with context.activate(), Workspace.open(root) as workspace:
        source = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/source",
            rationale="Need current original", question_slugs=["q"], scope={"facet_id":"primary"})["request"]
        with workspace.research.session(policy, run_id="scope-redirect", execution=execution) as session:
            with native.gate(session):
                native._begin(session)
            with pytest.raises(EvidenceWikiError):
                session.acquire(request_id="fetch", source_request_id=source["request_id"], expected_request_basis=digest(source))
            records = execution.records(session)
            assert not any(row["kind"] == "execution_native_observation" and row["data"].get("result") is not None
                for row in records)
    assert response.read_calls == 0


def test_original_seconds_caps_transport_despite_larger_run_policy(configured, monkeypatch):
    root, policy, execution, context = configured
    path = root / "docs/research-requirements.json"
    declaration = json.loads(path.read_bytes())
    declaration["request"]["payload"]["budgets"]["seconds"] = 1
    path.write_bytes(canonical(declaration) + b"\n")
    update_policy(root)
    transport = owner("_acquisition_transport")
    actual = transport.execute_planned_request
    timeouts = []

    def local(plan, **values):
        timeouts.append(values["timeout"])
        response = FakeResponse(b"Scoped original", url="https://example.org/source", headers={"Content-Type":"text/plain"})
        return actual(plan, **values, opener=lambda *_: response, resolve_hostnames=False)

    monkeypatch.setattr(transport, "execute_planned_request", local)
    with context.activate(), Workspace.open(root) as workspace:
        source = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/source",
            rationale="Need current original", question_slugs=["q"], scope={"facet_id":"primary"})["request"]
        with workspace.research.session(policy, run_id="original-time", execution=execution) as session:
            with native.gate(session):
                native._begin(session)
            try:
                session.acquire(request_id="fetch", source_request_id=source["request_id"], expected_request_basis=digest(source))
            except EvidenceWikiError:
                pass  # An already expired original deadline may refuse before dispatch.
    assert not timeouts or max(timeouts) <= 1


def test_new_run_cannot_reset_one_original_request_download_budget(configured, monkeypatch):
    root, policy, execution, context = configured
    path = root / "docs/research-requirements.json"
    declaration = json.loads(path.read_bytes())
    declaration["request"]["payload"]["budgets"]["downloads"] = 1
    path.write_bytes(canonical(declaration) + b"\n")
    update_policy(root)
    transport = owner("_acquisition_transport")
    actual = transport.execute_planned_request
    calls = []

    def local(plan, **values):
        calls.append(plan.url)
        response = FakeResponse(b"Scoped original", url=plan.url, headers={"Content-Type":"text/plain"})
        return actual(plan, **values, opener=lambda *_: response, resolve_hostnames=False)

    monkeypatch.setattr(transport, "execute_planned_request", local)
    with context.activate(), Workspace.open(root) as workspace:
        source = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/source",
            rationale="Need current original", question_slugs=["q"], scope={"facet_id":"primary"})["request"]
        for index in range(2):
            with workspace.research.session(policy, run_id="scope-run-" + str(index), execution=execution) as session:
                with native.gate(session):
                    native._begin(session)
                if index:
                    with pytest.raises(EvidenceWikiError):
                        session.acquire(request_id="fetch", source_request_id=source["request_id"], expected_request_basis=digest(source))
                else:
                    session.acquire(request_id="fetch", source_request_id=source["request_id"], expected_request_basis=digest(source))
    assert calls == ["https://example.org/source"]


def test_edited_v1_original_budget_cannot_launder_frozen_instruction_binding(configured, monkeypatch):
    root, policy, execution, context = configured
    path = root / "docs/research-requirements.json"
    declaration = json.loads(path.read_bytes())
    declaration["request"]["payload"]["budgets"]["downloads"] = 0
    path.write_bytes(canonical(declaration) + b"\n")
    update_policy(root)
    calls = []
    transport = owner("_acquisition_transport")
    actual = transport.execute_planned_request

    def local(plan, **values):
        calls.append(plan.url)
        response = FakeResponse(b"Scoped original", url=plan.url, headers={"Content-Type":"text/plain"})
        return actual(plan, **values, opener=lambda *_: response, resolve_hostnames=False)

    monkeypatch.setattr(transport, "execute_planned_request", local)
    with context.activate(), Workspace.open(root) as workspace:
        source = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/source",
            rationale="Need current original", question_slugs=["q"], scope={"facet_id":"primary"})["request"]
        with workspace.research.session(policy, run_id="edited-original", execution=execution) as session:
            with native.gate(session):
                native._begin(session)
            declaration["request"]["payload"]["budgets"]["downloads"] = 1
            path.write_bytes(canonical(declaration) + b"\n")
            # The protected strict-policy instruction hash intentionally remains
            # the original zero-download declaration; there is no owner revision.
            with pytest.raises(EvidenceWikiError):
                session.acquire(request_id="fetch", source_request_id=source["request_id"], expected_request_basis=digest(source))
    assert calls == []


def test_distinct_runs_keep_separate_provider_caps_within_original_budget(configured, monkeypatch):
    root, policy, execution, context = configured
    transport = owner("_acquisition_transport")
    actual = transport.execute_planned_request
    calls = []

    def local(plan, **values):
        calls.append(plan.url)
        response = FakeResponse(b"Scoped original", url=plan.url, headers={"Content-Type":"text/plain"})
        return actual(plan, **values, opener=lambda *_: response, resolve_hostnames=False)

    monkeypatch.setattr(transport, "execute_planned_request", local)
    with context.activate(), Workspace.open(root) as workspace:
        source = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/source",
            rationale="Need current original", question_slugs=["q"], scope={"facet_id":"primary"})["request"]
        for index in range(2):
            with workspace.research.session(policy, run_id="per-run-" + str(index), execution=execution) as session:
                with native.gate(session):
                    native._begin(session)
                result = session.acquire(request_id="fetch", source_request_id=source["request_id"], expected_request_basis=digest(source))
                assert result["reserved_provider_calls"] == 1
    assert calls == ["https://example.org/source"] * 2


def test_new_run_cannot_restart_original_request_seconds(configured, monkeypatch):
    from evidence_wiki import acquisition_budget

    root, policy, execution, context = configured
    path = root / "docs/research-requirements.json"
    declaration = json.loads(path.read_bytes())
    declaration["request"]["payload"]["budgets"]["seconds"] = 5
    path.write_bytes(canonical(declaration) + b"\n")
    update_policy(root)
    transport = owner("_acquisition_transport")
    monkeypatch.setattr(transport, "execute_planned_request",
        lambda *_args, **_kwargs: pytest.fail("Expired original request reached transport."))
    with context.activate(), Workspace.open(root) as workspace:
        source = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/source",
            rationale="Need current original", question_slugs=["q"], scope={"facet_id":"primary"})["request"]
        with workspace.research.session(policy, run_id="first-time", execution=execution) as session:
            with native.gate(session):
                native._begin(session)
            fetch = owner("fetch_sources")
            selected = fetch.acquisition_context(root, owner("verify_quotes").load_config(root),
                "web", 1, run_id=session.run_id)
            reservation = acquisition_budget.reserve(session, "first-reservation", source, selected, 1)
            started = datetime.fromisoformat(reservation["original_budget_started_utc"].replace("Z", "+00:00"))
            assert reservation["caps"]["seconds"] == 5
        remaining = 5.1 - (datetime.now(timezone.utc) - started).total_seconds()
        if remaining > 0:
            time.sleep(remaining)
        with workspace.research.session(policy, run_id="second-time", execution=execution) as session:
            with native.gate(session):
                native._begin(session)
            with pytest.raises(EvidenceWikiError):
                session.acquire(request_id="second-fetch", source_request_id=source["request_id"], expected_request_basis=digest(source))
